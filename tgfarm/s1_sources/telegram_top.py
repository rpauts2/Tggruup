"""Opt-in discovery and monitoring of large public Telegram communities.

This is deliberately separate from the configured RSS sources: the list is
dynamic, read-only and expensive to enumerate, so it runs on its own slow
schedule and never blocks the main collection cycle. Public groups are read as
upstream material exactly like channels; the copyright gate still applies.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.schemas import RawItem
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Source as SourceRow
from tgfarm.db.session import session_scope
from tgfarm.s1_sources import _mark_source, _persist_detailed
from tgfarm.s1_sources.telegram_source import _title_from
from tgfarm.tguser import TgChat

log = get_logger(__name__)

MAX_MESSAGES_PER_GROUP = 8
MAX_AGE_DAYS = 14
MIN_TEXT_CHARS = 40


def _configured_handles() -> set[str]:
    handles: set[str] = set()
    for spec in get_config().sources:
        if spec.kind != "telegram":
            continue
        raw = str((spec.params or {}).get("channel") or spec.url)
        handle = raw.rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()
        if handle:
            handles.add(handle)
    return handles


def discover_top_chats(
    limit: int | None = None,
    min_participants: int | None = None,
) -> list[dict]:
    """Public, large, active communities not already configured as sources."""
    settings = get_settings()
    limit = max(1, int(limit or settings.telegram_top_groups_limit))
    minimum = max(0, int(
        min_participants
        if min_participants is not None
        else settings.telegram_top_group_min_participants
    ))
    excluded = _configured_handles() | set(settings.channel_ids())
    chats = _list_chats_sync()
    picked: list[dict] = []
    for chat in chats:
        username = (chat.username or "").strip().lstrip("@")
        if not username or chat.participants < minimum:
            continue
        if username.lower() in excluded or chat.kind not in ("channel", "group"):
            continue
        picked.append({**chat.as_dict(), "username": username})
        if len(picked) >= limit:
            break
    return picked


def _list_chats_sync() -> list[TgChat]:
    from tgfarm import tguser

    if not tguser.is_authorized():
        raise RuntimeError("Telegram user session is not authorized")
    return asyncio.run(tguser.list_chats(limit=200))


def _ensure_source(chat: dict) -> str:
    source_id = f"tg_top_{chat['username'].lower()}"
    with session_scope() as session:
        row = session.get(SourceRow, source_id)
        if row is None:
            row = SourceRow(
                id=source_id,
                name=str(chat.get("title") or source_id)[:200],
                kind="telegram",
                url=f"https://t.me/{chat['username']}",
                niches=["society"],
                weight=0.35,
                lang="ru",
                enabled=True,
                min_interval_s=3600,
            )
            session.add(row)
    return source_id


async def _fetch_chat(client, chat: dict, cutoff: datetime) -> list[RawItem]:
    source_id = _ensure_source(chat)
    await RATE.http.take()
    entity = await client.get_entity(chat["username"])
    items: list[RawItem] = []
    async for message in client.iter_messages(entity, limit=MAX_MESSAGES_PER_GROUP):
        text = (message.message or "").strip()
        if len(text) < MIN_TEXT_CHARS:
            continue
        posted = message.date
        if posted is not None:
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=UTC)
            if posted < cutoff:
                continue
        else:
            posted = datetime.now(UTC)
        items.append(
            RawItem(
                source_id=source_id,
                external_id=f"{source_id}-{message.id}",
                url=f"https://t.me/{chat['username']}/{message.id}",
                title=_title_from(text)[:400],
                text=text[:6000],
                lang="ru",
                published_at=posted,
                niche_hint=["society"],
                source_weight=0.35,
                media_kind="video" if getattr(message, "media", None) else None,
            )
        )
    return items


async def collect_top_groups(limit: int | None = None) -> dict:
    """Fetch recent posts from the current top communities."""
    settings = get_settings()
    if not settings.telegram_top_groups_enabled:
        return {"enabled": False, "sources": 0, "items": 0, "written": 0}

    from tgfarm import tguser

    chats = await asyncio.to_thread(discover_top_chats, limit)
    if not chats:
        return {"enabled": True, "sources": 0, "items": 0, "written": 0}

    cutoff = datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS)
    collected: list[RawItem] = []
    failed: list[str] = []
    async with tguser.connect() as client:
        for chat in chats:
            try:
                collected.extend(await _fetch_chat(client, chat, cutoff))
            except Exception as exc:  # noqa: BLE001 - one bad group must not stop the run
                failed.append(chat["username"])
                log.info("top group %s failed: %s", chat.get("username"), exc)

    written, counts = await asyncio.to_thread(_persist_detailed, collected)
    for chat in chats:
        source_id = f"tg_top_{chat['username'].lower()}"
        await asyncio.to_thread(
            _mark_source,
            source_id,
            chat["username"] not in failed,
            None if chat["username"] not in failed else "fetch failed",
            items_new=counts.get(source_id, 0),
        )
    log.info("top groups: %d sources, %d items, %d new", len(chats), len(collected), written)
    return {
        "enabled": True,
        "sources": len(chats),
        "failed": failed,
        "items": len(collected),
        "written": written,
    }


__all__ = ["collect_top_groups", "discover_top_chats"]

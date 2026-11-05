"""Reconcile configured channels with Telegram reality.

`config/personas.yaml` is a wish list. This module finds out what is actually
true: which channels exist, how many people read them, whether the farm's bot is
an administrator there, and — the part that used to be missing entirely — what
the published posts actually earned.

Everything here is read-only against Telegram and writes only to our own
database.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope

log = get_logger(__name__)


def _normalise(text: str) -> str:
    return "".join(c for c in (text or "").lower() if c.isalnum() or c.isspace()).strip()


def _match(persona_id: str, title: str, username: str | None, chats: list) -> object | None:
    """Find the chat that corresponds to a configured channel.

    Username first — it is unambiguous. Then the linked discussion group, then a
    normalised title, then a fuzzy containment match. A wrong match here would
    attach statistics to the wrong channel, so the order matters.
    """
    if username:
        want = username.lower().lstrip("@")
        for chat in chats:
            if chat.username and chat.username.lower() == want:
                return chat
    key = _normalise(title)
    if not key:
        return None
    for chat in chats:
        if _normalise(chat.title) == key:
            return chat
    for chat in chats:
        if chat.linked_title and _normalise(chat.linked_title) == key:
            return chat
    for chat in chats:
        if chat.kind == "channel" and key and _normalise(chat.title) in key:
            return chat
    return None


async def sync_channels() -> dict:
    """Refresh channel reality from Telegram and report what does not line up."""
    from tgfarm.config import get_config
    from tgfarm.tguser import list_chats

    config = get_config()
    personas = config.by_platform("telegram")
    chats = await list_chats(limit=300)

    matched: dict[str, object] = {}
    for persona in personas:
        found = _match(persona.id, persona.title, persona.tg_username, chats)
        if found is not None:
            matched[persona.id] = found

    bot_admin = await _bot_admin_map()

    now = datetime.now(UTC)
    with session_scope() as session:
        rows = {row.id: row for row in session.execute(select(Channel)).scalars().all()}

        for persona in personas:
            row = rows.get(persona.id)
            if row is None:
                continue
            chat = matched.get(persona.id)
            if chat is None:
                continue
            row.tg_chat_id = chat.id
            row.tg_participants = chat.participants or None
            row.tg_verified = chat.is_verified
            row.tg_synced_at = now
            if chat.id in bot_admin:
                row.tg_bot_admin = bot_admin[chat.id]
            if chat.username:
                row.username = chat.username

        stored_ids = {row.tg_chat_id for row in rows.values() if row.tg_chat_id}

    configured_titles = {p.title for p in personas}
    unconfigured = [
        c.as_dict()
        for c in chats
        if c.id not in stored_ids
        and c.kind == "channel"
        and c.participants > 0
        and _normalise(c.title) not in {_normalise(t) for t in configured_titles}
    ]

    missing = [
        {"id": p.id, "title": p.title}
        for p in personas
        if p.id not in matched
    ]
    not_admin = [
        {"id": cid, "reason": "бот не администратор"}
        for cid, row in ((r.id, r) for r in rows.values())
        if row.tg_chat_id and row.tg_bot_admin is False
    ]

    report = {
        "ok": not missing and not not_admin,
        "telegram_chats": len(chats),
        "matched": len(matched),
        "configured": len(personas),
        "missing_in_telegram": missing,
        "bot_not_admin": not_admin,
        "unconfigured_channels": unconfigured[:25],
        "synced_at": now.isoformat(),
    }
    log.info(
        "telegram sync: %d/%d каналов найдено, %d чатов видно",
        len(matched), len(personas), len(chats),
    )
    return report


async def _bot_admin_map() -> dict[int, bool]:
    """Whether the farm's bot can post in each configured channel.

    Keyed by *Telethon* entity id, not by the Bot API chat id, because that is
    what the sync compares against. The two are not interchangeable: the API
    spells a channel as `-1001234567890` while MTProto reports the bare
    `1234567890`.
    """
    import httpx

    from tgfarm.core.settings import get_settings

    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return {}

    ids: dict[int, bool] = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        me = await _bot_id(token)
        for chat_id in settings.channel_ids:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getChatMember",
                    params={"chat_id": chat_id, "user_id": me},
                )
                data = resp.json()
                status = (data.get("result") or {}).get("status", "")
                ids[entity_id(chat_id)] = status in ("administrator", "creator")
            except Exception as exc:  # noqa: BLE001 - a missing permission is a report
                log.info("getChatMember failed for %s: %s", chat_id, exc)
    return ids


def entity_id(api_chat_id: int) -> int:
    """Bot API chat id → MTProto entity id.

    Channels and megagroups are exposed as `-100` + entity_id; plain chats use a
    negative id. Getting this wrong silently checks the permission of a chat that
    does not exist.
    """
    raw = str(api_chat_id)
    if raw.startswith("-100"):
        return int(raw[4:])
    return abs(api_chat_id)


_bot_id_cache: int | None = None


async def _bot_id(token: str) -> int:
    global _bot_id_cache
    if _bot_id_cache is not None:
        return _bot_id_cache
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        _bot_id_cache = int((resp.json().get("result") or {}).get("id", 0))
    return _bot_id_cache


async def collect_post_stats(limit: int = 500) -> dict:
    """Pull live statistics for published posts and store them as a daily snapshot.

    A post is only askable when the farm can name it on Telegram: a message id
    *and* the chat it lives in. `sync_channels` fills the chat id on the channel,
    the publisher fills it on the post, and either is enough — reading only the
    channel used to make every post published before the first sync unmeasurable,
    which is how the learning loop stayed hungry even after the collector worked.
    """
    from tgfarm.tguser import fetch_post_stats

    with session_scope() as session:
        rows = list(
            session.execute(
                select(Post)
                .where(Post.status == "published", Post.tg_message_id.isnot(None))
                .order_by(Post.published_at.desc())
                .limit(limit)
            ).scalars().all()
        )
        pairs: list[tuple[int, int]] = []
        chat_of: dict[int, int] = {}
        for row in rows:
            channel = session.get(Channel, row.channel_id)
            chat_id = getattr(channel, "tg_chat_id", None) or row.tg_chat_id
            if not chat_id:
                continue
            pairs.append((chat_id, row.tg_message_id))
            chat_of[row.id] = chat_id

    if not pairs:
        if rows:
            return {
                "ok": True,
                "posts": 0,
                "askable": 0,
                "published_with_message_id": len(rows),
                "reason": "posts have a message id but no chat id: run `sync_channels`",
            }
        return {
            "ok": True,
            "posts": 0,
            "askable": 0,
            "published_with_message_id": 0,
            "reason": "no posts with a telegram message id",
        }

    stats = await fetch_post_stats(pairs)
    if not stats:
        return {"ok": True, "posts": 0, "reason": "telegram returned nothing visible"}

    now = datetime.now(UTC)
    today = now.date().isoformat()
    written = 0
    with session_scope() as session:
        for row_id, chat_id in chat_of.items():
            key = (chat_id, session.get(Post, row_id).tg_message_id or 0)
            data = stats.get(key)
            if data is None:
                continue
            existing = session.execute(
                select(PostStat).where(
                    PostStat.post_id == row_id,
                    func.date(PostStat.collected_at) == today,
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = PostStat(post_id=row_id)
                session.add(existing)
            existing.collected_at = now
            existing.views = data["views"]
            existing.reactions = data["reactions"]
            existing.forwards = data["forwards"]
            existing.comments = 0
            written += 1

    log.info("collected live stats for %d posts (%d/%d visible)", written, len(stats), len(pairs))
    return {
        "ok": True,
        "posts": written,
        "visible": len(stats),
        "requested": len(pairs),
        "day": today,
    }


def channel_reality() -> list[dict]:
    """What we last learned about each configured channel, for the dashboard."""
    with session_scope() as session:
        rows = session.execute(select(Channel).order_by(Channel.id)).scalars().all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "tg_chat_id": r.tg_chat_id,
                "username": r.username,
                "participants": r.tg_participants,
                "verified": r.tg_verified,
                "bot_admin": r.tg_bot_admin,
                "synced_at": r.tg_synced_at.isoformat() if r.tg_synced_at else None,
            }
            for r in rows
        ]


__all__ = ["channel_reality", "collect_post_stats", "sync_channels"]
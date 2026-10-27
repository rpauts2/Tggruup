"""Telegram channels as a content source (S1).

Reading other channels is the same job as reading an RSS feed: fetch recent
messages, reduce them to title + body, hand them to the pipeline. The licence
question does not change — public channels are read, never republished verbatim,
and the S5 copyright gate still applies to anything derived from them.

This needs the read-only user session; without it the collector reports itself
unavailable instead of failing the run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from tgfarm.config import SourceSpec
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.schemas import RawItem

log = get_logger(__name__)

MAX_ITEMS_PER_SOURCE = 40


def target_of(spec: SourceSpec) -> str:
    """A channel is addressed by username, id or a t.me link in the URL."""
    raw = (spec.params or {}).get("channel") or spec.url
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        path = parsed.path.strip("/")
        if path.startswith(("s/", "+")):
            raise ValueError(f"приватную ссылку читать нельзя: {raw}")
        raw = path.split("/")[0]
    return raw.lstrip("@")


class TelegramCollector:
    """Read-only collector over a list of Telegram channels."""

    kind = "telegram"

    async def fetch(self, spec: SourceSpec, client) -> list[RawItem]:  # noqa: ARG002
        from tgfarm.tguser import fetch_messages, is_authorized

        if not is_authorized():
            raise RuntimeError(
                "нет авторизованной сессии Telegram — выполните `tgfarm telegram login`"
            )

        target = target_of(spec)
        limit = int((spec.params or {}).get("limit", MAX_ITEMS_PER_SOURCE))
        await RATE.http.take()
        messages = await fetch_messages(target, limit=limit)

        items: list[RawItem] = []
        for message in messages:
            text = (message.text or "").strip()
            if len(text) < 40:
                continue  # a bare link or an emoji is not a document
            title = _title_from(text)
            posted = None
            if message.posted_at:
                try:
                    posted = datetime.fromisoformat(message.posted_at)
                    if posted.tzinfo is None:
                        posted = posted.replace(tzinfo=UTC)
                except ValueError:
                    posted = None

            items.append(
                RawItem(
                    source_id=spec.id,
                    external_id=f"{spec.id}-{message.chat_id}-{message.id}",
                    url=f"https://t.me/{_handle(spec, message.chat_id)}/{message.id}",
                    title=title[:400],
                    text=text[:6000],
                    lang=spec.lang or "unknown",
                    published_at=posted,
                    niche_hint=list(spec.niches),
                    source_weight=spec.weight,
                    media_links=message.links[:10],
                    media_kind="video" if message.has_media else None,
                )
            )
        return items


def _title_from(text: str) -> str:
    """First line, or the opening sentence — enough for the clusterer."""
    line = text.strip().splitlines()[0].strip()
    if len(line) >= 12:
        return line
    for stop in (". ", " — ", " – "):
        if stop in text:
            return text.split(stop, 1)[0].strip()
    return text[:120]


def _handle(spec: SourceSpec, chat_id: int) -> str:
    return (spec.params or {}).get("handle") or f"c/{chat_id}"


__all__ = ["MAX_ITEMS_PER_SOURCE", "TelegramCollector", "target_of"]
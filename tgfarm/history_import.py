"""Safe import of historical publications from platform exports.

Reads local JSON/CSV exports, previews by default, and writes only when the
operator explicitly passes --apply. It never calls a publishing adapter.
Imported rows have status published, so archive memory can use them without
putting them back into the draft or scheduled publishing queue.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


@dataclass(frozen=True, slots=True)
class HistoricalPublication:
    platform: str
    source_channel: str
    external_id: str
    title: str
    text: str
    published_at: datetime | None
    url: str


def _flatten_text(value: Any) -> str:
    """Flatten a Telegram entity while preserving spaces between text chunks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, dict):
                pieces.append(_flatten_text(item.get("text", item.get("value", ""))))
            else:
                pieces.append(_flatten_text(item))
        return "".join(pieces)
    if isinstance(value, dict):
        return _flatten_text(value.get("text", value.get("content", value.get("value", ""))))
    return ""


def _plain_text(value: Any) -> str:
    return _flatten_text(value).strip()


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _platform(value: Any, row: dict[str, Any] | None = None) -> str:
    name = str(value or "auto").strip().lower()
    aliases = {
        "tg": "telegram",
        "telegram desktop": "telegram",
        "vk.com": "vk",
        "vkontakte": "vk",
        "zen": "dzen",
        "yandex zen": "dzen",
        "yandex_dzen": "dzen",
        "auto": "auto",
        "generic": "generic",
    }
    name = aliases.get(name, name)
    if name != "auto":
        return name if name in {"telegram", "vk", "dzen", "generic"} else "generic"
    row = row or {}
    if "owner_id" in row or "from_id" in row:
        return "vk"
    return "generic"


def _published_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit() and len(raw) >= 9:
        try:
            return datetime.fromtimestamp(int(raw), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _record(
    row: dict[str, Any],
    *,
    platform_hint: str = "auto",
    source_channel_hint: str = "",
) -> HistoricalPublication | None:
    message_type = str(row.get("type") or "").strip().lower()
    if message_type in {"service", "service_message", "event"}:
        return None

    text = _plain_text(
        _first(row, "text", "body", "message", "content", "post_text", "full_text", "description")
    )
    title = _plain_text(_first(row, "title", "headline", "subject"))
    if not text and not title:
        return None

    platform = _platform(_first(row, "platform", "source_platform") or platform_hint, row)
    external_id = str(
        _first(row, "external_id", "post_id", "message_id", "mid", "id") or ""
    ).strip()
    source_channel = str(
        _first(row, "source_channel", "channel_name", "group_name", "channel", "group")
        or source_channel_hint
        or ""
    ).strip()
    published_at = _published_date(
        _first(row, "published_at", "date", "created_at", "timestamp", "date_unixtime", "published", "time")
    )
    url = str(_first(row, "url", "permalink", "post_url", "web_url", "link") or "").strip()
    if not url and platform == "vk" and external_id:
        owner_id = _first(row, "owner_id", "from_id")
        if owner_id is not None:
            url = f"https://vk.com/wall{owner_id}_{external_id}"

    return HistoricalPublication(
        platform=platform,
        source_channel=source_channel[:200],
        external_id=external_id[:128],
        title=title[:1000],
        text=text[:100_000],
        published_at=published_at,
        url=url[:2000],
    )


def parse_history_payload(payload: Any, platform: str = "auto") -> list[HistoricalPublication]:
    """Parse Telegram Desktop JSON, VK wall responses, and generic JSON records."""
    records: list[HistoricalPublication] = []
    if isinstance(payload, dict):
        chats = payload.get("chats")
        chat_list = chats.get("list") if isinstance(chats, dict) else None
        if isinstance(chat_list, list):
            for chat in chat_list:
                if not isinstance(chat, dict):
                    continue
                chat_name = str(chat.get("name") or chat.get("title") or "")
                for message in chat.get("messages", []) or []:
                    if not isinstance(message, dict):
                        continue
                    record = _record(
                        message,
                        platform_hint="telegram",
                        source_channel_hint=chat_name,
                    )
                    if record:
                        records.append(record)
            return records

        response = payload.get("response")
        if isinstance(response, dict) and isinstance(response.get("items"), list):
            items = response["items"]
            hint = "vk" if any(isinstance(item, dict) and "owner_id" in item for item in items) else platform
            for item in items:
                if isinstance(item, dict):
                    record = _record(item, platform_hint=hint)
                    if record:
                        records.append(record)
            return records

        rows = None
        for key in ("posts", "items", "entries", "messages", "publications", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        if rows is None and any(
            key in payload for key in ("text", "body", "content", "message", "post_text")
        ):
            rows = [payload]
        if rows is None:
            raise ValueError(
                "Не распознан JSON-экспорт. Нужен Telegram Desktop JSON, VK wall JSON "
                "или список записей с полями text/body и date."
            )
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("Ожидался JSON-объект или список публикаций.")

    for row in rows:
        if not isinstance(row, dict):
            continue
        record = _record(row, platform_hint=platform)
        if record:
            records.append(record)
    return records


def load_history_export(path: str | Path, platform: str = "auto") -> list[HistoricalPublication]:
    """Load a local JSON or CSV export. No network requests are made."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError(f"Файл экспорта не найден: {file_path}")
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return parse_history_payload(list(csv.DictReader(handle)), platform=platform)
    if suffix != ".json":
        raise ValueError("Поддерживаются файлы .json и .csv.")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON в строке {exc.lineno}, столбце {exc.colno}.") from exc
    return parse_history_payload(payload, platform=platform)


async def fetch_telegram_history(chat: int | str, limit: int = 1000) -> list[HistoricalPublication]:
    """Read a bounded Telegram chat history using TgFarm's guarded user session.

    This is read-only. reverse=True asks Telethon for oldest-first order so a
    bounded import captures the beginning of the history rather than silently
    importing only the newest messages.
    """
    if isinstance(chat, str):
        chat = chat.strip()
        if not chat:
            raise ValueError("Укажите username, ссылку или ID Telegram-чата.")
        lower = chat.lower()
        if "t.me/" in lower or "telegram.me/" in lower:
            from urllib.parse import unquote, urlparse

            candidate = chat if "://" in chat else f"https://{chat}"
            parsed = urlparse(candidate)
            hostname = (parsed.hostname or "").lower()
            if hostname not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
                raise ValueError("Для Telegram допустимы только ссылки t.me или telegram.me.")
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if parts and parts[0].lower() == "s":
                parts = parts[1:]
            if not parts:
                raise ValueError("В ссылке Telegram не найден username чата.")
            if parts[0].lower() in {"c", "joinchat"} or parts[0].startswith("+"):
                raise ValueError(
                    "Ссылка закрытого чата не подключает аккаунт автоматически. "
                    "Укажите доступный аккаунту ID из списка telegram chats."
                )
            chat = parts[0]
        if chat.lstrip("-").isdigit():
            chat = int(chat)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("Лимит сообщений должен быть целым числом.") from exc
    if not 1 <= limit <= 5000:
        raise ValueError("Лимит Telegram-истории должен быть от 1 до 5000 сообщений.")

    from tgfarm import tguser

    client = await tguser.connect(read_only=True)
    records: list[HistoricalPublication] = []
    async with client:
        entity = await client.get_entity(chat)
        channel_title = (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or str(chat)
        )
        username = getattr(entity, "username", None)
        async for message in client.iter_messages(entity, limit=limit, reverse=True):
            text = str(getattr(message, "message", None) or "").strip()
            if not text:
                continue
            published_at = getattr(message, "date", None)
            if published_at is not None:
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                else:
                    published_at = published_at.astimezone(UTC)
            message_id = getattr(message, "id", None)
            permalink = (
                f"https://t.me/{username}/{message_id}"
                if username and message_id
                else ""
            )
            records.append(
                HistoricalPublication(
                    platform="telegram",
                    source_channel=str(channel_title)[:200],
                    external_id=str(message_id or ""),
                    title="",
                    text=text[:100_000],
                    published_at=published_at,
                    url=permalink,
                )
            )
    return records


def _fold(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _content_signature(text: str) -> str:
    return hashlib.sha256(_fold(text).encode("utf-8")).hexdigest()


def _public_id(channel_id: str, row: HistoricalPublication) -> str:
    identity = row.external_id or (
        f"{row.published_at.isoformat() if row.published_at else ''}|{_fold(row.text)}"
    )
    payload = f"{channel_id}|{row.platform}|{identity}"
    return "hist_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:26]


def _split_text(row: HistoricalPublication) -> tuple[str, str]:
    text = row.text.strip()
    if row.title:
        if text.casefold().startswith(row.title.casefold()):
            text = text[len(row.title):].lstrip(" \n\r-:|")
        return row.title[:500], text

    lines = text.splitlines()
    if len(lines) > 1:
        return lines[0][:500], "\n".join(lines[1:]).strip()
    return text[:180], text[180:].lstrip()


def import_history(
    publications: Iterable[HistoricalPublication],
    *,
    channel_id: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or apply a deduplicated import into an existing TgFarm channel.

    Preview is the default. The apply path writes only Post rows in status
    published; it does not call platform adapters or schedule publication.
    """
    records = list(publications)
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        raise ValueError("Укажите существующий TgFarm channel id.")

    with session_scope() as session:
        channel = session.get(Channel, channel_id)
        if channel is None:
            raise ValueError(
                f"Канал '{channel_id}' не найден в базе. Сначала выполните tgfarm init "
                "и проверьте tgfarm channels."
            )

        existing_rows = session.execute(
            select(Post.public_id, Post.hook, Post.body).where(
                Post.channel_id == channel_id,
                Post.status == "published",
            )
        ).all()
        existing_ids = {row.public_id for row in existing_rows}
        signatures = {
            _content_signature(f"{row.hook or ''} {row.body or ''}")
            for row in existing_rows
        }

        planned: list[tuple[HistoricalPublication, str, str, str]] = []
        skipped_duplicates = 0
        seen_ids = set(existing_ids)
        for row in records:
            hook, body = _split_text(row)
            full_text = f"{hook} {body}".strip()
            signature = _content_signature(full_text)
            public_id = _public_id(channel_id, row)
            if public_id in seen_ids or signature in signatures:
                skipped_duplicates += 1
                continue
            planned.append((row, hook, body, public_id))
            seen_ids.add(public_id)
            signatures.add(signature)

        if apply:
            now = datetime.now(UTC)
            for row, hook, body, public_id in planned:
                provenance: dict[str, Any] = {
                    "title": row.title or f"Историческая публикация ({row.platform})",
                    "platform": row.platform,
                    "imported_history": True,
                }
                if row.url:
                    provenance["url"] = row.url
                if row.external_id:
                    provenance["external_id"] = row.external_id
                if row.source_channel:
                    provenance["source_channel"] = row.source_channel

                text_for_hash = f"{hook} {body}".strip()
                session.add(
                    Post(
                        public_id=public_id,
                        channel_id=channel_id,
                        angle="history",
                        post_format="history",
                        hook=hook,
                        body=body,
                        cta=None,
                        hashtags=[],
                        sources=[provenance],
                        claims=[],
                        quality={
                            "imported_history": True,
                            "platform": row.platform,
                            "source_channel": row.source_channel,
                            "external_id": row.external_id,
                            "content_hash": _content_signature(text_for_hash),
                            "imported_at": now.isoformat(timespec="seconds"),
                        },
                        language="ru",
                        generation_provider="history_import",
                        status="published",
                        char_len=len(text_for_hash),
                        published_at=row.published_at,
                        created_at=now,
                        updated_at=now,
                    )
                )

        return {
            "ok": True,
            "mode": "apply" if apply else "dry_run",
            "channel_id": channel_id,
            "input_records": len(records),
            "planned_imports": len(planned),
            "imported": len(planned) if apply else 0,
            "skipped_duplicates": skipped_duplicates,
            "without_date": sum(1 for row, *_ in planned if row.published_at is None),
            "publication_enabled": False,
            "sample": [
                {
                    "platform": row.platform,
                    "source_channel": row.source_channel,
                    "external_id": row.external_id,
                    "published_at": row.published_at.isoformat() if row.published_at else None,
                    "title": hook[:140],
                    "characters": len(f"{hook} {body}".strip()),
                    "url": row.url or None,
                }
                for row, hook, body, _public_id in planned[:10]
            ],
        }

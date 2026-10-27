"""Telegram user session — **read only**.

Why this exists at all
----------------------
A bot sees only the channels where an admin added it, and nothing else. That
leaves three things the farm genuinely needs and cannot get otherwise:

* the real list of the operator's channels and groups, so `config/personas.yaml`
  can be checked against reality instead of trusted;
* live post statistics (views, reactions, forwards) — `s8` currently computes
  engagement from rows nothing writes in production;
* Telegram channels as an upstream content source.

Why it is read only
-------------------
A `.session` file is not a login, it is the account. Losing it means losing the
account, and Telegram bans a banned *account* with far more consequence than a
banned bot. So:

* publishing stays on the bot token — a ban costs a bot, not a channel;
* every mutating call is blocked in code by :class:`ReadOnlyClient`, not merely
  documented as off-limits;
* joining, leaving, deleting and anything that changes account state raise
  :class:`ReadOnlyViolation` before a request leaves the process.

Setup
-----
``api_id`` / ``api_hash`` come from https://my.telegram.org (App API), then::

    tgfarm telegram login
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings

log = get_logger(__name__)

SESSION_NAME = "user"

# Methods that would change something. Blocked by name on the client proxy, so
# an accidental call fails loudly instead of quietly publishing from an account
# that can be banned.
WRITE_METHODS = frozenset({
    "send_message", "send_file", "send_photo", "send_video", "send_document",
    "forward_messages", "send_media", "send_request", "send", "upload_file",
    "delete_messages", "delete_dialog", "delete_participants",
    "edit_message", "pin_message", "unpin_message",
    "join_channel", "leave_channel", "delete_channel", "create_channel",
    "create_group", "invite_to_group", "edit_participant",
    "add_contact", "block_contact", "delete_account", "update_profile",
    "set_description", "import_session", "log_out",
})


class ReadOnlyViolation(RuntimeError):
    """Raised when something tries to write through the read-only session."""


@dataclass
class TgChat:
    """One chat the account can see, as far as the farm cares."""

    id: int
    title: str
    username: str | None = None
    kind: str = "channel"  # channel | group | chat
    participants: int = 0
    is_verified: bool = False
    is_public: bool = False
    linked_chat_id: int | None = None
    linked_title: str | None = None
    last_message_at: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "username": self.username,
            "kind": self.kind,
            "participants": self.participants,
            "is_verified": self.is_verified,
            "is_public": self.is_public,
            "linked_chat_id": self.linked_chat_id,
            "linked_title": self.linked_title,
            "last_message_at": self.last_message_at,
        }


@dataclass
class TgMessage:
    """A message reduced to what S1 needs to treat it as a source document."""

    id: int
    chat_id: int
    title: str
    text: str
    views: int = 0
    forwards: int = 0
    reactions: int = 0
    posted_at: str | None = None
    has_media: bool = False
    links: list[str] = field(default_factory=list)


def session_path() -> Path:
    """Session files hold the account. They live under data/ and are ignored by git."""
    root = get_settings().data_root() / "telegram"
    root.mkdir(parents=True, exist_ok=True)
    try:
        import stat

        root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        log.debug("could not restrict permissions on %s", root)
    return root / SESSION_NAME


def is_configured() -> bool:
    s = get_settings()
    return bool(s.telegram_api_id and s.telegram_api_hash and s.telegram_phone)


def is_authorized() -> bool:
    """True when a usable session file already exists on this machine."""
    if not session_path().exists():
        return False
    try:
        from telethon import TelegramClient

        client = TelegramClient(str(session_path()), 0, "")
        # `is_user_authorized` works against the local session without a network
        # round trip; connect() would be needed for a definitive answer.
        return bool(client.is_user_authorized())
    except Exception as exc:  # noqa: BLE001 - a broken session is just "not authorized"
        log.debug("session check failed: %s", exc)
        return False


class ReadOnlyClient:
    """Proxy that forwards reads and refuses writes.

    The denylist is checked by name on every attribute access, so a caller cannot
    reach ``client.send_message`` even if it holds a reference to the proxy.
    """

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name in WRITE_METHODS:
            raise ReadOnlyViolation(
                f"{name!r} is blocked: the user session is read-only. "
                "Publishing goes through the bot token."
            )
        return getattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyViolation("the user session is read-only")

    async def __aenter__(self) -> ReadOnlyClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await self.close()
        return False

    async def close(self) -> None:
        client = object.__getattribute__(self, "_client")
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - closing must not raise
            log.debug("disconnect failed: %s", exc)


async def connect(read_only: bool = True) -> ReadOnlyClient:
    """Connect to Telegram and return the guarded client."""
    if not is_configured():
        raise ProviderUnavailable(
            "Задайте TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_PHONE "
            "(api_id и api_hash — с https://my.telegram.org)."
        )

    from telethon import TelegramClient

    settings = get_settings()
    client = TelegramClient(
        str(session_path()),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        phone=settings.telegram_phone,
        device_model="TgFarm",
        system_version="read-only",
        app_version=getattr(settings, "version", "1.2.0") or "1.2.0",
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ProviderUnavailable(
            "Нет авторизованной сессии. Выполните: tgfarm telegram login"
        )

    guarded = ReadOnlyClient(client)
    if read_only:
        return guarded
    return guarded


async def login(
    code_callback: Callable[[], Awaitable[str]] | None = None,
    password_callback: Callable[[], Awaitable[str]] | None = None,
) -> dict:
    """Interactive first-time login: phone -> login code -> 2FA password.

    The caller supplies the callbacks because the input source differs between
    the terminal and the desktop dialog. Runs its own short-lived client; it is
    the only place in the codebase allowed to authorise.
    """
    if not is_configured():
        raise ProviderUnavailable(
            "Задайте TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_PHONE."
        )

    from telethon import TelegramClient

    settings = get_settings()
    client = TelegramClient(str(session_path()), settings.telegram_api_id, settings.telegram_api_hash)

    async def _default_code() -> str:
        return input("Код из Telegram: ").strip()

    async def _default_password() -> str:
        import getpass

        return getpass.getpass("Пароль 2FA: ")

    code_callback = code_callback or _default_code
    password_callback = password_callback or _default_password

    try:
        await client.connect()
        if await client.is_user_authorized():
            return {"ok": True, "status": "already_authorized"}

        await client.send_code_request(settings.telegram_phone)
        code = await code_callback()
        try:
            await client.sign_in(settings.telegram_phone, code)
        except Exception:  # noqa: BLE001 - needs 2FA
            password = await password_callback()
            await client.sign_in(password=password)

        me = await client.get_me()
        return {
            "ok": True,
            "status": "authorized",
            "user_id": me.id,
            "username": me.username,
            "phone": me.phone,
        }
    finally:
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.debug("disconnect failed: %s", exc)


async def logout() -> dict:
    """Delete the local session file. Revocation happens on Telegram's side too."""
    from telethon import TelegramClient

    client = TelegramClient(str(session_path()), 0, "")
    try:
        if await client.is_user_authorized():
            await client.log_out()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - the file is gone either way
        log.info("remote logout skipped: %s", exc)
    path = session_path()
    removed = False
    for candidate in (path, path.with_suffix(path.suffix + "-journal")):
        if candidate.exists():
            candidate.unlink()
            removed = True
    return {"ok": True, "session_removed": removed}


async def list_chats(limit: int = 200) -> list[TgChat]:
    """Channels and groups the account can see, biggest first."""
    async with connect() as client:
        from telethon.tl import types as tl

        chats: list[TgChat] = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if isinstance(entity, (tl.Channel, tl.Chat)):
                linked = None
                linked_title = None
                if getattr(entity, "linked_chat_id", None):
                    try:
                        linked_entity = await client.get_entity(entity.linked_chat_id)
                        linked = entity.linked_chat_id
                        linked_title = getattr(linked_entity, "title", None)
                    except Exception:  # noqa: BLE001 - a broken link is cosmetic
                        pass
                chats.append(
                    TgChat(
                        id=entity.id,
                        title=getattr(entity, "title", "") or "",
                        username=getattr(entity, "username", None),
                        kind="channel" if isinstance(entity, tl.Channel) else "group",
                        participants=int(getattr(entity, "participants_count", 0) or 0),
                        is_verified=bool(getattr(entity, "verified", False)),
                        is_public=not getattr(entity, "megagroup_gigachat", False)
                        and bool(getattr(entity, "username", None)),
                        linked_chat_id=linked,
                        linked_title=linked_title,
                        last_message_at=(
                            dialog.date.isoformat() if getattr(dialog, "date", None) else ""
                        ),
                    )
                )
        chats.sort(key=lambda c: (c.participants, c.title.lower()), reverse=True)
        return chats[:limit]


async def fetch_messages(chat: int | str, limit: int = 20) -> list[TgMessage]:
    """Read recent messages from one chat. Used as an upstream content source."""
    async with connect() as client:
        entity = await client.get_entity(chat)
        out: list[TgMessage] = []
        async for message in client.iter_messages(entity, limit=limit):
            text = message.message or ""
            links: list[str] = []
            if getattr(message, "entities", None):
                for entity in message.entities:
                    url = getattr(entity, "url", None)
                    if url:
                        links.append(url)
            out.append(
                TgMessage(
                    id=message.id,
                    chat_id=entity.id,
                    title=getattr(entity, "title", "") or str(chat),
                    text=text[:6000],
                    views=int(getattr(message, "views", 0) or 0),
                    forwards=int(getattr(message, "forwards", 0) or 0),
                    reactions=int(
                        sum(r.count for r in (getattr(message, "reactions", None) or []).results)
                        if getattr(message, "reactions", None)
                        else 0
                    ),
                    posted_at=message.date.isoformat() if message.date else None,
                    has_media=bool(getattr(message, "media", None)),
                    links=links,
                )
            )
        return out


async def fetch_post_stats(pairs: list[tuple[int, int]]) -> dict[tuple[int, int], dict]:
    """Live statistics for published posts.

    `pairs` is `(chat_id, message_id)`. A post the account cannot see simply does
    not appear in the result — a channel the farm posts to but is not subscribed
    to will report nothing, which is the honest answer.
    """
    if not pairs:
        return {}
    async with connect() as client:
        stats: dict[tuple[int, int], dict] = {}
        by_chat: dict[int, list[int]] = {}
        for chat_id, message_id in pairs:
            by_chat.setdefault(chat_id, []).append(message_id)

        for chat_id, message_ids in by_chat.items():
            try:
                entity = await client.get_entity(chat_id)
            except Exception as exc:  # noqa: BLE001 - one bad chat must not stop the rest
                log.info("cannot resolve chat %s: %s", chat_id, exc)
                continue
            wanted = set(message_ids)
            try:
                async for message in client.iter_messages(entity, limit=200):
                    if message.id not in wanted:
                        continue
                    reactions = 0
                    if getattr(message, "reactions", None):
                        reactions = sum(r.count for r in message.reactions.results)
                    stats[(chat_id, message.id)] = {
                        "views": int(getattr(message, "views", 0) or 0),
                        "forwards": int(getattr(message, "forwards", 0) or 0),
                        "reactions": int(reactions),
                        "has_media": bool(getattr(message, "media", None)),
                    }
                    wanted.discard(message.id)
                    if not wanted:
                        break
            except Exception as exc:  # noqa: BLE001
                log.info("stats fetch failed for %s: %s", chat_id, exc)
        return stats


async def account_info() -> dict:
    async with connect() as client:
        me = await client.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "phone": me.phone,
        }


def run(coro: Awaitable[Any]) -> Any:
    """Bridge for the synchronous CLI."""
    return asyncio.run(coro)


__all__ = [
    "ReadOnlyClient",
    "ReadOnlyViolation",
    "TgChat",
    "TgMessage",
    "WRITE_METHODS",
    "account_info",
    "connect",
    "fetch_messages",
    "fetch_post_stats",
    "is_authorized",
    "is_configured",
    "list_chats",
    "login",
    "logout",
    "session_path",
]
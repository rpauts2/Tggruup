"""Official MAX Bot API helpers: webhook registration and verified destinations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

API_ROOT = "https://platform-api2.max.ru"
WEBHOOK_PATH = "/api/platforms/max/webhook"
WEBHOOK_EVENTS = (
    "bot_added",
    "bot_removed",
    "chat_title_changed",
    "bot_admin_permissions_changed",
)
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
_CHAT_ID_RE = re.compile(r"^-?[0-9]{1,20}$")


def build_max_webhook_url(public_base_url: str) -> str:
    """Require a public HTTPS origin on the only supported MAX webhook port (443)."""
    value = str(public_base_url or "").strip()
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("TGFARM_PUBLIC_BASE_URL содержит некорректный порт.") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not host
        or host in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Для MAX нужен TGFARM_PUBLIC_BASE_URL вида https://ваш-домен без пути и порта. "
            "Webhook доступен только через HTTPS на 443."
        )
    return f"https://{host}{WEBHOOK_PATH}"


def parse_max_discovered_chats(raw: str | list | None) -> list[dict[str, object]]:
    """Parse and sanitize the small owner-local discovery cache."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value[:1000]:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("id") or "").strip()
        if not _CHAT_ID_RE.fullmatch(chat_id) or chat_id == "0" or chat_id in seen:
            continue
        seen.add(chat_id)
        is_channel = item.get("is_channel") is True
        result.append({
            "id": chat_id,
            "name": str(item.get("name") or ("Канал MAX" if is_channel else "Групповой чат MAX"))[:160],
            "is_channel": is_channel,
            "kind": "channel" if is_channel else "chat",
            "added_at": str(item.get("added_at") or "")[:40],
            "needs_permission_check": bool(item.get("needs_permission_check", True)),
        })
    result.sort(key=lambda item: (not bool(item["is_channel"]), str(item["name"]).casefold(), str(item["id"])))
    return result


def apply_max_webhook_event(
    chats: list[dict[str, object]],
    update: dict[str, object],
) -> tuple[list[dict[str, object]], bool]:
    """Apply only lifecycle events for group chats/channels, never private dialogs."""
    event_type = str(update.get("update_type") or "").strip()
    chat_id = str(update.get("chat_id") or "").strip()
    if not _CHAT_ID_RE.fullmatch(chat_id) or chat_id == "0":
        return chats, False
    current = parse_max_discovered_chats(chats)
    index = next((i for i, row in enumerate(current) if row["id"] == chat_id), None)

    if event_type == "bot_added":
        # MAX's bot_started event refers to a private dialog, so only bot_added
        # with an explicit is_channel flag is admitted as a publishing target.
        if not isinstance(update.get("is_channel"), bool):
            return current, False
        is_channel = bool(update["is_channel"])
        title = str(update.get("title") or "").strip()
        row = {
            "id": chat_id,
            "name": title[:160] if title else ("Канал MAX" if is_channel else "Групповой чат MAX"),
            "is_channel": is_channel,
            "kind": "channel" if is_channel else "chat",
            "added_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "needs_permission_check": True,
        }
        if index is None:
            current.append(row)
        else:
            current[index] = {**current[index], **row}
        return parse_max_discovered_chats(current), True

    if event_type == "bot_removed":
        if index is None:
            return current, False
        del current[index]
        return parse_max_discovered_chats(current), True

    if event_type == "chat_title_changed":
        if index is None:
            return current, False
        title = str(update.get("title") or "").strip()
        if not title:
            return current, False
        current[index] = {**current[index], "name": title[:160], "needs_permission_check": True}
        return parse_max_discovered_chats(current), True

    if event_type == "bot_admin_permissions_changed":
        if index is None:
            return current, False
        current[index] = {**current[index], "needs_permission_check": True}
        return parse_max_discovered_chats(current), True

    return current, False


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict | None = None,
) -> dict:
    try:
        response = await client.request(
            method,
            f"{API_ROOT}{path}",
            params=params,
            json=json_body,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError("MAX не ответил вовремя. Повтори проверку позже.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось связаться с MAX API.") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"MAX API вернул некорректный ответ (HTTP {response.status_code}).") from exc
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise RuntimeError("MAX отклонил доступ. Проверь токен бота и его права.")
        if response.status_code == 404:
            raise RuntimeError("MAX не нашёл выбранный чат или канал.")
        if response.status_code == 429:
            raise RuntimeError("MAX временно ограничил запросы. Повтори позже.")
        raise RuntimeError(f"MAX API ответил HTTP {response.status_code}.")
    if not isinstance(payload, dict):
        raise RuntimeError("MAX API вернул неожиданный формат ответа.")
    if payload.get("success") is False:
        message = str(payload.get("message") or "").lower()
        if "already" not in message and "exist" not in message:
            raise RuntimeError("MAX не подтвердил выполнение запроса.")
    return payload


async def register_max_webhook(
    token: str,
    webhook_url: str,
    secret: str,
) -> dict[str, object]:
    """Verify the bot and install the official HTTPS subscription."""
    token = str(token or "").strip()
    if not token:
        raise ValueError("Введи MAX_ACCESS_TOKEN.")
    if not _SECRET_RE.fullmatch(str(secret or "")):
        raise ValueError("MAX webhook secret должен содержать 5–256 символов A–Z, a–z, 0–9, _ или -.")
    url = str(webhook_url or "").strip()
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MAX webhook URL содержит некорректный порт.") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() in {"", "localhost", "127.0.0.1", "::1"}
        or port not in (None, 443)
        or parsed.path != WEBHOOK_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MAX webhook URL не соответствует публичному HTTPS endpoint на 443.")

    headers = {"Authorization": token, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, headers=headers) as client:
        bot = await _json_request(client, "GET", "/me")
        result = await _json_request(
            client,
            "POST",
            "/subscriptions",
            json_body={
                "url": url,
                "update_types": list(WEBHOOK_EVENTS),
                "secret": secret,
            },
        )
    return {
        "url": url,
        "events": list(WEBHOOK_EVENTS),
        "bot_name": str(bot.get("name") or bot.get("username") or "MAX bot")[:160],
        "bot_id": str(bot.get("user_id") or bot.get("id") or "")[:40],
        "success": result.get("success") is not False,
    }


async def select_max_chat(token: str, chat_id: str) -> dict[str, object]:
    """Recheck current bot admin permissions and the explicit write grant."""
    value = str(chat_id or "").strip()
    if not _CHAT_ID_RE.fullmatch(value) or value == "0":
        raise ValueError("ID MAX должен быть числовым chat_id из webhook-события.")
    headers = {"Authorization": str(token or "").strip(), "Accept": "application/json"}
    if not headers["Authorization"]:
        raise RuntimeError("Сначала сохрани MAX_ACCESS_TOKEN.")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, headers=headers) as client:
        member = await _json_request(client, "GET", f"/chats/{value}/members/me")
        chat = await _json_request(client, "GET", f"/chats/{value}")
    is_admin = member.get("is_admin") is True
    is_owner = member.get("is_owner") is True
    permissions = {
        str(permission).strip().lower()
        for permission in (member.get("permissions") or [])
    }
    can_write = is_owner or (is_admin and bool(permissions & {"write", "post_edit_delete_message"}))
    if not can_write:
        raise RuntimeError(
            "MAX подтвердил присутствие бота, но не подтвердил право write для публикации в этом чате."
        )
    title = str(chat.get("title") or "")[:160]
    return {
        "id": value,
        "name": title or "MAX channel/chat",
        "is_channel": chat.get("type") == "channel" or chat.get("is_channel") is True,
        "role": "owner" if is_owner else "admin",
        "permissions": sorted(permissions),
    }


def verify_max_webhook_secret(received: str | None, expected: str | None) -> bool:
    import hmac

    received_value = str(received or "")
    expected_value = str(expected or "")
    return bool(expected_value) and hmac.compare_digest(received_value, expected_value)


__all__ = [
    "API_ROOT",
    "WEBHOOK_EVENTS",
    "WEBHOOK_PATH",
    "apply_max_webhook_event",
    "build_max_webhook_url",
    "parse_max_discovered_chats",
    "register_max_webhook",
    "select_max_chat",
    "verify_max_webhook_secret",
]

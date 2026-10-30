"""Google OAuth 2.0 flow for the YouTube Data API.

Uses authorization code + state, requests offline access, and verifies the
connected channel before TgFarm marks the integration as configured.
Secrets and provider responses are never logged.
"""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)

_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("YouTube OAuth callback URI имеет неверный формат.") from exc
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    allowed_schemes = {"https", "http"} if is_loopback else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError(
            "Callback URI YouTube должен быть HTTPS-адресом или loopback URL локальной панели."
        )
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_youtube_oauth(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, object]:
    """Create an authorization URL with a one-time CSRF state."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_id or not client_secret:
        raise ValueError("Сначала сохрани Google OAuth Client ID и Client Secret.")
    if len(client_id) > 2048 or len(client_secret) > 4096:
        raise ValueError("Параметры Google OAuth имеют недопустимый размер.")

    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_youtube_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def _exchange_token(data: dict[str, str]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(TOKEN_URL, data=data)
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Google OAuth. Проверь сеть и повтори попытку.") from exc
    if response.status_code >= 400:
        # Google error bodies can carry request details. Don't return them directly.
        raise RuntimeError(
            f"Google отклонил OAuth-запрос (HTTP {response.status_code}). "
            "Проверь Client ID, Client Secret и callback URI."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Google вернул неожиданный ответ OAuth.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Google вернул неожиданный ответ OAuth.")
    return payload


async def _verify_channel(access_token: str) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.get(
                CHANNELS_URL,
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось проверить YouTube-канал после OAuth.") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"Google выдал токен, но проверка YouTube-канала не прошла (HTTP {response.status_code}). "
            "Проверь YouTube Data API v3 и OAuth scopes."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("YouTube вернул неожиданные данные канала.") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    item = items[0] if isinstance(items, list) and items else {}
    snippet = item.get("snippet") if isinstance(item, dict) else {}
    snippet = snippet if isinstance(snippet, dict) else {}
    channel_id = str(item.get("id") or "") if isinstance(item, dict) else ""
    title = str(snippet.get("title") or "")
    if not channel_id or not title:
        raise RuntimeError("YouTube не подтвердил канал. Выбери канал и повтори OAuth.")
    return {"channel_id": channel_id, "channel_title": title}


async def complete_youtube_oauth(state: str, code: str) -> dict[str, str]:
    """Consume state, exchange code, and verify the channel identity."""
    state = str(state or "")
    code = str(code or "")
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ YouTube OAuth неполный. Запусти подключение ещё раз.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия YouTube OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия YouTube OAuth истекла. Запусти подключение заново.")

    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    token = await _exchange_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    access_token = str(token.get("access_token") or "")
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        # Google only issues a refresh token for some consent events. Reuse an
        # existing one if the operator reconnects the same app/account.
        from tgfarm.core.settings import get_settings

        refresh_token = str(get_settings().youtube_refresh_token or "").strip()
    if not access_token or not refresh_token:
        raise RuntimeError(
            "Google не выдал долговременный refresh token. Проверь access_type=offline, "
            "отзови старый grant в Google Account и подключи YouTube снова."
        )
    try:
        expires_in = max(60, int(token.get("expires_in") or 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    channel = await _verify_channel(access_token)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": str(int(time.time()) + expires_in),
        "channel_id": channel["channel_id"],
        "channel_title": channel["channel_title"],
        "account": channel["channel_title"],
    }


async def get_valid_youtube_access_token() -> str:
    """Return a valid access token, refreshing it when it expires."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    access_token = str(settings.youtube_access_token or "").strip()
    refresh_token = str(settings.youtube_refresh_token or "").strip()
    client_id = str(settings.youtube_client_id or "").strip()
    client_secret = str(settings.youtube_client_secret or "").strip()
    try:
        expires_at = int(settings.youtube_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if access_token and expires_at > int(time.time()) + 120:
        return access_token
    if not (refresh_token and client_id and client_secret):
        raise RuntimeError("YouTube OAuth не подключён или истёк. Переподключи канал через OAuth.")

    token = await _exchange_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    new_access_token = str(token.get("access_token") or "")
    if not new_access_token:
        raise RuntimeError("Google не вернул новый access token. Переподключи YouTube.")
    new_refresh_token = str(token.get("refresh_token") or refresh_token)
    try:
        expires_in = max(60, int(token.get("expires_in") or 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    new_expiry = str(int(time.time()) + expires_in)
    save_platform_credentials(
        "youtube",
        {
            "YOUTUBE_ACCESS_TOKEN": new_access_token,
            "YOUTUBE_REFRESH_TOKEN": new_refresh_token,
            "YOUTUBE_TOKEN_EXPIRES_AT": new_expiry,
        },
    )
    return new_access_token


__all__ = [
    "SCOPES",
    "STATE_TTL_SECONDS",
    "begin_youtube_oauth",
    "cancel_youtube_oauth",
    "complete_youtube_oauth",
    "get_valid_youtube_access_token",
]

"""Google OAuth and token refresh for the YouTube Data API.

OAuth state is one-time, expires quickly, and never appears in logs. Only the
refresh token and confirmed channel identity are persisted in the local .env.
"""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

_PENDING: dict[str, dict[str, object]] = {}


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("YouTube OAuth callback URL имеет неверный формат.") from exc
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("Callback YouTube должен быть HTTPS-адресом или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_youtube_oauth(client_id: str, client_secret: str, redirect_uri: str) -> dict[str, object]:
    """Build Google consent URL with offline access for long-running uploads."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_id or not client_secret:
        raise ValueError("Сначала сохрани YouTube Google OAuth Client ID и Client Secret.")
    if len(client_id) > 2048 or len(client_secret) > 4096:
        raise ValueError("Параметры Google OAuth имеют недопустимый размер.")
    state = secrets.token_urlsafe(32)
    with_state = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "created_at": time.time(),
    }
    _prune_pending(time.time())
    _PENDING[state] = with_state
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_youtube_oauth(state: str) -> None:
    _PENDING.pop(str(state or ""), None)


async def complete_youtube_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange the authorization code and confirm the channel with a read call."""
    state = str(state or "")
    code = str(code or "").split("#", 1)[0]
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ YouTube OAuth неполный. Запусти подключение заново.")
    transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия YouTube OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия YouTube OAuth истекла. Запусти подключение заново.")

    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google отклонил OAuth-код (HTTP {response.status_code}). "
                    "Проверь callback URL, OAuth consent и разрешённые redirect URI."
                )
            try:
                token_data = response.json()
            except ValueError as exc:
                raise RuntimeError("Google вернул неожиданный ответ при обмене OAuth-кода.") from exc
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            if not access_token:
                raise RuntimeError("Google не выдал access token.")
            if not refresh_token:
                raise RuntimeError(
                    "Google не вернул refresh token. Удали доступ TgFarm в аккаунте Google "
                    "и повтори подключение с согласием доступа."
                )
            channel_response = await client.get(
                CHANNELS_URL,
                params={"part": "id,snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if channel_response.status_code >= 400:
                raise RuntimeError(
                    f"YouTube не подтвердил канал (HTTP {channel_response.status_code}). "
                    "Проверь доступ youtube.readonly и наличие канала."
                )
            try:
                payload = channel_response.json()
            except ValueError as exc:
                raise RuntimeError("YouTube вернул неожиданный ответ списка каналов.") from exc
            items = payload.get("items") or []
            if not items:
                raise RuntimeError("Google OAuth подтверждён, но канал YouTube не найден.")
            channel = items[0]
            channel_id = str(channel.get("id") or "")
            title = str((channel.get("snippet") or {}).get("title") or "")
            if not channel_id:
                raise RuntimeError("YouTube не вернул ID подключённого канала.")
            return {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "channel_id": channel_id,
                "channel_title": title[:160],
                "account": title or channel_id,
            }
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Google/YouTube OAuth. Проверь интернет и повтори.") from exc


async def get_youtube_access_token() -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    if not (settings.youtube_client_id and settings.youtube_client_secret and settings.youtube_refresh_token):
        raise RuntimeError("Сначала подключи YouTube через OAuth.")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.youtube_client_id,
                    "client_secret": settings.youtube_client_secret,
                    "refresh_token": settings.youtube_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code >= 400:
                if response.status_code in (400, 401):
                    raise RuntimeError("YouTube OAuth истёк или отозван. Переподключи канал через OAuth.")
                raise RuntimeError(f"Google OAuth временно недоступен (HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Google вернул неожиданный ответ при обновлении токена.") from exc
            token = str(payload.get("access_token") or "")
            if not token:
                raise RuntimeError("Google не вернул access token при обновлении.")
            return token
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обновить YouTube OAuth. Проверь сеть и повтори.") from exc

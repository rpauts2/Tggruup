"""TikTok OAuth, refresh and creator-info helpers.

Direct-post publishing is not run as a background crosspost. The dashboard must
show current creator privacy options and require an explicit confirmation for
each video before bytes are sent to TikTok.
"""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
SCOPES = "user.info.basic,video.publish"

_PENDING: dict[str, dict[str, object]] = {}


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("TikTok OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Callback TikTok должен быть HTTPS-адресом или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_tiktok_oauth(client_key: str, client_secret: str, redirect_uri: str) -> dict[str, object]:
    """Build TikTok consent URL with one-time state."""
    client_key = str(client_key or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_key or not client_secret:
        raise ValueError("Сначала сохрани TikTok Client Key и Client Secret.")
    if len(client_key) > 2048 or len(client_secret) > 4096:
        raise ValueError("Параметры TikTok OAuth имеют недопустимый размер.")
    state = secrets.token_urlsafe(32)
    _prune_pending(time.time())
    _PENDING[state] = {
        "client_key": client_key,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "created_at": time.time(),
    }
    query = urlencode({
        "client_key": client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_tiktok_oauth(state: str) -> None:
    _PENDING.pop(str(state or ""), None)


async def complete_tiktok_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange the code, then verify the token can query TikTok creator info."""
    state = str(state or "")
    code = str(code or "").split("#", 1)[0]
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ TikTok OAuth неполный. Запусти подключение заново.")
    transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия TikTok OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия TikTok OAuth истекла. Запусти подключение заново.")

    client_key = str(transaction["client_key"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_key": client_key,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"TikTok отклонил OAuth-код (HTTP {response.status_code}). "
                    "Проверь callback URL, scope и настройки приложения."
                )
            try:
                token_data = response.json()
            except ValueError as exc:
                raise RuntimeError("TikTok вернул неожиданный ответ при обмене OAuth-кода.") from exc
            data = token_data.get("data") or {}
            access_token = str(data.get("access_token") or "")
            refresh_token = str(data.get("refresh_token") or "")
            open_id = str(data.get("open_id") or "")
            if not access_token or not refresh_token or not open_id:
                raise RuntimeError("TikTok не выдал полный набор OAuth-токенов. Подключи приложение заново.")
            try:
                expires_at = int(time.time()) + max(60, int(data.get("expires_in") or 86400))
                refresh_expires_at = int(time.time()) + max(60, int(data.get("refresh_expires_in") or 31536000))
            except (TypeError, ValueError):
                expires_at = int(time.time()) + 86400
                refresh_expires_at = int(time.time()) + 31536000

            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            creator_response = await client.post(CREATOR_INFO_URL, headers=headers, json={})
            if creator_response.status_code >= 400:
                raise RuntimeError(
                    f"TikTok OAuth прошёл, но creator-info не подтвердил права (HTTP {creator_response.status_code}). "
                    "Проверь одобрение scope video.publish."
                )
            try:
                creator_payload = creator_response.json()
            except ValueError as exc:
                raise RuntimeError("TikTok вернул неожиданный creator-info ответ.") from exc
            if (creator_payload.get("error") or {}).get("code") not in (None, "ok"):
                raise RuntimeError("TikTok не подтвердил scope video.publish. Проверь одобрение приложения.")
            creator = creator_payload.get("data") or {}
            username = str(creator.get("creator_username") or "")
            return {
                "client_key": client_key,
                "client_secret": client_secret,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expires_at": str(expires_at),
                "refresh_token_expires_at": str(refresh_expires_at),
                "open_id": open_id,
                "username": username[:160],
                "account": ("@" + username) if username else "TikTok creator",
            }
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к TikTok OAuth. Проверь сеть и повтори.") from exc


async def get_valid_tiktok_access_token(*, force_refresh: bool = False) -> str:
    """Refresh the TikTok token before expiry and persist rotating credentials."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    access = str(settings.tiktok_access_token or "").strip()
    refresh = str(settings.tiktok_refresh_token or "").strip()
    now = int(time.time())
    expires_at = int(settings.tiktok_token_expires_at or 0)
    if access and expires_at > now + 120 and not force_refresh:
        return access
    if not (settings.tiktok_client_key and settings.tiktok_client_secret and refresh):
        if access and expires_at == 0 and not force_refresh:
            return access
        raise RuntimeError("TikTok OAuth истёк или не подключён. Повтори вход через OAuth.")

    refresh_expiry = int(settings.tiktok_refresh_token_expires_at or 0)
    if refresh_expiry and refresh_expiry <= now:
        raise RuntimeError("TikTok refresh token истёк. Подключи аккаунт через OAuth заново.")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_key": settings.tiktok_client_key,
                    "client_secret": settings.tiktok_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                if response.status_code in (400, 401):
                    raise RuntimeError("TikTok OAuth истёк или отозван. Переподключи аккаунт.")
                raise RuntimeError(f"TikTok OAuth временно недоступен (HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("TikTok вернул неожиданный ответ при обновлении токена.") from exc
            data = payload.get("data") or {}
            new_access = str(data.get("access_token") or "")
            new_refresh = str(data.get("refresh_token") or refresh)
            if not new_access:
                raise RuntimeError("TikTok не вернул access token при обновлении.")
            try:
                new_access_expires = int(time.time()) + max(60, int(data.get("expires_in") or 86400))
                new_refresh_expires = int(time.time()) + max(60, int(data.get("refresh_expires_in") or (refresh_expiry - now if refresh_expiry else 31536000)))
            except (TypeError, ValueError):
                new_access_expires = int(time.time()) + 86400
                new_refresh_expires = refresh_expiry or (int(time.time()) + 31536000)
            save_platform_credentials("tiktok", {
                "TIKTOK_ACCESS_TOKEN": new_access,
                "TIKTOK_REFRESH_TOKEN": new_refresh,
                "TIKTOK_TOKEN_EXPIRES_AT": str(new_access_expires),
                "TIKTOK_REFRESH_TOKEN_EXPIRES_AT": str(new_refresh_expires),
            })
            return new_access
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обновить TikTok OAuth. Проверь сеть и повтори.") from exc


async def get_tiktok_creator_info(*, access_token: str | None = None) -> dict:
    """Return only the creator fields needed by the publishing confirmation UI."""
    token = access_token or await get_valid_tiktok_access_token()
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                CREATOR_INFO_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={},
            )
            if response.status_code >= 400:
                if response.status_code in (401, 403):
                    raise RuntimeError("TikTok отклонил доступ. Проверь токен и разрешение video.publish.")
                if response.status_code == 429:
                    raise RuntimeError("TikTok ограничил запросы. Повтори проверку позже.")
                raise RuntimeError(f"TikTok ответил HTTP {response.status_code}.")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("TikTok вернул неожиданный ответ creator-info.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к TikTok. Проверь сеть и повтори.") from exc
    error = payload.get("error") or {}
    if error.get("code") not in (None, "ok"):
        raise RuntimeError("TikTok не подтвердил возможность публикации. Проверь scope и статус приложения.")
    data = payload.get("data") or {}
    options = data.get("privacy_level_options") or []
    if not options:
        raise RuntimeError("TikTok не вернул доступные варианты приватности для этого аккаунта.")
    return {
        "username": str(data.get("creator_username") or ""),
        "nickname": str(data.get("creator_nickname") or ""),
        "privacy_level_options": [str(value) for value in options],
        "max_video_post_duration_sec": int(data.get("max_video_post_duration_sec") or 0),
        "comment_disabled": bool(data.get("comment_disabled")),
        "duet_disabled": bool(data.get("duet_disabled")),
        "stitch_disabled": bool(data.get("stitch_disabled")),
    }

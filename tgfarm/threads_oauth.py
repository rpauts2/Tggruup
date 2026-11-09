"""Threads OAuth 2.0 and long-lived token management.

The authorization callback is state-bound and one-time. Tokens are stored only
in the local .env and are never returned from this module to the browser.
"""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
SCOPES = "threads_basic,threads_content_publish"
AUTHORIZE_URL = "https://threads.net/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
GRAPH_URL = "https://graph.threads.net/v1.0"
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


class ThreadsOAuthTemporaryError(RuntimeError):
    """Network or provider-side refresh failure: no post exists yet, a retry is safe."""


def _refresh_is_temporary(status_code: int) -> bool:
    return status_code in (408, 425, 429) or status_code >= 500



def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Threads OAuth callback URL имеет неверный формат.") from exc
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"http", "https"} if is_loopback else {"https"})
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("Callback URL для Threads должен быть HTTPS-адресом или loopback URL для локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_threads_oauth(client_id: str, client_secret: str, redirect_uri: str) -> dict[str, object]:
    """Build a Threads consent URL with an unpredictable, one-use state."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_id or not client_secret:
        raise ValueError("Сначала сохрани Threads App ID и App Secret.")
    if len(client_id) > 1024 or len(client_secret) > 4096:
        raise ValueError("Параметры Threads OAuth-приложения имеют недопустимый размер.")
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_threads_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def complete_threads_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange one-time code, upgrade token lifetime and verify the profile."""
    state = str(state or "")
    code = str(code or "").split("#", 1)[0]
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Threads OAuth неполный. Запусти подключение ещё раз.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия Threads OAuth истекла или уже использована. Запусти подключение ещё раз.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Threads OAuth истекла. Запусти подключение ещё раз.")

    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(TOKEN_URL, params={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            })
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Threads отклонил OAuth-код (HTTP {response.status_code}). "
                    "Проверь redirect URI и настройки Meta App."
                )
            try:
                short_token_data = response.json()
            except ValueError as exc:
                raise RuntimeError("Threads вернул неожиданный ответ при обмене OAuth-кода.") from exc
            short_token = str(short_token_data.get("access_token") or "")
            if not short_token:
                raise RuntimeError("Threads не выдал пользовательский access token.")

            # Long-lived tokens reduce re-login frequency. Never persist the short token.
            long_response = await client.get(
                "https://graph.threads.net/access_token",
                params={
                    "grant_type": "th_exchange_token",
                    "client_secret": client_secret,
                    "access_token": short_token,
                },
            )
            if long_response.status_code >= 400:
                raise RuntimeError(
                    f"Threads не смог продлить OAuth-токен (HTTP {long_response.status_code}). "
                    "Запусти подключение заново."
                )
            try:
                token_data = long_response.json()
            except ValueError as exc:
                raise RuntimeError("Threads вернул неожиданный ответ при продлении токена.") from exc
            access_token = str(token_data.get("access_token") or "")
            if not access_token:
                raise RuntimeError("Threads не вернул долгоживущий токен.")
            try:
                expires_in = max(60, int(token_data.get("expires_in") or 5184000))
            except (TypeError, ValueError):
                expires_in = 5184000

            profile_response = await client.get(
                f"{GRAPH_URL}/me",
                params={"fields": "id,username", "access_token": access_token},
            )
            if profile_response.status_code >= 400:
                raise RuntimeError(
                    f"Threads выдал токен, но не подтвердил профиль (HTTP {profile_response.status_code})."
                )
            try:
                profile = profile_response.json()
            except ValueError as exc:
                raise RuntimeError("Threads вернул неожиданный профиль аккаунта.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Threads не ответил вовремя. Запусти OAuth-подключение ещё раз.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Threads. Проверь сеть и повтори попытку.") from exc

    user = profile if isinstance(profile, dict) else {}
    user_id = str(user.get("id") or token_data.get("user_id") or short_token_data.get("user_id") or "")
    username = str(user.get("username") or "").strip()
    if not user_id or not username:
        raise RuntimeError("Threads не вернул ID и имя подключённого аккаунта.")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "expires_at": str(int(time.time()) + expires_in),
        "user_id": user_id,
        "username": username,
        "account": "@" + username,
    }


async def get_valid_threads_access_token() -> str:
    """Refresh an expiring long-lived Threads token, persist rotations, return it."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    token = str(settings.threads_access_token or "").strip()
    try:
        expires_at = int(settings.threads_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if not token:
        raise RuntimeError("Threads не подключён. Повтори OAuth-подключение.")
    # Refresh only when fewer than seven days remain. Meta requires a still-valid long-lived token.
    if not expires_at or expires_at > int(time.time()) + 7 * 86400:
        return token
    if expires_at <= int(time.time()):
        raise RuntimeError("Срок Threads OAuth-токена истёк. Переподключи Threads.")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(
                "https://graph.threads.net/refresh_access_token",
                params={"grant_type": "th_refresh_token", "access_token": token},
            )
    except httpx.RequestError as exc:
        raise ThreadsOAuthTemporaryError("Не удалось обновить Threads OAuth-токен.") from exc
    if response.status_code >= 400:
        if _refresh_is_temporary(response.status_code):
            raise ThreadsOAuthTemporaryError(
                f"Threads временно не обновил OAuth-токен (HTTP {response.status_code})."
            )
        raise RuntimeError(
            f"Threads не обновил OAuth-токен (HTTP {response.status_code}). Переподключи аккаунт."
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Threads вернул неожиданный ответ при обновлении токена.") from exc
    new_token = str(data.get("access_token") or "")
    if not new_token:
        raise RuntimeError("Threads не вернул обновлённый access token.")
    try:
        lifetime = max(60, int(data.get("expires_in") or 5184000))
    except (TypeError, ValueError):
        lifetime = 5184000
    save_platform_credentials("threads", {
        "THREADS_ACCESS_TOKEN": new_token,
        "THREADS_TOKEN_EXPIRES_AT": str(int(time.time()) + lifetime),
    })
    return new_token

"""X OAuth 2.0 authorization-code flow with PKCE and refresh-token support.

Authorization codes, secrets, PKCE verifiers and access tokens are not logged.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
SCOPES = "tweet.read users.read tweet.write offline.access"
AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"


class XOAuthTemporaryError(RuntimeError):
    """Transient provider/network failure that should not permanently reject a post."""

_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("X OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Callback URL для X должен быть HTTPS-адресом или loopback URL для локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_x_oauth(client_id: str, client_secret: str, redirect_uri: str) -> dict[str, object]:
    """Build an X authorization URL using a one-time state and PKCE S256."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_id or not client_secret:
        raise ValueError("Сначала настрой X OAuth Client ID и Client Secret.")
    if len(client_id) > 1024 or len(client_secret) > 4096:
        raise ValueError("Параметры X OAuth-приложения имеют недопустимый размер.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "created_at": time.time(),
        }
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_x_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def _exchange_token(client: httpx.AsyncClient, *, data: dict[str, str], client_id: str, client_secret: str) -> dict:
    try:
        response = await client.post(TOKEN_URL, data=data, auth=(client_id, client_secret))
    except httpx.RequestError as exc:
        raise XOAuthTemporaryError("Не удалось подключиться к X OAuth. Проверь сеть и повтори попытку.") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"X отклонил OAuth-запрос (HTTP {response.status_code}). Проверь OAuth-настройки и callback URL."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("X вернул неожиданный ответ OAuth.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("X вернул неожиданный ответ OAuth.")
    return payload


async def complete_x_oauth(state: str, code: str) -> dict[str, str]:
    """Consume state, exchange authorization code, and verify the X identity."""
    state = str(state or "")
    code = str(code or "")
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ X OAuth неполный. Запусти подключение ещё раз.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия X OAuth истекла или уже использована. Запусти подключение ещё раз.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия X OAuth истекла. Запусти подключение ещё раз.")
    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    verifier = str(transaction["code_verifier"])
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            token = await _exchange_token(
                client,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
                client_id=client_id,
                client_secret=client_secret,
            )
            access_token = str(token.get("access_token") or "")
            refresh_token = str(token.get("refresh_token") or "")
            if not access_token or not refresh_token:
                raise RuntimeError("X не выдал долговечный пользовательский токен. Проверь разрешение offline.access.")
            scopes = str(token.get("scope") or "").split()
            if scopes and not {"users.read", "tweet.write"}.issubset(set(scopes)):
                raise RuntimeError("X не выдал разрешения users.read и tweet.write. Повтори авторизацию.")
            try:
                expires_in = max(60, int(token.get("expires_in") or 7200))
            except (TypeError, ValueError):
                expires_in = 7200
            profile_response = await client.get(
                "https://api.x.com/2/users/me",
                params={"user.fields": "id,name,username"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if profile_response.status_code >= 400:
                raise RuntimeError(
                    f"X выдал токен, но не подтвердил профиль (HTTP {profile_response.status_code})."
                )
            try:
                profile = profile_response.json()
            except ValueError as exc:
                raise RuntimeError("X вернул неожиданный профиль.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("X не ответил вовремя. Запусти OAuth-подключение ещё раз.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к X. Проверь сеть и повтори попытку.") from exc
    user = profile.get("data") if isinstance(profile, dict) else None
    user = user if isinstance(user, dict) else {}
    user_id = str(user.get("id") or "")
    username = str(user.get("username") or "")
    if not user_id or not username:
        raise RuntimeError("X не вернул ID и имя пользователя для подключённого аккаунта.")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": str(int(time.time()) + expires_in),
        "user_id": user_id,
        "username": username,
        "account": "@" + username,
    }


async def get_valid_x_access_token() -> str:
    """Return a non-expired user token, refreshing it when needed."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    access_token = str(settings.x_access_token or "").strip()
    refresh_token = str(settings.x_refresh_token or "").strip()
    client_id = str(settings.x_oauth_client_id or "").strip()
    client_secret = str(settings.x_oauth_client_secret or "").strip()
    try:
        expires_at = int(settings.x_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if access_token and expires_at > int(time.time()) + 120:
        return access_token
    if not (refresh_token and client_id and client_secret):
        if access_token and expires_at == 0 and not refresh_token:
            return access_token
        raise RuntimeError("Срок X OAuth-токена истёк. Переподключи X через OAuth.")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            token = await _exchange_token(
                client,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                client_id=client_id,
                client_secret=client_secret,
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError("X не ответил при обновлении OAuth-токена.") from exc
    new_access = str(token.get("access_token") or "")
    new_refresh = str(token.get("refresh_token") or refresh_token)
    if not new_access:
        raise RuntimeError("X не вернул обновлённый access token. Переподключи аккаунт.")
    try:
        expires_in = max(60, int(token.get("expires_in") or 7200))
    except (TypeError, ValueError):
        expires_in = 7200
    new_expiry = int(time.time()) + expires_in
    # Refresh tokens can rotate. Save the new pair atomically to local .env.
    save_platform_credentials("x", {
        "X_ACCESS_TOKEN": new_access,
        "X_REFRESH_TOKEN": new_refresh,
        "X_TOKEN_EXPIRES_AT": str(new_expiry),
    })
    return new_access

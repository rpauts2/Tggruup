"""Reddit authorization-code OAuth with a permanent refresh token."""

from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
ME_URL = "https://oauth.reddit.com/api/v1/me"
SCOPES = ("identity", "read", "submit")
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Reddit OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Reddit callback URL должен быть HTTPS или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_reddit_oauth(
    client_id: str, client_secret: str, redirect_uri: str
) -> dict[str, object]:
    """Create a one-use OAuth transaction and the official Reddit consent URL."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_id or not client_secret:
        raise ValueError("Сначала сохрани Reddit Client ID и Client Secret.")
    if len(client_id) > 1024 or len(client_secret) > 4096:
        raise ValueError("Параметры Reddit OAuth имеют недопустимый размер.")
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
        "response_type": "code",
        "state": state,
        "redirect_uri": redirect_uri,
        "duration": "permanent",
        "scope": " ".join(SCOPES),
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_reddit_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def complete_reddit_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange the code and verify the authenticated Reddit identity."""
    state = str(state or "")
    code = str(code or "").strip()
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Reddit OAuth неполный. Запусти подключение заново.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия Reddit OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Reddit OAuth истекла. Запусти подключение заново.")

    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    headers = {"User-Agent": "TgFarm/1.0 OAuth connection"}
    try:
        async with httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=False,
            headers=headers,
            auth=(client_id, client_secret),
        ) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Reddit отклонил OAuth-код (HTTP {response.status_code}).")
            try:
                token_data = response.json()
            except ValueError as exc:
                raise RuntimeError("Reddit вернул неожиданный ответ при выдаче токена.") from exc
            if token_data.get("error"):
                raise RuntimeError("Reddit отклонил OAuth-код. Проверь callback URL и настройки приложения.")
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            granted = set(str(token_data.get("scope") or "").split())
            if not access_token or not refresh_token:
                raise RuntimeError(
                    "Reddit не выдал постоянный refresh token. Запусти OAuth ещё раз и подтверди доступ."
                )
            if not set(SCOPES).issubset(granted):
                raise RuntimeError(
                    "Reddit выдал не все разрешения. Нужны identity, read и submit."
                )
            profile_response = await client.get(
                ME_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if profile_response.status_code >= 400:
                raise RuntimeError(f"Reddit не подтвердил профиль (HTTP {profile_response.status_code}).")
            profile = profile_response.json()
    except httpx.TimeoutException as exc:
        raise RuntimeError("Reddit не ответил вовремя. Запусти OAuth-подключение заново.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Reddit. Проверь сеть и повтори попытку.") from exc

    username = str((profile or {}).get("name") or "").strip()
    if not username:
        raise RuntimeError("Reddit не вернул имя подтверждённого аккаунта.")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "username": username,
        "user_agent": f"python:tgfarm:1.0 (by /u/{username})",
        "account": "u/" + username,
    }

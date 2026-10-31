"""Pinterest API v5 OAuth 2.0 with rotating access/refresh tokens."""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
API_ROOT = "https://api.pinterest.com/v5"
SCOPES = ("boards:read", "pins:read", "pins:write")
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Pinterest OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Callback URL Pinterest должен быть HTTPS-адресом или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_pinterest_oauth(
    app_id: str, app_secret: str, redirect_uri: str
) -> dict[str, object]:
    """Create a state-bound Pinterest consent URL."""
    app_id = str(app_id or "").strip()
    app_secret = str(app_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not app_id or not app_secret:
        raise ValueError("Сначала сохрани Pinterest App ID и App Secret.")
    if len(app_id) > 1024 or len(app_secret) > 4096:
        raise ValueError("Параметры Pinterest OAuth имеют недопустимый размер.")
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "app_id": app_id,
            "app_secret": app_secret,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    query = urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_pinterest_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


def _safe_error(status: int, stage: str) -> RuntimeError:
    return RuntimeError(
        f"Pinterest {stage} rejected the request (HTTP {status}). "
        "Check app access, redirect URI, board ownership and OAuth scopes."
    )


async def complete_pinterest_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange the one-time code, then verify the chosen board is accessible."""
    state = str(state or "")
    code = str(code or "")
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Pinterest OAuth неполный. Запусти подключение ещё раз.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия Pinterest OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Pinterest OAuth истекла. Запусти подключение заново.")

    app_id = str(transaction["app_id"])
    app_secret = str(transaction["app_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    from tgfarm.core.settings import get_settings

    board_id = str(get_settings().pinterest_board_id or "").strip()
    if not board_id or len(board_id) > 64:
        raise ValueError("Сначала сохрани ID Pinterest-доски, в которую будут добавляться Pins.")

    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
            token_response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(app_id, app_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_response.status_code >= 400:
                raise _safe_error(token_response.status_code, "OAuth token exchange")
            try:
                token_payload = token_response.json()
            except ValueError as exc:
                raise RuntimeError("Pinterest вернул неожиданный ответ OAuth.") from exc
            if not isinstance(token_payload, dict):
                raise RuntimeError("Pinterest вернул неожиданный ответ OAuth.")
            access_token = str(token_payload.get("access_token") or "")
            refresh_token = str(token_payload.get("refresh_token") or "")
            if not access_token or not refresh_token:
                raise RuntimeError("Pinterest не выдал access и refresh токены. Переподключи приложение.")
            board_response = await client.get(
                f"{API_ROOT}/boards/{board_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if board_response.status_code >= 400:
                raise _safe_error(board_response.status_code, "board verification")
            try:
                board = board_response.json()
            except ValueError as exc:
                raise RuntimeError("Pinterest вернул неожиданные данные доски.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Pinterest не ответил вовремя. Запусти OAuth-подключение ещё раз.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Pinterest. Проверь сеть и повтори попытку.") from exc

    if not isinstance(board, dict) or str(board.get("id") or "") != board_id:
        raise RuntimeError("Pinterest не подтвердил выбранную доску. Проверь её ID и права доступа.")
    try:
        expires_in = max(60, int(token_payload.get("expires_in") or 2592000))
    except (TypeError, ValueError):
        expires_in = 2592000
    try:
        refresh_expires_in = int(token_payload.get("refresh_token_expires_in") or 0)
    except (TypeError, ValueError):
        refresh_expires_in = 0
    board_name = str(board.get("name") or board_id)
    owner = board.get("owner") or {}
    username = str(owner.get("username") or "")
    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": str(int(time.time()) + expires_in),
        "refresh_token_expires_at": str(int(time.time()) + refresh_expires_in) if refresh_expires_in else "0",
        "board_id": board_id,
        "board_name": board_name,
        "account": ("@" + username + " · " if username else "") + board_name,
    }


async def get_valid_pinterest_access_token() -> str:
    """Refresh access tokens early and persist any rotating refresh token."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    access = str(settings.pinterest_access_token or "").strip()
    refresh = str(settings.pinterest_refresh_token or "").strip()
    app_id = str(settings.pinterest_app_id or "").strip()
    app_secret = str(settings.pinterest_app_secret or "").strip()
    try:
        expires_at = int(settings.pinterest_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    now = int(time.time())
    if access and expires_at > now + 7 * 86400:
        return access
    if not (refresh and app_id and app_secret):
        raise RuntimeError("Pinterest OAuth не подключён или истёк. Переподключи аккаунт.")

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                auth=(app_id, app_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обновить Pinterest OAuth-токен.") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"Pinterest не обновил OAuth-токен (HTTP {response.status_code}). Переподключи аккаунт."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Pinterest вернул неожиданные данные при обновлении токена.") from exc
    new_access = str(payload.get("access_token") or "")
    if not new_access:
        raise RuntimeError("Pinterest не вернул обновлённый access token.")
    new_refresh = str(payload.get("refresh_token") or refresh)
    try:
        expires_in = max(60, int(payload.get("expires_in") or 2592000))
    except (TypeError, ValueError):
        expires_in = 2592000
    try:
        refresh_expires_in = int(payload.get("refresh_token_expires_in") or 0)
    except (TypeError, ValueError):
        refresh_expires_in = 0
    values = {
        "PINTEREST_ACCESS_TOKEN": new_access,
        "PINTEREST_REFRESH_TOKEN": new_refresh,
        "PINTEREST_TOKEN_EXPIRES_AT": str(now + expires_in),
    }
    if refresh_expires_in:
        values["PINTEREST_REFRESH_TOKEN_EXPIRES_AT"] = str(now + refresh_expires_in)
    save_platform_credentials("pinterest", values)
    return new_access


__all__ = [
    "SCOPES",
    "STATE_TTL_SECONDS",
    "begin_pinterest_oauth",
    "cancel_pinterest_oauth",
    "complete_pinterest_oauth",
    "get_valid_pinterest_access_token",
]

"""OK (Odnoklassniki) server OAuth 2.0 authorization-code flow.

The owner registers an external OK application, saves its public/secret keys, then
presses "login": the browser opens connect.ok.ru, the owner confirms access, and the
callback captures the access token automatically. The signing secret is derived as
MD5(access_token + application_secret_key) per the official OK API contract.

Authorization codes, tokens and secrets are never logged or returned to the browser.
The pending transaction is one-time and expires. Group/photo/video publishing rights
(GROUP_CONTENT, PHOTO_CONTENT, VIDEO_CONTENT) still require OK approval via
api-support@ok.ru; the OAuth token alone does not grant them.
"""

from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

from tgfarm.s9_growth.odnoklassniki import build_ok_request_params, ok_session_secret

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://connect.ok.ru/oauth/authorize"
TOKEN_URL = "https://api.ok.ru/oauth/token.do"
API_URL = "https://api.ok.ru/fb.do"
SCOPES = ("VALUABLE_ACCESS", "LONG_ACCESS_TOKEN", "GROUP_CONTENT", "PHOTO_CONTENT", "VIDEO_CONTENT")
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("OK OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("OK callback URL должен быть HTTPS или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_ok_oauth(
    application_key: str, application_secret: str, redirect_uri: str
) -> dict[str, object]:
    """Create a one-use OAuth transaction and the official OK consent URL."""
    application_key = str(application_key or "").strip()
    application_secret = str(application_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not application_key or not application_secret:
        raise ValueError("Сначала сохрани публичный ключ и секрет приложения OK.")
    if len(application_key) > 1024 or len(application_secret) > 4096:
        raise ValueError("Параметры OK OAuth имеют недопустимый размер.")
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "application_key": application_key,
            "application_secret": application_secret,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    query = urlencode({
        "client_id": application_key,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": ";".join(SCOPES),
        "state": state,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_ok_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def complete_ok_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange the code, derive the signing secret, and verify the OK identity."""
    state = str(state or "")
    code = str(code or "").strip()
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ OK OAuth неполный. Запусти подключение заново.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия OK OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия OK OAuth истекла. Запусти подключение заново.")

    application_key = str(transaction["application_key"])
    application_secret = str(transaction["application_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": application_key,
                    "client_secret": application_secret,
                    "redirect_uri": redirect_uri,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(f"OK отклонил OAuth-код (HTTP {response.status_code}).")
            try:
                token_data = response.json()
            except ValueError as exc:
                raise RuntimeError("OK вернул неожиданный ответ при выдаче токена.") from exc
            if not isinstance(token_data, dict) or token_data.get("error"):
                raise RuntimeError("OK отклонил OAuth-код. Проверь callback URL и настройки приложения.")
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            if not access_token:
                raise RuntimeError("OK не выдал access token. Запусти OAuth ещё раз и подтверди доступ.")
            session_secret = ok_session_secret(access_token, application_secret)
            profile_params = build_ok_request_params(
                "users.getCurrentUser",
                application_key=application_key,
                application_secret=application_secret,
                access_token=access_token,
                session_secret_key=session_secret,
            )
            profile_response = await client.post(API_URL, data=profile_params)
            if profile_response.status_code >= 400:
                raise RuntimeError(f"OK не подтвердил профиль (HTTP {profile_response.status_code}).")
            try:
                profile = profile_response.json()
            except ValueError as exc:
                raise RuntimeError("OK вернул неожиданный профиль аккаунта.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("OK не ответил вовремя. Запусти OAuth-подключение заново.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к OK. Проверь сеть и повтори попытку.") from exc

    if not isinstance(profile, dict) or profile.get("error_code"):
        raise RuntimeError("OK выдал токен, но не подтвердил профиль аккаунта.")
    uid = str(profile.get("uid") or "").strip()
    name = str(profile.get("name") or "").strip()
    if not uid and not name:
        raise RuntimeError("OK не вернул идентификатор подключённого аккаунта.")
    return {
        "application_key": application_key,
        "application_secret": application_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_secret_key": session_secret,
        "uid": uid,
        "account": name or uid,
    }
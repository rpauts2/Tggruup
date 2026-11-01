"""Douyin Open Platform OAuth and profile verification."""
from __future__ import annotations
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://open.douyin.com/platform/oauth/connect/"
TOKEN_URL = "https://open.douyin.com/oauth/access_token/"
REFRESH_URL = "https://open.douyin.com/oauth/refresh_token/"
USER_INFO_URL = "https://open.douyin.com/oauth/userinfo/"
SCOPES = "user_info,video.create"
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Douyin callback URL имеет неверный формат.") from exc
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    allowed_schemes = {"https", "http"} if loopback else {"https"}
    if parsed.scheme not in allowed_schemes or not host or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.startswith("/"):
        raise ValueError("Для Douyin нужен HTTPS callback (или loopback HTTP для локального приложения).")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


def _data(payload: object, operation: str) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Douyin вернул неожиданный ответ: {operation}.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    code = data.get("error_code")
    if code in (None, "", 0, "0"):
        code = extra.get("error_code", 0)
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = -1
    if code:
        message = str(data.get("description") or extra.get("description") or "ошибка API")
        raise RuntimeError(f"Douyin отклонил {operation} (код {code}): {message[:160]}")
    return data


async def begin_douyin_oauth(client_key: str, client_secret: str, redirect_uri: str) -> dict[str, object]:
    key = str(client_key or "").strip()
    secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not key or not secret:
        raise ValueError("Сначала сохрани Douyin Client Key и Client Secret.")
    if len(key) > 2048 or len(secret) > 4096:
        raise ValueError("Параметры Douyin OAuth слишком длинные.")
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {"client_key": key, "client_secret": secret, "created_at": time.time()}
    query = urlencode({"client_key": key, "response_type": "code", "scope": SCOPES, "redirect_uri": redirect_uri, "state": state})
    return {"authorize_url": AUTHORIZE_URL + "?" + query, "redirect_uri": redirect_uri, "expires_in_s": STATE_TTL_SECONDS}


def cancel_douyin_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def _profile(access_token: str, expected_open_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(USER_INFO_URL, headers={"access-token": access_token}, json={})
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось проверить профиль Douyin.") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Douyin не подтвердил профиль (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Douyin вернул неожиданный ответ профиля.") from exc
    data = _data(payload, "проверка профиля")
    if str(data.get("open_id") or "") != expected_open_id:
        raise RuntimeError("Douyin вернул другой open_id. Переподключи аккаунт.")
    return data


async def complete_douyin_oauth(state: str, code: str) -> dict[str, str]:
    state = str(state or "")
    code = str(code or "").split("#", 1)[0]
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Douyin OAuth неполный.")
    with _PENDING_LOCK:
        tx = _PENDING.pop(state, None)
    if not tx:
        raise ValueError("Сессия Douyin OAuth истекла или уже использована.")
    if time.time() - float(tx.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Douyin OAuth истекла.")
    key = str(tx["client_key"])
    secret = str(tx["client_secret"])
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={"client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обменять Douyin OAuth code.") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Douyin отклонил OAuth code (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Douyin вернул неожиданный token response.") from exc
    data = _data(payload, "обмен OAuth code")
    access = str(data.get("access_token") or "")
    refresh = str(data.get("refresh_token") or "")
    open_id = str(data.get("open_id") or "")
    if not access or not refresh or not open_id:
        raise RuntimeError("Douyin не выдал полный набор токенов и open_id.")
    scopes = {x.strip() for x in str(data.get("scope") or "").split(",") if x.strip()}
    if scopes and "video.create" not in scopes:
        raise RuntimeError("Douyin OAuth не выдал video.create.")
    now = int(time.time())
    try:
        access_exp = now + max(60, int(data.get("expires_in") or 1296000))
        refresh_exp = now + max(60, int(data.get("refresh_expires_in") or 2592000))
    except (TypeError, ValueError):
        access_exp, refresh_exp = now + 1296000, now + 2592000
    profile = await _profile(access, open_id)
    nickname = str(profile.get("nickname") or "").strip()[:160]
    return {
        "client_key": key, "client_secret": secret, "access_token": access, "refresh_token": refresh,
        "token_expires_at": str(access_exp), "refresh_token_expires_at": str(refresh_exp),
        "open_id": open_id, "username": nickname, "account": nickname or ("Douyin creator " + open_id[:8]),
    }


async def get_valid_douyin_access_token() -> str:
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials
    settings = get_settings()
    access = str(settings.douyin_access_token or "").strip()
    refresh = str(settings.douyin_refresh_token or "").strip()
    now = int(time.time())
    try:
        access_exp = int(settings.douyin_token_expires_at or 0)
        refresh_exp = int(settings.douyin_refresh_token_expires_at or 0)
    except (TypeError, ValueError):
        access_exp = refresh_exp = 0
    if access and access_exp > now + 120:
        return access
    if not (settings.douyin_client_key and refresh and settings.douyin_open_id):
        raise RuntimeError("Douyin OAuth не подключён.")
    if refresh_exp and refresh_exp <= now:
        raise RuntimeError("Douyin refresh token истёк. Подключи аккаунт заново.")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                REFRESH_URL,
                files={"client_key": (None, settings.douyin_client_key), "grant_type": (None, "refresh_token"), "refresh_token": (None, refresh)},
            )
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обновить Douyin OAuth.") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Douyin не обновил access token (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Douyin вернул неожиданный refresh response.") from exc
    data = _data(payload, "обновление access token")
    new_access = str(data.get("access_token") or "")
    if not new_access:
        raise RuntimeError("Douyin не вернул новый access token.")
    new_refresh = str(data.get("refresh_token") or refresh)
    try:
        new_access_exp = now + max(60, int(data.get("expires_in") or 1296000))
        life = int(data.get("refresh_expires_in") or 0)
    except (TypeError, ValueError):
        new_access_exp, life = now + 1296000, 0
    new_refresh_exp = now + max(60, life) if life else refresh_exp
    updates = {"DOUYIN_ACCESS_TOKEN": new_access, "DOUYIN_REFRESH_TOKEN": new_refresh, "DOUYIN_TOKEN_EXPIRES_AT": str(new_access_exp)}
    if new_refresh_exp:
        updates["DOUYIN_REFRESH_TOKEN_EXPIRES_AT"] = str(new_refresh_exp)
    save_platform_credentials("douyin", updates)
    return new_access


async def get_douyin_user_info() -> dict[str, str]:
    from tgfarm.core.settings import get_settings
    settings = get_settings()
    open_id = str(settings.douyin_open_id or "").strip()
    if not open_id:
        raise RuntimeError("Douyin аккаунт не подключён через OAuth.")
    profile = await _profile(await get_valid_douyin_access_token(), open_id)
    return {"open_id": open_id, "nickname": str(profile.get("nickname") or settings.douyin_username or "").strip()[:160]}

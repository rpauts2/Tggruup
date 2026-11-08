"""Tumblr OAuth 2.0 and selected-blog verification."""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
AUTHORIZE_URL = "https://www.tumblr.com/oauth2/authorize"
TOKEN_URL = "https://api.tumblr.com/v2/oauth2/token"
API_ROOT = "https://api.tumblr.com/v2"
USER_AGENT = "TgFarm/1.2 (+https://github.com/rpauts2/Tggruup)"
SCOPES = "basic write offline_access"
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Tumblr OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Tumblr callback должен быть HTTPS-адресом или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_tumblr_oauth(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    preferred_blog: str = "",
) -> dict[str, object]:
    """Start Tumblr OAuth with a cryptographically random, one-use state."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    preferred_blog = str(preferred_blog or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Сначала сохрани Tumblr OAuth Client ID и Client Secret.")
    if len(client_id) > 1024 or len(client_secret) > 4096 or len(preferred_blog) > 255:
        raise ValueError("Параметры Tumblr OAuth имеют недопустимый размер.")
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "preferred_blog": preferred_blog,
            "created_at": time.time(),
        }
    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "redirect_uri": redirect_uri,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_tumblr_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


def _blog_matches(blog: dict, preferred: str) -> bool:
    preferred = str(preferred or "").strip().lower()
    if not preferred:
        return False
    name = str(blog.get("name") or "").strip().lower()
    url = str(blog.get("url") or "").strip().lower()
    hostname = (urlsplit(url).hostname or "").lower()
    candidates = {name, url, hostname}
    if name.endswith(".tumblr.com"):
        candidates.add(name.removesuffix(".tumblr.com"))
    if hostname.endswith(".tumblr.com"):
        candidates.add(hostname.removesuffix(".tumblr.com"))
    return preferred in candidates


async def _tumblr_user_info(client: httpx.AsyncClient, token: str) -> dict:
    response = await client.get(
        f"{API_ROOT}/user/info",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Tumblr выдал токен, но не подтвердил профиль (HTTP {response.status_code})."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Tumblr вернул неожиданный ответ профиля.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Tumblr вернул неожиданный формат профиля.")
    meta = payload.get("meta") or {}
    if isinstance(meta, dict) and int(meta.get("status") or 200) >= 400:
        raise RuntimeError("Tumblr не подтвердил OAuth-профиль.")
    user = payload.get("response") or {}
    if not isinstance(user, dict) or not isinstance(user.get("blogs"), list):
        raise RuntimeError("Tumblr не вернул список блогов пользователя.")
    return user


async def complete_tumblr_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange the OAuth code, select an accessible blog and verify it."""
    state = str(state or "")
    code = str(code or "").split("#", 1)[0]
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Tumblr OAuth неполный. Запусти подключение заново.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия Tumblr OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Tumblr OAuth истекла. Запусти подключение заново.")

    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    preferred_blog = str(transaction.get("preferred_blog") or "")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            token_response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
            )
            if token_response.status_code >= 400:
                raise RuntimeError(
                    f"Tumblr отклонил OAuth-код (HTTP {token_response.status_code}). "
                    "Проверь callback URL и настройки OAuth App."
                )
            try:
                token_data = token_response.json()
            except ValueError as exc:
                raise RuntimeError("Tumblr вернул неожиданный ответ при обмене OAuth-кода.") from exc
            if not isinstance(token_data, dict):
                raise RuntimeError("Tumblr вернул неожиданный ответ при обмене OAuth-кода.")
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            if not access_token or not refresh_token:
                raise RuntimeError("Tumblr не выдал access и refresh tokens. Проверь scope offline_access.")
            user = await _tumblr_user_info(client, access_token)
    except httpx.TimeoutException as exc:
        raise RuntimeError("Tumblr не ответил вовремя. Запусти OAuth-подключение заново.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Tumblr. Проверь сеть и повтори попытку.") from exc

    blogs = [item for item in user["blogs"] if isinstance(item, dict)]
    if not blogs:
        raise RuntimeError("Tumblr OAuth подключён, но аккаунт не вернул доступных блогов.")
    selected = None
    if preferred_blog:
        selected = next((blog for blog in blogs if _blog_matches(blog, preferred_blog)), None)
        if selected is None:
            raise RuntimeError("У Tumblr-аккаунта нет доступа к указанному Blog identifier. Проверь его в карточке площадки.")
    if selected is None:
        selected = next((blog for blog in blogs if blog.get("primary") is True), blogs[0])
    identifier = str(selected.get("name") or "").strip()
    blog_url = str(selected.get("url") or "").strip()
    parsed_blog_url = urlsplit(blog_url)
    if not identifier and parsed_blog_url.hostname:
        identifier = parsed_blog_url.hostname
    if not identifier or parsed_blog_url.scheme not in {"http", "https"} or not parsed_blog_url.hostname:
        raise RuntimeError("Tumblr не вернул корректный идентификатор и публичный URL выбранного блога.")
    blog_title = str(selected.get("title") or identifier).strip()
    username = str(user.get("name") or "").strip()
    try:
        expires_in = max(60, int(token_data.get("expires_in") or 2520))
    except (TypeError, ValueError):
        expires_in = 2520
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": str(int(time.time()) + expires_in),
        "blog_identifier": identifier,
        "blog_title": blog_title,
        "blog_url": blog_url,
        "username": username,
        "account": (("@" + username + " · ") if username else "") + blog_title,
    }


class TumblrOAuthError(RuntimeError):
    """A token-endpoint failure that carries the status it was refused with.

    The adapter has to tell a dead grant (HTTP 400/401 - retrying cannot help)
    from a network or 5xx failure (safe to retry, because no publish request
    exists yet). A bare RuntimeError erases that difference, which is how every
    refresh failure used to be recorded as `permanent`.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def get_valid_tumblr_access_token() -> str:
    """Refresh an expiring Tumblr token and persist rotated credentials."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    access = str(settings.tumblr_access_token or "").strip()
    refresh = str(settings.tumblr_refresh_token or "").strip()
    client_id = str(settings.tumblr_client_id or "").strip()
    client_secret = str(settings.tumblr_client_secret or "").strip()
    try:
        expires_at = int(settings.tumblr_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    now = int(time.time())
    if access and (not expires_at or expires_at > now + 180):
        return access
    if not (refresh and client_id and client_secret):
        raise TumblrOAuthError(
            "Tumblr OAuth не подключён или истёк. Переподключи аккаунт.", status_code=400
        )
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError("Tumblr не ответил при обновлении OAuth-токена.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обновить Tumblr OAuth-токен из-за сетевой ошибки.") from exc
    if response.status_code >= 400:
        raise TumblrOAuthError(
            f"Tumblr не обновил OAuth-токен (HTTP {response.status_code}). Переподключи аккаунт.",
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Tumblr вернул неожиданный ответ при обновлении OAuth-токена.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Tumblr вернул неожиданный ответ при обновлении OAuth-токена.")
    new_access = str(data.get("access_token") or "")
    new_refresh = str(data.get("refresh_token") or refresh)
    if not new_access:
        raise RuntimeError("Tumblr не вернул новый access token.")
    try:
        lifetime = max(60, int(data.get("expires_in") or 2520))
    except (TypeError, ValueError):
        lifetime = 2520
    updates = {
        "TUMBLR_ACCESS_TOKEN": new_access,
        "TUMBLR_TOKEN_EXPIRES_AT": str(now + lifetime),
    }
    if new_refresh:
        updates["TUMBLR_REFRESH_TOKEN"] = new_refresh
    save_platform_credentials("tumblr", updates)
    return new_access

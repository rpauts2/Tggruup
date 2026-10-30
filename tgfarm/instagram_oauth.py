"""Instagram Login OAuth, long-lived tokens and signed media URLs."""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
MEDIA_URL_TTL_SECONDS = 6 * 60 * 60
SCOPES = "instagram_business_basic,instagram_business_content_publish"
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_URL = "https://graph.instagram.com/v25.0"
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Instagram OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Callback URL для Instagram должен быть HTTPS-адресом или loopback URL для локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_instagram_oauth(app_id: str, app_secret: str, redirect_uri: str) -> dict[str, object]:
    """Start Instagram Login with a cryptographically random, one-use state."""
    app_id = str(app_id or "").strip()
    app_secret = str(app_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not app_id or not app_secret:
        raise ValueError("Сначала сохрани Instagram App ID и App Secret.")
    if len(app_id) > 1024 or len(app_secret) > 4096:
        raise ValueError("Параметры Instagram OAuth-приложения имеют недопустимый размер.")
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
        "scope": SCOPES,
        "state": state,
        "enable_fb_login": "0",
        "force_authentication": "1",
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
    }


def cancel_instagram_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def complete_instagram_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange auth code, get a long-lived token, and verify professional profile."""
    state = str(state or "")
    code = str(code or "").split("#", 1)[0]
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Instagram OAuth неполный. Запусти подключение заново.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия Instagram OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Instagram OAuth истекла. Запусти подключение заново.")

    app_id = str(transaction["app_id"])
    app_secret = str(transaction["app_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Instagram отклонил OAuth-код (HTTP {response.status_code}). "
                    "Проверь callback URL и Instagram App."
                )
            try:
                short_data = response.json()
            except ValueError as exc:
                raise RuntimeError("Instagram вернул неожиданный ответ при выдаче токена.") from exc
            short_token = str(short_data.get("access_token") or "")
            user_id = str(short_data.get("user_id") or "")
            if not short_token or not user_id:
                raise RuntimeError("Instagram не выдал user access token и user ID.")

            long_response = await client.get(
                "https://graph.instagram.com/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": app_secret,
                    "access_token": short_token,
                },
            )
            if long_response.status_code >= 400:
                raise RuntimeError(
                    f"Instagram не смог выдать долгоживущий токен (HTTP {long_response.status_code})."
                )
            try:
                long_data = long_response.json()
            except ValueError as exc:
                raise RuntimeError("Instagram вернул неожиданный ответ при продлении токена.") from exc
            access_token = str(long_data.get("access_token") or "")
            if not access_token:
                raise RuntimeError("Instagram не вернул долгоживущий токен.")
            try:
                expires_in = max(60, int(long_data.get("expires_in") or 5184000))
            except (TypeError, ValueError):
                expires_in = 5184000

            profile_response = await client.get(
                f"{GRAPH_URL}/me",
                params={"fields": "user_id,username", "access_token": access_token},
            )
            if profile_response.status_code >= 400:
                raise RuntimeError(
                    f"Instagram выдал токен, но не подтвердил профессиональный профиль (HTTP {profile_response.status_code})."
                )
            try:
                profile = profile_response.json()
            except ValueError as exc:
                raise RuntimeError("Instagram вернул неожиданный профиль.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Instagram не ответил вовремя. Запусти OAuth-подключение заново.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Instagram. Проверь сеть и повтори попытку.") from exc

    profile = profile if isinstance(profile, dict) else {}
    returned_id = str(profile.get("user_id") or profile.get("id") or user_id)
    username = str(profile.get("username") or "").strip()
    if not returned_id or returned_id != user_id or not username:
        raise RuntimeError("Instagram не подтвердил ожидаемый ID и username. Проверь, что это профессиональный аккаунт.")
    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "access_token": access_token,
        "expires_at": str(int(time.time()) + expires_in),
        "user_id": user_id,
        "username": username,
        "account": "@" + username,
    }


async def get_valid_instagram_access_token() -> str:
    """Refresh a still-valid long-lived token before expiry; persist its rotation."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    token = str(settings.instagram_access_token or "").strip()
    try:
        expires_at = int(settings.instagram_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if not token:
        raise RuntimeError("Instagram не подключён. Повтори OAuth-подключение.")
    now = int(time.time())
    if not expires_at or expires_at > now + 7 * 86400:
        return token
    if expires_at <= now:
        raise RuntimeError("Срок Instagram OAuth-токена истёк. Переподключи Instagram.")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
            )
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось обновить Instagram OAuth-токен.") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"Instagram не обновил OAuth-токен (HTTP {response.status_code}). Переподключи аккаунт."
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Instagram вернул неожиданный ответ при обновлении токена.") from exc
    new_token = str(data.get("access_token") or "")
    if not new_token:
        raise RuntimeError("Instagram не вернул обновлённый access token.")
    try:
        lifetime = max(60, int(data.get("expires_in") or 5184000))
    except (TypeError, ValueError):
        lifetime = 5184000
    save_platform_credentials("instagram", {
        "INSTAGRAM_ACCESS_TOKEN": new_token,
        "INSTAGRAM_TOKEN_EXPIRES_AT": str(now + lifetime),
    })
    return new_token


def make_signed_instagram_media_url(media_id: str, lifetime: int = MEDIA_URL_TTL_SECONDS) -> str | None:
    """Return an expiring signed public URL, only when an HTTPS public origin is configured."""
    from tgfarm.core.settings import get_settings
    from tgfarm.core.apiauth import ensure_token

    media_id = str(media_id or "").strip()
    base = str(get_settings().public_base_url or "").strip().rstrip("/")
    if not media_id or len(media_id) > 128 or not base:
        return None
    parsed = urlsplit(base)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or host in {"localhost", "127.0.0.1", "::1"}
        or host.endswith(".local")
    ):
        return None
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return None
    except ValueError:
        pass
    expires = int(time.time()) + max(60, min(int(lifetime), MEDIA_URL_TTL_SECONDS))
    message = f"{media_id}:{expires}".encode("utf-8")
    import hashlib
    import hmac
    signature = hmac.new(ensure_token().encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{base}/public/instagram-media/{media_id}?expires={expires}&sig={signature}"


def verify_signed_instagram_media_url(media_id: str, expires: int, signature: str) -> bool:
    """Validate expiring HMAC signatures without revealing the dashboard API token."""
    from tgfarm.core.apiauth import ensure_token

    try:
        expires = int(expires)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    if expires < now or expires > now + MEDIA_URL_TTL_SECONDS + 60:
        return False
    if not media_id or len(media_id) > 128 or len(signature) != 64:
        return False
    import hashlib
    import hmac
    expected = hmac.new(
        ensure_token().encode("utf-8"),
        f"{media_id}:{expires}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

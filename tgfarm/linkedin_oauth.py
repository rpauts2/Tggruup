"""LinkedIn member OAuth 2.0 and access-token lifecycle."""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
SCOPES = "openid profile w_member_social"
AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
NATIVE_AUTHORIZE_URL = "https://www.linkedin.com/oauth/native-pkce/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


class LinkedInOAuthTemporaryError(RuntimeError):
    """Network or provider-side refresh failure: no post exists yet, a retry is safe."""


def _refresh_is_temporary(status_code: int) -> bool:
    return status_code in (408, 425, 429) or status_code >= 500



def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("LinkedIn OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Callback URL для LinkedIn должен быть HTTPS-адресом или loopback URL для локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_linkedin_oauth(client_id: str, client_secret: str, redirect_uri: str) -> dict[str, object]:
    """Start a one-use LinkedIn 3-legged OAuth transaction."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not client_id or not client_secret:
        raise ValueError("Сначала сохрани LinkedIn Client ID и Client Secret.")
    if len(client_id) > 1024 or len(client_secret) > 4096:
        raise ValueError("Параметры LinkedIn OAuth-приложения имеют недопустимый размер.")
    parsed_redirect = urlsplit(redirect_uri)
    native_pkce = (parsed_redirect.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    code_verifier = secrets.token_urlsafe(64) if native_pkce else ""
    code_challenge = ""
    if native_pkce:
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")

    state = secrets.token_urlsafe(32)
    created_at = time.time()
    with _PENDING_LOCK:
        _prune_pending(created_at)
        _PENDING[state] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "native_pkce": native_pkce,
            "code_verifier": code_verifier,
            "created_at": created_at,
        }
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": SCOPES,
    }
    authorize_url = AUTHORIZE_URL
    if native_pkce:
        # LinkedIn's native flow requires PKCE and a loopback listener. LinkedIn
        # must enable native PKCE for this app before the provider accepts it.
        authorize_url = NATIVE_AUTHORIZE_URL
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    query = urlencode(params)
    return {
        "authorize_url": f"{authorize_url}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
        "flow": "native_pkce" if native_pkce else "web",
    }


def cancel_linkedin_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def complete_linkedin_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange a single-use authorization code and verify the OIDC profile."""
    state = str(state or "")
    code = str(code or "").strip()
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ LinkedIn OAuth неполный. Запусти подключение заново.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия LinkedIn OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия LinkedIn OAuth истекла. Запусти подключение заново.")

    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    native_pkce = bool(transaction.get("native_pkce"))
    token_data_request = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if native_pkce:
        token_data_request["code_verifier"] = str(transaction["code_verifier"])
    else:
        token_data_request["client_secret"] = client_secret
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            token_response = await client.post(
                TOKEN_URL,
                data=token_data_request,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_response.status_code >= 400:
                raise RuntimeError(
                    f"LinkedIn отклонил OAuth-код (HTTP {token_response.status_code}). "
                    "Проверь redirect URI и настройки приложения."
                )
            try:
                token_data = token_response.json()
            except ValueError as exc:
                raise RuntimeError("LinkedIn вернул неожиданный ответ при выдаче токена.") from exc
            access_token = str(token_data.get("access_token") or "")
            if not access_token:
                raise RuntimeError("LinkedIn не выдал access token.")

            profile_response = await client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if profile_response.status_code >= 400:
                raise RuntimeError(
                    f"LinkedIn выдал токен, но не подтвердил профиль (HTTP {profile_response.status_code})."
                )
            try:
                profile = profile_response.json()
            except ValueError as exc:
                raise RuntimeError("LinkedIn вернул неожиданный профиль.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("LinkedIn не ответил вовремя. Запусти OAuth-подключение заново.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к LinkedIn. Проверь сеть и повтори попытку.") from exc

    member_id = str(profile.get("sub") or "")
    if not member_id:
        raise RuntimeError("LinkedIn не вернул member ID в userinfo.")
    name = str(profile.get("name") or "").strip()
    try:
        expires_in = max(60, int(token_data.get("expires_in") or 5184000))
    except (TypeError, ValueError):
        expires_in = 5184000
    refresh_token = str(token_data.get("refresh_token") or "")
    try:
        refresh_expires_in = max(60, int(token_data.get("refresh_token_expires_in") or 0))
    except (TypeError, ValueError):
        refresh_expires_in = 0
    account = name or f"LinkedIn member {member_id}"
    result = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "expires_at": str(int(time.time()) + expires_in),
        "member_id": member_id,
        "member_name": name,
        "account": account,
        "refresh_token": refresh_token,
        "refresh_token_expires_at": str(int(time.time()) + refresh_expires_in) if refresh_expires_in else "0",
    }
    return result


async def get_valid_linkedin_access_token() -> str:
    """Refresh near-expiry LinkedIn access tokens when a refresh token is available."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    access_token = str(settings.linkedin_access_token or "").strip()
    refresh_token = str(settings.linkedin_refresh_token or "").strip()
    client_id = str(settings.linkedin_client_id or "").strip()
    client_secret = str(settings.linkedin_client_secret or "").strip()
    try:
        expires_at = int(settings.linkedin_token_expires_at or 0)
        refresh_expires_at = int(settings.linkedin_refresh_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
        refresh_expires_at = 0

    if not access_token:
        raise RuntimeError("LinkedIn не подключён. Повтори OAuth-подключение.")
    now = int(time.time())
    if not expires_at or expires_at > now + 7 * 86400:
        return access_token
    if expires_at <= now:
        if not refresh_token:
            raise RuntimeError("Срок LinkedIn OAuth-токена истёк. Переподключи LinkedIn.")
    if not refresh_token:
        # LinkedIn may not issue refresh tokens for every app/product tier.
        # Keep the still-valid access token until its expiry, then require reconnect.
        return access_token
    if refresh_expires_at and refresh_expires_at <= now:
        raise RuntimeError("Срок refresh token LinkedIn истёк. Переподключи аккаунт.")
    if not client_id or not client_secret:
        raise RuntimeError("Для обновления LinkedIn-токена нужны Client ID/Secret.")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.RequestError as exc:
        raise LinkedInOAuthTemporaryError("Не удалось обновить LinkedIn OAuth-токен.") from exc
    if response.status_code >= 400:
        if _refresh_is_temporary(response.status_code):
            raise LinkedInOAuthTemporaryError(
                f"LinkedIn временно не обновил OAuth-токен (HTTP {response.status_code})."
            )
        raise RuntimeError(
            f"LinkedIn не обновил OAuth-токен (HTTP {response.status_code}). Переподключи аккаунт."
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("LinkedIn вернул неожиданный ответ при обновлении токена.") from exc
    new_token = str(data.get("access_token") or "")
    if not new_token:
        raise RuntimeError("LinkedIn не вернул обновлённый access token.")
    try:
        lifetime = max(60, int(data.get("expires_in") or 5184000))
    except (TypeError, ValueError):
        lifetime = 5184000
    updates = {
        "LINKEDIN_ACCESS_TOKEN": new_token,
        "LINKEDIN_TOKEN_EXPIRES_AT": str(now + lifetime),
    }
    rotated_refresh = str(data.get("refresh_token") or "")
    if rotated_refresh:
        updates["LINKEDIN_REFRESH_TOKEN"] = rotated_refresh
    try:
        refresh_lifetime = max(60, int(data.get("refresh_token_expires_in") or 0))
    except (TypeError, ValueError):
        refresh_lifetime = 0
    if refresh_lifetime:
        updates["LINKEDIN_REFRESH_TOKEN_EXPIRES_AT"] = str(now + refresh_lifetime)
    save_platform_credentials("linkedin", updates)
    return new_token

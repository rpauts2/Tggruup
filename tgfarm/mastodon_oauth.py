"""Mastodon OAuth 2.0 authorization-code flow with PKCE.

Authorization codes, PKCE verifiers, client secrets and access tokens are never
logged or returned to the browser. The pending transaction is one-time and expires.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

STATE_TTL_SECONDS = 600
SCOPES = "read write:statuses"
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def normalize_instance_url(value: str) -> str:
    """Normalize an instance URL and refuse insecure/local targets."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Укажи адрес Mastodon-сервера.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Адрес Mastodon-сервера имеет неверный формат.") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("Mastodon OAuth работает только через HTTPS.")
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Укажи только домен HTTPS-сервера, без логина, пути или параметров.")
    if parsed.path not in ("", "/"):
        raise ValueError("Укажи только домен сервера, без дополнительного пути.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Локальные адреса нельзя использовать как Mastodon-инстанс.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Mastodon-инстанс должен быть публичным адресом.")
    if not all(part and len(part) <= 63 for part in host.split(".")):
        raise ValueError("Домен Mastodon-сервера имеет неверный формат.")
    if len(host) > 253:
        raise ValueError("Домен Mastodon-сервера слишком длинный.")
    host_for_netloc = f"[{host}]" if ":" in host else host
    return f"https://{host_for_netloc}" + (f":{port}" if port and port != 443 else "")


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OAuth callback URL имеет неверный формат.") from exc
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"https", "http"} if is_loopback else {"https"})
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("OAuth callback URL должен быть безопасным HTTPS-адресом.")
    return value


def _prune_pending(now: float) -> None:
    expired = [
        state for state, item in _PENDING.items()
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS
    ]
    for state in expired:
        _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_mastodon_oauth(instance: str, redirect_uri: str) -> dict[str, object]:
    """Register a client on the chosen instance and build a PKCE authorization URL."""
    instance = normalize_instance_url(instance)
    redirect_uri = _validate_redirect_uri(redirect_uri)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(32)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.post(
                f"{instance}/api/v1/apps",
                data={
                    "client_name": "TgFarm",
                    "redirect_uris": redirect_uri,
                    "scopes": SCOPES,
                    "website": "https://github.com/rpauts2/Tggruup",
                },
            )
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к выбранному Mastodon-серверу.") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"Mastodon не зарегистрировал OAuth-приложение (HTTP {response.status_code}). "
            "Проверь адрес инстанса и попробуй снова."
        )
    try:
        app = response.json()
    except ValueError as exc:
        raise RuntimeError("Mastodon вернул неожиданный ответ при регистрации приложения.") from exc
    client_id = str(app.get("client_id") or "")
    client_secret = str(app.get("client_secret") or "")
    if not client_id or not client_secret:
        raise RuntimeError("Mastodon не вернул параметры OAuth-приложения.")
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "instance": instance,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
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
    return {"authorize_url": f"{instance}/oauth/authorize?{query}", "expires_in_s": STATE_TTL_SECONDS}


def cancel_mastodon_oauth(state: str) -> None:
    """Discard a pending state if the user cancels authorization."""
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


async def complete_mastodon_oauth(state: str, code: str) -> dict[str, str]:
    """Consume the one-time state, exchange the code, and verify the account."""
    state = str(state or "")
    code = str(code or "")
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ OAuth неполный. Запусти подключение ещё раз.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия OAuth истекла или уже использована. Запусти подключение ещё раз.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия OAuth истекла. Запусти подключение ещё раз.")
    instance = str(transaction["instance"])
    client_id = str(transaction["client_id"])
    client_secret = str(transaction["client_secret"])
    redirect_uri = str(transaction["redirect_uri"])
    verifier = str(transaction["code_verifier"])
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.post(
                f"{instance}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Mastodon не завершил обмен OAuth-кода (HTTP {response.status_code}). "
                    "Запусти подключение заново."
                )
            try:
                token_data = response.json()
            except ValueError as exc:
                raise RuntimeError("Mastodon вернул неожиданный ответ при обмене OAuth-кода.") from exc
            access_token = str(token_data.get("access_token") or "")
            if not access_token:
                raise RuntimeError("Mastodon не вернул пользовательский токен.")
            scope_value = token_data.get("scope")
            if isinstance(scope_value, str) and scope_value and "write:statuses" not in scope_value.split():
                raise RuntimeError("Mastodon не выдал разрешение write:statuses. Повтори подключение.")
            profile_response = await client.get(
                f"{instance}/api/v1/accounts/verify_credentials",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if profile_response.status_code >= 400:
                raise RuntimeError(
                    f"Mastodon выдал токен, но не подтвердил профиль (HTTP {profile_response.status_code})."
                )
            try:
                profile = profile_response.json()
            except ValueError as exc:
                raise RuntimeError("Mastodon вернул неожиданный профиль аккаунта.") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Mastodon не ответил вовремя. Запусти OAuth-подключение ещё раз.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Mastodon. Проверь сеть и повтори попытку.") from exc
    acct = str(profile.get("acct") or profile.get("username") or "").strip()
    if not acct:
        raise RuntimeError("Mastodon не вернул имя подключённого аккаунта.")
    if "@" not in acct:
        acct += "@" + instance.split("://", 1)[-1]
    return {
        "instance": instance,
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "account": "@" + acct,
    }

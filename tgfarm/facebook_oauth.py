"""Facebook Pages OAuth and long-lived Page access-token management."""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

GRAPH_VERSION = "v26.0"
GRAPH_ROOT = f"https://graph.facebook.com/{GRAPH_VERSION}"
AUTHORIZE_URL = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
TOKEN_URL = f"{GRAPH_ROOT}/oauth/access_token"
STATE_TTL_SECONDS = 600
SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts")
_PENDING: dict[str, dict[str, object]] = {}
_PENDING_LOCK = threading.RLock()


def _validate_redirect_uri(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Facebook OAuth callback URL имеет неверный формат.") from exc
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
        raise ValueError("Callback URL Facebook должен быть HTTPS-адресом или loopback URL локальной панели.")
    return value


def _prune_pending(now: float) -> None:
    for state, item in list(_PENDING.items()):
        if now - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            _PENDING.pop(state, None)
    while len(_PENDING) >= 10:
        oldest = min(_PENDING, key=lambda key: float(_PENDING[key].get("created_at", 0)))
        _PENDING.pop(oldest, None)


async def begin_facebook_oauth(
    app_id: str, app_secret: str, page_id: str, redirect_uri: str
) -> dict[str, object]:
    """Create a state-bound Facebook Login flow; Page selection may happen after OAuth."""
    app_id = str(app_id or "").strip()
    app_secret = str(app_secret or "").strip()
    page_id = str(page_id or "").strip()
    redirect_uri = _validate_redirect_uri(redirect_uri)
    if not app_id or not app_secret:
        raise ValueError("Сначала сохрани Facebook App ID и App Secret.")
    if page_id and not page_id.isdigit():
        raise ValueError("ID Facebook Page должен состоять только из цифр.")
    if len(app_id) > 1024 or len(app_secret) > 4096 or len(page_id) > 32:
        raise ValueError("Параметры Facebook OAuth имеют недопустимый размер.")

    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _prune_pending(time.time())
        _PENDING[state] = {
            "app_id": app_id,
            "app_secret": app_secret,
            "page_id": page_id,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    query = urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    })
    return {
        "authorize_url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": redirect_uri,
        "expires_in_s": STATE_TTL_SECONDS,
        "page_id": page_id,
    }


def cancel_facebook_oauth(state: str) -> None:
    with _PENDING_LOCK:
        _PENDING.pop(str(state or ""), None)


def _safe_error(stage: str, status: int) -> RuntimeError:
    if status in (401, 403):
        detail = "Facebook отказал в доступе. Проверь разрешения приложения и роль страницы."
    elif status == 429:
        detail = "Facebook временно ограничил запрос. Повтори проверку позже."
    else:
        detail = f"Facebook {stage} вернул HTTP {status}."
    return RuntimeError(detail)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None) -> dict:
    try:
        response = await client.get(url, params=params, follow_redirects=False)
    except httpx.TimeoutException as exc:
        raise RuntimeError("Facebook не ответил вовремя. Повтори подключение.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Не удалось подключиться к Facebook. Проверь сеть.") from exc
    if response.status_code >= 400:
        raise _safe_error("Graph API", response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Facebook вернул неожиданный ответ Graph API.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Facebook вернул неожиданный формат Graph API.")
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        if code == "190":
            raise RuntimeError("Facebook access token недействителен. Переподключи страницу.")
        if code == "200":
            raise RuntimeError("Facebook не выдал нужные разрешения для этой страницы.")
        raise RuntimeError("Facebook отклонил Graph API запрос.")
    return payload


async def _page_from_accounts(
    client: httpx.AsyncClient, user_token: str, page_id: str
) -> dict:
    if not page_id or not str(page_id).isdigit():
        raise ValueError("Некорректный ID Facebook Page.")
    pages = await _list_pages_with_tokens(client, user_token)
    item = next((row for row in pages if row["id"] == page_id), None)
    if not item:
        raise RuntimeError("Выбранная Facebook Page не найдена среди страниц, которыми ты управляешь.")
    tasks = set(item.get("tasks") or [])
    if "CREATE_CONTENT" not in tasks and not ({"MANAGE", "ADMINISTER"} & tasks):
        raise RuntimeError(
            "Твой Facebook-профиль не имеет задачи CREATE_CONTENT для выбранной страницы."
        )
    if not item.get("access_token"):
        raise RuntimeError("Facebook не выдал Page access token для выбранной страницы.")
    return item


async def complete_facebook_oauth(state: str, code: str) -> dict[str, str]:
    """Exchange OAuth code, extend user token, and obtain the selected Page token."""
    state = str(state or "")
    code = str(code or "")
    if not state or not code or len(code) > 4096:
        raise ValueError("Ответ Facebook OAuth неполный. Запусти подключение ещё раз.")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction:
        raise ValueError("Сессия Facebook OAuth истекла или уже использована. Запусти подключение заново.")
    if time.time() - float(transaction.get("created_at", 0)) > STATE_TTL_SECONDS:
        raise ValueError("Сессия Facebook OAuth истекла. Запусти подключение заново.")

    app_id = str(transaction["app_id"])
    app_secret = str(transaction["app_secret"])
    page_id = str(transaction["page_id"])
    redirect_uri = str(transaction["redirect_uri"])
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        short = await _get_json(client, TOKEN_URL, {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        })
        short_token = str(short.get("access_token") or "")
        if not short_token:
            raise RuntimeError("Facebook не выдал short-lived user access token.")
        long_payload = await _get_json(client, TOKEN_URL, {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        })
        user_token = str(long_payload.get("access_token") or "")
        if not user_token:
            raise RuntimeError("Facebook не выдал long-lived user access token.")
        try:
            expires_in = max(60, int(long_payload.get("expires_in") or short.get("expires_in") or 0))
        except (TypeError, ValueError):
            expires_in = 0
        if page_id:
            page = await _page_from_accounts(client, user_token, page_id)
            available_pages = []
        else:
            rows = await _list_pages_with_tokens(client, user_token)
            available_pages = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "tasks": row["tasks"],
                    "can_publish": (
                        "CREATE_CONTENT" in row["tasks"]
                        or bool({"MANAGE", "ADMINISTER"} & set(row["tasks"]))
                    ),
                }
                for row in rows
            ]
            available_pages = [row for row in available_pages if row["can_publish"]]
            if not available_pages:
                raise RuntimeError(
                    "Facebook OAuth выполнен, но не найдено ни одной Page с задачей CREATE_CONTENT. "
                    "Проверь роли и разрешения приложения."
                )
            page = None

    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "page_id": page_id,
        "page_name": str(page.get("name") or page_id) if page else "",
        "page_access_token": str(page["access_token"]) if page else "",
        "user_access_token": user_token,
        "user_token_expires_at": str(int(time.time()) + expires_in) if expires_in else "0",
        "available_pages": available_pages,
        "account": str(page.get("name") or page_id) if page else "Facebook · выбери Page",
    }



async def get_valid_facebook_user_token() -> str:
    """Refresh the long-lived user token when close to expiry, also before a Page is selected."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    user_token = str(getattr(settings, "facebook_user_access_token", "") or "").strip()
    app_id = str(getattr(settings, "facebook_app_id", "") or "").strip()
    app_secret = str(getattr(settings, "facebook_app_secret", "") or "").strip()
    try:
        expires_at = int(getattr(settings, "facebook_user_token_expires_at", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0
    now = int(time.time())
    if not user_token:
        raise RuntimeError("Сначала подключи Facebook через OAuth.")
    if not expires_at or expires_at > now + 7 * 86400:
        return user_token
    if not app_id or not app_secret:
        raise RuntimeError("Нужны Facebook App ID и App Secret для обновления пользовательского токена.")

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        payload = await _get_json(client, TOKEN_URL, {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        })
    refreshed = str(payload.get("access_token") or "")
    if not refreshed:
        raise RuntimeError("Facebook не обновил long-lived user token.")
    try:
        expires_in = max(60, int(payload.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    save_platform_credentials(
        "facebook",
        {
            "FACEBOOK_USER_ACCESS_TOKEN": refreshed,
            "FACEBOOK_USER_TOKEN_EXPIRES_AT": str(now + expires_in) if expires_in else "0",
        },
    )
    return refreshed


async def list_facebook_pages() -> list[dict[str, object]]:
    """List Pages with publish permission; never return Page access tokens."""
    user_token = await get_valid_facebook_user_token()
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        rows = await _list_pages_with_tokens(client, user_token)
    result = []
    for row in rows:
        tasks = list(row.get("tasks") or [])
        can_publish = "CREATE_CONTENT" in tasks or bool({"MANAGE", "ADMINISTER"} & set(tasks))
        if not can_publish:
            continue
        result.append({
            "id": row["id"],
            "name": row["name"],
            "tasks": tasks,
            "can_publish": True,
        })
    return result


async def select_facebook_page(page_id: str) -> dict[str, str]:
    """Verify the selected Page against the current owner's live permissions."""
    user_token = await get_valid_facebook_user_token()
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        page = await _page_from_accounts(client, user_token, str(page_id or "").strip())
    return {
        "id": str(page["id"]),
        "name": str(page.get("name") or page["id"])[:160],
        "page_access_token": str(page["access_token"]),
    }


async def get_valid_facebook_page_token() -> str:
    """Rotate the long-lived user token before expiry and reload the Page token."""
    from tgfarm.core.settings import get_settings
    from tgfarm.platforms import save_platform_credentials

    settings = get_settings()
    page_token = str(settings.facebook_page_access_token or "").strip()
    user_token = str(settings.facebook_user_access_token or "").strip()
    app_id = str(settings.facebook_app_id or "").strip()
    app_secret = str(settings.facebook_app_secret or "").strip()
    page_id = str(settings.facebook_page_id or "").strip()
    try:
        expires_at = int(settings.facebook_user_token_expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    now = int(time.time())
    if page_token and (not expires_at or expires_at > now + 7 * 86400):
        return page_token
    if not (user_token and app_id and app_secret and page_id):
        if page_token:
            return page_token
        raise RuntimeError("Facebook Page не подключена. Пройди OAuth ещё раз.")

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        long_payload = await _get_json(client, TOKEN_URL, {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        })
        new_user_token = str(long_payload.get("access_token") or "")
        if not new_user_token:
            raise RuntimeError("Facebook не обновил long-lived user token.")
        try:
            expires_in = max(60, int(long_payload.get("expires_in") or 0))
        except (TypeError, ValueError):
            expires_in = 0
        page = await _page_from_accounts(client, new_user_token, page_id)

    values = {
        "FACEBOOK_USER_ACCESS_TOKEN": new_user_token,
        "FACEBOOK_PAGE_ACCESS_TOKEN": str(page["access_token"]),
        "FACEBOOK_PAGE_NAME": str(page.get("name") or page_id),
        "FACEBOOK_USER_TOKEN_EXPIRES_AT": str(now + expires_in) if expires_in else "0",
    }
    save_platform_credentials("facebook", values)
    return str(page["access_token"])


__all__ = [
    "GRAPH_VERSION",
    "SCOPES",
    "begin_facebook_oauth",
    "cancel_facebook_oauth",
    "complete_facebook_oauth",
    "get_valid_facebook_page_token",
    "list_facebook_pages",
    "select_facebook_page",
]

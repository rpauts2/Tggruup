"""One authorization contract for every catalogue platform.

The panel used to expose a different mechanism per card: a browser button here, an
OAuth link there, a token form somewhere else, and no single place that said "this
platform can be connected, this one cannot, and here is what will happen". This
module answers that once:

* ``auth_matrix()`` — for every platform in the catalogue: how it is connected
  (oauth / vkhost / browser / token / manual), what the operator will see, which
  env fields a token platform needs, and the login URL a browser platform opens;
* ``authorize()`` — starts the flow that this machine can actually perform:
  a headed browser profile for cookie-only sites, the VKHost implicit flow for
  VK, and a "here is the URL and the fields" answer for OAuth and token sites
  (the API layer owns those endpoints, they already exist);
* ``authorize_status()`` — what is known after the attempt, without inventing a
  success: the stored auth check, the saved profile and the capability level.

No tokens are returned to the caller: the browser flows store their session, the
VK flow stores the token through ``save_platform_credentials``, and the answer
only names what was saved.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tgfarm.browser_auth import PLATFORM_LOGIN_URLS, browser_session_status
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.platforms import CATALOG, _AUTH_FIELDS, _read_auth_checks
from tgfarm.platform_capability import capability_of

log = get_logger(__name__)

#: Platforms whose own OAuth endpoints exist in the API layer.
OAUTH_PLATFORMS: frozenset[str] = frozenset(
    {
        "reddit",
        "x",
        "threads",
        "facebook",
        "instagram",
        "pinterest",
        "youtube",
        "mastodon",
        "linkedin",
        "tiktok",
        "tumblr",
        "douyin",
    }
)

#: Token/webhook and legacy-credential platforms. A browser session is not a
#: substitute for these explicit API credentials.
TOKEN_PLATFORMS: frozenset[str] = frozenset(
    {"telegram", "max", "bluesky", "telegraph", "discord", "medium", "wechat", "odnoklassniki"}
)

#: Destinations whose current publishing path depends on an isolated browser
#: profile. Keep this list limited to platforms that actually use that profile.
BROWSER_ONLY: frozenset[str] = frozenset(
    {"dzen", "pikabu", "vc_ru", "habr", "rutube", "tenchat", "rednote", "twitch",
     "whatsapp_channels", "snapchat", "vk_clips"}
)

#: Fields a platform cannot work without. Everything else in the form is
#: optional context (channel list, webhook url, display name), so its absence
#: must not make a saved token look "not saved".
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "max": ("MAX_ACCESS_TOKEN",),
    "bluesky": ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"),
    "telegraph": ("TELEGRAPH_ACCESS_TOKEN",),
    "discord": ("DISCORD_WEBHOOK_URL",),
    "mastodon": ("MASTODON_INSTANCE", "MASTODON_ACCESS_TOKEN"),
    "vk": ("VK_ACCESS_TOKEN",),
    "medium": ("MEDIUM_ACCESS_TOKEN",),
    "wechat": ("WECHAT_APP_ID", "WECHAT_APP_SECRET"),
    "odnoklassniki": (
        "OK_APPLICATION_KEY",
        "OK_APPLICATION_SECRET",
        "OK_ACCESS_TOKEN",
        "OK_GROUP_ID",
    ),
}

#: OAuth app credentials required before a user can be redirected to a provider.
#: The user authorizes their own account only after these app-level values exist.
OAUTH_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "reddit": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
    "x": ("X_OAUTH_CLIENT_ID", "X_OAUTH_CLIENT_SECRET"),
    "threads": ("THREADS_APP_ID", "THREADS_APP_SECRET"),
    "facebook": ("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"),
    "instagram": ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"),
    "pinterest": ("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"),
    "youtube": ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
    "tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
    "mastodon": ("MASTODON_INSTANCE",),
    "linkedin": ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"),
    "tumblr": ("TUMBLR_CLIENT_ID", "TUMBLR_CLIENT_SECRET"),
    "douyin": ("DOUYIN_CLIENT_KEY", "DOUYIN_CLIENT_SECRET"),
}

#: Fields shown in the first step of OAuth setup. OAuth-issued tokens and
#: generated client credentials are deliberately excluded from the UI.
OAUTH_SETUP_FIELDS: dict[str, tuple[str, ...]] = dict(OAUTH_REQUIRED_FIELDS)

#: Human-readable field labels for the token forms.
FIELD_LABELS: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "Токен бота от @BotFather",
    "TELEGRAM_CHANNELS": "Канал(ы): @username или -100… через запятую",
    "MAX_ACCESS_TOKEN": "Токен бота MAX",
    "MAX_CHANNEL_ID": "ID канала MAX",
    "BLUESKY_HANDLE": "Handle в Bluesky (например name.bsky.social)",
    "BLUESKY_APP_PASSWORD": "App password (Settings → App passwords)",
    "TELEGRAPH_ACCESS_TOKEN": "Access token Telegraph",
    "DISCORD_WEBHOOK_URL": "URL вебхука канала Discord",
    "MASTODON_INSTANCE": "Инстанс (например https://mastodon.social)",
    "MASTODON_CLIENT_ID": "Client ID приложения",
    "MASTODON_CLIENT_SECRET": "Client secret",
    "MASTODON_ACCESS_TOKEN": "Access token",
    "VK_ACCESS_TOKEN": "Токен сообщества VK",
    "VK_GROUP_ID": "ID сообщества (например 234474988)",
    "VK_USER_TOKEN": "Личный токен VK (фото и метрики)",
    "TGFARM_PUBLIC_BASE_URL": "Публичный HTTPS-адрес фермы",
}

#: Short human instruction per mode.
MODE_HINTS: dict[str, str] = {
    "oauth": "Откроется страница согласия площадки; после подтверждения токен сохранится сам.",
    "vkhost": "Откроется VKHost: подтверди доступ, токен перехватится и сохранится автоматически.",
    "browser": "Откроется окно браузера с профилем площадки: войди в аккаунт, сессия сохранится.",
    "token": "Нужен ключ площадки: вставь значения ниже, ферма сохранит их локально.",
    "manual": "Автоматического входа нет: материал для этой площадки готовится вручную.",
}


def _mode_for(platform_id: str) -> str:
    pid = str(platform_id or "").strip().lower()
    if pid == "vk":
        return "vkhost"
    if pid in OAUTH_PLATFORMS:
        return "oauth"
    if pid in TOKEN_PLATFORMS:
        return "token"
    if pid == "dzen":
        return "browser"  # собственный профиль, вход через /api/dzen/login
    if pid in BROWSER_ONLY or pid in PLATFORM_LOGIN_URLS:
        return "browser"
    if _AUTH_FIELDS.get(pid):
        return "token"
    return "manual"


def _fields(platform_id: str) -> list[dict[str, Any]]:
    """Return safe field metadata, never the configured values themselves."""
    pid = str(platform_id or "").strip().lower()
    settings = get_settings()
    oauth_keys = OAUTH_SETUP_FIELDS.get(pid)
    rows: list[dict[str, Any]] = []
    for field in _AUTH_FIELDS.get(pid, []):
        key = str(field.get("key") or "")
        if not key or field.get("hidden"):
            continue
        if oauth_keys is not None and key not in oauth_keys:
            continue
        attribute = str(field.get("attribute") or key.lower())
        configured_value = getattr(settings, attribute, "")
        rows.append(
            {
                "key": key,
                "label": FIELD_LABELS.get(key, field.get("label") or key),
                "secret": bool(field.get("secret", True)),
                "placeholder": str(field.get("placeholder") or ""),
                "help": str(field.get("help") or ""),
                "has_value": bool(str(configured_value or "").strip()),
            }
        )
    return rows


#: Where the chosen destination lives in settings, per platform.
DESTINATION_FIELDS: dict[str, tuple[str, ...]] = {
    "vk": ("VK_GROUP_ID",),
    "max": ("MAX_CHANNEL_ID", "MAX_CHANNEL_NAME"),
    "telegram": ("TELEGRAM_CHANNELS",),
    "reddit": ("REDDIT_SUBREDDIT",),
    "pinterest": ("PINTEREST_BOARD_ID",),
    "facebook": ("FACEBOOK_PAGE_ID",),
    "youtube": ("YOUTUBE_CHANNEL_ID",),
    "linkedin": ("LINKEDIN_MEMBER_ID",),
    "mastodon": ("MASTODON_INSTANCE",),
    "tumblr": ("TUMBLR_BLOG_NAME",),
    "discord": ("DISCORD_WEBHOOK_URL",),
    "odnoklassniki": ("OK_GROUP_ID",),
}


def _destination_state(platform_id: str) -> tuple[str, str]:
    """(state, label) for the concrete place a publication would go to."""
    settings = get_settings()
    fields = DESTINATION_FIELDS.get(platform_id, ())
    for key in fields:
        value = str(getattr(settings, key.lower(), "") or "").strip()
        if value:
            return "selected", value if len(value) <= 40 else value[:40] + "…"
    if fields:
        return "missing", ""
    return "unknown", ""


def platform_statuses(
    platform_id: str,
    *,
    mode: str,
    authorized: Any,
    keys_present: bool,
    session_verified: bool = False,
    profile_saved: bool = False,
    authorization_blocked: bool = False,
) -> dict[str, dict[str, str]]:
    """Four independent statuses instead of one green tick.

    ``auth`` answers "did the account sign in", ``api`` "is an API path usable",
    ``destination`` "is a concrete channel/page chosen", ``publish`` "can the farm
    actually deliver there". A platform is never ``publish: ready`` without the
    first three.
    """
    capability = capability_of(platform_id)
    if authorized is True:
        auth = {"state": "ready", "label": "Авторизован"}
    elif session_verified:
        auth = {"state": "ready", "label": "Сессия браузера активна"}
    elif authorized is False:
        auth = {"state": "failed", "label": "Вход не прошёл"}
    elif keys_present or profile_saved:
        auth = {"state": "pending", "label": "Сохранено, не проверено"}
    else:
        auth = {"state": "none", "label": "Не подключено"}

    if capability != "publish":
        api = {"state": "manual", "label": "API-публикации нет"}
    elif mode == "browser":
        # Dzen uses its dedicated DzenWebPublisher profile rather than the
        # generic Playwright profile. Its successful login is recorded through
        # record_auth_check(), so the verified platform check is authoritative
        # even when browser_session_status() has no generic profile to inspect.
        if authorized is True or session_verified:
            api = {"state": "ready", "label": "Браузерная доставка доступна"}
        elif profile_saved:
            api = {"state": "pending", "label": "Сессия сохранена, нужна проверка"}
        else:
            api = {"state": "missing", "label": "Нужна браузерная сессия"}
    elif authorized is True:
        api = {"state": "ready", "label": "Доступ API подтверждён"}
    elif keys_present:
        api = {"state": "pending", "label": "Реквизиты сохранены, доступ не проверен"}
    elif mode in {"oauth", "vkhost", "token"}:
        api = {"state": "missing", "label": "Нужен доступ API"}
    else:
        api = {"state": "unknown", "label": "Способ доступа не подтверждён"}

    if authorization_blocked:
        api = {"state": "blocked", "label": "Нужно одобрение API площадки"}

    dest_state, dest_label = _destination_state(platform_id)
    destination = {
        "state": "ready" if dest_state == "selected" else dest_state,
        "label": ("Назначено: " + dest_label) if dest_state == "selected" else (
            "Нужно выбрать назначение" if dest_state == "missing" else "Назначение не задаётся"
        ),
    }

    if capability != "publish":
        publish = {"state": "manual", "label": "Только вручную"}
    elif api["state"] == "ready" and destination["state"] in {"ready", "unknown"}:
        publish = {"state": "ready", "label": "Готово к публикации"}
    elif auth["state"] == "failed":
        publish = {"state": "blocked", "label": "Публикация заблокирована"}
    elif authorization_blocked:
        publish = {"state": "blocked", "label": "Нужно одобрение API площадки"}
    else:
        publish = {"state": "blocked", "label": "Рано публиковать"}

    return {"auth": auth, "api": api, "destination": destination, "publish": publish}


def auth_matrix() -> dict[str, Any]:
    """Every destination with the exact way it is connected on this machine."""
    checks = _read_auth_checks()
    platforms: list[dict[str, Any]] = []
    for item in CATALOG:
        pid = str(item["id"])
        mode = _mode_for(pid)
        check = checks.get(pid) or {}
        authorized = check.get("authorized") if check.get("fingerprint") else None
        session = {}
        if mode == "browser":
            try:
                session = browser_session_status(pid)
            except Exception:  # noqa: BLE001 - a missing profile is not an error
                session = {}
        keys_present = False
        keys_absent: list[str] = []
        if mode in {"token", "oauth"} or pid == "vk":
            settings = get_settings()
            required = list(
                OAUTH_REQUIRED_FIELDS.get(pid, ())
                if mode == "oauth"
                else REQUIRED_FIELDS.get(pid, tuple(field["key"] for field in _fields(pid)[:1]))
            )
            missing = []
            for key in required:
                attribute = next(
                    (str(field.get("attribute") or key.lower())
                     for field in _AUTH_FIELDS.get(pid, []) if field.get("key") == key),
                    key.lower(),
                )
                if not str(getattr(settings, attribute, "") or "").strip():
                    missing.append(key)
            keys_present = bool(required) and not missing
            keys_absent = missing
        reddit_approved = bool(getattr(settings, "reddit_api_approved", False)) if mode == "oauth" and pid == "reddit" else True
        oauth_allowed = bool(mode == "oauth" and keys_present and reddit_approved)
        oauth_blocker = (
            "Reddit Data API не одобрен для этого приложения. OAuth и публикация останутся заблокированы, пока Reddit явно не одобрит доступ и владелец не включит REDDIT_API_APPROVED=true."
            if pid == "reddit" and not reddit_approved
            else ""
        )
        statuses = platform_statuses(
            pid,
            mode=mode,
            authorized=authorized,
            keys_present=keys_present,
            session_verified=bool(session.get("verified")),
            profile_saved=bool(session.get("profile_saved")),
            authorization_blocked=bool(pid == "reddit" and not reddit_approved),
        )
        platforms.append(
            {
                "id": pid,
                "keys_present": keys_present,
                "keys_absent": keys_absent,
                "configured": keys_present and (mode != "oauth" or oauth_allowed),
                "oauth_allowed": oauth_allowed,
                "oauth_blocker": oauth_blocker,
                "reddit_api_approved": reddit_approved if pid == "reddit" else None,
                "docs_url": str(item.get("docs_url") or ""),
                "requirements": str(item.get("requirements") or ""),
                "instance_url": (
                    str(getattr(get_settings(), "mastodon_instance", "") or "")
                    if pid == "mastodon" else ""
                ),
                "name": item.get("name") or pid,
                "category": item.get("category") or "",
                "mode": mode,
                "hint": MODE_HINTS.get(mode, ""),
                "login_url": PLATFORM_LOGIN_URLS.get(pid, "") if mode == "browser" else "",
                "fields": _fields(pid),
                "capability": capability_of(pid),
                "statuses": statuses,
                "authorized": authorized,
                "account": check.get("account") or "",
                "checked_at": check.get("checked_at") or "",
                "profile_saved": bool(session.get("profile_saved")),
                "session_verified": bool(session.get("verified")),
                "session_state": session.get("state") or "",
            }
        )
    return {
        "platforms": platforms,
        "modes": {mode: sum(1 for row in platforms if row["mode"] == mode) for mode in MODE_HINTS},
    }


def authorize_status(platform_id: str) -> dict[str, Any]:
    """What is known about one platform right now, without a network call."""
    pid = str(platform_id or "").strip().lower()
    matrix_row = next((row for row in auth_matrix()["platforms"] if row["id"] == pid), None)
    if matrix_row is None:
        raise ValueError("Неизвестная площадка.")
    check = _read_auth_checks().get(pid) or {}
    session: dict[str, Any] = {}
    if matrix_row["mode"] == "browser":
        try:
            session = browser_session_status(pid)
        except Exception:  # noqa: BLE001
            session = {}
    return {
        "platform": pid,
        "mode": matrix_row["mode"],
        "authorized": check.get("authorized"),
        "account": check.get("account") or "",
        "detail": check.get("detail") or "",
        "checked_at": check.get("checked_at") or "",
        "profile_saved": bool(session.get("profile_saved")),
        "session_verified": bool(session.get("verified")),
        "session_state": session.get("state") or "",
    }


async def authorize(platform_id: str, *, timeout_s: int = 420) -> dict[str, Any]:
    """Start the flow this machine can perform; never fabricate a success."""
    pid = str(platform_id or "").strip().lower()
    mode = _mode_for(pid)
    if not any(str(item["id"]) == pid for item in CATALOG):
        raise ValueError("Неизвестная площадка.")

    if mode == "vkhost":
        from tgfarm.browser_auth import capture_vkhost_token
        from tgfarm.platforms import record_auth_check, save_platform_credentials

        credentials = await asyncio.to_thread(lambda: asyncio.run(capture_vkhost_token(timeout_s)))
        await asyncio.to_thread(
            save_platform_credentials, "vk", {"VK_ACCESS_TOKEN": credentials["access_token"]}
        )
        await asyncio.to_thread(
            record_auth_check,
            "vk",
            True,
            account=credentials.get("account") or "",
            detail="Токен получен через VKHost, проверен users.get и сохранён локально.",
        )
        return {
            "platform": pid,
            "mode": mode,
            "state": "authorized",
            "account": credentials.get("account") or "",
            "detail": "Токен сохранён. Сам токен в интерфейс не возвращается.",
        }

    if mode == "browser":
        from tgfarm.browser_auth import login_in_browser

        if pid == "dzen":
            return {
                "platform": pid,
                "mode": mode,
                "state": "separate_window",
                "detail": "У Дзена отдельный профиль: используй кнопку «Дзен: войти».",
                "endpoint": "/api/dzen/login",
            }
        result = await asyncio.to_thread(lambda: asyncio.run(login_in_browser(pid, timeout_s)))
        state = "authorized" if result.get("verified") else "unverified"
        return {
            "platform": pid,
            "mode": mode,
            "state": state,
            "account": result.get("account") or "",
            "detail": result.get("detail") or "",
            "profile_saved": bool(result.get("profile_saved")),
        }

    if mode == "oauth":
        return {
            "platform": pid,
            "mode": mode,
            "state": "awaiting_oauth",
            "start_url": f"/api/platforms/{pid}/auth/start",
            "detail": "Откроется страница согласия; токен сохранит callback площадки.",
        }

    if mode == "token":
        return {
            "platform": pid,
            "mode": mode,
            "state": "awaiting_token",
            "fields": _fields(pid),
            "detail": "Вставь ключи и сохрани: значения уйдут в локальный .env.",
        }

    return {
        "platform": pid,
        "mode": "manual",
        "state": "manual_only",
        "detail": "Автоматического входа нет; этот материал готовится вручную.",
    }


__all__ = ["auth_matrix", "authorize", "authorize_status", "MODE_HINTS"]

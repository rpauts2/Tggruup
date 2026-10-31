"""Interactive, persistent browser sign-in helpers for platform accounts.

A browser profile is stored locally per platform. This is deliberately separate
from API credentials: a saved website session is not reported as permission to
publish through an API adapter.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import DATA_DIR, get_settings

log = get_logger(__name__)

# These URLs are public login/landing pages only. Credentials are entered by the
# owner directly at the platform; TgFarm never asks for or records site passwords.
PLATFORM_LOGIN_URLS: dict[str, str] = {
    "telegram": "https://web.telegram.org/",
    "vk": "https://vk.com/",
    "dzen": "https://dzen.ru/profile/editor/",
    "max": "https://web.max.ru/",
    "x": "https://x.com/",
    "reddit": "https://www.reddit.com/",
    "bluesky": "https://bsky.app/",
    "youtube": "https://studio.youtube.com/",
    "tiktok": "https://www.tiktok.com/",
    "instagram": "https://www.instagram.com/",
    "threads": "https://www.threads.net/",
    "facebook": "https://www.facebook.com/",
    "linkedin": "https://www.linkedin.com/",
    "pinterest": "https://www.pinterest.com/",
    "odnoklassniki": "https://ok.ru/",
    "rutube": "https://rutube.ru/",
    "discord": "https://discord.com/app",
    "mastodon": "https://joinmastodon.org/",
    "whatsapp_channels": "https://web.whatsapp.com/",
    "medium": "https://medium.com/",
    "vc_ru": "https://vc.ru/",
    "habr": "https://habr.com/",
    "pikabu": "https://pikabu.ru/",
    "tenchat": "https://tenchat.ru/",
    "twitch": "https://www.twitch.tv/",
    "tumblr": "https://www.tumblr.com/",
    "snapchat": "https://www.snapchat.com/",
    "wechat": "https://www.wechat.com/",
    "douyin": "https://www.douyin.com/",
    "rednote": "https://www.xiaohongshu.com/",
}

# Cookie names are used only as a conservative signal that the owner completed
# the sign-in. They are never read as credentials, exported, or returned.
_AUTH_COOKIE_MARKERS: dict[str, tuple[str, ...]] = {
    "telegram": ("stel_ssid", "stel_token"),
    "vk": ("remixsid", "remixsid6"),
    "x": ("auth_token",),
    "reddit": ("reddit_session",),
    "youtube": ("__secure-3psid", "__secure-1psid"),
    "tiktok": ("sessionid", "sessionid_ss"),
    "instagram": ("sessionid",),
    "threads": ("sessionid",),
    "facebook": ("c_user", "xs"),
    "linkedin": ("li_at",),
    "pinterest": ("_pinterest_sess",),
    "odnoklassniki": ("authcode",),
    "rutube": ("auth_token", "sessionid"),
    "discord": ("__secure-session_id",),
    "mastodon": ("_mastodon_session", "_session_id"),
    "whatsapp_channels": ("wa_auth", "session"),
    "medium": ("sid",),
    "vc_ru": ("auth", "session"),
    "habr": ("habr_session_id",),
    "pikabu": ("auth", "session"),
    "tenchat": ("session", "auth"),
    "twitch": ("auth-token",),
    "tumblr": ("sid",),
    "snapchat": ("web_session",),
    "wechat": ("slave_user", "wxuin"),
    "douyin": ("sessionid",),
    "rednote": ("web_session", "customer-sso-sid"),
    # Bluesky stores its login in browser local storage, not a normal cookie.
}

def _profile_dir(platform_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_]{2,40}", platform_id):
        raise ValueError("Недопустимый идентификатор площадки.")
    path = DATA_DIR / "platform_profiles" / platform_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(platform_id: str) -> Path:
    root = DATA_DIR / "platform_auth"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{platform_id}.json"


def browser_session_status(platform_id: str) -> dict[str, Any]:
    """Return safe status metadata. Never expose cookie/database contents."""
    path = _state_path(platform_id)
    profile = DATA_DIR / "platform_profiles" / platform_id
    saved = False
    checked_at = None
    state = "not_started"
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved = bool(payload.get("profile_saved"))
            checked_at = payload.get("checked_at")
            state = str(payload.get("state") or "unknown")
    except (OSError, ValueError, TypeError):
        state = "unknown"
    profile_saved = saved and profile.is_dir() and any(profile.iterdir())
    return {
        "available": platform_id in PLATFORM_LOGIN_URLS,
        "profile_saved": profile_saved,
        "verified": state == "verified" and profile_saved,
        "state": state if profile_saved else "not_started",
        "checked_at": checked_at if profile_saved else None,
        "note": (
            "Сайт-сессия сохранена локально. Для API-публикации отдельно нужны "
            "разрешения приложения и поддерживаемый адаптер."
            if profile_saved else
            "Отдельный браузерный профиль ещё не подключён."
        ),
    }


def _save_status(platform_id: str, *, state: str, profile_saved: bool) -> None:
    path = _state_path(platform_id)
    payload = {
        "platform": platform_id,
        "state": state,
        "profile_saved": bool(profile_saved),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


async def _launch_context(playwright, profile_dir: Path):
    settings = get_settings()
    kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "no_viewport": True,
        "locale": "ru-RU",
        "timezone_id": settings.tz,
        "accept_downloads": False,
        "args": ["--no-first-run", "--no-default-browser-check", "--start-maximized"],
    }
    channel = (settings.dzen_browser or "chrome").strip().lower()
    if channel in {"chrome", "msedge"}:
        try:
            return await playwright.chromium.launch_persistent_context(channel=channel, **kwargs)
        except Exception as exc:  # noqa: BLE001 - use bundled Chromium if installed
            if "not found" not in str(exc).lower():
                raise
    return await playwright.chromium.launch_persistent_context(**kwargs)


def _matches_auth_cookie(platform_id: str, cookies: list[dict[str, Any]]) -> bool:
    markers = _AUTH_COOKIE_MARKERS.get(platform_id, ())
    names = {str(cookie.get("name") or "").lower() for cookie in cookies}
    if platform_id == "facebook":
        return {"c_user", "xs"} <= names
    return any(marker in names for marker in markers)


async def login_in_browser(platform_id: str, timeout_s: int = 420) -> dict[str, Any]:
    """Open an isolated headed browser profile and wait for a known auth marker.

    For sites whose session storage is not publicly stable, the result is
    explicitly unverified even though the persistent profile is retained.
    """
    platform_id = str(platform_id or "").strip().lower()
    login_url = PLATFORM_LOGIN_URLS.get(platform_id)
    if not login_url:
        raise ValueError("Для этой площадки не настроен браузерный вход.")
    if platform_id == "dzen":
        raise ValueError("Для Дзена используй отдельную кнопку входа в его профиль.")
    if platform_id == "vk":
        raise ValueError("Для VK используй подключение токена через VKHost.")
    from playwright.async_api import async_playwright

    profile = _profile_dir(platform_id)
    timeout = max(60, min(1800, int(timeout_s)))
    started = time.monotonic()
    authorized = False
    account_hint = ""
    async with async_playwright() as playwright:
        context = await _launch_context(playwright, profile)
        try:
            pages = [page for page in context.pages if not page.is_closed()]
            page = pages[0] if pages else await context.new_page()
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            log.info("browser auth opened for %s", platform_id)
            while time.monotonic() - started < timeout:
                try:
                    cookies = await context.cookies([login_url])
                    if _matches_auth_cookie(platform_id, cookies):
                        authorized = True
                        break
                    if all(item.is_closed() for item in context.pages):
                        break
                except Exception as exc:  # noqa: BLE001 - browser may be closing
                    log.debug("browser auth probe %s: %s", platform_id, type(exc).__name__)
                    break
                await asyncio.sleep(1.5)
            # Closing the context flushes the persistent profile to disk.
        finally:
            await context.close()
    _save_status(
        platform_id,
        state="verified" if authorized else "saved_unverified",
        profile_saved=profile.is_dir() and any(profile.iterdir()),
    )
    return {
        "ok": True,
        "platform": platform_id,
        "authorized": authorized,
        "profile_saved": profile.is_dir() and any(profile.iterdir()),
        "state": "verified" if authorized else "saved_unverified",
        "detail": (
            "Вход распознан по сессионной cookie; профиль сохранён на этом компьютере."
            if authorized else
            "Браузерный профиль сохранён, но надёжный маркер входа не обнаружен. "
            "Это не подтверждает API-доступ для публикации."
        ),
    }


async def capture_vkhost_token(timeout_s: int = 420) -> dict[str, Any]:
    """Let the owner authorize via VKHost and capture the OAuth redirect fragment."""
    from playwright.async_api import async_playwright
    import httpx

    profile = DATA_DIR / "platform_profiles" / "vkhost"
    profile.mkdir(parents=True, exist_ok=True)
    timeout = max(60, min(1800, int(timeout_s)))
    started = time.monotonic()
    token = ""
    expires_in = 0
    user_id = ""
    async with async_playwright() as playwright:
        context = await _launch_context(playwright, profile)
        try:
            pages = [page for page in context.pages if not page.is_closed()]
            page = pages[0] if pages else await context.new_page()
            await page.goto("https://vkhost.github.io/", wait_until="domcontentloaded", timeout=60_000)
            log.info("VKHost opened; waiting for owner-approved OAuth redirect")
            while time.monotonic() - started < timeout:
                for candidate in list(context.pages):
                    if candidate.is_closed():
                        continue
                    current = candidate.url
                    parsed = urlparse(current)
                    if parsed.scheme != "https" or parsed.netloc.lower() != "oauth.vk.com":
                        continue
                    if not parsed.path.rstrip("/").endswith("blank.html"):
                        continue
                    fragment = parse_qs(parsed.fragment)
                    token = str((fragment.get("access_token") or [""])[0]).strip()
                    expires_raw = str((fragment.get("expires_in") or ["0"])[0])
                    user_id = str((fragment.get("user_id") or [""])[0]).strip()
                    try:
                        expires_in = max(0, int(expires_raw))
                    except ValueError:
                        expires_in = 0
                    if token:
                        break
                if token:
                    break
                await asyncio.sleep(0.5)
            if not token:
                raise TimeoutError(
                    "VKHost не вернул OAuth-токен. Заверши вход и подтверждение разрешений в открытом окне."
                )
        finally:
            await context.close()

    # Validate the token without writing it to logs or returning it to the UI.
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            "https://api.vk.com/method/users.get",
            params={"access_token": token, "v": "5.199"},
        )
        response.raise_for_status()
        body = response.json()
    if body.get("error"):
        raise ValueError("VK отклонил полученный токен. Проверь выбранное приложение и разрешения.")
    users = body.get("response") or []
    if not users:
        raise ValueError("VK вернул токен, но не подтвердил пользователя.")
    user = users[0]
    user_id = str(user.get("id") or user_id)
    account = " ".join(
        part for part in (str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip())
        if part
    ) or f"VK user {user_id}"
    return {
        "access_token": token,
        "user_id": user_id,
        "expires_in": expires_in,
        "account": account,
    }

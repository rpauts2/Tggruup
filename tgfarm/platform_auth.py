"""Live, redacted authorization checks for built-in platform adapters."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from tgfarm.core.settings import get_settings
from tgfarm.platforms import _configured_state, record_auth_check


async def _response_json(
    client: httpx.AsyncClient, url: str, *, params: dict[str, str] | None = None
) -> dict[str, Any]:
    response = await client.get(url, params=params)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        # Never return request URLs, headers, token fragments or raw error bodies.
        if response.status_code in (401, 403):
            raise RuntimeError("Площадка отклонила доступ: проверь токен и разрешения.")
        if response.status_code == 429:
            raise RuntimeError("Площадка временно ограничила запросы. Повтори проверку позже.")
        raise RuntimeError(f"Площадка ответила HTTP {response.status_code}.")
    if isinstance(payload, dict):
        return payload
    raise RuntimeError("Площадка вернула неожиданный формат ответа.")


async def check_platform_auth(platform_id: str) -> dict[str, Any]:
    """Make one read-only provider request and remember only redacted results."""
    platform_id = str(platform_id or "").strip().lower()
    settings = get_settings()
    account = ""
    detail = ""
    authorized: bool | None = None

    try:
        configured, config_hint = _configured_state(platform_id)
        if platform_id == "dzen":
            from tgfarm.dzen_web import DzenWebPublisher

            publisher = DzenWebPublisher(headless=False)
            result = await publisher.cached_status()
            authorized = result.get("authorized") is True
            account = str(result.get("account") or result.get("publisher_name") or "")
            detail = (
                "Авторизованная сессия Дзена найдена в профиле браузера."
                if authorized else
                str(result.get("detail") or "Нужен вход в Яндекс через окно браузера Дзена.")
            )
        elif platform_id == "telegram":
            token = str(settings.telegram_bot_token or "").strip()
            if not token:
                raise RuntimeError("Введи TELEGRAM_BOT_TOKEN.")
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = await _response_json(
                    client, f"https://api.telegram.org/bot{token}/getMe"
                )
            if payload.get("ok") is not True:
                raise RuntimeError("Telegram не подтвердил токен бота.")
            user = payload.get("result") or {}
            account = "@" + str(user.get("username")) if user.get("username") else str(user.get("first_name") or "Telegram bot")
            authorized = True
            detail = "Bot API принял токен. Отдельно проверь право бота публиковать в целевой канал."
        elif platform_id == "vk":
            token = str(settings.vk_access_token or "").strip()
            group_id = str(settings.vk_group_id or "").strip().lstrip("-")
            if not token or not group_id:
                raise RuntimeError("Введи VK_ACCESS_TOKEN и VK_GROUP_ID.")
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = await _response_json(
                    client,
                    "https://api.vk.com/method/groups.getById",
                    params={"access_token": token, "group_id": group_id, "v": "5.199"},
                )
            if payload.get("error"):
                code = payload["error"].get("error_code")
                raise RuntimeError(
                    "VK отклонил запрос: проверь токен, версию API и ID сообщества"
                    + (f" (код {code})." if code else ".")
                )
            groups = payload.get("response") or []
            if not groups:
                raise RuntimeError("VK не вернул сообщество для указанного ID.")
            account = str(groups[0].get("name") or f"Сообщество {group_id}")
            authorized = True
            detail = "VK API подтвердил токен и доступ к данным сообщества. Право публикации проверь отдельным тестом."
        elif platform_id == "max":
            token = str(settings.max_access_token or "").strip()
            channel_id = str(settings.max_channel_id or "").strip()
            if not token:
                raise RuntimeError("Введи MAX_ACCESS_TOKEN.")
            headers = {"Authorization": token}
            async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
                bot = await _response_json(client, "https://platform-api2.max.ru/me")
                account = str(bot.get("username") or bot.get("name") or bot.get("first_name") or bot.get("user_id") or "MAX bot")
                if channel_id:
                    await _response_json(
                        client, f"https://platform-api2.max.ru/chats/{quote(channel_id, safe='')}"
                    )
            authorized = True
            detail = (
                "Токен принят; целевой чат/канал тоже найден."
                if channel_id else
                "Токен принят. Добавь ID целевого канала и проверь права бота."
            )
        elif platform_id == "bluesky":
            handle = str(settings.bluesky_handle or "").strip()
            app_password = str(settings.bluesky_app_password or "").strip()
            if not handle or not app_password:
                raise RuntimeError("Введи BLUESKY_HANDLE и BLUESKY_APP_PASSWORD.")
            async with httpx.AsyncClient(timeout=15.0) as client:
                session_response = await client.post(
                    "https://bsky.social/xrpc/com.atproto.server.createSession",
                    json={"identifier": handle, "password": app_password},
                )
                try:
                    session_payload = session_response.json()
                except ValueError:
                    session_payload = {}
                if session_response.status_code >= 400:
                    if session_response.status_code in (401, 403):
                        raise RuntimeError("Bluesky отклонил app password. Создай новый пароль приложения и повтори.")
                    raise RuntimeError(f"Bluesky ответил HTTP {session_response.status_code}.")
                did = str(session_payload.get("did") or "")
                returned_handle = str(session_payload.get("handle") or handle)
                if not did:
                    raise RuntimeError("Bluesky не подтвердил DID аккаунта.")
                account = "@" + returned_handle
                authorized = True
                detail = "Bluesky app password принят, DID аккаунта подтверждён. Тест публикации отдельно проверяет write-доступ."
        elif platform_id == "x":
            token = str(settings.x_bearer_token or "").strip()
            user_id = str(settings.x_user_id or "").strip()
            if not token or not user_id:
                raise RuntimeError("Введи X_BEARER_TOKEN и X_USER_ID.")
            async with httpx.AsyncClient(
                timeout=15.0, headers={"Authorization": f"Bearer {token}"}
            ) as client:
                payload = await _response_json(
                    client,
                    f"https://api.x.com/2/users/{quote(user_id, safe='')}",
                    params={"user.fields": "name,username"},
                )
            user = payload.get("data") or {}
            if not user.get("id"):
                raise RuntimeError("X API не подтвердил профиль. Проверь User ID и права токена.")
            account = "@" + str(user.get("username") or user.get("name") or user_id)
            authorized = True
            detail = "X API подтвердил Bearer token и доступ к профилю. Наличие права создавать посты проверяется отдельно."
        elif platform_id == "reddit":
            from tgfarm.s9_growth.adapters import ADAPTERS, RedditAdapter

            adapter = ADAPTERS.get("reddit")
            if not isinstance(adapter, RedditAdapter):
                raise RuntimeError("Reddit-адаптер не зарегистрирован.")
            if not adapter.configured():
                raise RuntimeError("Заполни client ID/secret, refresh token, subreddit и User-Agent.")
            token = await adapter._access_token()
            subreddit = str(settings.reddit_subreddit or "").strip().removeprefix("r/")
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "User-Agent": str(settings.reddit_user_agent or ""),
                    "Authorization": f"Bearer {token}",
                },
            ) as client:
                about = await _response_json(
                    client,
                    f"https://oauth.reddit.com/r/{quote(subreddit, safe='')}/about",
                )
            community = about.get("data") or {}
            if not community.get("display_name"):
                raise RuntimeError("Reddit OAuth прошёл, но сообщество не найдено или чтение запрещено.")
            account = "OAuth · r/" + str(community.get("display_name"))
            authorized = True
            detail = "Reddit OAuth и read-доступ подтверждены для выбранного сообщества. Публикация отдельно зависит от scope submit и правил."
        else:
            raise RuntimeError("Для этой площадки пока нет API-проверки авторизации.")

    except httpx.TimeoutException:
        authorized = None
        detail = "Площадка не ответила вовремя. Это не доказывает, что токен неверный."
    except httpx.RequestError:
        authorized = None
        detail = "Не удалось подключиться к площадке. Проверь интернет и повтори попытку."
    except Exception as exc:  # noqa: BLE001 - return only a bounded, redacted operator hint
        detail = str(exc)[:400] or "Проверка доступа не пройдена."
    else:
        pass

    saved = record_auth_check(
        platform_id, authorized, account=account, detail=detail
    )
    return {
        "ok": authorized,
        "platform": platform_id,
        "authorized": authorized,
        "account": account,
        "detail": detail,
        "checked_at": saved.get("checked_at"),
        "auth": saved,
    }

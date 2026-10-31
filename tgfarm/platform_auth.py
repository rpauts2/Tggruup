"""Live, redacted authorization checks for built-in platform adapters."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from tgfarm.core.settings import get_settings
from tgfarm.core.security import redact_sensitive_text
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
        elif platform_id == "discord":
            webhook = str(settings.discord_webhook_url or "").strip()
            if not webhook:
                raise RuntimeError("Введи DISCORD_WEBHOOK_URL.")
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                payload = await _response_json(client, webhook)
            webhook_id = str(payload.get("id") or "")
            channel_id = str(payload.get("channel_id") or "")
            if not webhook_id or not channel_id:
                raise RuntimeError("Discord не подтвердил webhook и целевой канал.")
            channel_name = str(payload.get("name") or "webhook")
            account = f"Discord · #{channel_name}"
            authorized = True
            detail = "Discord подтвердил webhook и канал read-only запросом. Проверка не отправляла тестовое сообщение."
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
        elif platform_id == "odnoklassniki":
            from tgfarm.s9_growth.odnoklassniki import ok_api_call, ok_has_permission

            group_id = str(settings.ok_group_id or "").strip()
            if not group_id or not settings.ok_access_token:
                raise RuntimeError("Сначала сохрани OK application key/secret, access token и ID группы.")
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
                current_user = await ok_api_call(client, "users.getCurrentUser")
                uid = str(current_user or "").strip().strip('"')
                if not uid:
                    raise RuntimeError("OK не подтвердил пользователя через users.getCurrentUser.")
                group = await ok_api_call(
                    client,
                    "group.getInfo",
                    {"uids": group_id, "fields": "NAME,URL"},
                )
                if isinstance(group, list):
                    group_info = group[0] if group else {}
                elif isinstance(group, dict):
                    group_info = group.get(group_id) or group
                else:
                    group_info = {}
                if not isinstance(group_info, dict):
                    raise RuntimeError("OK не вернул информацию о выбранной группе.")
                group_name = str(group_info.get("name") or group_info.get("NAME") or "")
                if not await ok_has_permission(client, "GROUP_CONTENT"):
                    raise RuntimeError("У приложения нет GROUP_CONTENT для публикации в выбранную группу.")
                photo_permission = await ok_has_permission(client, "PHOTO_CONTENT")
                video_permission = await ok_has_permission(client, "VIDEO_CONTENT")
            account = f"OK · {group_name or group_id} · user {uid}"
            authorized = True
            detail = (
                "OK подтвердил текущего пользователя, группу и GROUP_CONTENT. "
                + ("PHOTO_CONTENT доступен. " if photo_permission else "PHOTO_CONTENT не выдан: публикация без фото. ")
                + ("VIDEO_CONTENT доступен." if video_permission else "VIDEO_CONTENT не выдан: публикация без видео.")
            )
        elif platform_id == "pinterest":
            from tgfarm.pinterest_oauth import get_valid_pinterest_access_token

            board_id = str(settings.pinterest_board_id or "").strip()
            if not board_id or not settings.pinterest_refresh_token:
                raise RuntimeError("Сначала подключи Pinterest через OAuth и укажи ID доски.")
            token = await get_valid_pinterest_access_token()
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                board = await _response_json(
                    client,
                    f"https://api.pinterest.com/v5/boards/{quote(board_id, safe='')}",
                )
            if str(board.get("id") or "") != board_id:
                raise RuntimeError("Pinterest не подтвердил выбранную доску. Проверь её ID и доступ.")
            board_name = str(board.get("name") or board_id)
            owner = board.get("owner") or {}
            username = str(owner.get("username") or "").strip()
            account = ("@" + username + " · " if username else "") + board_name
            authorized = True
            detail = "Pinterest OAuth и доступ к выбранной доске подтверждены read-only запросом."
        elif platform_id == "instagram":
            from tgfarm.instagram_oauth import get_valid_instagram_access_token

            expected_user_id = str(settings.instagram_user_id or "").strip()
            if not expected_user_id or not settings.instagram_access_token:
                raise RuntimeError("Сначала подключи Instagram через Instagram Login OAuth.")
            token = await get_valid_instagram_access_token()
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = await _response_json(
                    client,
                    "https://graph.instagram.com/v25.0/me",
                    params={"fields": "user_id,username", "access_token": token},
                )
            returned_id = str(payload.get("user_id") or payload.get("id") or "")
            username = str(payload.get("username") or settings.instagram_username or "").strip()
            if not returned_id or returned_id != expected_user_id or not username:
                raise RuntimeError("Instagram не подтвердил ожидаемый профессиональный профиль. Переподключи OAuth.")
            account = "@" + username
            authorized = True
            detail = "Instagram Login OAuth подтверждён read-запросом к профилю; доступ к публикации зависит от instagram_business_content_publish."
        elif platform_id == "threads":
            from tgfarm.threads_oauth import get_valid_threads_access_token

            user_id = str(settings.threads_user_id or "").strip()
            if not user_id or not settings.threads_access_token:
                raise RuntimeError("Сначала подключи аккаунт через Threads OAuth.")
            token = await get_valid_threads_access_token()
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = await _response_json(
                    client,
                    "https://graph.threads.net/v1.0/me",
                    params={"fields": "id,username", "access_token": token},
                )
            returned_id = str(payload.get("id") or "")
            username = str(payload.get("username") or settings.threads_username or "")
            if not returned_id or returned_id != user_id or not username:
                raise RuntimeError("Threads не подтвердил ожидаемый аккаунт. Переподключи OAuth.")
            account = "@" + username
            authorized = True
            detail = "Threads OAuth подтверждён read-запросом к профилю. Публикация отдельно зависит от разрешения threads_content_publish."
        elif platform_id == "mastodon":
            from tgfarm.mastodon_oauth import normalize_instance_url

            instance = normalize_instance_url(settings.mastodon_instance)
            token = str(settings.mastodon_access_token or "").strip()
            if not token:
                raise RuntimeError("Сначала нажми «Подключить через OAuth» и подтверди доступ на сервере Mastodon.")
            async with httpx.AsyncClient(
                timeout=15.0, headers={"Authorization": f"Bearer {token}"}
            ) as client:
                user = await _response_json(client, f"{instance}/api/v1/accounts/verify_credentials")
            acct = str(user.get("acct") or user.get("username") or "")
            if not acct:
                raise RuntimeError("Mastodon не подтвердил пользовательский профиль.")
            if "@" not in acct:
                acct += "@" + instance.split("://", 1)[-1]
            account = "@" + acct
            authorized = True
            detail = "Mastodon OAuth подтверждён; профиль доступен. Текстовая публикация использует API с защитой от повторной отправки."
        elif platform_id == "youtube":
            from tgfarm.youtube_oauth import get_valid_youtube_access_token

            expected_channel_id = str(settings.youtube_channel_id or "").strip()
            if not expected_channel_id or not settings.youtube_refresh_token:
                raise RuntimeError("Сначала подключи YouTube через Google OAuth в панели.")
            token = await get_valid_youtube_access_token()
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                payload = await _response_json(
                    client,
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"part": "snippet", "mine": "true"},
                )
            items = payload.get("items") or []
            channel = items[0] if items and isinstance(items[0], dict) else {}
            returned_id = str(channel.get("id") or "")
            snippet = channel.get("snippet") or {}
            title = str(snippet.get("title") or "")
            if not returned_id or returned_id != expected_channel_id or not title:
                raise RuntimeError("YouTube не подтвердил ожидаемый канал. Переподключи аккаунт.")
            account = title
            authorized = True
            detail = (
                "Google OAuth и YouTube Data API подтвердили доступ к каналу. "
                "Новый видеозагрузчик остаётся выключен, пока его не включат в панели."
            )
        elif platform_id == "linkedin":
            from tgfarm.linkedin_oauth import get_valid_linkedin_access_token

            member_id = str(settings.linkedin_member_id or "").strip()
            if not member_id or not settings.linkedin_access_token:
                raise RuntimeError("Сначала настрой LinkedIn OAuth-приложение и подключи аккаунт.")
            token = await get_valid_linkedin_access_token()
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                profile = await _response_json(client, "https://api.linkedin.com/v2/userinfo")
            returned_id = str(profile.get("sub") or "")
            name = str(profile.get("name") or settings.linkedin_member_name or "").strip()
            if not returned_id or returned_id != member_id:
                raise RuntimeError("LinkedIn не подтвердил ожидаемый аккаунт. Переподключи OAuth.")
            account = name or f"LinkedIn member {member_id}"
            authorized = True
            detail = "LinkedIn OAuth подтверждён запросом OpenID Connect userinfo. Публикация зависит от разрешения w_member_social."
        elif platform_id == "x":
            from tgfarm.x_oauth import get_valid_x_access_token

            user_id = str(settings.x_user_id or "").strip()
            if not user_id and not settings.x_refresh_token:
                raise RuntimeError("Сначала настрой X OAuth-приложение и подключи аккаунт через OAuth.")
            token = await get_valid_x_access_token()
            async with httpx.AsyncClient(
                timeout=15.0, headers={"Authorization": f"Bearer {token}"}
            ) as client:
                payload = await _response_json(
                    client,
                    "https://api.x.com/2/users/me",
                    params={"user.fields": "name,username"},
                )
            user = payload.get("data") or {}
            if not user.get("id"):
                raise RuntimeError("X OAuth не подтвердил профиль. Повтори авторизацию.")
            account = "@" + str(user.get("username") or user_id or user.get("id"))
            authorized = True
            detail = "X подтвердил пользовательский OAuth-токен и профиль; публикация использует права tweet.write."
        elif platform_id == "tiktok":
            from tgfarm.tiktok_oauth import get_tiktok_creator_info

            if not settings.tiktok_open_id or not settings.tiktok_refresh_token:
                raise RuntimeError("Сначала подключи TikTok через OAuth.")
            creator = await get_tiktok_creator_info()
            username = str(creator.get("username") or settings.tiktok_username or "").strip()
            account = ("@" + username) if username else "TikTok creator"
            authorized = True
            detail = (
                "TikTok OAuth подтверждён живым creator-info запросом. "
                "Настройки приватности обновляются перед каждой публикацией; "
                "отправка запускается только вручную из карточки поста."
            )
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
        detail = redact_sensitive_text(exc, limit=400) or "Проверка доступа не пройдена."
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

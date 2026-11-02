"""Central registry for distribution platforms.

Only adapters that exist in this codebase can be activated. Other popular
networks can be saved to the integration plan without pretending they can
already receive posts. Secrets remain in environment/.env and are never
returned by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from tgfarm.config import get_config
from tgfarm.core.settings import config_dir, get_settings

_STATE_LOCK = RLock()
STATE_FILENAME = "platforms.yaml"

# "built_in" means a real publishing route exists in S7 or S9. For planned
# platforms, the account/API flow, adapter and idempotency tests are not done.
CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "telegram", "name": "Telegram", "symbol": "TG", "color": "#36A9E8",
        "category": "Мессенджеры", "market": "Россия и мир", "priority": "core",
        "mode": "built_in", "roles": ["primary"],
        "description": "Основной канал публикации через Telegram Bot API.",
        "requirements": "Токен бота и целевой канал, куда бот имеет право публиковать.",
        "docs_url": "https://core.telegram.org/bots/api",
    },
    {
        "id": "vk", "name": "ВКонтакте", "symbol": "VK", "color": "#0077FF",
        "category": "Российские", "market": "Россия и СНГ", "priority": "core",
        "mode": "built_in", "roles": ["primary", "crosspost"],
        "description": "Публикации в сообщество и кросспост с переносом доступной обложки.",
        "requirements": "VK_ACCESS_TOKEN и VK_GROUP_ID. Токен удобно получить через vkhost.github.io (implicit flow, scope: wall,photos,groups,offline) и вставить в поля ниже.",
        "docs_url": "https://dev.vk.com/",
    },
    {
        "id": "vk_clips", "name": "VK Клипы", "symbol": "VK▶", "color": "#0077FF",
        "category": "Российские", "market": "Россия", "priority": "P1",
        "mode": "planned", "roles": ["video"],
        "description": (
            "Отдельная интеграция коротких вертикальных видео. Обычный wall.post или "
            "generic video.save не считается подтверждённой публикацией в ленту Клипов."
        ),
        "requirements": (
            "Проверить официальный clip-specific upload/publish flow и права сообщества; "
            "пока загрузка в VK Клипы не реализована."
        ),
        "docs_url": "https://dev.vk.com/",
    },
    {
        "id": "dzen", "name": "Дзен", "symbol": "Д", "color": "#F44336",
        "category": "Российские", "market": "Россия", "priority": "core",
        "mode": "built_in", "roles": ["primary"],
        "description": "Отдельные статьи через браузерный редактор и сохранённый профиль.",
        "requirements": "Вход в профиль браузера Дзена; это не публичный API-паблишер. Вход — кнопка «Войти» в этой карточке (headless-проверка статуса — рядом).",
        "docs_url": "https://dzen.ru/",
    },
    {
        "id": "max", "name": "MAX", "symbol": "M", "color": "#7D5CFF",
        "category": "Мессенджеры", "market": "Россия", "priority": "core",
        "mode": "built_in", "roles": ["crosspost"],
        "description": "Кросспост через API бота MAX.",
        "requirements": "MAX_ACCESS_TOKEN, TGFARM_PUBLIC_BASE_URL и выбранный через webhook чат/канал с правом write.",
        "docs_url": "https://dev.max.ru/",
    },
    {
        "id": "x", "name": "X", "symbol": "X", "color": "#E7EAF0",
        "category": "Мировые", "market": "Международная", "priority": "core",
        "mode": "built_in", "roles": ["crosspost"],
        "description": "Короткая версия поста для ленты X.",
        "requirements": "X_BEARER_TOKEN и X_USER_ID; доступ зависит от разрешений API.",
        "docs_url": "https://developer.x.com/en/docs/x-api",
    },
    {
        "id": "reddit", "name": "Reddit", "symbol": "r/", "color": "#FF4500",
        "category": "Сообщества", "market": "Мировые", "priority": "core",
        "mode": "built_in", "roles": ["crosspost"],
        "description": (
            "Нативные текстовые или ссылочные посты в один subreddit, "
            "с журналом и защитой от дублей."
        ),
        "requirements": (
            "REDDIT_CLIENT_ID/SECRET, REDDIT_REFRESH_TOKEN, REDDIT_SUBREDDIT "
            "и уникальный User-Agent."
        ),
        "docs_url": "https://www.reddit.com/dev/api/",
    },
    {
        "id": "bluesky", "name": "Bluesky", "symbol": "BS", "color": "#1185FE",
        "category": "Мировые", "market": "Мировые", "priority": "P1",
        "mode": "built_in", "roles": ["crosspost"],
        "description": "Короткие посты через официальный AT Protocol API с app password.",
        "requirements": "BLUESKY_HANDLE и отдельный app password из настроек безопасности Bluesky.",
        "docs_url": "https://docs.bsky.app/docs/starter-templates/post",
    },
    {
        "id": "youtube", "name": "YouTube", "symbol": "YT", "color": "#FF0033",
        "category": "Видео", "market": "Мировые", "priority": "P1",
        "mode": "built_in", "roles": ["video", "crosspost"],
        "description": "Загрузка оригинальных MP4/Shorts через YouTube Data API с resumable upload и OAuth; live-доставка управляется общим реестром кросспостинга.",
        "requirements": "Google OAuth Web Client, YouTube Data API v3, youtube.upload и youtube.readonly scopes.",
        "docs_url": "https://developers.google.com/youtube/v3/docs/videos/insert",
    },
    {
        "id": "tiktok", "name": "TikTok", "symbol": "TT", "color": "#25F4EE",
        "category": "Видео", "market": "Мировые", "priority": "P1",
        "mode": "built_in", "roles": ["video"],
        "description": "Оригинальный вертикальный MP4 через Content Posting API. Каждое видео требует отдельного просмотра и подтверждения перед Direct Post.",
        "requirements": "TikTok App с Content Posting API, scopes user.info.basic + video.publish и разрешённым callback URL. До аудита API-публикации могут оставаться приватными.",
        "docs_url": "https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post",
    },
    {
        "id": "instagram", "name": "Instagram", "symbol": "IG", "color": "#E4405F",
        "category": "Мировые", "market": "Мировые", "priority": "P1",
        "mode": "built_in", "roles": ["visual", "video", "crosspost"],
        "description": (
            "Нативные фото и Reels через Instagram Login Content Publishing API; медиа передаётся "
            "по подписанным HTTPS URL, результат контейнера проверяется до публикации."
        ),
        "requirements": (
            "Professional (Business/Creator) аккаунт, Instagram Login scopes instagram_business_basic "
            "и instagram_business_content_publish, плюс публичный HTTPS-домен в TGFARM_PUBLIC_BASE_URL."
        ),
        "docs_url": "https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/",
    },
    {
        "id": "threads", "name": "Threads", "symbol": "@", "color": "#D8D8E0",
        "category": "Мировые", "market": "Мировые", "priority": "P2",
        "mode": "built_in", "roles": ["text", "crosspost"],
        "description": "Нативные текстовые публикации через Threads API с отдельным OAuth и автообновлением токена.",
        "requirements": "Meta App с Threads API, THREADS_APP_ID/SECRET и scopes threads_basic, threads_content_publish.",
        "docs_url": "https://developers.facebook.com/docs/threads/",
    },
    {
        "id": "facebook", "name": "Facebook Pages", "symbol": "f", "color": "#1877F2",
        "category": "Мировые", "market": "Мировые", "priority": "P2",
        "mode": "built_in", "roles": ["text", "visual", "video", "crosspost"],
        "description": "Нативные публикации в управляемую Facebook Page: текст, ссылки, фото и видео через Graph API v26.0.",
        "requirements": "Facebook Login с pages_show_list, pages_read_engagement, pages_manage_posts; Page task CREATE_CONTENT; Facebook App ID/Secret и ID выбранной страницы.",
        "docs_url": "https://developers.facebook.com/documentation/pages-api/posts",
    },
    {
        "id": "linkedin", "name": "LinkedIn", "symbol": "in", "color": "#0A66C2",
        "category": "Профессиональные", "market": "Мировые", "priority": "P2",
        "mode": "built_in", "roles": ["text", "professional", "crosspost"],
        "description": "Экспертные публикации и B2B-контент через пользовательский OAuth.",
        "requirements": "LinkedIn Developer App с Sign In using OpenID Connect и Share on LinkedIn, разрешение w_member_social.",
        "docs_url": "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api",
    },
    {
        "id": "pinterest", "name": "Pinterest", "symbol": "P", "color": "#E60023",
        "category": "Визуальные", "market": "Мировые", "priority": "P2",
        "mode": "built_in", "roles": ["visual", "search", "crosspost"],
        "description": "Нативные image Pins через Pinterest API v5 с OAuth и размещением в выбранной доске.",
        "requirements": "PINTEREST App OAuth с boards:read, pins:read, pins:write и ID доски, доступной подключённому профилю.",
        "docs_url": "https://developers.pinterest.com/docs/work-with-organic-content-and-users/create-boards-and-pins/",
    },
    {
        "id": "odnoklassniki", "name": "Одноклассники", "symbol": "OK", "color": "#EE8208",
        "category": "Российские", "market": "Россия и СНГ", "priority": "P1",
        "mode": "built_in", "roles": ["text", "visual", "video", "crosspost"],
        "description": "Групповые медиатопики через официальный OK REST API: текст, фото и видео, с подписью запросов и подтверждением ID темы.",
        "requirements": "OK_APPLICATION_KEY/SECRET, access token с LONG_ACCESS_TOKEN и разрешением GROUP_CONTENT; PHOTO_CONTENT и VIDEO_CONTENT нужны для соответствующих вложений; OK_GROUP_ID — ID принадлежащей тебе группы.",
        "docs_url": "https://apiok.ru/dev/methods/rest/mediatopic/mediatopic.post",
    },
    {
        "id": "rutube", "name": "RUTUBE", "symbol": "Ru", "color": "#00A0E3",
        "category": "Российские", "market": "Россия", "priority": "P1",
        "mode": "planned", "roles": ["video"],
        "description": "Российский видеохостинг для видео и клипов. Это отдельная загрузка, не зеркалирование текста.",
        "requirements": "До разработки проверить доступный официальный путь загрузки и условия для авторов.",
        "docs_url": "https://rutube.ru/",
    },
    {
        "id": "discord", "name": "Discord", "symbol": "DC", "color": "#5865F2",
        "category": "Сообщества", "market": "Мировые", "priority": "P2",
        "mode": "built_in", "roles": ["community", "crosspost"],
        "description": "Нативные текстовые сообщения в принадлежащие владельцу каналы через Discord Execute Webhook API.",
        "requirements": "DISCORD_WEBHOOK_URL: URL webhook из принадлежащего владельцу Discord-канала. Массовые mentions отключены.",
        "docs_url": "https://discord.com/developers/docs/resources/webhook",
    },
    {
        "id": "mastodon", "name": "Mastodon", "symbol": "M", "color": "#6364FF",
        "category": "Федеративные", "market": "Мировые", "priority": "P1",
        "mode": "built_in", "roles": ["crosspost"],
        "description": "Нативные текстовые посты через OAuth 2.0 + PKCE на выбранном Mastodon-сервере.",
        "requirements": "OAuth-подключение к HTTPS-инстансу с разрешениями read и write:statuses.",
        "docs_url": "https://docs.joinmastodon.org/methods/statuses/",
    },
    {
        "id": "whatsapp_channels", "name": "WhatsApp Channels", "symbol": "WA", "color": "#25D366",
        "category": "Мессенджеры", "market": "Мировые", "priority": "P3",
        "mode": "planned", "roles": ["community"],
        "description": "Каналы дают охват, но возможности официального автоматического постинга нужно подтвердить отдельно.",
        "requirements": "Не путать WhatsApp Business Messaging API с управлением Channels.",
        "docs_url": "https://developers.facebook.com/docs/whatsapp/",
    },
    {
        "id": "medium", "name": "Medium", "symbol": "Me", "color": "#E7E7E7",
        "category": "Публикации", "market": "Мировые", "priority": "P3",
        "mode": "planned", "roles": ["article", "search"],
        "description": "Полноценные статьи и поисковый трафик; перед включением проверить актуальную доступность API создания публикаций.",
        "requirements": "Аккаунт автора и подтверждённый рабочий API-путь либо ручной экспорт.",
        "docs_url": "https://github.com/Medium/medium-api-docs",
    },
    {
        "id": "vc_ru", "name": "vc.ru", "symbol": "vc", "color": "#7D5FFF",
        "category": "Российские", "market": "Россия", "priority": "P2",
        "mode": "planned", "roles": ["article", "search"],
        "description": (
            "Длинные статьи для предпринимательской и технологической аудитории. "
            "Вход сохраняется в отдельном профиле браузера; создание и отправка публикаций "
            "пока выполняются через редактор площадки."
        ),
        "requirements": (
            "Войти через кнопку браузера. Публичный API создания статей не подтверждён, "
            "поэтому подключение профиля не включает автоматическую публикацию."
        ),
        "docs_url": "https://vc.ru/",
    },
    {
        "id": "habr", "name": "Хабр", "symbol": "H", "color": "#65A3BE",
        "category": "Профессиональные", "market": "Россия и СНГ", "priority": "P2",
        "mode": "planned", "roles": ["article", "technical"],
        "description": "Технические статьи. Понадобится отдельный редакторский профиль и проверка технических фактов.",
        "requirements": "Проверить разрешённый способ публикации и ограничения для аккаунта.",
        "docs_url": "https://habr.com/",
    },
    {
        "id": "pikabu", "name": "Пикабу", "symbol": "П", "color": "#6B8E23",
        "category": "Сообщества", "market": "Россия", "priority": "P3",
        "mode": "planned", "roles": ["community", "visual"],
        "description": (
            "Нативные посты с заголовком, текстовыми и медиа-блоками, тегами и сообществами. "
            "Посты нужно адаптировать под формат Пикабу, а не отправлять как голую ссылку."
        ),
        "requirements": (
            "Войти через отдельный профиль браузера. Публичный API создания постов не подтверждён; "
            "для размещения используйте редактор Пикабу и соблюдайте его правила и лимиты."
        ),
        "docs_url": "https://pikabu.ru/",
    },
    {
        "id": "tenchat", "name": "TenChat", "symbol": "TC", "color": "#3578F6",
        "category": "Российские", "market": "Россия", "priority": "P3",
        "mode": "planned", "roles": ["professional"],
        "description": "Деловые заметки и кейсы с отдельной адаптацией под профессиональную аудиторию.",
        "requirements": "Проверить доступность официального API для публикации.",
        "docs_url": "https://tenchat.ru/",
    },
    {
        "id": "twitch", "name": "Twitch", "symbol": "TW", "color": "#9146FF",
        "category": "Видео", "market": "Мировые", "priority": "P3",
        "mode": "planned", "roles": ["video", "community"],
        "description": "Скорее для анонсов стримов и клипов, чем для обычных текстовых постов.",
        "requirements": "Twitch OAuth и отдельный сценарий управления трансляциями/клипами.",
        "docs_url": "https://dev.twitch.tv/docs/",
    },
    {
        "id": "tumblr", "name": "Tumblr", "symbol": "T", "color": "#35465C",
        "category": "Сообщества", "market": "Мировые", "priority": "P3",
        "mode": "built_in", "roles": ["text", "crosspost"],
        "description": "Нативные текстовые публикации NPF через Tumblr API v2 и OAuth 2.0.",
        "requirements": "Tumblr OAuth App, scopes basic + write + offline_access; можно выбрать основной блог или указать его идентификатор.",
        "docs_url": "https://www.tumblr.com/docs/en/api/v2",
    },
    {
        "id": "snapchat", "name": "Snapchat", "symbol": "SC", "color": "#FFFC00",
        "category": "Видео", "market": "Мировые", "priority": "P3",
        "mode": "planned", "roles": ["video", "visual"],
        "description": "Вертикальные видео и публичные профили; доступ для публикации зависит от продукта и одобрения.",
        "requirements": "Публичный профиль и подходящий доступ Snap Developer.",
        "docs_url": "https://developers.snap.com/",
    },
    {
        "id": "wechat", "name": "WeChat / Weixin", "symbol": "WX", "color": "#07C160",
        "category": "Азиатские", "market": "Китай", "priority": "P3",
        "mode": "planned", "roles": ["article", "community"],
        "description": "Огромная экосистема, но требует отдельной региональной стратегии и аккаунта.",
        "requirements": "Регистрация в экосистеме WeChat и права на публикацию в Official Accounts.",
        "docs_url": "https://developers.weixin.qq.com/doc/",
    },
    {
        "id": "douyin", "name": "Douyin", "symbol": "抖", "color": "#111111",
        "category": "Азиатские", "market": "Китай", "priority": "P2",
        "mode": "built_in", "roles": ["video"],
        "description": (
            "Официальный Douyin Open Platform: загрузка оригинального MP4 и создание видео. "
            "Каждая отправка требует отдельного просмотра и подтверждения владельца."
        ),
        "requirements": "Douyin Web App, OAuth scopes user_info + video.create и доступ к API создания видео.",
        "docs_url": "https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/create-video",
    },
    {
        "id": "rednote", "name": "RedNote / Xiaohongshu", "symbol": "红", "color": "#FF2442",
        "category": "Азиатские", "market": "Китай и Азия", "priority": "P3",
        "mode": "planned", "roles": ["visual", "community"],
        "description": "Визуальные заметки и рекомендации; интерфейс и контент нужны отдельно локализовать.",
        "requirements": "Проверить доступ к creator/open platform API и региональные требования.",
        "docs_url": "https://www.xiaohongshu.com/",
    },
)


# Per-platform connection UX. Values are never returned to the browser; only
# a has_value flag is exposed. Credential-backed adapters save into local .env.
_AUTH_FIELDS: dict[str, list[dict[str, Any]]] = {
    "telegram": [
        {"key": "TELEGRAM_BOT_TOKEN", "attribute": "telegram_bot_token", "label": "Bot token", "secret": True, "placeholder": "Токен из @BotFather", "help": "Токен бота, который имеет право публиковать в целевой канал."},
        {"key": "TELEGRAM_CHANNELS", "attribute": "telegram_channels", "label": "ID целевого канала", "secret": False, "placeholder": "-1001234567890", "help": "Один или несколько chat ID через запятую."},
    ],
    "vk": [
        {"key": "VK_ACCESS_TOKEN", "attribute": "vk_access_token", "label": "Access token", "secret": True, "placeholder": "VK access token", "help": "Токен с разрешениями, нужными для публикации в сообщество."},
        {"key": "VK_GROUP_ID", "attribute": "vk_group_id", "label": "ID сообщества (необязательно)", "secret": False, "placeholder": "Можно оставить пустым и выбрать после OAuth", "help": "После VKHost OAuth нажми «Найти мои сообщества»: TgFarm покажет группы с ролью администратора или редактора и повторно проверит выбор."},
    ],
    "max": [
        {"key": "MAX_ACCESS_TOKEN", "attribute": "max_access_token", "label": "Токен бота MAX", "secret": True, "placeholder": "Токен из кабинета разработчика MAX", "help": "Токен хранится локально. После сохранения нажми «Подключить поиск чатов»: потребуется публичный HTTPS webhook на порту 443."},
        {"key": "MAX_CHANNEL_ID", "attribute": "max_channel_id", "label": "ID канала/чата (необязательно)", "secret": False, "placeholder": "Выбери найденный чат после webhook", "help": "Не нужно вводить вручную. TgFarm получит chat_id после того, как ты добавишь бота в канал или групповой чат."},
        {"key": "MAX_CHANNEL_NAME", "attribute": "max_channel_name", "label": "Название места (служебное)", "secret": False, "placeholder": "", "help": "", "hidden": True, "fingerprint": False},
        {"key": "MAX_CHANNEL_IS_CHANNEL", "attribute": "max_channel_is_channel", "label": "Тип места (служебное)", "secret": False, "placeholder": "", "help": "", "hidden": True, "fingerprint": False},
        {"key": "MAX_CHANNEL_BLOCKED", "attribute": "max_channel_blocked", "label": "Блокировка места (служебное)", "secret": False, "placeholder": "", "help": "", "hidden": True, "fingerprint": False},
        {"key": "MAX_WEBHOOK_SECRET", "attribute": "max_webhook_secret", "label": "Webhook secret (служебное)", "secret": True, "placeholder": "", "help": "", "hidden": True, "fingerprint": False},
        {"key": "MAX_WEBHOOK_URL", "attribute": "max_webhook_url", "label": "Webhook URL (служебное)", "secret": False, "placeholder": "", "help": "", "hidden": True, "fingerprint": False},
        {"key": "MAX_DISCOVERED_CHATS", "attribute": "max_discovered_chats", "label": "Найденные чаты (служебное)", "secret": False, "placeholder": "", "help": "", "hidden": True, "fingerprint": False},
    ],
    "discord": [
        {"key": "DISCORD_WEBHOOK_URL", "attribute": "discord_webhook_url", "label": "Discord Webhook URL", "secret": True, "placeholder": "https://discord.com/api/webhooks/…", "help": "Создай webhook в принадлежащем тебе канале. Ссылка включает секрет; не публикуй её. Пинги @everyone и массовые mentions отключены."},
    ],
    "bluesky": [
        {"key": "BLUESKY_HANDLE", "attribute": "bluesky_handle", "label": "Handle", "secret": False, "placeholder": "name.bsky.social", "help": "Handle аккаунта Bluesky, можно с собственным доменом."},
        {"key": "BLUESKY_APP_PASSWORD", "attribute": "bluesky_app_password", "label": "App password", "secret": True, "placeholder": "xxxx-xxxx-xxxx-xxxx", "help": "Создай отдельный пароль приложения в Settings → Privacy and Security → App passwords. Не вводи основной пароль."},
    ],
    "mastodon": [
        {"key": "MASTODON_INSTANCE", "attribute": "mastodon_instance", "label": "Сервер Mastodon", "secret": False, "placeholder": "https://mastodon.social", "help": "Можно использовать собственный публичный HTTPS-инстанс."},
        {"key": "MASTODON_CLIENT_ID", "attribute": "mastodon_client_id", "label": "OAuth client ID", "secret": False, "placeholder": "Заполняется автоматически", "help": "Создаётся на выбранном Mastodon-сервере."},
        {"key": "MASTODON_CLIENT_SECRET", "attribute": "mastodon_client_secret", "label": "OAuth client secret", "secret": True, "placeholder": "Заполняется автоматически", "help": "Секрет локального OAuth-приложения."},
        {"key": "MASTODON_ACCESS_TOKEN", "attribute": "mastodon_access_token", "label": "OAuth access token", "secret": True, "placeholder": "Заполняется автоматически", "help": "Пользовательский токен с разрешением write:statuses."},
    ],
    "x": [
        {"key": "X_OAUTH_CLIENT_ID", "attribute": "x_oauth_client_id", "label": "OAuth Client ID", "secret": False, "placeholder": "Client ID из X Developer Portal", "help": "В X Developer Portal включи OAuth 2.0 и укажи callback URL панели."},
        {"key": "X_OAUTH_CLIENT_SECRET", "attribute": "x_oauth_client_secret", "label": "OAuth Client Secret", "secret": True, "placeholder": "Client Secret из X Developer Portal", "help": "Используй приложение типа Web App/Confidential client. Пароль от аккаунта X не нужен."},
        {"key": "X_ACCESS_TOKEN", "attribute": "x_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний OAuth-токен."},
        {"key": "X_REFRESH_TOKEN", "attribute": "x_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний токен обновления."},
        {"key": "X_TOKEN_EXPIRES_AT", "attribute": "x_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия токена."},
        {"key": "X_USER_ID", "attribute": "x_user_id", "label": "User ID", "secret": False, "hidden": True, "placeholder": "", "help": "ID подтверждённого аккаунта."},
    ],
    "youtube": [
        {"key": "YOUTUBE_CLIENT_ID", "attribute": "youtube_client_id", "label": "Google OAuth Client ID", "secret": False, "placeholder": "Client ID из Google Cloud", "help": "Создай OAuth Client типа Web application, включи YouTube Data API v3 и добавь callback URI панели."},
        {"key": "YOUTUBE_CLIENT_SECRET", "attribute": "youtube_client_secret", "label": "Google OAuth Client Secret", "secret": True, "placeholder": "Client Secret из Google Cloud", "help": "Секрет хранится в локальном .env и не показывается повторно."},
        {"key": "YOUTUBE_ACCESS_TOKEN", "attribute": "youtube_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний OAuth access token."},
        {"key": "YOUTUBE_REFRESH_TOKEN", "attribute": "youtube_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Долговременный токен обновления пользователя."},
        {"key": "YOUTUBE_TOKEN_EXPIRES_AT", "attribute": "youtube_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "YOUTUBE_CHANNEL_ID", "attribute": "youtube_channel_id", "label": "Channel ID", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "ID канала, подтверждённого через Google OAuth."},
        {"key": "YOUTUBE_CHANNEL_TITLE", "attribute": "youtube_channel_title", "label": "Channel title", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Название подтверждённого канала."},
    ],
    "tiktok": [
        {"key": "TIKTOK_CLIENT_KEY", "attribute": "tiktok_client_key", "label": "TikTok Client Key", "secret": False, "placeholder": "Client Key", "help": "Приложение должно иметь Content Posting API и scope video.publish."},
        {"key": "TIKTOK_CLIENT_SECRET", "attribute": "tiktok_client_secret", "label": "TikTok Client Secret", "secret": True, "placeholder": "Client Secret", "help": "Хранится только локально в .env."},
        {"key": "TIKTOK_ACCESS_TOKEN", "attribute": "tiktok_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний access token."},
        {"key": "TIKTOK_REFRESH_TOKEN", "attribute": "tiktok_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний refresh token."},
        {"key": "TIKTOK_TOKEN_EXPIRES_AT", "attribute": "tiktok_token_expires_at", "label": "Access expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "TIKTOK_REFRESH_TOKEN_EXPIRES_AT", "attribute": "tiktok_refresh_token_expires_at", "label": "Refresh expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия refresh token."},
        {"key": "TIKTOK_OPEN_ID", "attribute": "tiktok_open_id", "label": "Open ID", "secret": False, "hidden": True, "placeholder": "", "help": "ID подтверждённого TikTok аккаунта."},
        {"key": "TIKTOK_USERNAME", "attribute": "tiktok_username", "label": "Username", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "douyin": [
        {"key": "DOUYIN_CLIENT_KEY", "attribute": "douyin_client_key", "label": "Douyin Client Key", "secret": False, "placeholder": "Client Key из Open Platform", "help": "Создай Web App и запроси разрешения user_info и video.create."},
        {"key": "DOUYIN_CLIENT_SECRET", "attribute": "douyin_client_secret", "label": "Douyin Client Secret", "secret": True, "placeholder": "Client Secret", "help": "Секрет сохраняется только в локальном .env."},
        {"key": "DOUYIN_ACCESS_TOKEN", "attribute": "douyin_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний access token."},
        {"key": "DOUYIN_REFRESH_TOKEN", "attribute": "douyin_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний refresh token."},
        {"key": "DOUYIN_TOKEN_EXPIRES_AT", "attribute": "douyin_token_expires_at", "label": "Access expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "DOUYIN_REFRESH_TOKEN_EXPIRES_AT", "attribute": "douyin_refresh_token_expires_at", "label": "Refresh expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия refresh token."},
        {"key": "DOUYIN_OPEN_ID", "attribute": "douyin_open_id", "label": "Open ID", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "ID подключённого аккаунта."},
        {"key": "DOUYIN_USERNAME", "attribute": "douyin_username", "label": "Nickname", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "threads": [
        {"key": "THREADS_APP_ID", "attribute": "threads_app_id", "label": "Threads App ID", "secret": False, "placeholder": "Meta App ID", "help": "Идентификатор Meta-приложения с добавленным продуктом Threads API."},
        {"key": "THREADS_APP_SECRET", "attribute": "threads_app_secret", "label": "Threads App Secret", "secret": True, "placeholder": "Meta App Secret", "help": "Секрет приложения. Хранится локально и не показывается повторно."},
        {"key": "THREADS_ACCESS_TOKEN", "attribute": "threads_access_token", "label": "Access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний OAuth-токен."},
        {"key": "THREADS_TOKEN_EXPIRES_AT", "attribute": "threads_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия токена."},
        {"key": "THREADS_USER_ID", "attribute": "threads_user_id", "label": "User ID", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "ID подтверждённого аккаунта."},
        {"key": "THREADS_USERNAME", "attribute": "threads_username", "label": "Username", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "instagram": [
        {"key": "INSTAGRAM_APP_ID", "attribute": "instagram_app_id", "label": "Instagram App ID", "secret": False, "placeholder": "App ID из Meta Developer Portal", "help": "Используй Instagram API with Instagram Login для Professional (Business/Creator) аккаунта."},
        {"key": "INSTAGRAM_APP_SECRET", "attribute": "instagram_app_secret", "label": "Instagram App Secret", "secret": True, "placeholder": "App Secret из Meta Developer Portal", "help": "Секрет хранится локально и не показывается повторно."},
        {"key": "INSTAGRAM_ACCESS_TOKEN", "attribute": "instagram_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний долгоживущий токен."},
        {"key": "INSTAGRAM_TOKEN_EXPIRES_AT", "attribute": "instagram_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "INSTAGRAM_USER_ID", "attribute": "instagram_user_id", "label": "Instagram user ID", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "ID подтверждённого аккаунта."},
        {"key": "INSTAGRAM_USERNAME", "attribute": "instagram_username", "label": "Username", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "facebook": [
        {"key": "FACEBOOK_APP_ID", "attribute": "facebook_app_id", "label": "Facebook App ID", "secret": False, "placeholder": "App ID из Meta Developer Portal", "help": "Facebook Login должен быть включён в Meta App."},
        {"key": "FACEBOOK_APP_SECRET", "attribute": "facebook_app_secret", "label": "Facebook App Secret", "secret": True, "placeholder": "App Secret", "help": "Секрет приложения хранится локально и не показывается повторно."},
        {"key": "FACEBOOK_PAGE_ID", "attribute": "facebook_page_id", "label": "ID Facebook Page (необязательно)", "secret": False, "placeholder": "Можно оставить пустым: выберешь Page после OAuth", "help": "Оставь пустым: после входа TgFarm получит список управляемых Pages через OAuth. При ручном ID сервер всё равно проверит CREATE_CONTENT. Личные профили и группы не поддерживаются."},
        {"key": "FACEBOOK_PAGE_ACCESS_TOKEN", "attribute": "facebook_page_access_token", "label": "Page access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний токен страницы."},
        {"key": "FACEBOOK_PAGE_NAME", "attribute": "facebook_page_name", "label": "Page name", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Название подключённой страницы."},
        {"key": "FACEBOOK_USER_ACCESS_TOKEN", "attribute": "facebook_user_access_token", "label": "Long-lived user token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний токен для обновления Page token."},
        {"key": "FACEBOOK_USER_TOKEN_EXPIRES_AT", "attribute": "facebook_user_token_expires_at", "label": "User token expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Срок действия long-lived user token."},
    ],
    "odnoklassniki": [
        {"key": "OK_APPLICATION_KEY", "attribute": "ok_application_key", "label": "Публичный ключ приложения", "secret": False, "placeholder": "Application key", "help": "Публичный ключ приложения ОК из кабинета разработчика."},
        {"key": "OK_APPLICATION_SECRET", "attribute": "ok_application_secret", "label": "Секрет приложения", "secret": True, "placeholder": "Application secret key", "help": "Секрет используется только для подписи API-запросов и хранится локально."},
        {"key": "OK_ACCESS_TOKEN", "attribute": "ok_access_token", "label": "Long access token", "secret": True, "placeholder": "Долгоживущий access token", "help": "Запроси LONG_ACCESS_TOKEN и права GROUP_CONTENT у api-support@ok.ru. Пароль от аккаунта не нужен."},
        {"key": "OK_SESSION_SECRET_KEY", "attribute": "ok_session_secret_key", "label": "Session secret key", "secret": True, "placeholder": "Session secret key (если выдан)", "help": "Если не задан, подпись рассчитывается официальным способом MD5(access_token + application_secret_key)."},
        {"key": "OK_GROUP_ID", "attribute": "ok_group_id", "label": "ID группы", "secret": False, "placeholder": "12345678901234", "help": "Числовой ID группы, в которой приложение имеет GROUP_CONTENT."},
    ],
    "pinterest": [
        {"key": "PINTEREST_APP_ID", "attribute": "pinterest_app_id", "label": "Pinterest App ID", "secret": False, "placeholder": "App ID из Pinterest Developer Portal", "help": "Зарегистрируй приложение и добавь точный callback URL из панели."},
        {"key": "PINTEREST_APP_SECRET", "attribute": "pinterest_app_secret", "label": "Pinterest App Secret", "secret": True, "placeholder": "App Secret", "help": "Секрет хранится локально и не показывается повторно."},
        {"key": "PINTEREST_BOARD_ID", "attribute": "pinterest_board_id", "label": "ID доски", "secret": False, "placeholder": "ID существующей Pinterest-доски", "help": "Укажи ID доски, куда будут добавляться Pins. OAuth проверит доступ к этой доске."},
        {"key": "PINTEREST_ACCESS_TOKEN", "attribute": "pinterest_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний access token."},
        {"key": "PINTEREST_REFRESH_TOKEN", "attribute": "pinterest_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний rotating refresh token."},
        {"key": "PINTEREST_TOKEN_EXPIRES_AT", "attribute": "pinterest_token_expires_at", "label": "Access expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "PINTEREST_REFRESH_TOKEN_EXPIRES_AT", "attribute": "pinterest_refresh_token_expires_at", "label": "Refresh expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия refresh token."},
    ],
    "linkedin": [
        {"key": "LINKEDIN_CLIENT_ID", "attribute": "linkedin_client_id", "label": "LinkedIn Client ID", "secret": False, "placeholder": "Client ID из Developer Portal", "help": "Приложение должно иметь OpenID Connect и Share on LinkedIn."},
        {"key": "LINKEDIN_CLIENT_SECRET", "attribute": "linkedin_client_secret", "label": "LinkedIn Client Secret", "secret": True, "placeholder": "Client Secret из Developer Portal", "help": "Секрет хранится только в локальном .env."},
        {"key": "LINKEDIN_ACCESS_TOKEN", "attribute": "linkedin_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний OAuth-токен."},
        {"key": "LINKEDIN_REFRESH_TOKEN", "attribute": "linkedin_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний токен обновления, если LinkedIn его выдал."},
        {"key": "LINKEDIN_TOKEN_EXPIRES_AT", "attribute": "linkedin_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "LINKEDIN_REFRESH_TOKEN_EXPIRES_AT", "attribute": "linkedin_refresh_token_expires_at", "label": "Refresh expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия refresh token."},
        {"key": "LINKEDIN_MEMBER_ID", "attribute": "linkedin_member_id", "label": "Member ID", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "ID подтверждённого аккаунта."},
        {"key": "LINKEDIN_MEMBER_NAME", "attribute": "linkedin_member_name", "label": "Member name", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "youtube": [
        {"key": "YOUTUBE_CLIENT_ID", "attribute": "youtube_client_id", "label": "Google OAuth Client ID", "secret": False, "placeholder": "Client ID из Google Cloud", "help": "Создай OAuth Web application и зарегистрируй callback URL из панели."},
        {"key": "YOUTUBE_CLIENT_SECRET", "attribute": "youtube_client_secret", "label": "Google OAuth Client Secret", "secret": True, "placeholder": "Client Secret", "help": "Хранится локально в .env и не отображается повторно."},
        {"key": "YOUTUBE_REFRESH_TOKEN", "attribute": "youtube_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний долгоживущий OAuth токен."},
        {"key": "YOUTUBE_CHANNEL_ID", "attribute": "youtube_channel_id", "label": "Channel ID", "secret": False, "hidden": True, "placeholder": "", "help": "ID канала подтверждается через YouTube API."},
        {"key": "YOUTUBE_CHANNEL_TITLE", "attribute": "youtube_channel_title", "label": "Channel title", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого канала."},
    ],
    "tiktok": [
        {"key": "TIKTOK_CLIENT_KEY", "attribute": "tiktok_client_key", "label": "TikTok Client Key", "secret": False, "placeholder": "Client Key from TikTok Developer Portal", "help": "Приложение должно иметь Content Posting API и нужный scope."},
        {"key": "TIKTOK_CLIENT_SECRET", "attribute": "tiktok_client_secret", "label": "TikTok Client Secret", "secret": True, "placeholder": "Client Secret", "help": "Хранится только локально; не делись секретом в чате."},
        {"key": "TIKTOK_ACCESS_TOKEN", "attribute": "tiktok_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний access token."},
        {"key": "TIKTOK_REFRESH_TOKEN", "attribute": "tiktok_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний refresh token."},
        {"key": "TIKTOK_TOKEN_EXPIRES_AT", "attribute": "tiktok_token_expires_at", "label": "Access expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "TIKTOK_REFRESH_TOKEN_EXPIRES_AT", "attribute": "tiktok_refresh_token_expires_at", "label": "Refresh expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия refresh token."},
        {"key": "TIKTOK_OPEN_ID", "attribute": "tiktok_open_id", "label": "Open ID", "secret": False, "hidden": True, "placeholder": "", "help": "ID подтверждённого TikTok аккаунта."},
        {"key": "TIKTOK_USERNAME", "attribute": "tiktok_username", "label": "Username", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "tumblr": [
        {"key": "TUMBLR_CLIENT_ID", "attribute": "tumblr_client_id", "label": "Tumblr OAuth Client ID", "secret": False, "placeholder": "Consumer key из Tumblr OAuth Apps", "help": "Зарегистрируй приложение на tumblr.com/oauth/apps и укажи callback URL панели."},
        {"key": "TUMBLR_CLIENT_SECRET", "attribute": "tumblr_client_secret", "label": "Tumblr OAuth Client Secret", "secret": True, "placeholder": "Consumer secret", "help": "Секрет хранится только в локальном .env и не показывается повторно."},
        {"key": "TUMBLR_BLOG_IDENTIFIER", "attribute": "tumblr_blog_identifier", "label": "Blog identifier (необязательно)", "secret": False, "placeholder": "my-blog или my-blog.tumblr.com", "help": "Оставь пустым, чтобы после OAuth выбрать основной блог аккаунта."},
        {"key": "TUMBLR_ACCESS_TOKEN", "attribute": "tumblr_access_token", "label": "OAuth access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний OAuth access token."},
        {"key": "TUMBLR_REFRESH_TOKEN", "attribute": "tumblr_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний rotating refresh token."},
        {"key": "TUMBLR_TOKEN_EXPIRES_AT", "attribute": "tumblr_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия access token."},
        {"key": "TUMBLR_USERNAME", "attribute": "tumblr_username", "label": "Username", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя аккаунта, подтверждённого Tumblr OAuth."},
        {"key": "TUMBLR_BLOG_TITLE", "attribute": "tumblr_blog_title", "label": "Blog title", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Заголовок выбранного блога."},
        {"key": "TUMBLR_BLOG_URL", "attribute": "tumblr_blog_url", "label": "Blog URL", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Публичный URL выбранного блога."},
    ],
    "reddit": [
        {"key": "REDDIT_CLIENT_ID", "attribute": "reddit_client_id", "label": "Client ID", "secret": False, "placeholder": "Reddit app client ID", "help": "Создаётся в настройках Reddit developer app."},
        {"key": "REDDIT_CLIENT_SECRET", "attribute": "reddit_client_secret", "label": "Client secret", "secret": True, "placeholder": "Reddit app client secret", "help": "Секрет приложения, не пароль Reddit."},
        {"key": "REDDIT_REFRESH_TOKEN", "attribute": "reddit_refresh_token", "label": "OAuth refresh token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Выдаётся автоматически через Reddit OAuth и хранится локально."},
        {"key": "REDDIT_SUBREDDIT", "attribute": "reddit_subreddit", "label": "Fallback-сообщество (необязательно)", "secret": False, "placeholder": "Можно оставить пустым", "help": "Используется только как запасная настройка. В автономном режиме TgFarm подбирает subreddit отдельно под каждую тему и читает его актуальные правила."},
        {"key": "REDDIT_USER_AGENT", "attribute": "reddit_user_agent", "label": "User-Agent", "secret": False, "placeholder": "python:tgfarm:1.0 (by /u/your_name)", "help": "Уникальная описательная строка. Не оставляй шаблонное имя."},
    ],
}
_AUTH_URLS: dict[str, str] = {
    "telegram": "https://web.telegram.org/",
    "dzen": "https://dzen.ru/profile/editor/",
    "vk": "https://vk.com/",
    "vk_clips": "https://vk.com/clips",
    "max": "https://web.max.ru/",
    "x": "https://x.com/",
    "reddit": "https://www.reddit.com/",
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
    "bluesky": "https://bsky.app/",
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

def _auth_state_path() -> Path:
    from tgfarm.core.settings import DATA_DIR
    return DATA_DIR / "platform_auth_checks.json"


def _read_auth_checks() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(_auth_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _credential_fingerprint(platform_id: str) -> str:
    settings = get_settings()
    fields = _AUTH_FIELDS.get(platform_id, [])
    material = "\x1f".join(
        str(getattr(settings, field["attribute"], "") or "")
        for field in fields if field.get("fingerprint", True)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def record_auth_check(
    platform_id: str, authorized: bool | None, *, account: str = "", detail: str = ""
) -> dict[str, Any]:
    """Persist only a redacted connection result, never credentials or tokens."""
    platform_id = str(platform_id).lower()
    checks = _read_auth_checks()
    checks[platform_id] = {
        "authorized": authorized,
        "account": str(account or "")[:160],
        "detail": str(detail or "")[:400],
        "checked_at": datetime.now(UTC).isoformat(),
        "fingerprint": _credential_fingerprint(platform_id),
    }
    path = _auth_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(checks, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return _auth_descriptor(platform_id)


def _auth_descriptor(platform_id: str) -> dict[str, Any]:
    platform_id = str(platform_id).lower()
    settings = get_settings()
    fields = []
    for spec in _AUTH_FIELDS.get(platform_id, []):
        if spec.get("hidden"):
            continue
        fields.append({
            "key": spec["key"],
            "label": spec["label"],
            "secret": bool(spec["secret"]),
            "has_value": bool(str(getattr(settings, spec["attribute"], "") or "").strip()),
            "placeholder": spec["placeholder"],
            "help": spec["help"],
        })
    checks = _read_auth_checks()
    check = checks.get(platform_id) or {}
    valid_check = check.get("fingerprint") == _credential_fingerprint(platform_id)
    supported = bool(_BY_ID.get(platform_id, {}).get("mode") == "built_in")
    configured, _detail = _configured_state(platform_id) if supported else (False, "")
    if not supported:
        status = "not_integrated"
        status_label = "Вход на сайт отдельно, интеграции пока нет"
    elif valid_check and check.get("authorized") is True:
        status = "authorized"
        status_label = "Доступ подтверждён"
    elif valid_check and check.get("authorized") is False:
        status = "failed"
        status_label = "Проверка доступа не пройдена"
    elif valid_check and check.get("authorized") is None:
        status = "unverified"
        status_label = "Не удалось проверить сеть"
    elif configured:
        status = "unverified"
        status_label = "Данные сохранены, нужна проверка"
    else:
        status = "needs_setup"
        status_label = "Не настроено"
    if platform_id == "dzen":
        method = "browser"
    elif platform_id == "vk":
        method = "vkhost"
    elif platform_id in {"vc_ru", "pikabu"}:
        method = "browser"
    elif platform_id in {"mastodon", "x", "threads", "linkedin", "youtube", "tiktok", "douyin", "instagram", "reddit", "pinterest", "facebook", "tumblr"}:
        method = "oauth"
    elif fields:
        method = "credentials"
    else:
        method = "external"
    try:
        from tgfarm.browser_auth import browser_session_status
        browser_session = browser_session_status(platform_id) if platform_id in _AUTH_URLS else {
            "available": False, "profile_saved": False, "verified": False, "state": "not_available"
        }
    except Exception:  # noqa: BLE001 - auth metadata must never break the platform dashboard
        browser_session = {
            "available": platform_id in _AUTH_URLS,
            "profile_saved": False,
            "verified": False,
            "state": "unknown",
        }
    return {
        "method": method,
        "browser_session": browser_session,
        "status": status,
        "status_label": status_label,
        "configured": configured,
        "authorized": check.get("authorized") if valid_check else None,
        "account": check.get("account", "") if valid_check else "",
        "detail": check.get("detail", "") if valid_check else "",
        "checked_at": check.get("checked_at") if valid_check else None,
        "fields": fields,
        "connect_url": _AUTH_URLS.get(platform_id, ""),
        "instance_url": str(getattr(settings, "mastodon_instance", "") or "") if platform_id == "mastodon" else "",
        "integration_ready": supported,
        "note": (
            "Вход выполняется в отдельном окне браузера и сохраняется в профиле Дзена."
            if platform_id == "dzen" else
            "MAX использует официальный Bot API. Укажи TGFARM_PUBLIC_BASE_URL на публичный HTTPS-домен без порта/пути, нажми «Подключить поиск чатов», добавь бота в канал или групповой чат и выбери пришедший chat_id. Перед сохранением TgFarm проверяет администраторское право write."
            if platform_id == "max" else
            "VKHost откроется в отдельном окне. После подтверждения VK вернёт токен, TgFarm проверит его и сохранит локально. ID сообщества задаётся отдельно."
            if platform_id == "vk" else
            "Instagram Login откроет официальное окно Meta. Нужен профессиональный аккаунт Business или Creator, Instagram App ID/Secret и разрешения instagram_business_basic + instagram_business_content_publish."
            if platform_id == "instagram" else
            "Reddit OAuth откроет официальное окно согласия. Нужен Web App Client ID/Secret и redirect URL из панели; постоянный refresh token будет сохранён автоматически. После подключения укажи subreddit."
            if platform_id == "reddit" else
            "Вход в vc.ru сохраняется в отдельном профиле браузера. Профиль не даёт API-доступа: публикацию нужно выполнять через редактор vc.ru."
            if platform_id == "vc_ru" else
            "Вход в Пикабу сохраняется в отдельном профиле браузера. Профиль не даёт API-доступа: публикацию нужно выполнять через редактор Пикабу с нативным оформлением поста."
            if platform_id == "pikabu" else
            "Можно войти в сайт в отдельном локальном профиле браузера. Эта сессия не заменяет OAuth/API-разрешения и не включает публикацию без рабочего адаптера."
            if not supported else
            "Сайт откроется отдельно, но сам по себе вход на сайт не подключает API к TgFarm."
            if not supported else
            "Для YouTube настрой OAuth Web Client в Google Cloud, включи YouTube Data API v3 и добавь callback URI панели. Загрузка по умолчанию остаётся Private; для публикаций нужна квота Google и доступ к youtube.upload."
            if platform_id == "youtube" else
            "Для TikTok требуется одобренный Content Posting API и video.publish. Перед каждой прямой публикацией TgFarm показывает актуальные privacy options и ждёт отдельного подтверждения оператора."
            if platform_id == "tiktok" else
            ("Для X, Threads, LinkedIn, Instagram или Reddit сохрани Client/App ID и Secret, затем нажми «Подключить через OAuth». Callback URL должен быть разрешён в кабинете разработчика; проверка API не заменяет тест публикации."
            if platform_id in {"x", "threads", "linkedin", "youtube", "tiktok", "instagram", "reddit"} else "После сохранения ключей нажми «Проверить доступ». Проверка API не заменяет тест публикации.")
        ),
    }


def save_platform_credentials(platform_id: str, values: dict[str, str]) -> dict[str, Any]:
    """Save allowlisted credentials to the local .env without returning secrets."""
    platform_id = str(platform_id).lower()
    allowed = {field["key"]: field for field in _AUTH_FIELDS.get(platform_id, [])}
    if not allowed:
        raise ValueError("Для этой площадки нет формы токенов: используй её способ входа.")
    updates: dict[str, str] = {}
    for key, raw_value in values.items():
        if key not in allowed:
            raise ValueError(f"Поле {key!r} не разрешено для этой площадки.")
        if not isinstance(raw_value, str):
            raise ValueError("Значения ключей должны быть текстом.")
        value = raw_value.strip()
        if not value:
            continue  # Empty fields mean "leave the saved value unchanged".
        if len(value) > 4096 or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError(f"Поле {key} имеет недопустимый формат или слишком длинное значение.")
        updates[key] = value
    if not updates:
        raise ValueError("Введи хотя бы одно новое значение. Пустые поля не затирают сохранённые ключи.")

    from tgfarm.core.settings import ROOT, reset_settings_cache
    env_path = ROOT / ".env"
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    def dotenv_quote(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    found: set[str] = set()
    output: list[str] = []
    for line in existing:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else ""
        if key in updates:
            if key not in found:
                output.append(f"{key}={dotenv_quote(updates[key])}")
                found.add(key)
            # Drop duplicate definitions so an old secret cannot override the
            # new value later in the same .env file.
        else:
            output.append(line)
    missing = {key: value for key, value in updates.items() if key not in found}
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.extend(f"{key}={dotenv_quote(value)}" for key, value in missing.items())
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tgfarm-env-", dir=str(env_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, env_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    reset_settings_cache()
    checks = _read_auth_checks()
    # Rotating hidden OAuth tokens does not change the authorized app/account.
    # Visible credentials and identity fields still invalidate the previous check.
    if any(allowed[key].get("fingerprint", True) for key in updates):
        checks.pop(platform_id, None)
    path = _auth_state_path()
    try:
        path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return _auth_descriptor(platform_id)


_BY_ID = {item["id"]: item for item in CATALOG}
_SUPPORTED_IDS = {item["id"] for item in CATALOG if item["mode"] == "built_in"}
_DEFAULT_PLANNED = [
    "reddit", "tiktok", "vk_clips", "threads",
    "rutube",
]


def _state_path() -> Path:
    return config_dir() / STATE_FILENAME


def _default_state() -> dict[str, Any]:
    cfg = get_config()
    configured_crossposts = set(
        str(item).lower()
        for item in (cfg.schedule.crosspost.get("platforms") or ["vk", "x"])
    )
    enabled: dict[str, bool] = {}
    for platform in CATALOG:
        if platform["mode"] != "built_in":
            continue
        platform_id = platform["id"]
        if platform_id == "telegram":
            enabled[platform_id] = True
        elif platform_id in {"dzen", "vk"}:
            enabled[platform_id] = (
                platform_id in configured_crossposts
                or any(p.enabled and p.platform == platform_id for p in cfg.personas)
            )
        else:
            enabled[platform_id] = platform_id in configured_crossposts
    return {"enabled": enabled, "planned": list(_DEFAULT_PLANNED)}


def _read_state() -> dict[str, Any]:
    defaults = _default_state()
    path = _state_path()
    if not path.is_file():
        return defaults
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Не удалось прочитать {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} должен содержать YAML-объект.")
    overrides = raw.get("enabled") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    for platform_id in _SUPPORTED_IDS:
        if platform_id in overrides:
            defaults["enabled"][platform_id] = bool(overrides[platform_id])
    planned = raw.get("planned")
    if isinstance(planned, list):
        defaults["planned"] = list(dict.fromkeys(
            value for value in planned
            if isinstance(value, str) and value in _BY_ID and _BY_ID[value]["mode"] == "planned"
        ))
    return defaults


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "enabled": {
            platform_id: bool(state["enabled"].get(platform_id, False))
            for platform_id in sorted(_SUPPORTED_IDS)
        },
        "planned": [
            platform_id for platform_id in dict.fromkeys(state.get("planned", []))
            if platform_id in _BY_ID and _BY_ID[platform_id]["mode"] == "planned"
        ],
    }
    fd, temp_name = tempfile.mkstemp(prefix=".platforms-", suffix=".yaml.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(normalized, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _configured_state(platform_id: str) -> tuple[bool, str]:
    settings = get_settings()
    if platform_id == "telegram":
        if settings.telegram_bot_token:
            return True, "Токен бота задан; доступность канала отдельно зависит от его прав."
        return False, "Нужно задать TELEGRAM_BOT_TOKEN и выбрать целевой канал."
    if platform_id == "vk":
        if settings.vk_access_token and settings.vk_group_id:
            return True, "Данные VK заданы; реальную публикацию нужно проверить smoke-тестом."
        return False, "Нужны VK_ACCESS_TOKEN и VK_GROUP_ID."
    if platform_id == "youtube":
        if (
            settings.youtube_client_id
            and settings.youtube_client_secret
            and settings.youtube_refresh_token
            and settings.youtube_channel_id
        ):
            return True, (
                "YouTube OAuth подтверждён для канала "
                + (settings.youtube_channel_title or settings.youtube_channel_id)
                + "; перед включением проверь приватность и тестовую загрузку."
            )
        if settings.youtube_client_id and settings.youtube_client_secret:
            return False, "OAuth-приложение настроено; нажми «Подключить через OAuth» и подтверди канал."
        return False, "Нужны YOUTUBE_CLIENT_ID и YOUTUBE_CLIENT_SECRET из Google Cloud."
    if platform_id == "tiktok":
        if (
            settings.tiktok_client_key
            and settings.tiktok_client_secret
            and settings.tiktok_refresh_token
            and settings.tiktok_open_id
        ):
            return True, "TikTok OAuth сохранён; перед каждой публикацией будут заново проверяться настройки автора и приватность."
        if settings.tiktok_client_key and settings.tiktok_client_secret:
            return False, "TikTok App настроено; подключи аккаунт через OAuth и проверь scope video.publish."
        return False, "Нужны TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET из Developer Portal."
    if platform_id == "max":
        if settings.max_access_token and settings.max_channel_id and not bool(getattr(settings, "max_channel_blocked", False)):
            return True, "MAX Bot API и выбранный чат/канал настроены; право write проверяется при доступе и отправке."
        if settings.max_channel_blocked:
            return False, "MAX изменил права бота или удалил его из чата. Повторно выбери адресат после проверки write."
        return False, "Нужен MAX_ACCESS_TOKEN и выбранный через webhook чат/канал. Для webhook настрой TGFARM_PUBLIC_BASE_URL."
    if platform_id == "discord":
        from urllib.parse import urlsplit
        webhook = str(settings.discord_webhook_url or "").strip()
        parsed = urlsplit(webhook)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "discord.com"
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/api/webhooks/\d{17,22}/[A-Za-z0-9._-]{16,}/?", parsed.path)
        )
        return (
            True, "Discord webhook URL задан; read-only проверка подтвердит канал."
        ) if bool(valid) else (
            False, "Вставь полный Discord Webhook URL из принадлежащего тебе канала."
        )
    if platform_id == "bluesky":
        if settings.bluesky_handle and settings.bluesky_app_password:
            return True, "Handle и app password заданы; проверь сессию и write-доступ."
        return False, "Нужны BLUESKY_HANDLE и BLUESKY_APP_PASSWORD."
    if platform_id == "threads":
        if settings.threads_app_id and settings.threads_app_secret and settings.threads_access_token and settings.threads_user_id:
            return True, "Threads OAuth подключён; проверка профиля выполняется через API."
        if settings.threads_app_id and settings.threads_app_secret:
            return False, "Meta App настроено; подключи Threads-аккаунт через OAuth."
        return False, "Нужны Threads App ID/Secret и подключение аккаунта через OAuth."
    if platform_id == "mastodon":
        if settings.mastodon_instance and settings.mastodon_access_token:
            return True, "Mastodon OAuth-токен сохранён; повторно проверь срок доступа и scope."
        return False, "Нажми «Подключить через OAuth», выбери сервер и подтверди доступ к профилю и публикациям."
    if platform_id == "x":
        if (settings.x_access_token or settings.x_refresh_token) and settings.x_user_id:
            return True, "X OAuth-подключение найдено; проверь доступ к профилю и право tweet.write."
        return False, "Сначала настрой OAuth-приложение X и подключи аккаунт через официальное окно авторизации."
    if platform_id == "facebook":
        if (
            settings.facebook_app_id
            and settings.facebook_app_secret
            and settings.facebook_page_id
            and settings.facebook_page_access_token
        ):
            return True, "Facebook Page OAuth connected; read-only check verifies the selected Page and CREATE_CONTENT permission."
        if settings.facebook_app_id and settings.facebook_app_secret and settings.facebook_page_id:
            return False, "Facebook App and Page ID saved; connect the Page through Facebook Login OAuth."
        return False, "Нужны Facebook App ID/Secret и ID страницы, затем подключи управляемую Page через OAuth."
    if platform_id == "odnoklassniki":
        if (
            settings.ok_application_key
            and settings.ok_application_secret
            and settings.ok_access_token
            and settings.ok_group_id
        ):
            return True, "OK API credentials and group are set; the read-only check will verify current-user, group and GROUP_CONTENT access."
        return False, "Нужны OK_APPLICATION_KEY/SECRET, OK_ACCESS_TOKEN и OK_GROUP_ID; запроси LONG_ACCESS_TOKEN и GROUP_CONTENT."
    if platform_id == "pinterest":
        if (
            settings.pinterest_app_id
            and settings.pinterest_app_secret
            and settings.pinterest_access_token
            and settings.pinterest_refresh_token
            and settings.pinterest_board_id
        ):
            return True, "Pinterest OAuth подключён; токен обновляется автоматически, выбранная доска проверяется перед публикацией."
        if settings.pinterest_app_id and settings.pinterest_app_secret and settings.pinterest_board_id:
            return False, "Pinterest App и доска сохранены; подключи аккаунт через OAuth."
        return False, "Нужны Pinterest App ID/Secret и ID доски, затем подключи профиль через OAuth."
    if platform_id == "instagram":
        if settings.instagram_access_token and settings.instagram_user_id and settings.public_base_url:
            return True, "Instagram OAuth сохранён; нужен публичный HTTPS-домен для безопасной передачи фото и Reels в Meta."
        if settings.instagram_access_token and settings.instagram_user_id:
            return False, "Instagram подключён, но нужен публичный HTTPS-домен в TGFARM_PUBLIC_BASE_URL для передачи медиа."
        if settings.instagram_app_id and settings.instagram_app_secret:
            return False, "Instagram App настроено; подключи профессиональный аккаунт через OAuth."
        return False, "Нужны Instagram App ID/Secret и подключение Business/Creator аккаунта через OAuth."
    if platform_id == "linkedin":
        if settings.linkedin_access_token and settings.linkedin_member_id:
            return True, "LinkedIn OAuth сохранён; проверка профиля выполняется через OpenID Connect."
        if settings.linkedin_client_id and settings.linkedin_client_secret:
            return False, "LinkedIn App настроено; подключи аккаунт через OAuth."
        return False, "Нужны LinkedIn Client ID/Secret и подключение аккаунта через OAuth."
    if platform_id == "youtube":
        if settings.youtube_refresh_token and settings.youtube_client_id and settings.youtube_client_secret:
            return True, "Google OAuth-токен и YouTube канал сохранены; проверь права youtube.upload."
        if settings.youtube_client_id and settings.youtube_client_secret:
            return False, "Google OAuth App настроено; подключи канал YouTube через OAuth."
        return False, "Нужны Google OAuth Client ID/Secret и подключение канала."
    if platform_id == "tiktok":
        if settings.tiktok_refresh_token and settings.tiktok_open_id and settings.tiktok_client_key and settings.tiktok_client_secret:
            return True, "TikTok OAuth-токен сохранён; публикация запускается отдельным подтверждением для каждого видео."
        if settings.tiktok_client_key and settings.tiktok_client_secret:
            return False, "TikTok App настроено; подключи аккаунт через OAuth и проверь scope video.publish."
        return False, "Нужны TikTok Client Key/Secret и подключение аккаунта через OAuth."
    if platform_id == "douyin":
        if settings.douyin_client_key and settings.douyin_client_secret and settings.douyin_refresh_token and settings.douyin_open_id:
            return True, "Douyin OAuth подключён; каждое создание видео требует отдельного подтверждения и проходит модерацию."
        if settings.douyin_client_key and settings.douyin_client_secret:
            return False, "Douyin App настроено; подключи аккаунт через OAuth с разрешениями user_info и video.create."
        return False, "Нужны Douyin Client Key/Secret и подключение через Open Platform OAuth."
    if platform_id == "reddit":
        user_agent = settings.reddit_user_agent.strip()
        configured = all((
            settings.reddit_client_id,
            settings.reddit_client_secret,
            settings.reddit_refresh_token,
            user_agent,
        )) and "/u/YOUR_REDDIT_USERNAME" not in user_agent
        if configured:
            target = (
                f"Фиксированный fallback: r/{settings.reddit_subreddit.strip()}. "
                if settings.reddit_subreddit.strip()
                else "Фиксированный subreddit не задан: TgFarm будет выбирать сообщество под каждый пост. "
            )
            return True, (
                target
                + "Перед отправкой проверяются актуальные правила, ограничения, формат и тематическое совпадение."
            )
        return False, (
            "Нужны Reddit OAuth client ID/secret, refresh token и свой REDDIT_USER_AGENT. "
            "REDDIT_SUBREDDIT теперь необязателен: цель выбирает редакторский планировщик."
        )
    if platform_id == "tumblr":
        if (
            settings.tumblr_client_id and settings.tumblr_client_secret
            and settings.tumblr_access_token and settings.tumblr_refresh_token
            and settings.tumblr_blog_identifier
        ):
            return True, (
                "Tumblr OAuth подключён к блогу "
                + (settings.tumblr_blog_title or settings.tumblr_blog_identifier)
                + "; публикация отправляет нативный NPF-текст."
            )
        if settings.tumblr_client_id and settings.tumblr_client_secret:
            return False, "Tumblr App настроено; подключи аккаунт через OAuth и выбери блог."
        return False, "Нужны Tumblr OAuth Client ID/Secret и подключение через OAuth."
    if platform_id == "dzen":
        try:
            profile = settings.dzen_profile_path()
            ready = profile.is_dir() and next(profile.iterdir(), None) is not None
        except OSError:
            ready = False
        if ready:
            return True, "Профиль браузера найден; авторизацию проверь в разделе диагностики."
        return False, "Нужен авторизованный профиль браузера. Нажми «Дзен: войти» после запуска фермы."
    return False, "Интеграция публикации ещё не реализована."


def get_platform_catalog() -> dict[str, Any]:
    """Return platform metadata and local readiness without ever exposing secrets."""
    state = _read_state()
    cfg = get_config()
    platforms: list[dict[str, Any]] = []
    for item in CATALOG:
        row = dict(item)
        platform_id = row["id"]
        supported = row["mode"] == "built_in"
        row["supported"] = supported
        if platform_id in MANUAL_ONLY_AUTOMATION:
            row["automation_mode"] = "manual"
            row["automation_note"] = MANUAL_ONLY_AUTOMATION[platform_id]
        elif platform_id in AUTOMATIC_CROSSPOST_IDS:
            row["automation_mode"] = "automatic"
            row["automation_note"] = "Может участвовать в автоматическом распределении при включённой площадке и подтверждённом подключении."
        elif "primary" in row.get("roles", []) and supported:
            row["automation_mode"] = "primary"
            row["automation_note"] = "Основная очередь площадки; расписание рассчитывается по её каналу."
        elif supported:
            row["automation_mode"] = "manual"
            row["automation_note"] = "Автоматическая доставка для этого формата не включена."
        else:
            row["automation_mode"] = "unavailable"
            row["automation_note"] = "Нет рабочего автоматического адаптера."
        row["enabled"] = bool(state["enabled"].get(platform_id, False)) if supported else False
        row["planned"] = platform_id in state["planned"] if not supported else False
        row["configured"], row["connection_detail"] = (
            _configured_state(platform_id) if supported
            else (False, "Сначала потребуется разработать и проверить адаптер.")
        )
        if not supported:
            row["status"] = "planned" if row["planned"] else "not_implemented"
            row["status_label"] = "В плане интеграций" if row["planned"] else "Интеграция не добавлена"
        elif not row["enabled"]:
            row["status"] = "paused"
            row["status_label"] = "Выключена"
        elif not row["configured"]:
            row["status"] = "needs_setup"
            row["status_label"] = "Нужна настройка"
        else:
            row["status"] = "configured"
            row["status_label"] = "Настройки найдены"
        row["channel_count"] = sum(
            1 for persona in cfg.personas if persona.platform == platform_id and persona.enabled
        )
        row["auth"] = _auth_descriptor(platform_id)
        # Editorial guidance is independent from publisher/API availability.
        from tgfarm.platform_editorial import get_editorial_profile

        row["editorial"] = get_editorial_profile(platform_id)
        row["roles_label"] = ", ".join({
            "primary": "Основная публикация",
            "crosspost": "Кросспостинг",
            "video": "Видео",
            "visual": "Визуальный контент",
            "text": "Текст",
            "professional": "Профессиональный контент",
            "search": "Поисковый трафик",
            "community": "Сообщества",
            "article": "Статьи",
            "technical": "Технические статьи",
        }.get(role, role) for role in row["roles"])
        platforms.append(row)

    enabled = [p for p in platforms if p["supported"] and p["enabled"]]
    configured = [p for p in enabled if p["configured"]]
    planned = [p for p in platforms if p["planned"]]
    return {
        "platforms": platforms,
        "summary": {
            "total": len(platforms),
            "supported": sum(1 for p in platforms if p["supported"]),
            "enabled": len(enabled),
            "configured": len(configured),
            "needs_setup": sum(1 for p in enabled if not p["configured"]),
            "planned": len(planned),
            "market_coverage": len({p["market"] for p in enabled}),
        },
        "note": (
            "Наличие ключей не является проверкой авторизации. Перед live-запуском проверь OAuth, "
            "права приложения и правила конкретного сообщества одним контролируемым постом."
        ),
    }


def is_platform_enabled(platform_id: str) -> bool:
    """Fail closed for unknown or not-yet-implemented publishers."""
    item = _BY_ID.get(str(platform_id).lower())
    if item is None or item["mode"] != "built_in":
        return False
    return bool(_read_state()["enabled"].get(item["id"], False))


MANUAL_ONLY_AUTOMATION: dict[str, str] = {
    "tiktok": "Каждая публикация требует отдельного выбора настроек и явного согласия автора.",
    "douyin": "Каждое создание видео требует отдельного просмотра ролика и явного подтверждения владельца.",
}

AUTOMATIC_CROSSPOST_IDS = frozenset({
    "vk", "max", "x", "reddit", "bluesky", "youtube", "threads", "mastodon",
    "instagram", "linkedin", "discord", "pinterest", "odnoklassniki", "facebook", "tumblr",
})


def enabled_crosspost_platforms() -> list[str]:
    """Return only enabled destinations with an unattended publisher.

    Manual-only destinations remain visible and can keep their OAuth connection,
    but a green toggle never silently turns a consent-gated workflow into a bot.
    """
    return [
        item["id"]
        for item in CATALOG
        if item["id"] in AUTOMATIC_CROSSPOST_IDS
        and item["mode"] == "built_in"
        and "crosspost" in item["roles"]
        and is_platform_enabled(item["id"])
    ]


def set_platform_enabled(platform_id: str, enabled: bool) -> dict[str, Any]:
    """Persist a supported platform switch; planned networks cannot be activated yet."""
    platform_id = str(platform_id).lower()
    item = _BY_ID.get(platform_id)
    if item is None:
        raise ValueError("Неизвестная площадка.")
    if item["mode"] != "built_in":
        raise ValueError("Для этой площадки ещё нет рабочего адаптера. Добавь её в план интеграций.")
    with _STATE_LOCK:
        state = _read_state()
        state["enabled"][platform_id] = bool(enabled)
        _write_state(state)
    return next(
        p for p in get_platform_catalog()["platforms"]
        if p["id"] == platform_id
    )


def set_platform_planned(platform_id: str, planned: bool) -> dict[str, Any]:
    """Add/remove an unsupported platform from the owner's integration priorities."""
    platform_id = str(platform_id).lower()
    item = _BY_ID.get(platform_id)
    if item is None:
        raise ValueError("Неизвестная площадка.")
    if item["mode"] == "built_in":
        raise ValueError("Рабочие площадки включаются переключателем «Публикация», а не списком планов.")
    with _STATE_LOCK:
        state = _read_state()
        selected = list(state["planned"])
        if planned and platform_id not in selected:
            selected.append(platform_id)
        elif not planned:
            selected = [value for value in selected if value != platform_id]
        state["planned"] = selected
        _write_state(state)
    return next(
        p for p in get_platform_catalog()["platforms"]
        if p["id"] == platform_id
    )


__all__ = [
    "CATALOG",
    "enabled_crosspost_platforms",
    "get_platform_catalog",
    "is_platform_enabled",
    "set_platform_enabled",
    "set_platform_planned",
]

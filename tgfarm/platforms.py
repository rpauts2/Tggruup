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
        "requirements": "VK_ACCESS_TOKEN и VK_GROUP_ID.",
        "docs_url": "https://dev.vk.com/",
    },
    {
        "id": "dzen", "name": "Дзен", "symbol": "Д", "color": "#F44336",
        "category": "Российские", "market": "Россия", "priority": "core",
        "mode": "built_in", "roles": ["primary"],
        "description": "Отдельные статьи через браузерный редактор и сохранённый профиль.",
        "requirements": "Вход в профиль браузера Дзена; это не публичный API-паблишер.",
        "docs_url": "https://dzen.ru/",
    },
    {
        "id": "max", "name": "MAX", "symbol": "M", "color": "#7D5CFF",
        "category": "Мессенджеры", "market": "Россия", "priority": "core",
        "mode": "built_in", "roles": ["crosspost"],
        "description": "Кросспост через API бота MAX.",
        "requirements": "MAX_ACCESS_TOKEN и MAX_CHANNEL_ID.",
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
        "mode": "planned", "roles": ["video"],
        "description": "Видео, Shorts, заголовки, описания и обложки. Нужен отдельный видеопайплайн.",
        "requirements": "Google OAuth, YouTube Data API и квота загрузок.",
        "docs_url": "https://developers.google.com/youtube/v3/docs/videos/insert",
    },
    {
        "id": "tiktok", "name": "TikTok", "symbol": "TT", "color": "#25F4EE",
        "category": "Видео", "market": "Мировые", "priority": "P1",
        "mode": "planned", "roles": ["video"],
        "description": "Короткие вертикальные ролики. Потребуется подготовка видео, субтитров и отдельный сценарий публикации.",
        "requirements": "OAuth и допуск Content Posting API; отдельные настройки аудитории/публикации.",
        "docs_url": "https://developers.tiktok.com/products/content-posting-api/",
    },
    {
        "id": "instagram", "name": "Instagram", "symbol": "IG", "color": "#E4405F",
        "category": "Мировые", "market": "Мировые", "priority": "P1",
        "mode": "planned", "roles": ["visual", "video"],
        "description": "Reels, изображения и карусели. Нельзя считать обычный личный профиль готовым к API-публикации.",
        "requirements": "Профессиональный аккаунт Meta, OAuth, права и доступ к Content Publishing API.",
        "docs_url": "https://developers.facebook.com/docs/instagram-platform/content-publishing/",
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
        "mode": "planned", "roles": ["text", "visual", "video"],
        "description": "Страницы и публикации для международной аудитории.",
        "requirements": "Page access token, права Graph API и доступ к странице.",
        "docs_url": "https://developers.facebook.com/docs/graph-api/",
    },
    {
        "id": "linkedin", "name": "LinkedIn", "symbol": "in", "color": "#0A66C2",
        "category": "Профессиональные", "market": "Мировые", "priority": "P2",
        "mode": "planned", "roles": ["text", "professional"],
        "description": "Экспертные публикации, кейсы и B2B-контент с отдельной деловой редактурой.",
        "requirements": "OAuth, w_member_social или права публикации от имени организации.",
        "docs_url": "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api",
    },
    {
        "id": "pinterest", "name": "Pinterest", "symbol": "P", "color": "#E60023",
        "category": "Визуальные", "market": "Мировые", "priority": "P2",
        "mode": "planned", "roles": ["visual", "search"],
        "description": "Долгоживущие визуальные пины со ссылкой на исходную статью или сайт.",
        "requirements": "OAuth, доступ к Pins API и подготовка вертикальных изображений.",
        "docs_url": "https://developers.pinterest.com/",
    },
    {
        "id": "odnoklassniki", "name": "Одноклассники", "symbol": "OK", "color": "#EE8208",
        "category": "Российские", "market": "Россия и СНГ", "priority": "P1",
        "mode": "planned", "roles": ["text", "visual"],
        "description": "Отдельная адаптация для российской аудитории и сообществ.",
        "requirements": "Проверить текущий API публикации, права группы и OAuth-приложение.",
        "docs_url": "https://apiok.ru/",
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
        "mode": "planned", "roles": ["community"],
        "description": "Публикации в принадлежащие владельцу серверы и каналы через бота или webhook.",
        "requirements": "Bot token или webhook в выбранном сервере; права на канал.",
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
        "description": "Отдельные длинные материалы для предпринимательской и технологической аудитории.",
        "requirements": "Проверить доступный способ авторской публикации и правила платформы.",
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
        "description": "Посты с картинками и обсуждения; нужен нативный формат вместо рекламного кросспоста.",
        "requirements": "Проверить доступный официальный способ публикации и антиспам-правила.",
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
        "mode": "planned", "roles": ["text", "visual"],
        "description": "Текст, изображения и ссылки для нишевых сообществ.",
        "requirements": "OAuth и приложение Tumblr API.",
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
        "category": "Азиатские", "market": "Китай", "priority": "P3",
        "mode": "planned", "roles": ["video"],
        "description": "Китайская отдельная платформа коротких видео; не путать с международным TikTok.",
        "requirements": "Региональный аккаунт и допуск к Open Platform.",
        "docs_url": "https://developer.open-douyin.com/",
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
        {"key": "VK_GROUP_ID", "attribute": "vk_group_id", "label": "ID сообщества", "secret": False, "placeholder": "123456789", "help": "Числовой ID сообщества без знака минус."},
    ],
    "max": [
        {"key": "MAX_ACCESS_TOKEN", "attribute": "max_access_token", "label": "Токен бота MAX", "secret": True, "placeholder": "Токен из кабинета разработчика MAX", "help": "Токен сохраняется в локальном .env и не показывается повторно."},
        {"key": "MAX_CHANNEL_ID", "attribute": "max_channel_id", "label": "ID канала или чата", "secret": False, "placeholder": "ID канала", "help": "Бот должен иметь права публикации в целевом канале."},
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
    "threads": [
        {"key": "THREADS_APP_ID", "attribute": "threads_app_id", "label": "Threads App ID", "secret": False, "placeholder": "Meta App ID", "help": "Идентификатор Meta-приложения с добавленным продуктом Threads API."},
        {"key": "THREADS_APP_SECRET", "attribute": "threads_app_secret", "label": "Threads App Secret", "secret": True, "placeholder": "Meta App Secret", "help": "Секрет приложения. Хранится локально и не показывается повторно."},
        {"key": "THREADS_ACCESS_TOKEN", "attribute": "threads_access_token", "label": "Access token", "secret": True, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний OAuth-токен."},
        {"key": "THREADS_TOKEN_EXPIRES_AT", "attribute": "threads_token_expires_at", "label": "OAuth expiry", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Внутренний срок действия токена."},
        {"key": "THREADS_USER_ID", "attribute": "threads_user_id", "label": "User ID", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "ID подтверждённого аккаунта."},
        {"key": "THREADS_USERNAME", "attribute": "threads_username", "label": "Username", "secret": False, "hidden": True, "fingerprint": False, "placeholder": "", "help": "Имя подключённого аккаунта."},
    ],
    "reddit": [
        {"key": "REDDIT_CLIENT_ID", "attribute": "reddit_client_id", "label": "Client ID", "secret": False, "placeholder": "Reddit app client ID", "help": "Создаётся в настройках Reddit developer app."},
        {"key": "REDDIT_CLIENT_SECRET", "attribute": "reddit_client_secret", "label": "Client secret", "secret": True, "placeholder": "Reddit app client secret", "help": "Секрет приложения, не пароль Reddit."},
        {"key": "REDDIT_REFRESH_TOKEN", "attribute": "reddit_refresh_token", "label": "Refresh token", "secret": True, "placeholder": "Permanent OAuth refresh token", "help": "OAuth-токен с нужными scope, например read и submit."},
        {"key": "REDDIT_SUBREDDIT", "attribute": "reddit_subreddit", "label": "Сообщество по умолчанию", "secret": False, "placeholder": "learnpython", "help": "Название без префикса r/."},
        {"key": "REDDIT_USER_AGENT", "attribute": "reddit_user_agent", "label": "User-Agent", "secret": False, "placeholder": "python:tgfarm:1.0 (by /u/your_name)", "help": "Уникальная описательная строка. Не оставляй шаблонное имя."},
    ],
}
_AUTH_URLS: dict[str, str] = {
    "telegram": "https://web.telegram.org/",
    "dzen": "https://dzen.ru/profile/editor/",
    "vk": "https://vk.com/",
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
    elif platform_id in {"mastodon", "x", "threads"}:
        method = "oauth"
    elif fields:
        method = "credentials"
    else:
        method = "external"
    return {
        "method": method,
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
            "Сайт откроется отдельно, но сам по себе вход на сайт не подключает API к TgFarm."
            if not supported else
            ("Для X или Threads сначала сохрани App ID/Secret, затем нажми «Подключить через OAuth». Callback URL должен быть разрешён в кабинете разработчика; проверка API не заменяет тест публикации."
            if platform_id in {"x", "threads"} else "После сохранения ключей нажми «Проверить доступ». Проверка API не заменяет тест публикации.")
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
    "reddit", "youtube", "tiktok", "instagram", "threads",
    "odnoklassniki", "rutube", "linkedin", "pinterest",
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
    if platform_id == "max":
        if settings.max_access_token and settings.max_channel_id:
            return True, "Данные MAX заданы; реальную публикацию нужно проверить."
        return False, "Нужны MAX_ACCESS_TOKEN и MAX_CHANNEL_ID."
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
    if platform_id == "reddit":
        user_agent = settings.reddit_user_agent.strip()
        configured = all((
            settings.reddit_client_id,
            settings.reddit_client_secret,
            settings.reddit_refresh_token,
            settings.reddit_subreddit,
            user_agent,
        )) and "/u/YOUR_REDDIT_USERNAME" not in user_agent
        if configured:
            return True, (
                "OAuth-настройки найдены; проверь право submit и правила сообщества "
                "одним live smoke-тестом."
            )
        return False, (
            "Нужны Reddit OAuth client ID/secret, refresh token, REDDIT_SUBREDDIT "
            "и свой REDDIT_USER_AGENT."
        )
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


def enabled_crosspost_platforms() -> list[str]:
    """Supported crosspost adapters whose global switches are on."""
    return [
        item["id"]
        for item in CATALOG
        if item["mode"] == "built_in"
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

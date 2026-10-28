"""Central registry for distribution platforms.

Only adapters that exist in this codebase can be activated. Other popular
networks can be saved to the integration plan without pretending they can
already receive posts. Secrets remain in environment/.env and are never
returned by this module.
"""
from __future__ import annotations

import os
import tempfile
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
        "category": "Сообщества", "market": "Мировые", "priority": "P1",
        "mode": "planned", "roles": ["crosspost"],
        "description": "Нативные посты под конкретные сабреддиты. Не просто копировать один текст во все сообщества.",
        "requirements": "OAuth-приложение, разрешения API и правила каждого subreddit.",
        "docs_url": "https://www.reddit.com/dev/api/",
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
        "mode": "planned", "roles": ["text", "visual"],
        "description": "Текстовые заметки и обсуждения в экосистеме Meta.",
        "requirements": "Meta app, OAuth и разрешения Threads API.",
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
        "id": "bluesky", "name": "Bluesky", "symbol": "BS", "color": "#1185FE",
        "category": "Мировые", "market": "Мировые", "priority": "P2",
        "mode": "planned", "roles": ["text"],
        "description": "Короткие посты и карточки ссылок через AT Protocol.",
        "requirements": "Авторизация приложения и проверка лимитов/медиа-правил.",
        "docs_url": "https://docs.bsky.app/docs/starter-templates/post",
    },
    {
        "id": "mastodon", "name": "Mastodon", "symbol": "M", "color": "#6364FF",
        "category": "Федеративные", "market": "Мировые", "priority": "P3",
        "mode": "planned", "roles": ["text"],
        "description": "Федеративные посты; настройки и адрес сервера отличаются между инстансами.",
        "requirements": "Сервер пользователя, OAuth и scope write:statuses.",
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

_BY_ID = {item["id"]: item for item in CATALOG}
_SUPPORTED_IDS = {item["id"] for item in CATALOG if item["mode"] == "built_in"}
_DEFAULT_PLANNED = [
    "reddit", "youtube", "tiktok", "instagram", "threads",
    "odnoklassniki", "rutube", "linkedin", "pinterest",
]

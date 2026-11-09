"""Manual publishing packages for platforms without a verified automatic publisher.

This module never contacts destination platforms and never reports a post as
published. It returns an editorially adapted package plus local media links.
"""
from __future__ import annotations

import re
from typing import Any

from tgfarm.core.text import truncate

MANUAL_DESTINATIONS: dict[str, dict[str, Any]] = {
    "vk_clips": {
        "name": "VK Клипы", "url": "https://vk.com/clips", "format": "vertical_video", "needs_video": True,
        "notes": ["Проверь вертикальный MP4.", "Загрузи ролик через официальный раздел VK Клипов.", "Обычная запись на стене VK не считается публикацией в Клипы."],
    },
    "rutube": {
        "name": "RUTUBE", "url": "https://studio.rutube.ru/", "format": "video", "needs_video": True,
        "notes": ["Загрузи оригинальный MP4 через RUTUBE Studio.", "Проверь категорию, возрастную маркировку и доступ.", "В TgFarm нет подтверждённого публичного API загрузки RUTUBE."],
    },
    "vc_ru": {
        "name": "vc.ru", "url": "https://vc.ru/editor", "format": "article",
        "notes": ["Проверь рубрику и теги в редакторе vc.ru.", "Черновик не отправляется автоматически."],
    },
    "habr": {
        "name": "Хабр", "url": "https://habr.com/ru/articles/new/", "format": "technical_article",
        "notes": ["Проверь технические утверждения, код и ссылки.", "Выбери хаб и теги вручную.", "Публичного API создания статей у Хабра нет."],
    },
    "pikabu": {
        "name": "Пикабу", "url": "https://pikabu.ru/create", "format": "community_post",
        "notes": ["Выбери сообщество и нативные теги вручную.", "Проверь правила выбранного сообщества."],
    },
    "tenchat": {
        "name": "TenChat", "url": "https://tenchat.ru/", "format": "professional_post",
        "notes": ["Проверь деловой тон и личные данные.", "Проверь доступность подходящего редактора аккаунта."],
    },
    "medium": {
        "name": "Medium", "url": "https://medium.com/new-story", "format": "article",
        "notes": ["Это ручной экспорт, не API-интеграция: Medium больше не выдаёт новые API integration tokens.", "Самостоятельно проверь правила площадки и отредактируй материал перед отправкой."],
    },
    "whatsapp_channels": {
        "name": "WhatsApp Channels", "url": "https://web.whatsapp.com/", "format": "channel_update",
        "notes": ["Публикация выполняется владельцем канала.", "WhatsApp Business Messaging API не является API публикации в Channels."],
    },
    "twitch": {
        "name": "Twitch", "url": "https://dashboard.twitch.tv/", "format": "stream_announcement",
        "notes": ["Подходит для анонса трансляции или события, не для обычного зеркала поста.", "Проверь категорию, расписание и ссылку на эфир."],
    },
    "snapchat": {
        "name": "Snapchat", "url": "https://profile.snapchat.com/", "format": "vertical_video", "needs_video": True,
        "notes": ["Используй оригинальный вертикальный ролик и официальный Public Profile workflow.", "Доступность зависит от типа профиля и одобрения Snap."],
    },
    "wechat": {
        "name": "WeChat / Weixin", "url": "https://mp.weixin.qq.com/", "format": "official_account_article",
        "notes": ["Нужен WeChat Official Account с правом публикации.", "Проверь китайскую локализацию и региональные требования."],
    },
    "douyin": {
        "name": "Douyin", "url": "https://creator.douyin.com/", "format": "vertical_video", "needs_video": True,
        "notes": ["Нужен подходящий региональный аккаунт и доступ к creator tools.", "Проверь аудиторию и музыку в официальном редакторе."],
    },
    "rednote": {
        "name": "RedNote / Xiaohongshu", "url": "https://creator.xiaohongshu.com/", "format": "visual_note",
        "notes": ["Нужна корректная локализация на китайский язык.", "Проверь creator permissions и правила коммерческого контента."],
    },
    "tiktok": {
        "name": "TikTok", "url": "https://www.tiktok.com/upload", "format": "vertical_video",
        "needs_video": True,
        "notes": [
            "TikTok требует отдельного выбора privacy options и согласия владельца на каждый ролик.",
            "Загрузи вертикальный MP4 в официальном загрузчике; проверь права на музыку.",
            "Согласованная публикация через API идёт только из карточки поста, не из очереди.",
        ],
    },
}


def _clean_text(value: str) -> str:
    text = str(value or "")
    for source, target in (("**", ""), ("__", ""), ("~~", ""), (chr(96), "")):
        text = text.replace(source, target)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_manual_package(platform_id: str, post: dict[str, Any]) -> dict[str, Any]:
    """Build a platform-specific manual handoff with no external side effects."""
    platform_id = str(platform_id or "").strip().lower()
    spec = MANUAL_DESTINATIONS.get(platform_id)
    if spec is None:
        raise ValueError("Для этой площадки не предусмотрен ручный пакет публикации.")
    title = _clean_text(str(post.get("hook") or post.get("title") or ""))
    body = _clean_text(str(post.get("body") or ""))
    if not title or not body:
        raise ValueError("Для ручного пакета нужны заголовок и основной текст поста.")

    tags: list[str] = []
    seen_tags: set[str] = set()
    for raw in (post.get("hashtags") or [])[:12]:
        tag = re.sub(r"[^\w]", "", str(raw or "").strip().lstrip("#"), flags=re.UNICODE)[:40]
        if tag and tag.casefold() not in seen_tags:
            tags.append(tag)
            seen_tags.add(tag.casefold())

    lines = body.splitlines()
    if lines and all(re.fullmatch(r"#[0-9a-zа-яё_]+", item.strip(), re.I) for item in lines[-1].split()):
        body = "\n".join(lines[:-1]).strip()

    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in post.get("sources") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith("https://") or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({"title": str(item.get("title") or "")[:180], "url": url})
        if len(sources) >= 8:
            break
    if sources:
        source_text = "\n".join(
            f"{item['title'] + ': ' if item['title'] else ''}{item['url']}" for item in sources
        )
        body = "\n\n".join(part for part in (body, "Источники:\n" + source_text) if part)
    cta = _clean_text(str(post.get("cta") or ""))
    if cta and cta not in body:
        body = "\n\n".join(part for part in (body, cta) if part)

    if platform_id == "pikabu" and len(title) > 140:
        raise ValueError("Заголовок Пикабу длиннее 140 символов. Сократи его перед ручной отправкой.")
    if platform_id == "habr":
        body = truncate(body, 80_000)
    elif platform_id in {"whatsapp_channels", "twitch"}:
        body = truncate(body, 4_000)
    elif platform_id in {"vk_clips", "rutube", "snapchat", "douyin", "tiktok"}:
        body = truncate(body, 5_000)
    else:
        body = truncate(body, 30_000)

    media = []
    for item in post.get("media") or []:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha") or "").strip()
        if not sha:
            continue
        kind = str(item.get("kind") or "image")
        if kind == "video":
            media.append({"kind": kind, "url": f"/api/media/{sha}", "filename": f"tgfarm-{post.get('id','post')}.mp4"})
        elif kind in {"article_image", "ai_art", "template", "infographic", "quote"}:
            media.append({"kind": kind, "url": f"/api/media/{sha}", "filename": f"tgfarm-{post.get('id','post')}.jpg"})

    needs_video = bool(spec.get("needs_video"))
    has_video = any(item["kind"] == "video" for item in media)
    notes = list(spec.get("notes") or [])
    if needs_video and not has_video:
        notes.insert(0, "Вертикального MP4 пока нет. Сначала создай его в TgFarm, затем вернись к ручной подготовке.")
    return {
        "ok": True,
        "platform": platform_id,
        "name": spec["name"],
        "url": spec["url"],
        "format": spec["format"],
        "title": title,
        "body": body,
        "hashtags": tags,
        "sources": sources,
        "media": media,
        "needs_video": needs_video,
        "has_video": has_video,
        "ready": not (needs_video and not has_video),
        "notes": notes,
        "manual_only": True,
        "publish_action": "none",
    }

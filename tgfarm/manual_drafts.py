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


# Emphasis markers are stripped only when they are a real paired delimiter at a
# word boundary. A blind str.replace() also ate "__init__", "my__var", "2**10"
# and a lone "~~" inside a quoted string, corrupting technical articles.
_BOLD_RE = re.compile(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])", re.S)
_UNDER_RE = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])", re.S)
_STRIKE_RE = re.compile(r"(?<![\w~])~~(?=\S)(.+?)(?<=\S)~~(?![\w~])", re.S)
_CODE_RE = re.compile(r"(?<!`)`(?=\S)([^`]+)`(?!`)")
_WORD_ONLY_RE = re.compile(r"\w+")
_HASHTAG_RE = re.compile(r"#[0-9a-zа-яё_]+", re.I)
_LETTER_RE = re.compile(r"[a-zа-яё]", re.I)


def _strip_dunder(match: re.Match[str]) -> str:
    # "__init__" and "__name__" are Python identifiers, not emphasis. Only a
    # multi-word "__bold text__" span is unambiguous enough to strip.
    return match.group(0) if _WORD_ONLY_RE.fullmatch(match.group(1)) else match.group(1)


def _clean_text(value: str) -> str:
    text = str(value or "")
    text = _BOLD_RE.sub(r"\1", text)
    text = _UNDER_RE.sub(_strip_dunder, text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _body_limit(platform_id: str) -> int:
    """Body cap for a handoff: the tighter of our ceiling and the editor's own limit.

    The editorial profile owns the platform's real limit; a package that exceeds it
    would be rejected by the editor the operator pastes it into.
    """
    from tgfarm.platform_editorial import PROFILES

    if platform_id == "habr":
        ceiling = 80_000
    elif platform_id in {"whatsapp_channels", "twitch"}:
        ceiling = 4_000
    elif platform_id in {"vk_clips", "rutube", "snapchat", "douyin", "tiktok"}:
        ceiling = 5_000
    else:
        ceiling = 30_000
    declared = (PROFILES.get(platform_id) or {}).get("max_chars")
    if isinstance(declared, int) and declared > 0:
        return min(ceiling, declared)
    return ceiling


def _body_limit_source(platform_id: str, limit: int) -> str:
    """Say who owns the applied cap: the profile's editor limit or our ceiling.

    The internal ceiling is TgFarm's own unverified cap and must not be
    presented to the operator as a platform rule.
    """
    from tgfarm.platform_editorial import PROFILES

    declared = (PROFILES.get(platform_id) or {}).get("max_chars")
    if isinstance(declared, int) and declared == limit:
        return f"лимит {limit} символов задан редакторским профилем площадки в TgFarm"
    return f"внутренний лимит пакета {limit}, не подтверждённый лимит площадки"


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
    notes: list[str] = list(spec.get("notes") or [])

    tags: list[str] = []
    seen_tags: set[str] = set()
    for raw in (post.get("hashtags") or [])[:12]:
        tag = re.sub(r"[^\w]", "", str(raw or "").strip().lstrip("#"), flags=re.UNICODE)[:40]
        if tag and tag.casefold() not in seen_tags:
            tags.append(tag)
            seen_tags.add(tag.casefold())

    lines = body.splitlines()
    tail = lines[-1].split() if lines else []
    remainder = "\n".join(lines[:-1]).strip()
    # Only an unambiguous word-hashtag line is redundant with `hashtags`.
    # "#1" / "#2024" are numbered references, and the strip must never empty the
    # body: the operator still needs something to paste.
    if (
        tail
        and remainder
        and all(_HASHTAG_RE.fullmatch(token) and _LETTER_RE.search(token) for token in tail)
    ):
        body = remainder

    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    skipped_sources = 0
    for item in post.get("sources") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        # http:// is a legitimate attribution and this module never fetches it.
        # Anything else stays out of the body, but the drop is now disclosed.
        if not url.startswith(("http://", "https://")) or len(sources) >= 8:
            skipped_sources += 1
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({"title": str(item.get("title") or "")[:180], "url": url})
    if skipped_sources:
        notes.append(
            f"Часть источников не попала в пакет ({skipped_sources} шт.): пакет принимает только "
            "http/https и не больше 8 ссылок. Проверь остальные вручную."
        )
    if sources:
        source_text = "\n".join(
            f"{item['title'] + ': ' if item['title'] else ''}{item['url']}" for item in sources
        )
        body = "\n\n".join(part for part in (body, "Источники:\n" + source_text) if part)
    cta = _clean_text(str(post.get("cta") or ""))
    if cta:
        # A plain substring test both missed a CTA differing only in whitespace
        # and matched one buried inside an unrelated word ("лайк" in "Дизлайк"),
        # silently dropping the call to action. Match the phrase, not any slice.
        pattern = r"\s+".join(re.escape(token) for token in cta.split())
        if cta[0].isalnum():
            pattern = r"(?<!\w)" + pattern
        if cta[-1].isalnum():
            pattern += r"(?!\w)"
        if not re.search(pattern, body):
            body = "\n\n".join(part for part in (body, cta) if part)

    if platform_id == "pikabu" and len(title) > 140:
        raise ValueError("Заголовок Пикабу длиннее 140 символов. Сократи его перед ручной отправкой.")
    if platform_id == "twitch" and len(title) > 140:
        raise ValueError(
            "Заголовок Twitch длиннее 140 символов — это документированный лимит названия трансляции. "
            "Сократи его перед ручной отправкой."
        )
    if platform_id == "twitch":
        notes.append("Лимит заголовка Twitch — 140 символов (dev.twitch.tv/docs/api/reference).")
    elif platform_id == "pikabu":
        notes.append(
            f"Заголовок: {len(title)} символов. Порог 140 — существующее правило пакета TgFarm, "
            "официальным источником Пикабу не подтверждён."
        )
    else:
        # No official limit could be verified for these destinations, so disclose
        # the length instead of inventing a plausible-looking number.
        notes.append(
            f"Заголовок: {len(title)} символов. Лимит заголовка площадки не подтверждён официальным "
            "источником — проверь его в редакторе перед отправкой."
        )

    body_limit = _body_limit(platform_id)
    original_body = body.strip()
    body = truncate(body, body_limit)
    if body != original_body:
        # truncate() is silent. Handing a cut article over as "ready" without
        # saying so is an honesty defect, so the cut is always disclosed.
        notes.append(
            f"Текст сокращён с {len(original_body)} до {len(body)} символов: "
            f"{_body_limit_source(platform_id, body_limit)}. Полный текст остался в TgFarm — "
            "проверь, что обрезка не потеряла важное."
        )

    media = []
    name_counts: dict[str, int] = {}
    for item in post.get("media") or []:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha") or "").strip()
        if not sha:
            continue
        kind = str(item.get("kind") or "image")
        if kind == "video":
            ext = "mp4"
        elif kind in {"article_image", "ai_art", "template", "infographic", "quote"}:
            ext = "jpg"
        else:
            continue
        # Every image used to collapse onto one filename, so a multi-image
        # package handed the operator N downloads with a single name.
        name_counts[ext] = name_counts.get(ext, 0) + 1
        suffix = "" if name_counts[ext] == 1 else f"-{name_counts[ext]}"
        media.append({
            "kind": kind,
            "url": f"/api/media/{sha}",
            "filename": f"tgfarm-{post.get('id', 'post')}{suffix}.{ext}",
        })

    needs_video = bool(spec.get("needs_video"))
    has_video = any(item["kind"] == "video" for item in media)
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

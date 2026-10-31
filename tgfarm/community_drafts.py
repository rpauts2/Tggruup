"""Assisted, draft-only composition for vc.ru and Pikabu.

The operator authenticates in a persistent local browser profile. TgFarm fills
an editor draft and deliberately never clicks a publish/submit control.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import DATA_DIR

log = get_logger(__name__)

PLATFORMS = {
    "vc_ru": {
        "name": "vc.ru",
        "origin": "https://vc.ru",
        "start_url": "https://vc.ru/",
        "hosts": {"vc.ru", "www.vc.ru"},
        "max_images": 1,
        "max_image_bytes": 10 * 1024 * 1024,
    },
    "pikabu": {
        "name": "Пикабу",
        "origin": "https://pikabu.ru",
        "start_url": "https://pikabu.ru/",
        "hosts": {"pikabu.ru", "www.pikabu.ru"},
        "max_images": 3,
        "max_image_bytes": 10 * 1024 * 1024,
    },
}
_DRAFT_STATE = "community_drafts.json"
_LOCKS = {platform: threading.Lock() for platform in PLATFORMS}
_IMAGE_KINDS = {"article_image": 0, "ai_art": 1, "photo": 2, "image": 3}


class CommunityDraftError(RuntimeError):
    """The assisted draft workflow could not safely prepare a draft."""


def _state_path() -> Path:
    return DATA_DIR / _DRAFT_STATE


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(value: dict[str, Any]) -> None:
    import os
    import tempfile

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
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


def _safe_editor_url(platform_id: str, url: str) -> bool:
    spec = PLATFORMS.get(platform_id)
    if not spec:
        return False
    parsed = urlparse(str(url or ""))
    return (
        parsed.scheme == "https"
        and parsed.hostname in spec["hosts"]
        and parsed.username is None
        and parsed.password is None
    )


def _clean_markup_text(value: str) -> str:
    """Turn the farm's light Markdown into readable editor text."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"!\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*([^\*\n]+)\*", r"\1", text)
    text = text.replace(chr(96), "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def compose_editor_text(post: dict[str, Any], platform_id: str) -> str:
    """Create a plain-text version that survives rich-text editors."""
    body = _clean_markup_text(str(post.get("body") or ""))
    additions: list[str] = []
    cta = _clean_markup_text(str(post.get("cta") or ""))
    if cta and cta not in body:
        additions.append(cta)
    sources = post.get("sources") or []
    source_lines = []
    for source in sources[:5]:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        title = str(source.get("title") or "").strip()
        if url.startswith(("https://", "http://")):
            source_lines.append(f"{title}: {url}" if title else url)
    if source_lines:
        additions.append("Источники:\n" + "\n".join(source_lines))
    hashtags = [
        "#" + re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]", "", str(tag)).strip("_")
        for tag in (post.get("hashtags") or [])[:8]
    ]
    hashtags = [tag for tag in hashtags if tag != "#"]
    if hashtags and platform_id != "pikabu":
        additions.append(" ".join(hashtags))
    if additions:
        body = "\n\n".join(part for part in (body, *additions) if part)
    if platform_id == "pikabu" and len(str(post.get("title") or "").strip()) > 140:
        raise CommunityDraftError("Заголовок Пикабу длиннее 140 символов. Сократи его в TgFarm и повтори подготовку.")
    if not body:
        raise CommunityDraftError("У поста пустое тело: нечего переносить в редактор.")
    return body


def _image_candidates(media: list[dict[str, Any]], platform_id: str) -> list[Path]:
    """Use editorial photos/AI art, never text-heavy farm templates or infographics."""
    spec = PLATFORMS[platform_id]
    candidates: list[tuple[int, int, Path]] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").lower()
        if kind not in _IMAGE_KINDS:
            continue
        path = Path(str(item.get("path") or ""))
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            if path.stat().st_size > spec["max_image_bytes"]:
                continue
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                continue
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                if width < 320 or height < 180:
                    continue
        except Exception:  # noqa: BLE001 - malformed media is simply not uploadable
            continue
        candidates.append((_IMAGE_KINDS[kind], 0 if item.get("is_primary") else 1, path))
    candidates.sort(key=lambda entry: (entry[0], entry[1], str(entry[2]).lower()))
    return [entry[2] for entry in candidates[: spec["max_images"]]]


def _fingerprint(platform_id: str, title: str, body: str, images: list[Path]) -> str:
    digest = hashlib.sha256()
    digest.update(platform_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(title.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body.encode("utf-8"))
    for path in images:
        digest.update(b"\0")
        digest.update(path.name.encode("utf-8", errors="replace"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


async def _first_visible(page, selectors: tuple[str, ...], *, last: bool = False):
    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        indexes = range(count - 1, -1, -1) if last else range(count)
        for index in indexes:
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except Exception:  # noqa: BLE001 - stale DOM node
                continue
    return None


async def _click_editor_entry(page, platform_id: str) -> None:
    if platform_id == "vc_ru":
        names = (re.compile(r"^\s*Написать\s*$", re.I),)
        fallbacks = ('a[href="/editor"]', 'a[href^="/editor"]')
    else:
        names = (re.compile(r"Добавить пост", re.I), re.compile(r"Создать пост", re.I))
        fallbacks = (
            'button[aria-label*="Добавить пост" i]',
            'a[aria-label*="Добавить пост" i]',
            'a[href*="/create"]',
        )
    for name in names:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=name)
                count = await locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible() and await item.is_enabled():
                        await item.click(timeout=8_000)
                        await page.wait_for_timeout(1_200)
                        return
            except Exception:  # noqa: BLE001 - try the next accessible route
                continue
    for selector in fallbacks:
        try:
            item = await _first_visible(page, (selector,))
            if item is not None:
                await item.click(timeout=8_000)
                await page.wait_for_timeout(1_200)
                return
        except Exception:  # noqa: BLE001 - next candidate
            continue
    label = "«Написать»" if platform_id == "vc_ru" else "«Добавить пост»"
    raise CommunityDraftError(
        f"Не нашёл кнопку {label}. Проверь вход в аккаунт и открой редактор вручную, затем повтори подготовку."
    )


async def _fill_editor_fields(page, platform_id: str, title: str, body: str) -> None:
    title_locator = await _first_visible(page, (
        'input[placeholder*="заголов" i]',
        'textarea[placeholder*="заголов" i]',
        'input[aria-label*="заголов" i]',
        'textarea[aria-label*="заголов" i]',
        '[contenteditable="true"][data-placeholder*="заголов" i]',
        '[contenteditable="true"][aria-label*="заголов" i]',
        'input[name*="title" i]',
    ))
    if title_locator is None:
        raise CommunityDraftError(
            f"{PLATFORMS[platform_id]['name']}: не удалось однозначно найти поле заголовка. "
            "Черновик не отмечен как подготовленный."
        )

    body_candidates = (
        '[contenteditable="true"][data-placeholder*="текст" i]',
        '[contenteditable="true"][aria-label*="текст" i]',
        'textarea[placeholder*="текст" i]:not([placeholder*="заголов" i])',
        'textarea[aria-label*="текст" i]:not([aria-label*="заголов" i])',
        'textarea:not([placeholder*="заголов" i])',
        '[contenteditable="true"]',
        '[role="textbox"]',
    )
    body_locator = await _first_visible(page, body_candidates, last=True)
    if body_locator is None:
        raise CommunityDraftError(
            f"{PLATFORMS[platform_id]['name']}: не удалось найти основной блок текста. "
            "TgFarm не будет вставлять материал в случайное поле."
        )
    try:
        await title_locator.fill(title, timeout=8_000)
        await body_locator.fill(body, timeout=12_000)
    except Exception as exc:  # noqa: BLE001 - rich editors vary by platform version
        raise CommunityDraftError(
            f"{PLATFORMS[platform_id]['name']}: редактор не принял текст ({type(exc).__name__})."
        ) from exc


async def _first_file_input(page):
    """File inputs are intentionally hidden by editors, so visibility is irrelevant."""
    for selector in (
        'input[type="file"][accept*="image" i]',
        'input[type="file"][accept*=".jpg" i]',
        'input[type="file"][accept*=".png" i]',
    ):
        locator = page.locator(selector)
        count = await locator.count()
        if count:
            return locator.first
    generic = page.locator('input[type="file"]')
    # Editors sometimes omit the accept attribute. Only use an untyped input
    # when it is the only file control on the page, never guess between uploads.
    if await generic.count() == 1:
        return generic.first
    return None


async def _upload_images(page, platform_id: str, images: list[Path]) -> int:
    """Attach media using the editor's image file input; never activate publish."""
    if not images:
        return 0
    file_input = await _first_file_input(page)
    if file_input is None:
        labels = (
            re.compile(r"Фото или видео|Изображение|Добавить фото|Добавить изображение", re.I),
            re.compile(r"Фото|Картинка", re.I),
        )
        opened = False
        for label in labels:
            for role in ("button", "menuitem"):
                try:
                    candidate = page.get_by_role(role, name=label)
                    for index in range(await candidate.count()):
                        item = candidate.nth(index)
                        if await item.is_visible() and await item.is_enabled():
                            await item.click(timeout=5_000)
                            opened = True
                            break
                    if opened:
                        break
                except Exception:  # noqa: BLE001 - try a different media control
                    continue
            if opened:
                break
        if opened:
            await page.wait_for_timeout(500)
            file_input = await _first_file_input(page)
    if file_input is None:
        raise CommunityDraftError(
            f"{PLATFORMS[platform_id]['name']}: редактор не показал загрузчик изображения. "
            "TgFarm не отметил частично заполненную форму как готовый черновик."
        )
    try:
        multiple = await file_input.get_attribute("multiple")
        selected = images if multiple is not None else images[:1]
        await file_input.set_input_files([str(path) for path in selected], timeout=20_000)
        await page.wait_for_timeout(1_500)
        return len(selected)
    except Exception as exc:  # noqa: BLE001 - don't mark a partial upload as complete
        raise CommunityDraftError(
            f"{PLATFORMS[platform_id]['name']}: загрузка изображения не завершилась ({type(exc).__name__})."
        ) from exc


def _load_post(post_public_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from tgfarm.db.models import Media, Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            raise CommunityDraftError("Пост TgFarm не найден.")
        media = session.execute(
            select(Media).where(Media.post_id == post.id).order_by(Media.is_primary.desc(), Media.id.asc())
        ).scalars().all()
        return {
            "public_id": post.public_id,
            "title": str(post.hook or "").strip(),
            "body": str(post.body or ""),
            "cta": str(post.cta or ""),
            "hashtags": list(post.hashtags or []),
            "sources": list(post.sources or []),
            "media": [
                {"kind": item.kind, "path": item.path, "is_primary": bool(item.is_primary)}
                for item in media
            ],
        }


async def prepare_community_draft(platform_id: str, post_public_id: str) -> dict[str, Any]:
    """Open a saved profile and prepare/reopen a platform draft without publishing."""
    from playwright.async_api import async_playwright

    from tgfarm.browser_auth import _launch_context

    platform_id = str(platform_id or "").strip().lower()
    post_public_id = str(post_public_id or "").strip()
    if platform_id not in PLATFORMS:
        raise CommunityDraftError("Подготовка черновиков доступна только для vc.ru и Пикабу.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", post_public_id):
        raise CommunityDraftError("Некорректный идентификатор поста.")
    lock = _LOCKS[platform_id]
    if not lock.acquire(blocking=False):
        raise CommunityDraftError(f"Браузер {PLATFORMS[platform_id]['name']} уже используется другой операцией.")
    try:
        spec = PLATFORMS[platform_id]
        profile = DATA_DIR / "platform_profiles" / platform_id
        if not profile.is_dir() or not any(profile.iterdir()):
            raise CommunityDraftError(
                f"Сначала нажми «Войти в браузере» для {spec['name']} и авторизуйся в открытом окне."
            )

        post = await asyncio.to_thread(_load_post, post_public_id)
        title = post["title"]
        if not title:
            raise CommunityDraftError("У поста нет заголовка.")
        body = compose_editor_text(post, platform_id)
        images = _image_candidates(post["media"], platform_id)
        if not images:
            raise CommunityDraftError(
                "Не нашёл подходящего изображения. Для этих редакторов беру только фото/AI-art "
                "и не использую шаблонные обложки с текстом или инфографику."
            )
        fingerprint = _fingerprint(platform_id, title, body, images)
        key = f"{platform_id}:{post_public_id}"
        state = _read_state()
        previous = state.get(key) if isinstance(state.get(key), dict) else None
        existing_url = str((previous or {}).get("draft_url") or "")
        same_version = bool(previous and previous.get("fingerprint") == fingerprint)
        if previous and existing_url and not same_version:
            raise CommunityDraftError(
                "Для этого поста уже подготовлен черновик с другой версией текста или изображений. "
                "Не создаю второй черновик автоматически. Открой прежний черновик и обнови его вручную, "
                "либо удали его на площадке и сбрось запись в data/community_drafts.json."
            )
        if existing_url and not _safe_editor_url(platform_id, existing_url):
            raise CommunityDraftError("Сохранённый адрес черновика не принадлежит целевой площадке.")

        async with async_playwright() as playwright:
            context = await _launch_context(playwright, profile)
            try:
                pages = [page for page in context.pages if not page.is_closed()]
                page = pages[0] if pages else await context.new_page()
                if existing_url:
                    await page.goto(existing_url, wait_until="domcontentloaded", timeout=60_000)
                    if same_version and previous.get("status") == "prepared":
                        return {
                            "ok": True,
                            "platform": platform_id,
                            "post_public_id": post_public_id,
                            "status": "already_prepared",
                            "draft_url": page.url if _safe_editor_url(platform_id, page.url) else existing_url,
                            "media_count": int(previous.get("media_count") or 0),
                            "detail": "Открыл ранее подготовленный черновик. Повторно ничего не создавал.",
                        }
                    if same_version:
                        return {
                            "ok": True,
                            "platform": platform_id,
                            "post_public_id": post_public_id,
                            "status": "needs_review",
                            "draft_url": page.url if _safe_editor_url(platform_id, page.url) else existing_url,
                            "media_count": int(previous.get("media_count") or 0),
                            "detail": "Предыдущая попытка прервалась. Открыл тот же редактор, новый пост не создавал. Проверь поля и медиа вручную.",
                        }
                else:
                    await page.goto(spec["start_url"], wait_until="domcontentloaded", timeout=60_000)
                    await _click_editor_entry(page, platform_id)
                if not _safe_editor_url(platform_id, page.url):
                    raise CommunityDraftError("После открытия редактора браузер оказался вне домена площадки.")
                # Persist the editor URL before any form interaction. If the
                # browser crashes mid-fill, the next click reopens this draft
                # instead of creating another one.
                state[key] = {
                    "platform": platform_id,
                    "post_public_id": post_public_id,
                    "fingerprint": fingerprint,
                    "draft_url": page.url,
                    "status": "preparing",
                    "media_count": 0,
                    "prepared_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
                await asyncio.to_thread(_write_state, state)
                await _fill_editor_fields(page, platform_id, title, body)
                media_count = await _upload_images(page, platform_id, images)
                # vc.ru documents a 10-second autosave interval; wait beyond it
                # before closing the persistent browser context. Pikabu drafts
                # are also autosaved, but need no such long debounce window.
                await page.wait_for_timeout(12_000 if platform_id == "vc_ru" else 3_000)
                draft_url = page.url
                if not _safe_editor_url(platform_id, draft_url):
                    raise CommunityDraftError("Не удалось подтвердить безопасный адрес редактора.")
            finally:
                await context.close()

        state[key] = {
            "platform": platform_id,
            "post_public_id": post_public_id,
            "fingerprint": fingerprint,
            "draft_url": draft_url,
            "status": "prepared",
            "media_count": media_count,
            "prepared_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        await asyncio.to_thread(_write_state, state)
        log.info("prepared draft-only composition for %s post %s", platform_id, post_public_id)
        return {
            "ok": True,
            "platform": platform_id,
            "post_public_id": post_public_id,
            "status": "prepared",
            "draft_url": draft_url,
            "media_count": media_count,
            "manual_steps": (
                ["выбрать тему vc.ru", "проверить обложку и блоки"]
                if platform_id == "vc_ru"
                else ["выбрать теги Пикабу", "при необходимости выбрать сообщество"]
            ),
            "detail": (
                "Черновик заполнен в редакторе. TgFarm не нажимал «Опубликовать». "
                "Проверь оформление и заверши публикацию вручную."
            ),
        }
    finally:
        lock.release()

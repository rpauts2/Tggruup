"""Native editor delivery for vc.ru drafts and scheduled Pikabu posts.

The operator authenticates in a persistent local browser profile. vc.ru stays
draft-only until a current publishing interface is verified; Pikabu uses its
native delayed-publication queue with duplicate protection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from datetime import UTC, datetime, timedelta
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
_STATE_LOCK = threading.RLock()
_LOCKS = {platform: threading.Lock() for platform in PLATFORMS}
_IMAGE_KINDS = {"article_image": 0, "ai_art": 1, "photo": 2, "image": 3}


class CommunityDraftError(RuntimeError):
    """The assisted draft workflow could not safely prepare a draft."""


def _state_path() -> Path:
    return DATA_DIR / _DRAFT_STATE


def _read_state_unlocked() -> dict[str, Any]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _read_state() -> dict[str, Any]:
    with _STATE_LOCK:
        return _read_state_unlocked()


def _write_state_unlocked(value: dict[str, Any]) -> None:
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


def _write_state(value: dict[str, Any]) -> None:
    with _STATE_LOCK:
        _write_state_unlocked(value)


def _write_state_entry(key: str, entry: dict[str, Any]) -> None:
    """Merge a platform/post entry without losing a concurrent update to another platform."""
    with _STATE_LOCK:
        state = _read_state_unlocked()
        state[key] = entry
        _write_state_unlocked(state)


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


def _safe_target_url(platform_id: str, value: str | None) -> str | None:
    """Accept only a canonical public topic/community page on its own platform."""
    if not value:
        return None
    raw = str(value).strip()
    if not _safe_editor_url(platform_id, raw):
        raise CommunityDraftError("Адрес целевого сообщества/темы не принадлежит выбранной площадке.")
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if platform_id == "pikabu":
        valid = bool(re.fullmatch(r"/community/[A-Za-z0-9_-]{2,80}", path))
    else:
        valid = bool(
            re.fullmatch(r"/[A-Za-z0-9_-]{1,50}", path)
            and path not in {"/rules", "/support", "/editor", "/search", "/login", "/register"}
        )
    if not valid or parsed.query or parsed.fragment:
        raise CommunityDraftError("Адрес должен вести на публичную страницу конкретного сообщества или темы.")
    return f"https://{parsed.hostname}{path}"


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
        raise CommunityDraftError(
            "Заголовок Пикабу длиннее 140 символов. Сократи его в TgFarm и повтори подготовку."
        )
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
        f"Не нашёл кнопку {label}. Проверь вход в аккаунт и открой редактор "
        "вручную, затем повтори подготовку."
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


async def prepare_community_draft(
    platform_id: str,
    post_public_id: str,
    *,
    target_url: str | None = None,
    target_name: str | None = None,
) -> dict[str, Any]:
    """Open a saved profile and prepare/reopen a destination-specific draft."""

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
        raise CommunityDraftError(
            f"Браузер {PLATFORMS[platform_id]['name']} уже используется другой операцией."
        )
    try:
        spec = PLATFORMS[platform_id]
        target_url = _safe_target_url(platform_id, target_url)
        target_name = str(target_name or "").strip()[:120]
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
        if target_url:
            fingerprint = hashlib.sha256(
                f"{fingerprint}\0{target_url}".encode("utf-8")
            ).hexdigest()
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
                            "draft_url": (
                                page.url if _safe_editor_url(platform_id, page.url) else existing_url
                            ),
                            "media_count": int(previous.get("media_count") or 0),
                            "detail": "Открыл ранее подготовленный черновик. Повторно ничего не создавал.",
                        }
                    if same_version:
                        return {
                            "ok": True,
                            "platform": platform_id,
                            "post_public_id": post_public_id,
                            "status": "needs_review",
                            "draft_url": (
                                page.url if _safe_editor_url(platform_id, page.url) else existing_url
                            ),
                            "media_count": int(previous.get("media_count") or 0),
                            "detail": (
                                "Предыдущая попытка прервалась. Открыл тот же редактор, новый пост "
                                "не создавал. Проверь поля и медиа вручную."
                            ),
                        }
                else:
                    # Opening the editor from a community/topic page gives the
                    # native site a chance to preselect that destination. The
                    # UI must still verify the selected target before publishing.
                    await page.goto(
                        target_url or spec["start_url"],
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await _click_editor_entry(page, platform_id)
                if not _safe_editor_url(platform_id, page.url):
                    raise CommunityDraftError(
                        "После открытия редактора браузер оказался вне домена площадки."
                    )
                # Persist the editor URL before any form interaction. If the
                # browser crashes mid-fill, the next click reopens this draft
                # instead of creating another one.
                await asyncio.to_thread(
                    _write_state_entry,
                    key,
                    {
                        "platform": platform_id,
                        "post_public_id": post_public_id,
                        "fingerprint": fingerprint,
                        "draft_url": page.url,
                        "status": "preparing",
                        "media_count": 0,
                        "target_url": target_url,
                        "target_name": target_name,
                        "prepared_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                )
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

        await asyncio.to_thread(
            _write_state_entry,
            key,
            {
                "platform": platform_id,
                "post_public_id": post_public_id,
                "fingerprint": fingerprint,
                "draft_url": draft_url,
                "status": "prepared",
                "media_count": media_count,
                "target_url": target_url,
                "target_name": target_name,
                "prepared_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        log.info("prepared draft-only composition for %s post %s", platform_id, post_public_id)
        return {
            "ok": True,
            "platform": platform_id,
            "post_public_id": post_public_id,
            "status": "prepared",
            "draft_url": draft_url,
            "media_count": media_count,
            "target_url": target_url,
            "target_name": target_name or None,
            "manual_steps": (
                (["проверить, что выбрана тема " + target_name] if target_url and target_name else ["выбрать тему vc.ru"])
                + ["проверить обложку и блоки"]
                if platform_id == "vc_ru"
                else (["проверить, что выбрано сообщество " + target_name] if target_url and target_name else ["выбрать сообщество"])
                + ["проверить теги Пикабу; для AI-контента поставить специальный тег"]
            ),
            "detail": (
                "Черновик заполнен в редакторе. TgFarm не нажимал «Опубликовать». "
                "Проверь оформление и заверши публикацию вручную."
            ),
        }
    finally:
        lock.release()

PIKABU_MAX_PENDING = 12
PIKABU_MAX_HORIZON_DAYS = 10
PIKABU_MIN_SCHEDULE_GAP = timedelta(hours=1)
PIKABU_CONSERVATIVE_DAILY_CREATION_CAP = 1
PIKABU_AI_DISCLOSURE_TAG = "Контент нейросетей"


def normalize_pikabu_tags(tags: list[str] | tuple[str, ...]) -> list[str]:
    """Validate real, regular Pikabu tags before interacting with the native picker."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = re.sub(r"\s+", " ", str(raw or "").strip().lstrip("#")).strip()
        key = tag.casefold()
        if not tag or key in seen:
            continue
        if len(tag) > 50:
            raise CommunityDraftError("Тег Пикабу длиннее 50 символов.")
        if tag.casefold() in {"моё", "мое", "18+", "длиннопост", "текст", "видео", "гифка"}:
            continue
        seen.add(key)
        result.append(tag)
    if not 2 <= len(result) <= 20:
        raise CommunityDraftError("Пикабу требует от 2 до 20 обычных тегов, не считая служебных отметок.")
    if sum(len(tag) for tag in result) + max(0, len(result) - 1) > 250:
        raise CommunityDraftError("Общая длина тегов Пикабу превышает 250 символов.")
    return result


def _as_local_datetime(value: Any, timezone_name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise CommunityDraftError("В настройках указан неизвестный часовой пояс.") from exc
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def plan_pikabu_schedule(
    *,
    now: datetime | None = None,
    entries: dict[str, Any] | None = None,
    timezone_name: str = "Europe/Moscow",
) -> datetime:
    """Choose a conservative native-Pikabu slot without exceeding documented limits.

    TgFarm deliberately reserves at most one new scheduled post per local
    creation day because an account's actual daily allowance depends on its
    public rating. The site counts scheduled posts on the day they are created.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise CommunityDraftError("В настройках указан неизвестный часовой пояс.") from exc
    current = _as_local_datetime(now or datetime.now(UTC), timezone_name)
    assert current is not None
    state = entries if isinstance(entries, dict) else {}
    pending: list[datetime] = []
    created_today = 0
    pending_count = 0

    for key, raw in state.items():
        if not str(key).startswith("pikabu:") or not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").lower()
        if status not in {"scheduled", "submitting", "unknown"}:
            continue
        created_at = _as_local_datetime(raw.get("created_at"), timezone_name)
        if created_at is not None and created_at.date() == current.date():
            created_today += 1
        scheduled_at = _as_local_datetime(raw.get("scheduled_at"), timezone_name)
        if status in {"submitting", "unknown"}:
            # An uncertain submit may already exist remotely. Count it and
            # never retry it automatically, even if its scheduled time is absent.
            pending_count += 1
        elif scheduled_at is not None and scheduled_at > current:
            pending.append(scheduled_at)
            pending_count += 1

    if pending_count >= PIKABU_MAX_PENDING:
        raise CommunityDraftError("В отложке Пикабу уже достигнут лимит 12 постов.")
    candidate = current + PIKABU_MIN_SCHEDULE_GAP
    if pending:
        candidate = max(candidate, max(pending) + PIKABU_MIN_SCHEDULE_GAP)
    if created_today >= PIKABU_CONSERVATIVE_DAILY_CREATION_CAP:
        tomorrow = current.date() + timedelta(days=1)
        candidate = max(candidate, datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, tzinfo=zone
        ))
    horizon = current + timedelta(days=PIKABU_MAX_HORIZON_DAYS)
    if candidate > horizon:
        raise CommunityDraftError("Свободного времени в пределах 10 дней для отложенной публикации Пикабу нет.")
    return candidate.astimezone(zone)


async def _pikabu_tag_input(page):
    selectors = (
        "[class*='editor-tags__host'] input",
        "input[class*='pkb-input-tag__input']",
        "input[placeholder*='тег' i]",
        "textarea[placeholder*='тег' i]",
        "input[name='tags']",
        "[contenteditable='true'][data-placeholder*='тег' i]",
        "[contenteditable='true'][aria-label*='тег' i]",
    )
    return await _first_visible(page, selectors)


async def _pikabu_select_tag(page, tag: str) -> None:
    """Select an exact suggestion from the site's tag picker, never free-form text."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    tag_input = await _pikabu_tag_input(page)
    if tag_input is None:
        raise CommunityDraftError("В редакторе Пикабу не найдено поле тематических тегов.")
    await tag_input.click()
    await tag_input.press_sequentially(tag, delay=18)
    await page.wait_for_timeout(350)

    for selector in ("[role='option']", "[class*='pkb-select-option__host']", "[class*='tag-option']"):
        options = page.locator(selector)
        for index in range(await options.count()):
            option = options.nth(index)
            try:
                if not await option.is_visible():
                    continue
                label = re.sub(r"\s+", " ", (await option.inner_text()).strip()).strip(" ,")
                if label.casefold() == tag.casefold():
                    await option.click(timeout=5_000)
                    await page.wait_for_timeout(180)
                    return
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
    # Do not press Enter or comma blindly: a typed, unknown tag may be ignored
    # by Pikabu and leave a post below the documented two-tag minimum.
    raise CommunityDraftError(f"Тег «{tag}» не появился в списке разрешённых тегов Пикабу.")


async def _pikabu_select_tags(page, tags: list[str]) -> None:
    normalized = normalize_pikabu_tags(tags)
    tag_input = await _pikabu_tag_input(page)
    if tag_input is None:
        next_button = page.get_by_role("button", name=re.compile(r"^продолжить$", re.I)).last
        if await next_button.count() and await next_button.is_visible():
            await next_button.click()
            await page.wait_for_timeout(700)
    for tag in normalized:
        await _pikabu_select_tag(page, tag)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(250)
    # The editor must show each selected tag as a chip before we proceed.
    chip_text = ""
    host = page.locator("[class*='editor-tags__host']").first
    if await host.count():
        chip_text = await host.inner_text()
    missing = [tag for tag in normalized if tag.casefold() not in chip_text.casefold()]
    if missing:
        raise CommunityDraftError(
            "Редактор Пикабу не подтвердил выбор тегов: " + ", ".join(missing[:4])
        )


async def _pikabu_fill_body(page, body: str) -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    editor = await _first_visible(page, (
        "[contenteditable='true'][data-placeholder*='текст' i]",
        "[contenteditable='true'][aria-label*='текст' i]",
        "[contenteditable='true']",
    ), last=True)
    if editor is None:
        fallback = await _first_visible(page, (
            "textarea[placeholder*='текст' i]",
            "textarea[placeholder*='истори' i]",
            "textarea[name='body']",
        ), last=True)
        if fallback is None:
            raise CommunityDraftError("В редакторе Пикабу не найдено поле текста.")
        await fallback.fill(body, timeout=12_000)
        return
    try:
        await editor.click()
        await editor.press("Control+A")
        lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for index, line in enumerate(lines):
            if line:
                await page.keyboard.insert_text(line)
            if index < len(lines) - 1:
                await page.keyboard.press("Enter")
        await page.wait_for_timeout(250)
    except PlaywrightTimeout as exc:
        raise CommunityDraftError("Редактор Пикабу не принял текст поста.") from exc


async def _pikabu_verify_selected_community(page, target_url: str, target_name: str) -> None:
    parsed = urlparse(target_url)
    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    link = page.locator(f"a[href='/community/{slug}'], a[href^='/community/{slug}/']")
    found = False
    for index in range(await link.count()):
        item = link.nth(index)
        try:
            if await item.is_visible() and target_name.casefold() in (await item.inner_text()).casefold():
                found = True
                break
        except Exception:
            continue
    if not found:
        # The editor may render the selected community as a destination chip.
        # The flow starts from that community's own URL; require its exact name
        # to remain visible after the editor transition.
        text = await page.locator("body").inner_text()
        found = len(target_name) >= 2 and target_name.casefold() in text.casefold()
    if not found:
        raise CommunityDraftError(
            "Пикабу не подтвердил выбранное сообщество в редакторе. Публикация остановлена."
        )


async def _pikabu_set_schedule(page, scheduled_at: datetime) -> None:
    label = page.locator("label[data-test='setting-scheduled']").first
    if await label.count():
        checkbox = label.locator("input[type='checkbox']").first
        if await checkbox.count() and not await checkbox.is_checked():
            await label.click()
    else:
        checkbox_text = page.get_by_text(re.compile(r"^отложенная публикация$", re.I)).last
        if not await checkbox_text.count() or not await checkbox_text.is_visible():
            raise CommunityDraftError("В редакторе Пикабу не найдена настройка отложенной публикации.")
        await checkbox_text.click()
    await page.wait_for_timeout(450)

    date_value = scheduled_at.strftime("%d.%m.%Y")
    date_input = await _first_visible(page, (
        "input[type='date']",
        "input[placeholder*='дата' i]",
        "input[aria-label*='дата' i]",
    ), last=True)
    if date_input is not None:
        input_type = (await date_input.get_attribute("type") or "").lower()
        await date_input.fill(scheduled_at.strftime("%Y-%m-%d") if input_type == "date" else date_value)
    else:
        day_select = page.locator("[class*='scheduled-story__day']").first
        if not await day_select.count() or not await day_select.is_visible():
            raise CommunityDraftError("Не найден календарь отложенной публикации Пикабу.")
        if date_value not in await day_select.inner_text():
            await day_select.click()
            options = page.locator("[class*='pkb-select-option__host']")
            selected = None
            for index in range(await options.count()):
                option = options.nth(index)
                if await option.is_visible() and date_value in (await option.inner_text()):
                    selected = option
                    break
            if selected is None:
                raise CommunityDraftError("Пикабу не предложил выбранную дату в пределах доступного горизонта.")
            await selected.click()

    hour_input = page.locator("input[class*='pkb-input-time__input_hour']").first
    minute_input = page.locator("input[class*='pkb-input-time__input_minute']").first
    if await hour_input.count() and await minute_input.count():
        await hour_input.fill(scheduled_at.strftime("%H"))
        await minute_input.fill(scheduled_at.strftime("%M"))
        await minute_input.press("Tab")
    else:
        time_input = await _first_visible(page, (
            "input[type='time']",
            "input[placeholder*='время' i]",
            "input[aria-label*='время' i]",
        ), last=True)
        if time_input is None:
            raise CommunityDraftError("Не найдено поле времени отложенной публикации Пикабу.")
        await time_input.fill(scheduled_at.strftime("%H:%M"))

    # Re-read the rendered values: a closed dropdown or a rejected time must
    # never be mistaken for an accepted schedule.
    visible_date = await day_select.inner_text() if 'day_select' in locals() and await day_select.count() else ""
    if date_value not in visible_date and date_input is None:
        raise CommunityDraftError("Редактор Пикабу не подтвердил выбранную дату.")
    if await hour_input.count() and await minute_input.count():
        if (
            (await hour_input.input_value()).zfill(2) != scheduled_at.strftime("%H")
            or (await minute_input.input_value()).zfill(2) != scheduled_at.strftime("%M")
        ):
            raise CommunityDraftError("Редактор Пикабу не подтвердил выбранное время.")


async def publish_pikabu_scheduled(
    post_public_id: str,
    *,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Create one native scheduled Pikabu post through the owner's saved browser session."""
    from playwright.async_api import async_playwright

    from tgfarm.browser_auth import _launch_context, browser_session_status
    from tgfarm.community_targets import (
        recommend_automatic_pikabu_target,
        revalidate_pikabu_target,
    )
    from tgfarm.core.settings import get_settings

    post_public_id = str(post_public_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", post_public_id):
        raise CommunityDraftError("Некорректный идентификатор поста Пикабу.")
    title = _clean_markup_text(title)
    body = _clean_markup_text(body)
    if not 2 <= len(title) <= 140:
        raise CommunityDraftError("Заголовок Пикабу должен содержать от 2 до 140 символов.")
    if not body or len(body) > 100_000:
        raise CommunityDraftError("Текст Пикабу пустой или превышает 100 000 символов.")
    override = {
        "public_id": post_public_id, "title": title, "body": body,
        "cta": "", "sources": [], "hashtags": [], "media": [],
    }
    selection = await recommend_automatic_pikabu_target(post_public_id, post_override=override)
    if not selection.get("ok"):
        return {"ok": False, "status": "skipped", "detail": selection.get("reason") or "Нет подходящего сообщества Пикабу."}
    target = selection["selected"]
    tags = list(selection.get("tags") or [])
    target_url = _safe_target_url("pikabu", str(target.get("url") or ""))
    target_name = str(target.get("name") or target.get("title") or "").strip()[:120]
    if not target_url or not target_name:
        raise CommunityDraftError("Не удалось подтвердить сообщество Пикабу.")
    selected_tags = normalize_pikabu_tags([*tags, PIKABU_AI_DISCLOSURE_TAG])

    content = f"{title}\n{body}".casefold()
    blocked_markers = {
        "политический материал": ("выборы", "президент", "политика", "государственная дума", "военная операция"),
        "небезопасный визуальный контент": ("кровь", "расчлен", "увеч", "труп", "жестокое насилие"),
        "18+ материал": ("эротика", "порнограф", "обнаженн", "секс"),
    }
    for label, markers in blocked_markers.items():
        if any(marker in content for marker in markers):
            raise CommunityDraftError(
                f"Публикация на Пикабу пропущена: для категории «{label}» нужна отдельная точная маркировка."
            )

    settings = get_settings()
    timezone_name = str(getattr(settings, "tz", "") or "Europe/Moscow")
    key = f"pikabu:{post_public_id}"
    fingerprint_base = _fingerprint("pikabu", title, body, [])
    fingerprint = hashlib.sha256(
        f"{fingerprint_base}\0{target_url}\0{json.dumps(selected_tags, ensure_ascii=False)}".encode("utf-8")
    ).hexdigest()
    state = _read_state()
    previous = state.get(key) if isinstance(state.get(key), dict) else {}
    if previous and previous.get("fingerprint") == fingerprint:
        status = str(previous.get("status") or "")
        if status in {"scheduled", "submitting", "unknown"}:
            return {
                "ok": status == "scheduled",
                "status": status,
                "scheduled_at": previous.get("scheduled_at"),
                "url": previous.get("draft_url"),
                "detail": "Публикация уже запланирована." if status == "scheduled" else
                    "Результат предыдущей отправки неоднозначен; повтор не выполнялся во избежание дубля.",
            }
    if previous and previous.get("status") == "scheduled":
        # A changed fingerprint is not a cancellation of the already queued
        # remote item. Keep the original record intact and require the owner
        # to cancel it on Pikabu before scheduling a revised version.
        raise CommunityDraftError(
            "Для этого материала уже есть отложенная публикация Пикабу. "
            "Сначала отмени прежнюю запись в очереди площадки; новую версию автоматически не дублирую."
        )
    if previous and previous.get("status") in {"submitting", "unknown"}:
        raise CommunityDraftError("Предыдущая отправка Пикабу имеет неоднозначный результат; повтор заблокирован.")

    # CommunityPolicy gate: the same contract the panel shows is re-assessed
    # here, right before the browser is touched. Unknown or stale rules mean no
    # publication — the pipeline keeps going with the other platforms instead.
    from tgfarm.community_policy import assess_policy, policy_for_plan, record_policy_snapshot

    policy = policy_for_plan("pikabu", {
        "selected": target,
        "rules_source": selection.get("rules_source") or "",
        "auto_publish_supported": selection.get("auto_publish_supported"),
    })
    verdict = assess_policy(policy) if policy is not None else {
        "status": "unknown",
        "allow_publish": False,
        "reason": "destination policy is missing",
        "blockers": ["rules_unavailable"],
    }
    # W29-B: the verdict above re-stamps checked_at as now, so it can never be
    # stale; the publish must additionally stand on the *stored* proof that the
    # rules were checked for this destination (a selector run or the maintenance
    # rules watch wrote it).  The proof is read before this publish records its
    # own snapshot, so the gate judges the existing history, not a row written
    # seconds ago by the very call that asks about it.
    from tgfarm.community_targets import check_destination_rules_proof

    rules_proof = await asyncio.to_thread(
        check_destination_rules_proof,
        "pikabu",
        target,
        rules_source=str(selection.get("rules_source") or ""),
    )
    if policy is not None:
        await asyncio.to_thread(record_policy_snapshot, policy, verdict)
    if not verdict.get("allow_publish"):
        return {
            "ok": False,
            "status": "skipped",
            "detail": (
                "Публикация в сообщество Пикабу пропущена: правила не подтверждены "
                f"({verdict.get('reason') or 'unknown'})."
            ),
            "policy_verdict": verdict,
        }
    if not rules_proof.get("publishable"):
        rejection = rules_proof.get("rejection") or {}
        return {
            "ok": False,
            "status": "skipped",
            "detail": (
                "Публикация в сообщество Пикабу пропущена: проверка правил не "
                "подтверждена сохранённым снимком "
                f"({rejection.get('reason_code') or 'unknown'}: "
                f"{rejection.get('reason') or 'нет подробности'})."
            ),
            "policy_verdict": verdict,
            "rules_proof": rules_proof,
            "rejected": [rejection] if rejection else [],
        }
    # Carried into every durable state below, so the queue file always says which
    # rules verdict the publisher acted on — and, after the final re-read, which
    # verdict actually authorised the click.
    policy_state = {
        "community_policy": policy.to_dict() if policy is not None else {},
        "policy_assessment": verdict,
    }
    lock = _LOCKS["pikabu"]
    if not lock.acquire(blocking=False):
        raise CommunityDraftError("Браузер Пикабу уже используется другой операцией.")
    try:
        session = browser_session_status("pikabu")
        if not session.get("verified") or not session.get("profile_saved"):
            raise CommunityDraftError("Сначала подключи и проверь авторизованный профиль Пикабу.")
        profile = DATA_DIR / "platform_profiles" / "pikabu"
        scheduled_at = plan_pikabu_schedule(
            now=datetime.now(UTC),
            entries=_read_state(),
            timezone_name=timezone_name,
        )
        # Store the claim before touching the browser. A crash after the final
        # button click must never cause an automatic second post.
        _write_state_entry(key, {
            "platform": "pikabu",
            "post_public_id": post_public_id,
            "fingerprint": fingerprint,
            "status": "preparing",
            "created_at": datetime.now(UTC).isoformat(),
            "scheduled_at": scheduled_at.isoformat(),
            "target_url": target_url,
            "target_name": target_name,
            "tags": selected_tags,
            **policy_state,
        })
        submitted = False
        context = None
        try:
            async with async_playwright() as playwright:
                context = await _launch_context(playwright, profile)
                pages = [page for page in context.pages if not page.is_closed()]
                page = pages[0] if pages else await context.new_page()
                existing_url = str(previous.get("draft_url") or "")
                if existing_url and _safe_editor_url("pikabu", existing_url):
                    await page.goto(existing_url, wait_until="domcontentloaded", timeout=60_000)
                else:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                    await _click_editor_entry(page, "pikabu")

                body_text = (await page.locator("body").inner_text()).casefold()
                if re.search(r"пожалуйста,\s*авторизуйтесь|для просмотра данной страницы|войдите в аккаунт", body_text):
                    raise CommunityDraftError("Сессия Пикабу закончилась. Повторно войди в браузере.")
                if re.search(r"captcha|пикапча|подтвердите,?\s*что вы не робот", body_text):
                    raise CommunityDraftError("Пикабу запросил проверку человека. Автоматическая отправка остановлена.")

                # If the selected community has an established draft, verify it
                # is still attached before proceeding. Otherwise the official
                # editor path must have preselected the community page.
                await _pikabu_verify_selected_community(page, target_url, target_name)
                title_locator = await _first_visible(page, (
                    "input[placeholder*='заголов' i]",
                    "textarea[placeholder*='заголов' i]",
                    "input[aria-label*='заголов' i]",
                    "textarea[aria-label*='заголов' i]",
                    "input[name='title']",
                ))
                if title_locator is None:
                    raise CommunityDraftError("Не найдено поле заголовка Пикабу.")
                await title_locator.fill(title, timeout=8_000)
                await _pikabu_fill_body(page, body)
                images = _image_candidates(_load_post(post_public_id).get("media", []), "pikabu")
                if images:
                    await _upload_images(page, "pikabu", images)

                await _pikabu_select_tags(page, selected_tags)
                # The selected community must remain visible after navigating
                # through the native multi-step editor.
                await _pikabu_verify_selected_community(page, target_url, target_name)
                for _ in range(3):
                    publish_action = page.get_by_role("button", name=re.compile(r"^опубликовать(?:\s+пост)?$", re.I))
                    if await publish_action.count() and await publish_action.last.is_visible():
                        break
                    next_button = page.get_by_role("button", name=re.compile(r"^продолжить$", re.I)).last
                    if not await next_button.count() or not await next_button.is_visible():
                        break
                    await next_button.click()
                    await page.wait_for_timeout(700)

                await _pikabu_set_schedule(page, scheduled_at)
                # The official delayed-publication control changes the action
                # to "Сохранить пост". Never click the immediate-publish action.
                save_button = page.get_by_role("button", name=re.compile(r"^сохранить\s+пост$", re.I)).last
                if not await save_button.count() or not await save_button.is_visible() or not await save_button.is_enabled():
                    raise CommunityDraftError("Пикабу не переключил действие на «Сохранить пост» после включения отложки.")

                # Last possible moment to ask the site: the editor has been
                # filled and the schedule set, and the next statement is the
                # click. Rules are re-read now, because everything before this
                # point was minutes of page work.
                latest = await revalidate_pikabu_target(override, target)
                latest_assessment = latest.get("policy_assessment") or {}
                policy_state = {
                    "community_policy": latest.get("community_policy") or {},
                    "policy_assessment": latest_assessment,
                }
                # The selection refused a community with blockers; the same
                # standard applies at the last moment, otherwise a rule that
                # appeared while the editor was open would be published against.
                new_blockers = [str(x) for x in (latest.get("blockers") or [])]
                refusal = "; ".join(new_blockers[:3]) or str(
                    latest_assessment.get("reason") or "правила изменились или недоступны"
                )
                if (
                    new_blockers
                    or not latest_assessment.get("allow_publish")
                    or not latest_assessment.get("auto_publish")
                ):
                    raise CommunityDraftError(
                        "Пикабу: финальная перечитка правил остановила отложенную публикацию. "
                        + refusal
                    )

                _write_state_entry(key, {
                    "platform": "pikabu",
                    "post_public_id": post_public_id,
                    "fingerprint": fingerprint,
                    "status": "submitting",
                    "created_at": datetime.now(UTC).isoformat(),
                    "scheduled_at": scheduled_at.isoformat(),
                    "target_url": target_url,
                    "target_name": target_name,
                    "tags": selected_tags,
                    "draft_url": page.url if _safe_editor_url("pikabu", page.url) else "",
                    **policy_state,
                })
                submitted = True
                await save_button.click(timeout=12_000)
                await page.wait_for_timeout(1_800)
                alert_text = ""
                for selector in ("[role='alert']", "[class*='toast']", "[class*='notification']"):
                    locator = page.locator(selector)
                    if await locator.count():
                        try:
                            alert_text += "\n" + "\n".join(await locator.all_inner_texts())
                        except Exception:
                            continue
                if re.search(r"не удалось|произошла ошибка|исправьте ошиб|ошибка публикации", alert_text, re.I):
                    submitted = False
                    raise CommunityDraftError("Пикабу отклонил отложенную публикацию.")
                success = bool(re.search(r"пост сохран[её]н|добавлен в отлож|отложенн(?:ый|ая) пост сохранён|публикация запланирована", alert_text, re.I))
                body_after = (await page.locator("body").inner_text()).casefold()
                success = success or ("отложенные посты" in body_after and "сохранить пост" not in body_after)
                if not success:
                    _write_state_entry(key, {
                        "platform": "pikabu",
                        "post_public_id": post_public_id,
                        "fingerprint": fingerprint,
                        "status": "unknown",
                        "created_at": datetime.now(UTC).isoformat(),
                        "scheduled_at": scheduled_at.isoformat(),
                        "target_url": target_url,
                        "target_name": target_name,
                        "tags": selected_tags,
                        "draft_url": page.url if _safe_editor_url("pikabu", page.url) else "",
                        **policy_state,
                    })
                    return {
                        "ok": False,
                        "status": "unknown",
                        "scheduled_at": scheduled_at.isoformat(),
                        "url": page.url if _safe_editor_url("pikabu", page.url) else None,
                        "detail": "Кнопка сохранения нажата, но Пикабу не показал подтверждение. Повтор заблокирован для защиты от дубля.",
                    }
                result_url = page.url if _safe_editor_url("pikabu", page.url) else target_url
        except Exception as exc:
            if submitted:
                _write_state_entry(key, {
                    "platform": "pikabu",
                    "post_public_id": post_public_id,
                    "fingerprint": fingerprint,
                    "status": "unknown",
                    "created_at": datetime.now(UTC).isoformat(),
                    "scheduled_at": scheduled_at.isoformat(),
                    "target_url": target_url,
                    "target_name": target_name,
                    "tags": selected_tags,
                    "draft_url": "",
                    **policy_state,
                })
                return {
                    "ok": False,
                    "status": "unknown",
                    "scheduled_at": scheduled_at.isoformat(),
                    "url": None,
                    "detail": f"После отправки возникла неоднозначная ошибка ({type(exc).__name__}); автоматический повтор заблокирован.",
                }
            _write_state_entry(key, {
                "platform": "pikabu",
                "post_public_id": post_public_id,
                "fingerprint": fingerprint,
                "status": "failed",
                "created_at": datetime.now(UTC).isoformat(),
                "scheduled_at": scheduled_at.isoformat(),
                "target_url": target_url,
                "target_name": target_name,
                "tags": selected_tags,
                "error": str(exc)[:300],
                **policy_state,
            })
            if isinstance(exc, CommunityDraftError):
                raise
            raise CommunityDraftError(f"Не удалось подготовить отложенную публикацию Пикабу ({type(exc).__name__}).") from exc
        finally:
            if context is not None:
                await context.close()

        _write_state_entry(key, {
            "platform": "pikabu",
            "post_public_id": post_public_id,
            "fingerprint": fingerprint,
            "status": "scheduled",
            "created_at": datetime.now(UTC).isoformat(),
            "scheduled_at": scheduled_at.isoformat(),
            "target_url": target_url,
            "target_name": target_name,
            "tags": selected_tags,
            "draft_url": result_url,
            **policy_state,
        })
        return {
            "ok": True,
            "status": "scheduled",
            "scheduled_at": scheduled_at.isoformat(),
            "url": result_url,
            "target_url": target_url,
            "target_name": target_name,
            "tags": selected_tags,
            "policy_assessment": policy_state.get("policy_assessment") or {},
            "detail": "Отложенная публикация сохранена в нативной очереди Пикабу после перечитки правил.",
        }
    finally:
        lock.release()

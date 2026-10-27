"""Publish to Dzen through its web editor.

Dzen has no public publishing API. This module drives the author dashboard in
a dedicated Chrome profile: the operator logs in once with `tgfarm dzen-login`,
the profile keeps the Yandex session, and every publish opens the editor in a
headed browser. Input is paced like a human's (variable per-character delay,
pauses between paragraphs and actions), because an instant `fill()` of a 2500
character article is both unnatural and fragile in a Draft.js editor.

The automation never tries to solve or bypass a captcha: if Dzen shows one it
stops and asks the operator to finish the login manually.
"""

from __future__ import annotations

import asyncio
import base64
import io
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings

log = get_logger(__name__)

EDITOR_URL = "https://dzen.ru/profile/editor/create"
PUBLIC_HOST = "dzen.ru"

# One persistent Chrome profile cannot be opened by two processes at once.
# Publishing, manual login and metrics collection all share this lock; a caller
# that must not queue (analytics) checks `locked()` before waiting.
DZEN_BROWSER_LOCK = asyncio.Lock()

# Last auth-probe result: the panel must not launch a browser on every
# refresh — it fights the publisher for the profile lock.
_auth_state_cache: dict | None = None
_auth_state_at: float = 0.0

CREATE_BUTTON = re.compile(r"Создать публикацию|Новая публикация|^\s*Создать\s*$", re.I)
ARTICLE_ITEM = re.compile(r"^\s*Статья\s*$|Написать статью|Создать статью", re.I)
POST_ITEM = re.compile(r"^\s*Пост\s*$|Написать пост|Создать пост", re.I)
TITLE_FIELD = re.compile(r"Заголовок|Название", re.I)
BODY_FIELD = re.compile(r"Начните писать|Текст статьи|Введите текст|Основной текст|Что нового|нового", re.I)
STUDIO_MARKER = re.compile(r"Публикации|Статистика|Настройки|Создать публикацию", re.I)
TITLE_SELECTOR = "div[class*='editor__titleInput'] [contenteditable='true']"
BODY_SELECTOR = "div[class*='zenDraftEditor'] [contenteditable='true']"
PUBLISH_SELECTOR = "[data-testid='article-publish-btn']"
PUBLISH_CONFIRM_SELECTOR = "[data-testid='publish-btn']"
PUBLICATION_TITLE_SELECTOR = "[data-testid='article-title-input']"
ADD_PUBLICATION_SELECTOR = "[data-testid='add-publication-button']"
PUBLISH_BUTTON = re.compile(r"^\s*Опубликовать(\s+статью)?\s*$|Опубликовать сейчас", re.I)
CAPTCHA_TEXT = re.compile(r"Подтвердите, что вы не робот|Я не робот|капч", re.I)
PUBLICATION_URL = re.compile(r"https://dzen\.ru/(?:a|media)/[^\s\"'<>]+")

# A Dzen "article" is the long-form surface; shorter pieces go out as a "post".
# The cut mirrors S10's article floor (1200 chars) so the choice tracks the
# generated format instead of a second, unrelated threshold.
DZEN_ARTICLE_MIN_CHARS = 1200


def dzen_publication_kind(text: str | None) -> str:
    """Return "article" for long texts and "post" for short ones."""
    if len((text or "").strip()) >= DZEN_ARTICLE_MIN_CHARS:
        return "article"
    return "post"



class DzenWebError(RuntimeError):
    """The editor could not be driven."""


class DzenNotLoggedIn(DzenWebError):
    """The persistent profile has no live Yandex session."""


class DzenCaptcha(DzenWebError):
    """Dzen asked for human confirmation; automation must stop here."""


@dataclass(slots=True)
class DzenPublishResult:
    status: str
    url: str | None = None
    remote_id: str | None = None
    error: str | None = None


def chunk_text(
    text: str, rng: random.Random, min_chars: int = 24, max_chars: int = 80
) -> list[str]:
    """Split text into variable chunks; joining them returns the input exactly.

    Typing the same chunk length forever is as regular as a metronome. Variable
    chunks let the per-character delay vary without changing the text.
    """
    if not text:
        return []
    if min_chars < 1 or max_chars < min_chars:
        raise ValueError("invalid chunk bounds")
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        size = rng.randint(min_chars, max_chars)
        chunks.append(text[cursor : cursor + size])
        cursor += size
    return chunks


def publication_url(text: str) -> str | None:
    """Find the public article URL in a page URL, an href or a page dump."""
    match = PUBLICATION_URL.search(text or "")
    return match.group(0) if match else None


def split_body_for_image(body: str, after: int = 2) -> tuple[str, str]:
    """Split assembled article copy so the lead photo sits after the intro.

    Dzen has no cover field; the photo belongs between the opening context and
    the rest of the article. Sources and hashtags stay below it.
    """
    paragraphs = [part for part in (body or "").split("\n\n") if part.strip()]
    if not paragraphs:
        return "", ""
    if len(paragraphs) == 1:
        return paragraphs[0], ""
    cut = min(max(1, after), len(paragraphs) - 1)
    return "\n\n".join(paragraphs[:cut]), "\n\n".join(paragraphs[cut:])


def clipboard_png(data: bytes) -> bytes:
    """Re-encode an image as PNG for the browser clipboard.

    Chromium on Windows rejects `image/jpeg` in `ClipboardItem` with a
    NotAllowedError; PNG is the format the Dzen Draft.js paste handler accepts.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()


class _Human:
    """Small, deliberately unremarkable mouse and keyboard pacing."""

    def __init__(self, page, delay_ms: int, rng: random.Random) -> None:
        self.page = page
        self.delay_ms = max(15, delay_ms)
        self.rng = rng

    async def pause(self, low: float = 0.35, high: float = 1.1) -> None:
        await asyncio.sleep(self.rng.uniform(low, high))

    async def click(self, locator) -> None:
        await locator.scroll_into_view_if_needed()
        box = await locator.bounding_box()
        if box:
            x = box["x"] + box["width"] * self.rng.uniform(0.35, 0.65)
            y = box["y"] + box["height"] * self.rng.uniform(0.35, 0.65)
            await self.page.mouse.move(x, y, steps=self.rng.randint(9, 22))
        await self.pause(0.25, 0.8)
        await locator.click()
        await self.pause(0.45, 1.2)

    async def type(self, locator, text: str, clear: bool = True) -> None:
        await locator.click()
        await self.pause(0.4, 1.0)
        if clear:
            await locator.press("Control+A")
            await locator.press("Delete")
            await self.pause(0.2, 0.5)
        await self.type_text(text)

    async def type_text(self, text: str) -> None:
        paragraphs = text.split("\n\n")
        for index, paragraph in enumerate(paragraphs):
            if index:
                await self.page.keyboard.press("Enter")
                await self.pause(0.5, 1.3)
            for chunk in chunk_text(paragraph, self.rng):
                delay = max(15, int(self.rng.gauss(self.delay_ms, self.delay_ms * 0.35)))
                await self.page.keyboard.type(chunk, delay=delay)
                if chunk.rstrip().endswith((".", "!", "?", "...", ":")):
                    await self.pause(0.25, 0.9)


class DzenWebPublisher:
    """Drive dzen.ru/profile/editor in a persistent Chrome profile."""

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        *,
        browser: str | None = None,
        headless: bool | None = None,
        human_delay_ms: int | None = None,
        rng: random.Random | None = None,
    ) -> None:
        settings = get_settings()
        self.profile_dir = (
            Path(profile_dir) if profile_dir else settings.dzen_profile_path()
        )
        self.browser = (browser or settings.dzen_browser).strip().lower()
        self.headless = settings.dzen_headless if headless is None else headless
        self.human_delay_ms = (
            settings.dzen_human_delay_ms if human_delay_ms is None else human_delay_ms
        )
        self.publish_timeout_s = settings.dzen_publish_timeout_s
        self.login_timeout_s = settings.dzen_login_timeout_s
        self.rng = rng or random.Random()
        self._human: _Human | None = None

    # ------------------------------------------------------------------ public
    def profile_ready(self) -> bool:
        try:
            return any(self.profile_dir.iterdir())
        except OSError:
            return False

    async def auth_check(self, timeout_s: int = 25) -> dict:
        """Headless probe: is the Yandex session in the profile still alive?

        Sessions expire quietly; the first symptom used to be a phone-number
        form flashing during a publish. `authorized=None` means "could not
        check" (browser busy or playwright trouble) — never a false alarm.
        """
        from datetime import UTC, datetime

        from playwright.async_api import async_playwright

        global _auth_state_cache, _auth_state_at
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            await asyncio.wait_for(DZEN_BROWSER_LOCK.acquire(), timeout=10)
        except TimeoutError:
            return {"authorized": None, "busy": True, "checked_at": checked_at}
        try:
            async with async_playwright() as playwright:
                context = await asyncio.wait_for(self._launch(playwright), timeout=15)
                try:
                    page = await asyncio.wait_for(self._first_page(context), timeout=10)
                    await asyncio.wait_for(self._goto_editor(page), timeout=15)
                    authorized = bool(await self._is_logged_in(page))
                    _auth_state_cache = {
                        "authorized": authorized,
                        "busy": False,
                        "checked_at": checked_at,
                    }
                    _auth_state_at = time.monotonic()
                    return dict(_auth_state_cache)
                finally:
                    await context.close()
        except Exception as exc:  # noqa: BLE001 - a probe reports, not raises
            return {"authorized": None, "busy": False, "error": str(exc)[:180], "checked_at": checked_at}
        finally:
            DZEN_BROWSER_LOCK.release()

    async def cached_status(self, max_age_s: int = 600) -> dict:
        """Panel-facing status: fresh cache when available, probe when stale."""
        if _auth_state_cache is not None and (time.monotonic() - _auth_state_at) < max_age_s:
            return dict(_auth_state_cache)
        return await self.auth_check()

    async def login(self, timeout_s: int | None = None) -> dict:
        """Open the editor headed and wait until the operator has logged in."""
        async with DZEN_BROWSER_LOCK:
            return await self._login(timeout_s)

    async def _login(self, timeout_s: int | None = None) -> dict:
        from playwright.async_api import async_playwright

        timeout = self.login_timeout_s if timeout_s is None else timeout_s
        async with async_playwright() as playwright:
            context = await self._launch(playwright)
            try:
                page = await self._first_page(context)
                await self._goto_editor(page)
                if await self._is_logged_in(page):
                    return await self._login_success(page)

                log.warning(
                    "войдите в Яндекс в открытом окне; профиль сохранится в %s",
                    self.profile_dir,
                )
                deadline = time.monotonic() + max(30, timeout)
                while time.monotonic() < deadline:
                    await asyncio.sleep(2.0)
                    for candidate in context.pages:
                        if candidate.is_closed():
                            continue
                        if await self._is_logged_in(candidate):
                            await asyncio.sleep(1.0)
                            return await self._login_success(candidate)
                await self._save_debug(page, "dzen-login-timeout")
                raise DzenNotLoggedIn(
                    "сессия Яндекса не появилась за отведённое время; "
                    "запустите `tgfarm dzen-login` ещё раз"
                )
            finally:
                await context.close()

    async def publish(
        self,
        title: str,
        body: str,
        cover_path: str | Path | None = None,
        inline_image: str | Path | None = None,
        inline_caption: str | None = None,
        inline_images: list[tuple[str | Path, str | None]] | None = None,
    ) -> DzenPublishResult:
        """Create and publish an article, returning its public URL."""
        if inline_images is None and inline_image is not None:
            inline_images = [(inline_image, inline_caption)]
        async with DZEN_BROWSER_LOCK:
            return await self._publish(title, body, cover_path, inline_images)

    async def _publish(
        self,
        title: str,
        body: str,
        cover_path: str | Path | None = None,
        inline_images: list[tuple[str | Path, str | None]] | None = None,
    ) -> DzenPublishResult:
        from playwright.async_api import async_playwright

        title = (title or "").strip()[:200]
        body = (body or "").strip()
        if not title:
            raise DzenWebError("пустой заголовок")
        if not body:
            raise DzenWebError("пустой текст статьи")
        images = [(Path(p), c) for p, c in (inline_images or []) if p and Path(p).exists()]
        if not images and not cover_path:
            raise DzenWebError("нужно хотя бы одно изображение")

        async with async_playwright() as playwright:
            context = await self._launch(playwright)
            try:
                page = await self._first_page(context)
                self._human = _Human(page, self.human_delay_ms, self.rng)
                try:
                    await context.grant_permissions(
                        ["clipboard-read", "clipboard-write"], origin="https://dzen.ru"
                    )
                except Exception as exc:  # noqa: BLE001 - older Chromium builds
                    log.debug("clipboard permission unavailable: %s", exc)
                await self._goto_editor(page)
                if not await self._is_logged_in(page):
                    await self._save_debug(page, "dzen-not-logged-in")
                    raise DzenNotLoggedIn(
                        "нет сессии Дзена — один раз выполните `tgfarm dzen-login`"
                    )

                kind = dzen_publication_kind(body)
                log.info("dzen: публикация как %s (%d симв.)", kind, len(body or ""))
                await self._open_editor(page, kind)
                if await self._find_title(page) is not None:
                    await self._fill_title(page, title)
                images = [(Path(p), c) for p, c in (inline_images or []) if p and Path(p).exists()]
                inline_ok = False
                if images:
                    try:
                        paths = [p for p, _ in images]
                        captions = [c for _, c in images]
                        if len(paths) == 1:
                            await self._fill_body_with_image(
                                page, body, paths[0], captions[0], kind
                            )
                        else:
                            await self._fill_body_with_images(
                                page, body, paths, captions, kind
                            )
                        inline_ok = True
                    except Exception as exc:  # noqa: BLE001 - text is still publishable
                        log.warning("image insert failed (%s); publishing text only", exc)
                        await self._fill_body(page, body)
                else:
                    await self._fill_body(page, body)
                if cover_path and not inline_ok:
                    await self._upload_cover(page, Path(cover_path))

                await self._click_publish(page, title)
                url = await self._wait_published(page)
                await self._save_debug(page, "dzen-published")
                return DzenPublishResult(
                    status="posted",
                    url=url,
                    remote_id=self._remote_id(url),
                )
            except DzenWebError:
                page = await self._first_page(context)
                await self._save_debug(page, "dzen-failed")
                raise
            except Exception as exc:  # noqa: BLE001 - add context and artifacts
                page = await self._first_page(context)
                await self._save_debug(page, "dzen-failed")
                raise DzenWebError(f"редактор Дзена: {exc}") from exc
            finally:
                self._human = None
                await context.close()

    # ----------------------------------------------------------------- browser
    async def _launch(self, playwright):
        kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "no_viewport": True,
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "accept_downloads": False,
            "args": ["--no-first-run", "--no-default-browser-check", "--start-maximized"],
        }
        if self.browser in ("chrome", "msedge"):
            try:
                return await playwright.chromium.launch_persistent_context(
                    channel=self.browser, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - fall back only if absent
                if "not found" not in str(exc).lower():
                    raise
                log.warning(
                    "%s не найден, использую встроенный Chromium: %s",
                    self.browser,
                    exc,
                )
        try:
            return await playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 - actionable message
            raise DzenWebError(
                "не удалось запустить браузер; установите Chrome или выполните "
                f"`playwright install chromium` ({exc})"
            ) from exc

    async def _first_page(self, context):
        pages = [page for page in context.pages if not page.is_closed()]
        return pages[0] if pages else await context.new_page()

    async def _goto_editor(self, page) -> None:
        await page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(self.rng.uniform(1.2, 2.4))
        await self._wait_out_captcha(page)

    async def _is_logged_in(self, page) -> bool:
        if page.is_closed():
            return False
        url = page.url.lower()
        if "passport.yandex" in url or "oauth.yandex" in url:
            return False
        if "showcaptcha" in url:
            return False
        if "dzen.ru/profile/editor" not in url:
            return False
        try:
            if "не найдена" in (await page.title()).lower():
                return False
            if await self._find_title(page) is not None:
                return True
            if await page.get_by_role("button", name=CREATE_BUTTON).count():
                return True
            return await self._find_visible([page.get_by_text(STUDIO_MARKER)]) is not None
        except Exception:  # noqa: BLE001 - navigation in progress
            return False

    async def _login_success(self, page) -> dict:
        await page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(1.0)
        screenshot = await self._save_debug(page, "dzen-login")
        return {
            "ok": True,
            "profile": str(self.profile_dir),
            "url": page.url,
            "screenshot": str(screenshot),
        }

    # --------------------------------------------------------------- selectors
    async def _find_visible(self, candidates: list):
        for locator in candidates:
            try:
                count = await locator.count()
            except Exception:  # noqa: BLE001 - detached locator
                continue
            for index in range(min(count, 4)):
                item = locator.nth(index)
                try:
                    if await item.is_visible():
                        return item
                except Exception:  # noqa: BLE001 - detached node
                    continue
        return None

    async def _find_title(self, page):
        return await self._find_visible(
            [
                page.locator(TITLE_SELECTOR),
                page.get_by_placeholder(TITLE_FIELD),
                page.get_by_role("textbox", name=TITLE_FIELD),
                page.locator(
                    "input[placeholder*='аголов'], textarea[placeholder*='аголов'], "
                    "[contenteditable='true'][data-placeholder*='аголов']"
                ),
                page.locator("[data-testid*='title'] input, [data-testid*='title'] textarea"),
            ]
        )

    async def _find_body(self, page):
        named = await self._find_visible(
            [
                page.locator(BODY_SELECTOR),
                page.locator("[aria-describedby='placeholder-ZenDraftEditor']"),
                page.locator(".ProseMirror[contenteditable='true']"),
                page.get_by_role("textbox", name=BODY_FIELD),
                page.locator(
                    "[contenteditable='true'][data-placeholder*='ачните'], "
                    "[contenteditable='true'][data-placeholder*='Что'], "
                    "[contenteditable='true'][data-placeholder*='нового'], "
                    "[contenteditable='true'][aria-label*='екст']"
                ),
            ]
        )
        if named is not None:
            return named

        # Last resort: the largest visible contenteditable, which is the article
        # canvas rather than the title or a one-line field.
        locator = page.locator("[contenteditable='true']")
        best = None
        best_area = 0.0
        try:
            count = await locator.count()
        except Exception:  # noqa: BLE001
            return None
        for index in range(min(count, 8)):
            item = locator.nth(index)
            try:
                if not await item.is_visible():
                    continue
                placeholder = (await item.get_attribute("data-placeholder")) or ""
                if "Что" in placeholder or "нового" in placeholder:
                    return item
                box = await item.bounding_box()
            except Exception:  # noqa: BLE001
                continue
            if box and box["width"] * box["height"] > best_area:
                best, best_area = item, box["width"] * box["height"]
        return best

    # ------------------------------------------------------------------- flow
    async def _open_editor(self, page, kind: str = "article") -> None:
        if await self._find_title(page) is not None:
            await self._dismiss_help(page)
            return

        await self._dismiss_overlays(page)
        create = await self._find_visible(
            [
                page.locator(ADD_PUBLICATION_SELECTOR),
                page.get_by_role("button", name=CREATE_BUTTON),
                page.get_by_role("link", name=CREATE_BUTTON),
                page.get_by_text(CREATE_BUTTON, exact=False),
            ]
        )
        if create is None:
            raise DzenWebError(
                "не нашёл кнопку создания публикации; профиль вошёл в другой "
                "аккаунт или Дзен изменил интерфейс"
            )
        await self._human.click(create)
        await self._wait_out_captcha(page)

        item_re = ARTICLE_ITEM if kind == "article" else POST_ITEM
        item = await self._find_visible(
            [
                page.get_by_role("menuitem", name=item_re),
                page.get_by_role("button", name=item_re),
                page.get_by_role("link", name=item_re),
                page.get_by_text(item_re, exact=True),
            ]
        )
        if item is None:
            # Fall back to the article surface if Dzen does not expose a post entry.
            item = await self._find_visible(
                [
                    page.get_by_role("menuitem", name=ARTICLE_ITEM),
                    page.get_by_role("button", name=ARTICLE_ITEM),
                    page.get_by_role("link", name=ARTICLE_ITEM),
                    page.get_by_text(ARTICLE_ITEM, exact=True),
                ]
            )
            if item is None:
                raise DzenWebError("в меню создания нет пункта «Статья»")
        await self._human.click(item)
        await asyncio.sleep(self.rng.uniform(2.0, 3.0))
        await self._wait_out_captcha(page)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if await self._find_title(page) is not None or await self._find_body(page) is not None:
                await self._dismiss_help(page)
                return
            await asyncio.sleep(0.5)
        raise DzenWebError("редактор публикации не открылся")

    async def _dismiss_help(self, page) -> None:
        """Close the first-run help modal; its overlay blocks editor clicks."""
        try:
            dialog = page.get_by_role("dialog").first
            if not await dialog.is_visible():
                return
            await page.keyboard.press("Escape")
            await asyncio.sleep(self.rng.uniform(0.6, 1.2))
            if await dialog.is_visible():
                close = await self._find_visible(
                    [
                        dialog.locator("[class*='closeCross']"),
                        dialog.get_by_role("button", name=re.compile("Закрыть|закрыть")),
                    ]
                )
                if close is not None:
                    await close.click()
                    await asyncio.sleep(self.rng.uniform(0.4, 0.9))
        except Exception:  # noqa: BLE001 - the help modal is optional
            pass

    async def _dismiss_overlays(self, page) -> None:
        """Close promo dialogs that cover the Studio header (donations, etc.)."""
        try:
            dialogs = page.get_by_role("dialog")
            for index in range(min(await dialogs.count(), 3)):
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                close = await self._find_visible(
                    [
                        dialog.locator("[data-testid='close-button']"),
                        dialog.get_by_role("button", name=re.compile("Закрыть|закрыть")),
                    ]
                )
                if close is not None:
                    await close.click()
                    await asyncio.sleep(self.rng.uniform(0.6, 1.2))
        except Exception:  # noqa: BLE001 - overlays are optional
            pass

    async def _fill_title(self, page, title: str) -> None:
        field = await self._find_title(page)
        if field is None:
            raise DzenWebError("не нашёл поле заголовка")
        await self._human.type(field, title)

    async def _fill_body(self, page, body: str) -> None:
        field = await self._find_body(page)
        if field is None:
            raise DzenWebError("не нашёл поле текста статьи")
        await self._human.type(field, body)

    async def _fill_body_with_image(
        self,
        page,
        body: str,
        image: Path,
        caption: str | None = None,
        kind: str = "article",
    ) -> None:
        """Type the intro, paste the lead photo at the caret, then continue.

        Draft.js accepts an image from the clipboard and uploads it as a native
        image block. That avoids depending on a toolbar button whose markup can
        change, and keeps the photo at the exact paragraph where it belongs.
        """
        field = await self._find_body(page)
        if field is None:
            raise DzenWebError("?? ????? ???? ?????? ??????")
        first, rest = split_body_for_image(body)
        await self._human.type(field, first)
        if rest:
            await page.keyboard.press("Enter")
            await self._human.pause(0.5, 1.2)
        if kind == "post":
            await self._upload_inline_image(page, image)
        else:
            await self._paste_image(page, image)
        if rest:
            await page.keyboard.press("Control+End")
            await self._human.pause(0.4, 0.9)
            await page.keyboard.press("Enter")
            await self._human.pause(0.5, 1.2)
            await self._human.type_text(rest)
        if caption:
            await self._fill_image_caption(page, caption)

    async def _fill_body_with_images(
        self,
        page,
        body: str,
        images: list[Path],
        captions: list[str | None] | None = None,
        kind: str = "article",
    ) -> None:
        captions = captions or [None] * len(images)
        await self._fill_body_with_image(page, body, images[0], captions[0], kind)
        for image, caption in zip(images[1:], captions[1:]):
            await page.keyboard.press("Control+End")
            await self._human.pause(0.4, 0.9)
            await page.keyboard.press("Enter")
            await self._human.pause(0.5, 1.2)
            if kind == "post":
                await self._upload_inline_image(page, image)
            else:
                await self._paste_image(page, image)
            if caption:
                await self._fill_image_caption(page, caption)

    async def _fill_image_caption(self, page, caption: str) -> None:
        try:
            box = page.locator(
                "figure textarea, div[class*='image-caption'] textarea"
            ).last
            if await box.count() and await box.is_visible():
                await box.fill(caption[:2000])
                await self._human.pause(0.3, 0.7)
        except Exception as exc:  # noqa: BLE001 - caption is optional
            log.debug("image caption failed: %s", exc)

    async def _paste_image(self, page, image: Path) -> None:
        data = image.read_bytes()
        if not data:
            raise DzenWebError("пустой файл изображения")
        payload = base64.b64encode(clipboard_png(data)).decode("ascii")
        before = await page.locator("img").count()
        await page.evaluate(
            """async ({b64}) => {
              const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
              const blob = new Blob([bytes], {type: 'image/png'});
              await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
            }""",
            {"b64": payload},
        )
        await self._human.pause(0.4, 0.9)
        await page.keyboard.press("Control+V")
        await asyncio.sleep(self.rng.uniform(4.0, 7.0))
        try:
            await page.wait_for_function(
                "(count) => document.querySelectorAll('img').length > count",
                arg=before,
                timeout=30_000,
            )
        except Exception as exc:  # noqa: BLE001 - upload failed, caller falls back
            raise DzenWebError(f"изображение не появилось в статье: {exc}") from exc

    async def _upload_inline_image(self, page, image: Path) -> None:
        # The inline image control is the paperclip icon button in the brief editor toolbar.
        locator = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                buttons = page.locator("button")
                count = await buttons.count()
                for idx in range(count):
                    html = await buttons.nth(idx).inner_html()
                    if "paperclip" in (html or "").lower():
                        candidate = buttons.nth(idx)
                        if await candidate.is_visible():
                            locator = candidate
                            break
                if locator is not None:
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)

        if locator is None:
            # Fall back to a visible file input, if the site exposes one.
            file_input = page.locator("input[type='file']").first
            if await file_input.count():
                await file_input.set_files(str(image))
                await asyncio.sleep(self.rng.uniform(4.0, 7.0))
                return
            raise RuntimeError("inline image upload control not found")

        async with page.expect_file_chooser(timeout=15_000) as chooser_info:
            await locator.click()
        chooser = await chooser_info.value
        await chooser.set_files(str(image))
        await asyncio.sleep(self.rng.uniform(5.0, 8.0))


    async def _upload_cover(self, page, cover: Path) -> None:
        if not cover.exists():
            log.warning("обложка не найдена: %s", cover)
            return
        try:
            button = await self._find_visible(
                [
                    page.get_by_role("button", name=re.compile("обложк", re.I)),
                    page.get_by_text(re.compile("Добавить обложку|Загрузить обложку", re.I)),
                ]
            )
            if button is not None:
                async with page.expect_file_chooser(timeout=10_000) as chooser_info:
                    await self._human.click(button)
                chooser = await chooser_info.value
                await chooser.set_files(str(cover))
            else:
                inputs = page.locator("input[type='file']")
                if not await inputs.count():
                    log.warning("не нашёл загрузку обложки; публикую без неё")
                    return
                await inputs.first.set_input_files(str(cover))
            await asyncio.sleep(self.rng.uniform(3.0, 5.0))
        except Exception as exc:  # noqa: BLE001 - cover is optional
            log.warning("обложка не загрузилась (%s); продолжаю без неё", exc)

    async def _click_publish(self, page, title: str) -> None:
        button = await self._find_visible(
            [
                page.locator(PUBLISH_SELECTOR),
                page.get_by_role("button", name=PUBLISH_BUTTON),
                page.get_by_text(PUBLISH_BUTTON, exact=True),
            ]
        )
        if button is None:
            raise DzenWebError("не нашёл кнопку «Опубликовать»")
        await self._human.click(button)
        confirm = page.locator(PUBLISH_CONFIRM_SELECTOR)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if await confirm.count() and await confirm.first.is_visible():
                break
            await asyncio.sleep(0.4)
        if not await confirm.count():
            # Some editor versions publish from the header without a second step.
            await self._wait_out_captcha(page)
            return

        title_input = page.locator(PUBLICATION_TITLE_SELECTOR)
        if await title_input.count() and await title_input.first.is_visible():
            if not (await title_input.first.input_value()).strip():
                await self._human.type(title_input.first, title)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if await confirm.first.is_enabled():
                break
            await asyncio.sleep(0.4)
        if not await confirm.first.is_enabled():
            raise DzenWebError("кнопка публикации осталась неактивной")
        await self._human.click(confirm.first)
        await asyncio.sleep(self.rng.uniform(2.0, 3.5))
        await self._wait_out_captcha(page)

    async def _wait_published(self, page) -> str:
        deadline = time.monotonic() + max(30, self.publish_timeout_s)
        while time.monotonic() < deadline:
            await self._wait_out_captcha(page)
            url = publication_url(page.url)
            if url:
                return url
            try:
                hrefs = await page.locator(
                    "a[href*='dzen.ru/a/'], a[href*='/a/'], a[href*='/media/']"
                ).all()
                for link in hrefs:
                    href = await link.get_attribute("href")
                    found = publication_url(href or "")
                    if found:
                        return found
                published_text = await page.get_by_text(re.compile("Опубликовано|Публикация опубликована")).count()
                if published_text:
                    await asyncio.sleep(1.5)
                    # Publication succeeded even if Dzen does not expose a stable
                    # per-article URL on the publications list page.
                    if "state=published" in (page.url or ""):
                        return None
                    url = publication_url(page.url)
                    if url:
                        return url
            except Exception as exc:  # noqa: BLE001 - page is navigating
                pass
            await asyncio.sleep(1.0)
        raise DzenWebError("не дождался подтверждения публикации")

    # ------------------------------------------------------------------ debug
    async def _captcha_on_page(self, page) -> bool:
        try:
            if "showcaptcha" in page.url.lower():
                return True
            captcha = page.get_by_text(CAPTCHA_TEXT)
            return bool(await captcha.count())
        except Exception:  # noqa: BLE001 - a closed page is not a captcha
            return False

    async def _wait_out_captcha(self, page) -> None:
        """Wait for the operator to solve the "I'm not a robot" box.

        The window is headed, so the human sees the checkbox; the farm polls
        until it disappears. Solving it never happens in code — only a human
        click, then the publish continues where it stopped.
        """
        if not await self._captcha_on_page(page):
            return
        wait_s = max(5, int(get_settings().dzen_captcha_wait_s))
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001 - focus is a courtesy, not a requirement
            pass
        log.warning(
            "дзен показал капчу — нажми «Я не робот» в окне фермы; жду %s с",
            wait_s,
        )
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(3.0)
            if not await self._captcha_on_page(page):
                log.info("капча решена — продолжаю публикацию")
                return
        raise DzenCaptcha(
            "капча не решена за отведённое время — повтор через час"
        )

    async def _assert_no_captcha(self, page) -> None:
        if "showcaptcha" in page.url.lower():
            raise DzenCaptcha(
                "Дзен показал капчу; откройте `tgfarm dzen-login` и пройдите её вручную"
            )
        try:
            captcha = page.get_by_text(CAPTCHA_TEXT)
            for index in range(min(await captcha.count(), 4)):
                if await captcha.nth(index).is_visible():
                    raise DzenCaptcha(
                        "Дзен показал капчу; откройте `tgfarm dzen-login` "
                        "и пройдите её вручную"
                    )
        except DzenCaptcha:
            raise
        except Exception:  # noqa: BLE001 - text probe is best-effort
            pass

    async def _save_debug(self, page, name: str) -> Path:
        root = get_settings().data_root()
        png = root / f"{name}.png"
        html = root / f"{name}.html"
        try:
            await page.screenshot(path=str(png), full_page=True)
            html.write_text(await page.content(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - debug must not mask the error
            log.debug("не удалось сохранить %s: %s", name, exc)
        return png

    @staticmethod
    def _remote_id(url: str | None) -> str | None:
        if not url:
            return None
        return url.rstrip("/").rsplit("/", 1)[-1] or None


__all__ = [
    "DzenCaptcha",
    "DzenNotLoggedIn",
    "DzenPublishResult",
    "DzenWebError",
    "DzenWebPublisher",
    "chunk_text",
    "dzen_publication_kind",
    "publication_url",
]

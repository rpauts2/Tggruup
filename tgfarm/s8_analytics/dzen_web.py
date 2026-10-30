"""Read Dzen Studio publication metrics into the shared analytics store.

Dzen has no analytics API either. The authenticated Studio page renders a
normal HTML table, so a headless Chrome session in the same profile as the
publisher is enough. This module deliberately keeps the browser work separate
from the pure parsing/recording helpers: the latter are unit-tested without
launching a browser.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.core.security import redact_sensitive_text
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import ChannelStat, CrosspostLog, PostStat
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

log = get_logger(__name__)

EDITOR_ENTRY = "https://dzen.ru/profile/editor/create"
PUBLISHER_ID_RE = re.compile(r"/profile/editor/id/([0-9a-f]+)/")
PUBLICATION_ID_RE = re.compile(r"dzen\.ru/(?:a|b)/([A-Za-z0-9_-]+)")
MEDIA_ID_RE = re.compile(r"/media/[^/]+/([A-Za-z0-9_-]+)")
SUBSCRIBERS_RE = re.compile(r"(\d[\d\s\u00a0\u202f]*)\s+подписчик", re.I)
STATE_FILE = "dzen_stats_state.json"


class DzenStatsError(RuntimeError):
    """The Studio page could not be read."""


class DzenStatsNotLoggedIn(DzenStatsError):
    """The persistent profile has no live Yandex session."""


@dataclass(frozen=True, slots=True)
class DzenPublicationMetrics:
    url: str
    remote_id: str | None
    title: str = ""
    impressions: int = 0
    opens: int = 0
    views: int = 0
    reactions: int = 0
    comments: int = 0
    forwards: int = 0

    @property
    def ctr(self) -> float:
        return round(min(1.0, self.opens / self.impressions), 4) if self.impressions else 0.0

    @property
    def read_rate(self) -> float:
        return round(min(1.0, self.views / self.opens), 4) if self.opens else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "remote_id": self.remote_id,
            "title": self.title,
            "impressions": self.impressions,
            "opens": self.opens,
            "views": self.views,
            "reactions": self.reactions,
            "comments": self.comments,
            "forwards": self.forwards,
            "ctr": self.ctr,
            "read_rate": self.read_rate,
        }


def _to_int(value: Any) -> int:
    """Parse Dzen's human-formatted counters (`1 234`, `1,2 тыс.`, `—`)."""
    text = str(value or "").replace("\u00a0", " ").replace("\u202f", " ").strip().lower()
    if not text or text in {"-", "—", "–", "считаем", "нет данных"}:
        return 0
    compact = re.sub(r"\s+", "", text)
    multiplier = 1
    for suffix, factor in (("млрд", 1_000_000_000), ("млн", 1_000_000), ("тыс", 1_000)):
        if suffix in compact:
            multiplier = factor
            break
    match = re.search(r"-?\d+(?:[.,]\d+)?", compact)
    if not match:
        return 0
    number = float(match.group(0).replace(",", "."))
    return int(round(number * multiplier))


def publication_id(url: str | None) -> str | None:
    value = str(url or "").strip()
    match = PUBLICATION_ID_RE.search(value)
    if match:
        return match.group(1)
    match = MEDIA_ID_RE.search(value)
    return match.group(1) if match else None


def parse_table_rows(rows: list[dict[str, Any]]) -> list[DzenPublicationMetrics]:
    """Parse the JSON-shaped rows extracted by `_read_rows` from the DOM.

    Column order follows the Studio table:
    4 impressions, 5 openings, 6 reads/views, 9 comments, 10 likes, 11 shares.
    """
    out: list[DzenPublicationMetrics] = []
    seen: set[str] = set()
    for row in rows:
        href = str(row.get("href") or "").strip()
        remote_id = publication_id(href)
        if not href or not remote_id or remote_id in seen:
            continue
        cells = [str(cell or "") for cell in (row.get("cells") or [])]
        if len(cells) < 12:
            continue
        seen.add(remote_id)
        out.append(
            DzenPublicationMetrics(
                url=href.split("?", 1)[0],
                remote_id=remote_id,
                title=cells[2] or str(row.get("title") or "")[:200],
                impressions=_to_int(cells[4]),
                opens=_to_int(cells[5]),
                views=_to_int(cells[6]),
                comments=_to_int(cells[9]),
                reactions=_to_int(cells[10]),
                forwards=_to_int(cells[11]),
            )
        )
    return out


async def _read_rows(page) -> list[dict[str, Any]]:
    tables = page.locator("table")
    for index in range(await tables.count()):
        rows = await tables.nth(index).evaluate(
            """table => Array.from(table.querySelectorAll('tr'))
                .map(tr => {
                    const cells = Array.from(tr.querySelectorAll('td'))
                        .map(td => (td.innerText || '').replace(/\\s+/g, ' ').trim());
                    const link = tr.querySelector(
                        "a[href*='/a/'], a[href*='/b/'], a[href*='/media/']"
                    );
                    return {
                        href: link ? link.href : null,
                        title: link ? (link.innerText || '').trim() : '',
                        cells,
                    };
                })"""
        )
        if any(row.get("href") and len(row.get("cells") or []) >= 12 for row in rows):
            return rows
    raise DzenStatsError("таблица публикаций не найдена на странице статистики")


async def _discover_publisher_id(page, timeout_s: int) -> str:
    configured = (get_settings().dzen_publisher_id or "").strip()
    if configured:
        return configured
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hrefs = await page.locator("a[href*='/publications-stat']").evaluate_all(
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        for href in hrefs:
            match = PUBLISHER_ID_RE.search(str(href))
            if match:
                return match.group(1)
        if "passport.yandex" in page.url or "oauth.yandex" in page.url:
            raise DzenStatsNotLoggedIn("сессия Дзена истекла - выполните `tgfarm dzen-login`")
        await asyncio.sleep(1.0)
    raise DzenStatsNotLoggedIn(
        "не удалось определить id издателя; задайте DZEN_PUBLISHER_ID в .env"
    )


async def _save_debug(page, name: str) -> Path | None:
    try:
        path = get_settings().data_root() / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        return path
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real error
        log.debug("could not save Dzen stats screenshot: %s", redact_sensitive_text(exc, limit=160))
        return None


async def collect_publication_metrics(
    *, publisher_id: str | None = None, timeout_s: int | None = None
) -> tuple[list[DzenPublicationMetrics], int | None]:
    """Open Studio and return publication rows plus the channel subscriber count."""
    from playwright.async_api import async_playwright

    settings = get_settings()
    profile = settings.dzen_profile_path()
    if not any(profile.iterdir()):
        raise DzenStatsNotLoggedIn(
            f"профиль Dzen пуст: {profile}; выполните `tgfarm dzen-login`"
        )
    limit = max(15, int(timeout_s or settings.dzen_stats_timeout_s))
    browser = (settings.dzen_browser or "chrome").strip().lower()

    async with async_playwright() as playwright:
        kwargs = {
            "user_data_dir": str(profile),
            "headless": True,
            "viewport": {"width": 1440, "height": 1000},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "args": ["--no-first-run", "--no-default-browser-check"],
        }
        context = await playwright.chromium.launch_persistent_context(
            channel=browser, **kwargs
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(EDITOR_ENTRY, wait_until="domcontentloaded", timeout=limit * 1000)
            await page.wait_for_timeout(2500)
            if "passport.yandex" in page.url or "oauth.yandex" in page.url:
                raise DzenStatsNotLoggedIn(
                    "сессия Дзена истекла - выполните `tgfarm dzen-login`"
                )
            resolved_id = (publisher_id or "").strip() or await _discover_publisher_id(
                page, limit
            )
            body = await page.locator("body").inner_text()
            subscribers_match = SUBSCRIBERS_RE.search(body)
            subscribers = _to_int(subscribers_match.group(1)) if subscribers_match else None

            stats_url = (
                "https://dzen.ru/profile/editor/id/"
                f"{resolved_id}/publications-stat?statType=publications"
            )
            await page.goto(stats_url, wait_until="domcontentloaded", timeout=limit * 1000)
            try:
                await page.wait_for_selector(
                    "th[data-column-id='publication']", timeout=limit * 1000
                )
            except Exception as exc:  # noqa: BLE001 - add a useful artifact
                await _save_debug(page, "dzen-stats-failed")
                if "passport.yandex" in page.url or "oauth.yandex" in page.url:
                    raise DzenStatsNotLoggedIn(
                        "сессия Дзена истекла - выполните `tgfarm dzen-login`"
                    ) from exc
                raise DzenStatsError(
                    "статистика Дзена не загрузилась; проверьте вход и DZEN_PUBLISHER_ID"
                ) from exc
            await page.wait_for_timeout(1500)
            rows = parse_table_rows(await _read_rows(page))
            return rows, subscribers
        finally:
            await context.close()


def _normalise_url(url: str) -> str:
    return str(url or "").split("?", 1)[0].rstrip("/")


def record_publication_metrics(items: list[DzenPublicationMetrics]) -> dict[str, Any]:
    """Store changed snapshots for publications already logged as posted."""
    saved = unchanged = 0
    unmatched: list[str] = []
    with session_scope() as session:
        by_remote: dict[str, str] = {}
        by_url: dict[str, str] = {}
        for row in session.execute(
            select(CrosspostLog).where(
                CrosspostLog.platform == "dzen", CrosspostLog.status == "posted"
            )
        ).scalars():
            if row.remote_id:
                by_remote[row.remote_id] = row.post_public_id
            if row.url:
                by_url[_normalise_url(row.url)] = row.post_public_id

        for item in items:
            public_id = by_remote.get(item.remote_id or "") or by_url.get(
                _normalise_url(item.url)
            )
            if not public_id:
                unmatched.append(item.remote_id or item.url)
                continue
            post = session.execute(
                select(PostRow).where(PostRow.public_id == public_id)
            ).scalar_one_or_none()
            if post is None:
                unmatched.append(public_id)
                continue
            latest = session.execute(
                select(PostStat)
                .where(PostStat.post_id == post.id)
                .order_by(desc(PostStat.collected_at))
                .limit(1)
            ).scalar_one_or_none()
            values = (item.impressions, item.opens, item.views, item.reactions, item.comments, item.forwards)
            if latest and (
                latest.impressions,
                latest.opens,
                latest.views,
                latest.reactions,
                latest.comments,
                latest.forwards,
            ) == values:
                unchanged += 1
                continue
            session.add(
                PostStat(
                    post_id=post.id,
                    impressions=item.impressions,
                    opens=item.opens,
                    views=item.views,
                    reactions=item.reactions,
                    comments=item.comments,
                    forwards=item.forwards,
                )
            )
            saved += 1
    return {
        "saved": saved,
        "unchanged": unchanged,
        "unmatched": len(unmatched),
        "unmatched_ids": unmatched[:8],
    }


def record_subscribers(count: int | None) -> bool:
    if count is None:
        return False
    with session_scope() as session:
        channel = session.execute(
            select(ChannelRow).where(ChannelRow.platform == "dzen").limit(1)
        ).scalar_one_or_none()
        if channel is None:
            return False
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        row = session.execute(
            select(ChannelStat).where(
                ChannelStat.channel_id == channel.id, ChannelStat.day == day
            )
        ).scalar_one_or_none()
        if row is None:
            row = ChannelStat(channel_id=channel.id, day=day)
            session.add(row)
        row.subscribers = count
        row.collected_at = datetime.now(UTC)
        return True


def _state_path() -> Path:
    return get_settings().data_root() / STATE_FILE


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(payload: dict[str, Any]) -> None:
    try:
        _state_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.debug("could not persist Dzen stats state: %s", redact_sensitive_text(exc, limit=160))


def metrics_status() -> dict[str, Any]:
    state = _read_state()
    with session_scope() as session:
        count = session.execute(
            select(func.count(PostStat.id))
            .join(PostRow, PostRow.id == PostStat.post_id)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(ChannelRow.platform == "dzen")
        ).scalar_one()
        latest = session.execute(
            select(PostStat.collected_at)
            .join(PostRow, PostRow.id == PostStat.post_id)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(ChannelRow.platform == "dzen")
            .order_by(desc(PostStat.collected_at))
            .limit(1)
        ).scalar_one_or_none()
        mapped = session.execute(
            select(func.count(CrosspostLog.id)).where(
                CrosspostLog.platform == "dzen", CrosspostLog.status == "posted"
            )
        ).scalar_one()
    return {
        "enabled": get_settings().dzen_stats_enabled,
        "snapshots": int(count or 0),
        "latest_snapshot": (
            (latest if latest.tzinfo else latest.replace(tzinfo=UTC)).isoformat()
            if latest
            else None
        ),
        "mapped_publications": int(mapped or 0),
        "last_run": state.get("collected_at"),
        "last_result": {
            key: state.get(key) for key in ("seen", "saved", "unchanged", "unmatched")
        }
        if state
        else None,
        "last_error": state.get("error"),
    }


async def collect_and_record(*, force: bool = False) -> dict[str, Any]:
    """Collect, persist, and throttle automatic runs by DZEN_STATS_INTERVAL_MIN."""
    from tgfarm.dzen_web import DZEN_BROWSER_LOCK

    settings = get_settings()
    if not force and not settings.dzen_stats_enabled:
        return {"skipped": True, "reason": "disabled"}
    if DZEN_BROWSER_LOCK.locked():
        return {"skipped": True, "reason": "browser busy"}
    state = _read_state()
    if not force and state.get("collected_at"):
        try:
            last = datetime.fromisoformat(str(state["collected_at"]))
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            age_min = (datetime.now(UTC) - last).total_seconds() / 60
            if age_min < max(1, settings.dzen_stats_interval_min):
                return {"skipped": True, "reason": "interval", "age_min": round(age_min, 1)}
        except ValueError:
            pass

    started = datetime.now(UTC)
    try:
        async with DZEN_BROWSER_LOCK:
            items, subscribers = await collect_publication_metrics()
        result = record_publication_metrics(items)
        record_subscribers(subscribers)
        payload = {
            "collected_at": started.isoformat(),
            "seen": len(items),
            "subscribers": subscribers,
            **result,
        }
        _write_state(payload)
        log.info(
            "s8 dzen metrics: %d seen, %d saved, %d unchanged, %d unmatched",
            len(items),
            result["saved"],
            result["unchanged"],
            result["unmatched"],
        )
        return payload
    except Exception as exc:
        _write_state(
            {"collected_at": started.isoformat(), "error": redact_sensitive_text(exc, limit=300), "seen": 0}
        )
        raise


__all__ = [
    "DzenPublicationMetrics",
    "DzenStatsError",
    "DzenStatsNotLoggedIn",
    "collect_and_record",
    "collect_publication_metrics",
    "metrics_status",
    "parse_table_rows",
    "publication_id",
    "record_publication_metrics",
    "record_subscribers",
]

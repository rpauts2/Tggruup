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

from sqlalchemy import desc, func, or_, select

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


@dataclass(slots=True)
class DzenScrapeResult:
    """What one Studio visit actually yielded, including what it did not.

    ``table_rows`` versus ``listed_publications`` is the difference between a
    collector that read the whole channel and one that read the first page; the
    old code could not tell them apart.
    """

    items: list[DzenPublicationMetrics]
    dropped: list[dict[str, Any]]
    table_rows: int
    headers: list[str]
    columns: dict[str, int]
    listed_publications: int | None
    subscribers: int | None

    def drop_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.dropped:
            reason = str(entry.get("reason") or "unknown")
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def as_report(self) -> dict[str, Any]:
        missing = [role for role in METRIC_ROLES if role not in self.columns]
        return {
            "table_rows": self.table_rows,
            "parsed": len(self.items),
            "dropped": len(self.dropped),
            "drop_reasons": self.drop_reasons(),
            "drop_samples": self.dropped[:5],
            "listed_publications": self.listed_publications,
            "columns": dict(self.columns),
            "metrics_not_in_table": missing,
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


LISTED_PUBLICATIONS_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f]*)\s*(?:публикац|статей)", re.I
)

#: Metric roles, keyed by the words the Studio header itself uses.
#:
#: The publications table is not a stable contract: the 2026 redesign replaced
#: the "Показы / Открытия / Дочитывания" block with a single selectable metric
#: plus icon columns, so a row that used to have thirteen cells now has fewer,
#: and the column that holds "views" moves. Reading it by position therefore
#: drops rows in silence - the live run of 2026-11-04 scraped four rows out of a
#: channel that lists eighteen. Column identity is taken from the header instead,
#: and the positional read survives only as an explicit fallback.
COLUMN_ROLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("impressions", ("показ", "show", "impression")),
    ("opens", ("открыт", "open", "clickthrough")),
    ("views", ("дочитыв", "просмотр", "прочитыв", "view", "read")),
    ("reactions", ("лайк", "реакц", "like")),
    ("comments", ("комментар", "comment")),
    ("forwards", ("репост", "поделит", "рассылк", "share", "repost")),
)

#: Roles that are metrics; anything else the header offers is ignored.
METRIC_ROLES: tuple[str, ...] = tuple(role for role, _words in COLUMN_ROLES)

#: Legacy positional layout, kept as the documented fallback only.
POSITIONAL_MIN_CELLS = 12
POSITIONAL_COLUMNS: dict[str, int] = {
    "impressions": 4,
    "opens": 5,
    "views": 6,
    "comments": 9,
    "reactions": 10,
    "forwards": 11,
}

DROP_REASONS: tuple[str, ...] = (
    "no_publication_link",
    "duplicate_remote_id",
    "metric_columns_out_of_range",
    "no_metric_column_identified",
)


def publication_id(url: str | None) -> str | None:
    value = str(url or "").strip()
    match = PUBLICATION_ID_RE.search(value)
    if match:
        return match.group(1)
    match = MEDIA_ID_RE.search(value)
    return match.group(1) if match else None


def _column_role(header: str | None) -> str | None:
    """Which metric a Studio header describes, or ``None`` for "not a metric".

    "Время просмотра" contains the word for views but reports minutes, and
    "Подписки" is a subscriber delta; both must stay unmapped rather than be
    written into ``views``.
    """
    text = " ".join(str(header or "").replace("\u00a0", " ").replace("\u202f", " ").lower().split())
    if not text or "время" in text or "подписк" in text:
        return None
    for role, words in COLUMN_ROLES:
        if any(word in text for word in words):
            return role
    return None


def map_columns(headers: list[str] | None) -> dict[str, int]:
    """``role -> cell index`` for one Studio table; the first header wins.

    Deterministic on purpose: two headers claiming one role is a page we do not
    understand, and guessing which column the operator meant would put the wrong
    number on a real post.
    """
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers or []):
        role = _column_role(header)
        if role is None or role in mapping:
            continue
        mapping[role] = index
    return mapping


def listed_publications(rows: list[dict[str, Any]]) -> int | None:
    """How many publications the page says the table covers.

    Studio's summary row ("Всего за 30 дней - 18 публикаций") is the only honest
    denominator available: it separates "the channel has 18 articles and we read
    4" from "the page really only had 4".
    """
    for row in rows:
        if str(row.get("href") or "").strip():
            continue
        text = " ".join(str(cell or "") for cell in (row.get("cells") or []))
        match = LISTED_PUBLICATIONS_RE.search(text)
        if match:
            return _to_int(match.group(1)) or None
    return None


def _title_column(headers: list[str] | None) -> int | None:
    """Which cell holds the publication cell, so the headline is not guessed.

    The headline is the only attribution key for the 27 Dzen rows whose publisher
    never stored a URL, so reading it out of the metric column would silently
    destroy every match.
    """
    for index, header in enumerate(headers or []):
        text = " ".join(str(header or "").lower().split())
        if text == "publication" or "публикац" in text:
            return index
    return None


def _cell(cells: list[str], index: int) -> str:
    return cells[index] if 0 <= index < len(cells) else ""


def _row_title(cells: list[str], row: dict[str, Any], title_index: int | None) -> str:
    """Headline of a table row, from the column the header names if there is one."""
    if title_index is not None:
        value = _cell(cells, title_index)
        if value:
            return value[:200]
    return _cell(cells, 2) or str(row.get("title") or "")[:200]


def parse_rows(
    rows: list[dict[str, Any]],
    headers: list[str] | None = None,
) -> tuple[list[DzenPublicationMetrics], list[dict[str, Any]], int | None]:
    """Parse a Studio table into metrics, naming every row that was not parsed.

    Returns ``(items, dropped, listed)``. A row is dropped only when the table
    genuinely does not carry it, and the reason says which case it was, so a
    collector that reads nothing is distinguishable from one that reads nothing
    useful.
    """
    columns = map_columns(headers)
    title_index = _title_column(headers)
    usable = {role: index for role, index in columns.items() if index >= 0}
    out: list[DzenPublicationMetrics] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        href = str(row.get("href") or "").strip()
        remote_id = publication_id(href)
        if not href or not remote_id:
            dropped.append({"reason": "no_publication_link", "href": href[:120]})
            continue
        if remote_id in seen:
            dropped.append({"reason": "duplicate_remote_id", "remote_id": remote_id})
            continue
        cells = [str(cell or "") for cell in (row.get("cells") or [])]

        if usable:
            in_range = {role: index for role, index in usable.items() if index < len(cells)}
            if not in_range:
                dropped.append(
                    {
                        "reason": "metric_columns_out_of_range",
                        "remote_id": remote_id,
                        "cells": len(cells),
                        "expected": dict(usable),
                    }
                )
                continue
            seen.add(remote_id)
            values = {role: _to_int(_cell(cells, index)) for role, index in in_range.items()}
            out.append(
                DzenPublicationMetrics(
                    url=href.split("?", 1)[0],
                    remote_id=remote_id,
                    title=_row_title(cells, row, title_index),
                    impressions=values.get("impressions", 0),
                    opens=values.get("opens", 0),
                    views=values.get("views", 0),
                    reactions=values.get("reactions", 0),
                    comments=values.get("comments", 0),
                    forwards=values.get("forwards", 0),
                )
            )
            continue

        # No usable header: fall back to the documented legacy column order.
        if len(cells) < POSITIONAL_MIN_CELLS:
            dropped.append(
                {
                    "reason": "no_metric_column_identified",
                    "remote_id": remote_id,
                    "cells": len(cells),
                }
            )
            continue
        seen.add(remote_id)
        out.append(
            DzenPublicationMetrics(
                url=href.split("?", 1)[0],
                remote_id=remote_id,
                title=_row_title(cells, row, title_index),
                impressions=_to_int(_cell(cells, POSITIONAL_COLUMNS["impressions"])),
                opens=_to_int(_cell(cells, POSITIONAL_COLUMNS["opens"])),
                views=_to_int(_cell(cells, POSITIONAL_COLUMNS["views"])),
                comments=_to_int(_cell(cells, POSITIONAL_COLUMNS["comments"])),
                reactions=_to_int(_cell(cells, POSITIONAL_COLUMNS["reactions"])),
                forwards=_to_int(_cell(cells, POSITIONAL_COLUMNS["forwards"])),
            )
        )

    return out, dropped, listed_publications(rows)


def parse_table_rows(
    rows: list[dict[str, Any]], headers: list[str] | None = None
) -> list[DzenPublicationMetrics]:
    """Parse the JSON-shaped rows extracted by `_read_rows` from the DOM.

    Column order follows the Studio table when it is readable; without a header
    the legacy order (4 impressions, 5 openings, 6 reads/views, 9 comments,
    10 likes, 11 shares) is used. Use `parse_rows` when the drops matter too.
    """
    return parse_rows(rows, headers)[0]


async def _read_rows(page) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(rows, headers)`` of the publications table.

    A table qualifies when it carries a publication link and either the legacy
    thirteen-column layout or a header that names at least one metric. The old
    test was `len(cells) >= 12` alone, which is exactly how the redesigned table
    was rejected without anybody being told.
    """
    tables = page.locator("table")
    table_count = await tables.count()
    for index in range(table_count):
        table = tables.nth(index)
        data = await table.evaluate(
            """table => {
                const headers = Array.from(table.querySelectorAll('thead th'))
                    .map(th => (th.getAttribute('data-column-id') || '') + '|' +
                        (th.innerText || '').replace(/\\s+/g, ' ').trim());
                const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr => {
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
                });
                return {headers, rows};
            }"""
        )
        headers = _header_labels(data.get("headers") or [])
        rows = data.get("rows") or []
        if not isinstance(rows, list) or not isinstance(headers, list):
            continue
        has_metric_row = any(
            row.get("href")
            and (
                len(row.get("cells") or []) >= POSITIONAL_MIN_CELLS
                or bool(map_columns(headers))
            )
            for row in rows
        )
        if has_metric_row:
            return rows, headers
    raise DzenStatsError(
        f"таблица публикаций не найдена на странице статистики ({table_count} таблиц прочитано)"
    )


def _header_labels(raw: list[str]) -> list[str]:
    """Prefer the stable `data-column-id`, fall back to the visible label.

    Studio renames its labels; `data-column-id` is the attribute the page itself
    keys columns by, which is why the loader waits for it.
    """
    labels: list[str] = []
    for entry in raw:
        column_id, _, text = str(entry or "").partition("|")
        column_id = column_id.strip().lower()
        text = text.strip()
        if column_id and column_id not in {"", "publication"}:
            labels.append(column_id)
        elif text:
            labels.append(text)
        else:
            labels.append("")
    return labels


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


async def scrape_publication_metrics(
    *, publisher_id: str | None = None, timeout_s: int | None = None
) -> DzenScrapeResult:
    """Open Studio and return publication rows, what was dropped, and subscribers.

    The drop list and `listed_publications` are the point: without them a run
    that read four of eighteen rows looks identical to a run that read all of
    them.
    """
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
            rows, headers = await _read_rows(page)
            items, dropped, listed = parse_rows(rows, headers)
            return DzenScrapeResult(
                items=items,
                dropped=dropped,
                table_rows=len(rows),
                headers=headers,
                columns=map_columns(headers),
                listed_publications=listed,
                subscribers=subscribers,
            )
        finally:
            await context.close()


async def collect_publication_metrics(
    *, publisher_id: str | None = None, timeout_s: int | None = None
) -> tuple[list[DzenPublicationMetrics], int | None]:
    """Compatibility wrapper: publication rows plus the subscriber count."""
    result = await scrape_publication_metrics(
        publisher_id=publisher_id, timeout_s=timeout_s
    )
    return result.items, result.subscribers


def _normalise_url(url: str) -> str:
    return str(url or "").split("?", 1)[0].rstrip("/")


def _normalise_title(title: str) -> str:
    """Comparable form of an article title for the title-only fallback.

    Dzen's Studio list sometimes shows a truncated title and the publisher does
    not always learn the article URL, so the only stable link between a farm post
    and a collected row is the headline. Punctuation and the ellipsis are dropped
    and the text is lowercased; nothing else is guessed.
    """
    text = " ".join(str(title or "").replace("…", " ").split()).lower()
    for char in "«»\"'`.,:;!?()[]{}—–-":
        text = text.replace(char, " ")
    return " ".join(text.split())


def _remote_from_url(url: str) -> str | None:
    """Article id from a publication URL (``dzen.ru/a/auXXXX``)."""
    path = str(url or "").split("?", 1)[0].rstrip("/")
    last = path.rsplit("/", 1)[-1] if "/" in path else ""
    return last if len(last) >= 6 else None


def _title_matches(left: str, right: str) -> bool:
    """True when two normalised titles are the same or one contains the other.

    Studio's publication cell is not only the headline: it often carries the
    publication date or the author around it, so requiring the stored hook to be a
    *prefix* lost every match while the identity of the article was unknown. The
    containment rule keeps the safety property that matters — at least 40
    characters of evidence — and the caller still attributes only an unambiguous
    headline, so two posts sharing a phrase never share one article's metrics.
    """
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 40 and shorter in longer


def _unmatched_sample(item: DzenPublicationMetrics, reason: str) -> dict[str, str]:
    """What the collector actually saw for a row it could not attribute.

    Without the scraped headline the next diagnosis is guesswork: an empty title
    means the parser read the wrong column, a populated one means the stored post
    never carried this headline.
    """
    return {
        "reason": reason,
        "remote_id": str(item.remote_id or ""),
        "title": " ".join(str(item.title or "").split())[:80],
    }


def _dry_run_only_public_ids(session) -> set[str]:
    """Posts whose Dzen journal says "we never actually published them".

    A dry run journals a ``dry_run`` row and still flips the post to
    ``published``, so the post looks delivered while nothing left the machine.
    Measured on a read-only copy of the production database (2026-11-05): the 22
    published Dzen posts that have no ``posted`` row are exactly the 22 whose only
    Dzen journal row is a dry run — they were never "lost writes", they were never
    sent. Their headline must not be a candidate for a real Studio article, because
    attaching live numbers to an unpublished post teaches the farm a result it
    never produced.
    """
    posted = {
        row
        for (row,) in session.execute(
            select(CrosspostLog.post_public_id).where(
                CrosspostLog.platform == "dzen",
                # A row that means the browser was really pointed at Dzen: a
                # confirmed publish, or an attempt whose outcome is unknown.
                CrosspostLog.status.in_(("posted", "unknown", "publishing")),
                CrosspostLog.post_public_id.isnot(None),
            )
        ).all()
    }
    dry = {
        row
        for (row,) in session.execute(
            select(CrosspostLog.post_public_id).where(
                CrosspostLog.platform == "dzen",
                CrosspostLog.status == "dry_run",
                CrosspostLog.post_public_id.isnot(None),
            )
        ).all()
    }
    return dry - posted


def _headline_index(session) -> dict[str, list[str]]:
    """Normalised headline -> farm post ids that could be this article.

    Two sources are indexed, because a post can be published with no journal row
    and an article nobody can point at is an article nobody can measure. The
    second source is narrowed by ``_dry_run_only_public_ids``: on the production
    copy every published Dzen post that lacked a ``posted`` row turned out to have
    a ``dry_run`` one instead, so what was left in the index are the genuine
    bookkeeping accidents rather than the publications that never happened. The
    uniqueness rule below still gates every attribution, so widening the index
    cannot invent a match.
    """
    by_title: dict[str, list[str]] = {}

    def add(public_id: str | None, hook: str | None) -> None:
        key = _normalise_title(hook or "")
        if key and public_id:
            ids = by_title.setdefault(key, [])
            if public_id not in ids:
                ids.append(public_id)

    never_sent = _dry_run_only_public_ids(session)
    for row in session.execute(
        select(CrosspostLog).where(
            CrosspostLog.platform == "dzen", CrosspostLog.status == "posted"
        )
    ).scalars():
        if not row.post_public_id:
            continue
        hook = session.execute(
            select(PostRow.hook).where(PostRow.public_id == row.post_public_id)
        ).scalar_one_or_none()
        add(row.post_public_id, hook)

    for public_id, hook in session.execute(
        select(PostRow.public_id, PostRow.hook)
        .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
        .where(
            ChannelRow.platform == "dzen",
            PostRow.status == "published",
        )
    ).all():
        if public_id in never_sent:
            continue
        add(public_id, hook)

    return by_title


def _journal_id_index(session) -> tuple[dict[str, str], dict[str, str]]:
    """``remote_id -> post`` and ``url -> post`` straight from the publish journal.

    Read across every Dzen status except ``dry_run``, not just the ``posted``
    ones. The browser publisher is the only writer that ever learns the article
    id, and an operator reconciliation adds a *second* ``posted`` row copying the
    id of the row it answered — which is the empty ``publishing`` one. Filtering
    by ``posted`` would therefore discard the ids we do have whenever a post was
    confirmed by hand, and the dry-run placeholder ``dry-<post id>`` is not a
    Dzen id at all, so it stays out by name as well as by status.
    """
    by_remote: dict[str, str] = {}
    by_url: dict[str, str] = {}
    for row in session.execute(
        select(CrosspostLog)
        .where(CrosspostLog.platform == "dzen", CrosspostLog.status != "dry_run")
        .order_by(CrosspostLog.id)
    ).scalars():
        if not row.post_public_id:
            continue
        if row.url:
            by_url[_normalise_url(row.url)] = row.post_public_id
            from_url = publication_id(row.url) or _remote_from_url(row.url)
            if from_url:
                by_remote[from_url] = row.post_public_id
        ref = str(row.remote_id or "").strip()
        if ref and not ref.startswith("dry"):
            by_remote[ref] = row.post_public_id
    return by_remote, by_url


def _remember_remote_id(session, public_id: str, remote_key: str, url: str | None) -> bool:
    """Put the identifier on the journal row we just attributed, if it lacks one.

    CrosspostLog is append-only and this writes NULLs only: a row whose publisher
    already stored an id is left exactly as it was. Without the step, the couple
    of hundred Dzen posts published before the id was kept have to be guessed by
    headline on every future collection, and every headline change in Studio
    breaks them again.
    """
    if not remote_key:
        return False
    row = session.execute(
        select(CrosspostLog)
        .where(
            CrosspostLog.post_public_id == public_id,
            CrosspostLog.platform == "dzen",
            CrosspostLog.status == "posted",
            or_(CrosspostLog.remote_id.is_(None), CrosspostLog.remote_id == ""),
        )
        .order_by(desc(CrosspostLog.id))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return False
    row.remote_id = remote_key
    if url and not row.url:
        row.url = _normalise_url(str(url))
    return True


def record_publication_metrics(items: list[DzenPublicationMetrics]) -> dict[str, Any]:
    """Store changed snapshots for publications already logged as posted."""
    saved = unchanged = 0
    by_id = by_url_hit = by_title_hit = ids_stored = 0
    unmatched: list[dict[str, str]] = []
    with session_scope() as session:
        by_remote, by_url = _journal_id_index(session)
        by_title = _headline_index(session)

        for item in items:
            remote_key = item.remote_id or _remote_from_url(item.url) or ""
            public_id = by_remote.get(remote_key) if remote_key else None
            if public_id:
                by_id += 1
            else:
                public_id = by_url.get(_normalise_url(item.url))
                if public_id:
                    by_url_hit += 1
            if not public_id and item.title:
                key = _normalise_title(item.title)
                candidates: list[str] = []
                for known_title, public_ids in by_title.items():
                    if _title_matches(key, known_title):
                        candidates.extend(public_ids)
                # Only an unambiguous headline may be attributed: two posts with
                # the same title would otherwise share one article's metrics.
                unique = list(dict.fromkeys(candidates))
                if len(unique) == 1:
                    public_id = unique[0]
                    by_title_hit += 1
                elif len(unique) > 1:
                    unmatched.append(_unmatched_sample(item, "headline_ambiguous"))
                    continue
            if not public_id:
                unmatched.append(_unmatched_sample(item, "headline_not_attributable"))
                continue
            # A title match is the last guess we are willing to make: the id we
            # just proved belongs here is stored so the next run matches exactly.
            if remote_key and _remember_remote_id(session, public_id, remote_key, item.url):
                ids_stored += 1
            post = session.execute(
                select(PostRow).where(PostRow.public_id == public_id)
            ).scalar_one_or_none()
            if post is None:
                unmatched.append(_unmatched_sample(item, "log_points_at_missing_post"))
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
        "unmatched_samples": unmatched[:8],
        # Which key attributed what: `matched_by_title` is the number that drops
        # as the journal fills with ids, and `remote_ids_stored` says how fast.
        "matched_by_remote_id": by_id,
        "matched_by_url": by_url_hit,
        "matched_by_title": by_title_hit,
        "remote_ids_stored": ids_stored,
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
        # The honest measure of the attribution problem: how many of those rows
        # name the article they posted. Everything else is a headline guess, and
        # a headline guess only works while nobody edits the title.
        mapped_with_id = session.execute(
            select(func.count(CrosspostLog.id)).where(
                CrosspostLog.platform == "dzen",
                CrosspostLog.status == "posted",
                CrosspostLog.remote_id.isnot(None),
                CrosspostLog.remote_id != "",
            )
        ).scalar_one()
        published_posts = session.execute(
            select(func.count(PostRow.id))
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(ChannelRow.platform == "dzen", PostRow.status == "published")
        ).scalar_one()
        measured_posts = session.execute(
            select(func.count(func.distinct(PostStat.post_id)))
            .join(PostRow, PostRow.id == PostStat.post_id)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(ChannelRow.platform == "dzen")
        ).scalar_one()
    return {
        "enabled": get_settings().dzen_stats_enabled,
        "snapshots": int(count or 0),
        "published_posts": int(published_posts or 0),
        "measured_posts": int(measured_posts or 0),
        "latest_snapshot": (
            (latest if latest.tzinfo else latest.replace(tzinfo=UTC)).isoformat()
            if latest
            else None
        ),
        "mapped_publications": int(mapped or 0),
        "mapped_publications_with_id": int(mapped_with_id or 0),
        "last_run": state.get("collected_at"),
        "last_result": {
            key: state.get(key)
            for key in (
                "seen",
                "saved",
                "unchanged",
                "unmatched",
                "table_rows",
                "parsed",
                "dropped",
                "drop_reasons",
                "listed_publications",
                "metrics_not_in_table",
                "matched_by_remote_id",
                "matched_by_url",
                "matched_by_title",
                "remote_ids_stored",
                "status",
            )
            if key in state
        }
        if state
        else None,
        "last_error": state.get("error"),
        "last_reason": state.get("reason"),
    }


async def collect_and_record(*, force: bool = False) -> dict[str, Any]:
    """Collect, persist, and throttle automatic runs by DZEN_STATS_INTERVAL_MIN.

    Every path out of this function carries a ``status`` and, whenever nothing
    was stored, a ``reason``: "collected zero rows" and "could not collect" are
    different facts and the dashboard must not print them the same way.
    """
    from tgfarm.dzen_web import DZEN_BROWSER_LOCK

    settings = get_settings()
    if not force and not settings.dzen_stats_enabled:
        return {"status": "skipped", "skipped": True, "reason": "DZEN_STATS_ENABLED=false"}
    if DZEN_BROWSER_LOCK.locked():
        return {
            "status": "skipped",
            "skipped": True,
            "reason": "браузер Дзена занят публикацией",
        }
    state = _read_state()
    if not force and state.get("collected_at"):
        try:
            last = datetime.fromisoformat(str(state["collected_at"]))
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            age_min = (datetime.now(UTC) - last).total_seconds() / 60
            if age_min < max(1, settings.dzen_stats_interval_min):
                return {
                    "status": "skipped",
                    "skipped": True,
                    "reason": "интервал сбора ещё не истёк",
                    "age_min": round(age_min, 1),
                }
        except ValueError:
            pass

    started = datetime.now(UTC)
    try:
        async with DZEN_BROWSER_LOCK:
            scrape = await scrape_publication_metrics()
        items, subscribers = scrape.items, scrape.subscribers
        result = record_publication_metrics(items)
        record_subscribers(subscribers)
        payload: dict[str, Any] = {
            "collected_at": started.isoformat(),
            "status": "collected",
            "seen": len(items),
            "subscribers": subscribers,
            "saved": result["saved"],
            "unchanged": result["unchanged"],
            "unmatched": result["unmatched"],
            "unmatched_samples": result["unmatched_samples"],
            **scrape.as_report(),
        }
        if not items and not payload["saved"]:
            payload["reason"] = _empty_run_reason(scrape)
        _write_state(payload)
        log.info(
            "s8 dzen metrics: %d table rows, %d parsed, %d dropped, %d saved, "
            "%d unchanged, %d unmatched (page lists %s)",
            scrape.table_rows,
            len(items),
            len(scrape.dropped),
            result["saved"],
            result["unchanged"],
            result["unmatched"],
            scrape.listed_publications,
        )
        return payload
    except Exception as exc:
        reason = redact_sensitive_text(exc, limit=300)
        _write_state(
            {
                "collected_at": started.isoformat(),
                "status": "blocked",
                "error": reason,
                "seen": 0,
            }
        )
        raise


def _empty_run_reason(scrape: DzenScrapeResult) -> str:
    """Why a visit produced nothing, in one sentence the operator can act on."""
    if scrape.table_rows == 0:
        return "таблица статистики пустая: публикаций в выбранном периоде нет"
    reasons = scrape.drop_reasons()
    if reasons.get("no_publication_link"):
        return (
            f"{scrape.table_rows} строк таблицы, ни одна не содержит ссылку на "
            "публикацию - Studio изменила разметку"
        )
    if reasons.get("no_metric_column_identified") or reasons.get("metric_columns_out_of_range"):
        return (
            f"колонки метрик не распознаны в заголовке таблицы ({scrape.headers[:8]!r}); "
            "метрики не записаны, чтобы не угадать столбец"
        )
    return f"{scrape.table_rows} строк таблицы, измерить нечего"


__all__ = [
    "DzenPublicationMetrics",
    "DzenScrapeResult",
    "DzenStatsError",
    "DzenStatsNotLoggedIn",
    "collect_and_record",
    "collect_publication_metrics",
    "map_columns",
    "metrics_status",
    "parse_rows",
    "parse_table_rows",
    "publication_id",
    "record_publication_metrics",
    "record_subscribers",
    "scrape_publication_metrics",
]

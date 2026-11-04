"""S1 — source collection: fetch feeds, persist new items, index embeddings.

Two layers of "we already have this": the exact `(source_id, external_id)` pair
per outlet, and a content fingerprint over title + body that catches the same
document reprinted by a second outlet under a second guid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import select

from tgfarm.config import SourceSpec, get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.schemas import RawItem
from tgfarm.core.settings import get_settings
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.models import Source as SourceRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import get_index, pack
from tgfarm.llm.embedding import get_embedder
from tgfarm.s1_sources.base import (
    ContentFingerprint,
    SourceCollectionResult,
    find_content_reprint,
)
from tgfarm.s1_sources.collectors import FETCHERS, collect_detailed

log = get_logger(__name__)

MAX_AGE_DAYS = 21

# How far back ingest looks for "someone already published this exact copy".
# Two days covers the usual wire-to-aggregator lag without letting the candidate
# set grow into the whole retention window; older reprints are still caught by
# S2's dedup pass, which runs over its own cluster window.
INGEST_DEDUP_WINDOW_HOURS = 48
# Bound the in-memory scan: at farm scale a feed can dump thousands of rows, and
# the fingerprint comparison is O(candidates) per incoming item.
INGEST_DEDUP_SCAN_LIMIT = 20_000


def due_sources(now: datetime | None = None) -> list[SourceSpec]:
    """Configured sources whose min_interval has elapsed since the last fetch."""
    now = now or datetime.now(UTC)
    specs = {s.id: s for s in get_config().sources}
    due: list[SourceSpec] = []
    with session_scope() as session:
        rows = session.execute(select(SourceRow)).scalars().all()
        for row in rows:
            spec = specs.get(row.id)
            if spec is None or not spec.enabled or not row.enabled:
                continue
            cooldown_until = row.cooldown_until
            if cooldown_until is not None:
                if cooldown_until.tzinfo is None:
                    cooldown_until = cooldown_until.replace(tzinfo=UTC)
                else:
                    cooldown_until = cooldown_until.astimezone(UTC)
                if cooldown_until > now:
                    continue
            if row.last_fetched_at is None:
                due.append(spec)
                continue
            last = row.last_fetched_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last).total_seconds() >= effective_interval(row):
                due.append(spec)
    return due


def effective_interval(row: SourceRow, settings=None) -> int:
    """Base cadence stretched by idle streaks and consecutive failures.

    A feed that has produced nothing for six fetches is unlikely to break news
    in the next ten minutes; backing it off frees the cycle for live sources
    without disabling it. Failures use the same exponential ladder so a dead
    host is retried every few hours instead of every cycle.
    """
    settings = settings or get_settings()
    base = max(60, int(row.min_interval_s or 600))
    if not settings.source_adaptive_cadence:
        return base
    idle = min(max(0, int(row.idle_streak or 0)), max(0, settings.source_idle_backoff_max))
    failures = min(
        max(0, int(row.failures or 0)), max(0, settings.source_failure_backoff_max)
    )
    return min(base * (2 ** (idle + failures)), 21_600)


def _mark_source(
    source_id: str,
    ok: bool,
    error: str | None = None,
    *,
    items_new: int = 0,
) -> None:
    with session_scope() as session:
        row = session.get(SourceRow, source_id)
        if row is None:
            return
        now = datetime.now(UTC)
        row.last_fetched_at = now
        row.last_error = (error or "")[:500] or None
        if not ok:
            row.failures += 1
            settings = get_settings()
            failure_limit = max(1, int(settings.source_auto_cooldown_after_failures))
            if row.failures >= failure_limit:
                cooldown_hours = max(1, int(settings.source_auto_cooldown_hours))
                row.cooldown_until = now + timedelta(hours=cooldown_hours)
                row.last_status = "cooldown"
                log.warning(
                    "source %s parked until %s after %d consecutive failures",
                    source_id,
                    row.cooldown_until.isoformat(timespec="seconds"),
                    row.failures,
                )
            else:
                row.cooldown_until = None
                row.last_status = "error"
        else:
            row.last_status = "ok"
            row.failures = 0
            row.cooldown_until = None
            row.last_yield = max(0, int(items_new))
            row.idle_streak = 0 if items_new else row.idle_streak + 1


async def _record_source_outcomes(
    results: list[SourceCollectionResult],
    counts: dict[str, int],
    *,
    skip_success_ids: set[str] | None = None,
) -> None:
    """Persist each fetch outcome without treating failed feeds as empty success."""
    skip_success_ids = skip_success_ids or set()
    for result in results:
        if result.error is not None:
            await to_thread(_mark_source, result.source_id, False, result.error)
        elif result.source_id not in skip_success_ids:
            await to_thread(
                _mark_source,
                result.source_id,
                True,
                items_new=counts.get(result.source_id, 0),
            )


def _load_fingerprint_registry(
    session,
    *,
    since: datetime,
    limit: int = INGEST_DEDUP_SCAN_LIMIT,
) -> list[ContentFingerprint]:
    """Recent surviving documents, reduced to fingerprints.

    Rows already flagged as duplicates are skipped so a reprint points at the
    original instead of at another reprint, and rows that carry no fingerprint
    (the column post-dates them) are left out entirely: a missing fingerprint
    cannot prove similarity to anything, and keeping it would only tempt a
    `hamming(0, x)` comparison that means nothing.
    """
    rows = session.execute(
        select(RawItemRow.id, RawItemRow.title, RawItemRow.text, RawItemRow.simhash)
        .where(
            RawItemRow.fetched_at >= since,
            RawItemRow.duplicate_of.is_(None),
            RawItemRow.simhash.isnot(None),
            RawItemRow.simhash != 0,
        )
        .order_by(RawItemRow.fetched_at.desc())
        .limit(limit)
    ).all()
    return [
        ContentFingerprint.of(int(row.id), row.title, row.text, simhash_value=row.simhash)
        for row in rows
    ]


def _persist_detailed(
    items: list[RawItem],
    *,
    dedup_window_hours: int = INGEST_DEDUP_WINDOW_HOURS,
) -> tuple[int, dict[str, int]]:
    """Insert unseen items and return the count per source.

    "Unseen" is two different questions. `(source_id, external_id)` says *this
    outlet* already handed us the document; the content fingerprint says the
    document itself is already in the store from *another* outlet. A reprint that
    passes the first test and fails the second is still written — it is evidence
    that the story reached a second audience — but it is flagged with
    `duplicate_of` at ingest so nothing downstream can read it as a fresh,
    independent signal.
    """
    if not items:
        return 0, {}

    embedder = get_embedder()
    index = get_index(embedder.dim)
    cutoff = datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS)
    written = 0
    reprints = 0
    counts: dict[str, int] = {}

    with session_scope() as session:
        # one query per source, chunked to stay under SQLite's bound-parameter cap
        by_source: dict[str, list[str]] = {}
        for item in items:
            by_source.setdefault(item.source_id, []).append(item.external_id)

        existing: set[tuple[str, str]] = set()
        for source_id, externals in by_source.items():
            for chunk in _batched(externals, 400):
                stmt = select(RawItemRow.external_id).where(
                    RawItemRow.source_id == source_id,
                    RawItemRow.external_id.in_(chunk),
                )
                for (external_id,) in session.execute(stmt):
                    existing.add((source_id, external_id))

        # A batch can legitimately contain the same document twice — a feed that
        # lists an article under two categories, or a source present in two
        # niches. The uniqueness check below only sees what is already committed,
        # so duplicates *within* the batch have to be collapsed here or the
        # second INSERT violates the constraint.
        unique: dict[tuple[str, str], RawItem] = {}
        for item in items:
            unique.setdefault((item.source_id, item.external_id), item)
        items = list(unique.values())

        pending = [i for i in items if (i.source_id, i.external_id) not in existing]

        registry: list[ContentFingerprint] = []
        if pending and dedup_window_hours > 0:
            since = datetime.now(UTC) - timedelta(hours=max(1, int(dedup_window_hours)))
            registry = _load_fingerprint_registry(session, since=since)

        for item in pending:
            published = item.published_at
            if published is not None and published < cutoff:
                continue
            if published is None:
                published = datetime.now(UTC)

            vec = embedder.embed(item.blend)
            fingerprint = ContentFingerprint.of(None, item.title, item.text)
            original_id = find_content_reprint(fingerprint, registry)
            row = RawItemRow(
                source_id=item.source_id,
                external_id=item.external_id,
                url=item.url,
                title=item.title,
                text=item.text,
                lang=item.lang,
                published_at=published,
                niche_hint=item.niche_hint,
                source_weight=item.source_weight,
                media_links=item.media_links,
                media_kind=item.media_kind,
                embedding=pack(vec),
                simhash=fingerprint.simhash,
                duplicate_of=original_id,
            )
            session.add(row)
            session.flush()  # need row.id for the in-memory index
            index.add(row.id, vec)
            written += 1
            counts[item.source_id] = counts.get(item.source_id, 0) + 1

            if original_id is None:
                # Only surviving documents join the registry: a reprint must not
                # become the canonical copy of a third copy.
                registry.append(
                    ContentFingerprint.of(
                        row.id, item.title, item.text, simhash_value=fingerprint.simhash
                    )
                )
            else:
                reprints += 1

    log.info(
        "persisted %d new items (%d flagged as already-in-store reprints), "
        "index now %d vectors",
        written,
        reprints,
        len(index),
    )
    return written, counts


def _persist(items: list[RawItem]) -> int:
    return _persist_detailed(items)[0]


def _batched(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def run(limit: int | None = None) -> int:
    """Full S1 cycle: fetch due sources -> persist -> index."""
    with StageTimer("s1_collect") as timer:
        if not get_settings().online:
            log.warning("s1: network collection disabled; source health left unchanged")
            timer.items_out = 0
            return 0

        specs = due_sources()
        if not specs:
            timer.items_out = 0
            log.info("s1: no source due yet")
            return 0

        results = await collect_detailed(FETCHERS, specs)
        all_items = [item for result in results for item in result.items]
        items = all_items
        skip_success_ids: set[str] = set()
        if limit and len(items) > limit:
            items = items[:limit]
            selected_ids = {item.source_id for item in items}
            skipped_ids = {item.source_id for item in all_items[limit:]}
            skip_success_ids = skipped_ids - selected_ids

        written, counts = await to_thread(_persist_detailed, items)
        await _record_source_outcomes(
            results,
            counts,
            skip_success_ids=skip_success_ids,
        )
        timer.items_in = len(items)
        timer.items_out = written
        return written


async def run_all_sources() -> int:
    """Ignore min_interval — used by the CLI smoke command."""
    settings = get_settings()
    if not settings.online:
        return 0
    specs = [s for s in get_config().sources if s.enabled]
    results = await collect_detailed(FETCHERS, specs)
    items = [item for result in results for item in result.items]
    written, counts = await to_thread(_persist_detailed, items)
    await _record_source_outcomes(results, counts)
    return written


def embedding_of(row: RawItemRow) -> np.ndarray:
    from tgfarm.db.vector import unpack

    return unpack(row.embedding) if row.embedding is not None else np.zeros(0, dtype=np.float32)

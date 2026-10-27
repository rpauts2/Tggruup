"""S1 — source collection: fetch feeds, persist new items, index embeddings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import select

from tgfarm.config import SourceSpec, get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.schemas import RawItem
from tgfarm.core.settings import get_settings
from tgfarm.core.text import simhash
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.models import Source as SourceRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import get_index, pack
from tgfarm.llm.embedding import get_embedder
from tgfarm.s1_sources.collectors import FETCHERS, collect

log = get_logger(__name__)

MAX_AGE_DAYS = 21


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
        row.last_fetched_at = datetime.now(UTC)
        row.last_status = "ok" if ok else "error"
        row.last_error = (error or "")[:500] or None
        if not ok:
            row.failures += 1
        else:
            row.failures = 0
            row.last_yield = max(0, int(items_new))
            row.idle_streak = 0 if items_new else row.idle_streak + 1


def _persist_detailed(items: list[RawItem]) -> tuple[int, dict[str, int]]:
    """Insert unseen items and return the count per source."""
    if not items:
        return 0, {}

    embedder = get_embedder()
    index = get_index(embedder.dim)
    cutoff = datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS)
    written = 0
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
        for item in pending:
            published = item.published_at
            if published is not None and published < cutoff:
                continue
            if published is None:
                published = datetime.now(UTC)

            vec = embedder.embed(item.blend)
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
                simhash=simhash(item.title + " " + item.text[:600]),
            )
            session.add(row)
            session.flush()  # need row.id for the in-memory index
            index.add(row.id, vec)
            written += 1
            counts[item.source_id] = counts.get(item.source_id, 0) + 1

    log.info("persisted %d new items (index now %d vectors)", written, len(index))
    return written, counts


def _persist(items: list[RawItem]) -> int:
    return _persist_detailed(items)[0]


def _batched(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def run(limit: int | None = None) -> int:
    """Full S1 cycle: fetch due sources -> persist -> index."""
    with StageTimer("s1_collect") as timer:
        specs = due_sources()
        if not specs:
            timer.items_out = 0
            log.info("s1: no source due yet")
            return 0

        items = await collect(FETCHERS, specs)
        if limit:
            items = items[:limit]

        written, counts = await to_thread(_persist_detailed, items)
        for spec in specs:
            await to_thread(
                _mark_source,
                spec.id,
                True,
                items_new=counts.get(spec.id, 0),
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
    items = await collect(FETCHERS, specs)
    written, counts = await to_thread(_persist_detailed, items)
    for spec in specs:
        await to_thread(
            _mark_source,
            spec.id,
            True,
            items_new=counts.get(spec.id, 0),
        )
    return written


def embedding_of(row: RawItemRow) -> np.ndarray:
    from tgfarm.db.vector import unpack

    return unpack(row.embedding) if row.embedding is not None else np.zeros(0, dtype=np.float32)

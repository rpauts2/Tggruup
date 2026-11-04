"""Backfill: apply the ingest reprint rule to the rows that predate it.

Ingest compares an incoming item against documents *already in the store*
(`tgfarm.s1_sources._persist_detailed`), so a reprint is only recognised from the
day that rule landed. Every row written before it — and every pair the 48-hour
ingest window had already aged out of when the copy arrived — still sits in
`raw_items` with `duplicate_of` NULL, and trends, niches, heat and `item_count`
all read those as independent documents. The corpus is where the damage is, not
the inlet.

This module is a *chronological sweep* of the unlinked rows: each row is compared
against the surviving originals that appeared before it, using the exact predicate
ingest uses — `is_content_reprint`, i.e. simhash Hamming within
`SIMHASH_MAX_DISTANCE` **and** token-set Jaccard at or above
`REPRINT_MIN_TOKEN_OVERLAP`. No threshold is re-declared here and the look-back
window is read from the ingest constants at call time, so the two paths decide the
same way about the same pair.

Guarantees, in the order the pipeline needs them:

* **Never points forward.** A row links only to a strictly earlier document,
  ordered by `published_at` with `fetched_at` as the fallback and the lower id as
  the tie-break. Following `duplicate_of` therefore always moves backwards in
  time, which makes a cycle structurally impossible rather than merely
  checked-for: if A and B look alike, only the later of the two can ever get a
  link, so A→B and B→A cannot both be written. Id order and time order genuinely
  disagree in this store (feeds backdate articles — rows 2 and 3 of the live
  corpus are older than row 1), so the sweep is ordered by *time*, not by id.
* **Idempotent.** Only rows with `duplicate_of IS NULL` are candidates and only
  rows with `duplicate_of IS NULL` can act as originals, and every write re-asserts
  the NULL inside its own WHERE clause. A second pass over the same corpus links
  nothing; a row that ingest linked while this sweep was mid-page is not
  re-pointed.
* **Bounded.** A wall-clock deadline and/or a row cap stop the sweep between rows,
  links are committed per page, and the report carries how many rows were scanned,
  how many links were written and how many candidates are left past the cut point
  for the next run — plus the cursor to resume from. A cycle whose watchdog fires
  at 2700 s must not starve S5 for want of a bound here.
* **Chunked.** Pages are keyset-paginated on (moment, id) — never `OFFSET`, which
  re-reads everything it has already stepped past — and the in-memory pool of
  earlier originals is trimmed to the look-back window and capped, so memory
  tracks the window, not the table. Embedding blobs are never selected; they are
  what actually makes this table large.
* **Honest about missing data.** A row with no fingerprint (`simhash` 0 or NULL,
  written before the column existed) is excluded on both sides of the comparison:
  `hamming(0, x)` measures nothing, and reading it as "identical" would flag the
  whole store. Rows with neither `published_at` nor `fetched_at` are excluded too,
  because a document with no clock cannot be placed in the sweep — both classes
  are counted and reported rather than dropped silently.

Nothing here is destructive: links written by ingest or by S2's `_dedup_pass` are
read as given, never rewritten and never cleared.

Two deliberate differences from ingest, both in the stricter direction:

* ingest names the *lowest id* among the copies it sees (`find_content_reprint`);
  this names the *earliest document*. Id is only a proxy for time, and pointing at
  a later article is exactly what the rule above forbids.
* ingest's window is `fetched_at >= now - 48 h`; this is "the original appeared
  within 48 h before the copy" — the same look-back measured on the documents' own
  clocks, because a backfill has no meaningful "now" for a row from September.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, case, func, or_, select, update

from tgfarm.core.logging import get_logger
from tgfarm.core.run import ensure_utc
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.s1_sources.base import ContentFingerprint, is_content_reprint

log = get_logger(__name__)

# A pipeline cycle is killed by the watchdog at 2700 s and S5 has starved behind a
# backlog before. This is hygiene, not the news: it gets one slice, small enough
# that every stage after it still runs on the same cycle.
DEFAULT_BUDGET_SECONDS = 60.0
# Rows per read. A row here is id + title + text + simhash, never the embedding.
DEFAULT_PAGE_SIZE = 200

COMPLETE = "complete"
BUDGET_STOPPED = "budget"
ROW_CAP_STOPPED = "row_cap"


@dataclass(frozen=True, slots=True)
class Cursor:
    """The last row the sweep looked at, addressed by (its moment, its id).

    Returned in the report and accepted again as `resume_after`, so a bounded run
    picks up where the previous one stopped instead of re-classifying history.
    """

    moment: datetime
    row_id: int

    @property
    def key(self) -> tuple[datetime, int]:
        return (ensure_utc(self.moment), self.row_id)


@dataclass(slots=True)
class _Document:
    """One stored row, reduced to what the reprint comparison needs."""

    row_id: int
    moment: datetime
    fingerprint: ContentFingerprint

    @property
    def key(self) -> tuple[datetime, int]:
        return (self.moment, self.row_id)


@dataclass(slots=True)
class BackfillReport:
    """What one bounded pass did, and what it left behind."""

    rows_scanned: int = 0
    links_found: int = 0
    links_written: int = 0
    candidates_remaining: int = 0
    pages_read: int = 0
    pool_peak: int = 0
    unfingerprinted_rows: int = 0
    undated_rows: int = 0
    stopped_by: str = COMPLETE
    elapsed_s: float = 0.0
    window_hours: int = 0
    dry_run: bool = False
    stopped_at: Cursor | None = None

    @property
    def budget_exhausted(self) -> bool:
        """True when the pass stopped because it ran out of budget, not work."""
        return self.stopped_by != COMPLETE

    @property
    def next_cursor(self) -> Cursor | None:
        """Feed this back as `resume_after` to continue where the pass stopped."""
        return self.stopped_at

    def as_dict(self) -> dict[str, Any]:
        """Plain shape for the maintenance report / run journal."""
        return {
            "rows_scanned": self.rows_scanned,
            "links_found": self.links_found,
            "links_written": self.links_written,
            "candidates_remaining": self.candidates_remaining,
            "pages_read": self.pages_read,
            "pool_peak": self.pool_peak,
            "unfingerprinted_rows": self.unfingerprinted_rows,
            "undated_rows": self.undated_rows,
            "stopped_by": self.stopped_by,
            "budget_exhausted": self.budget_exhausted,
            "elapsed_s": self.elapsed_s,
            "window_hours": self.window_hours,
            "dry_run": self.dry_run,
        }

    def __str__(self) -> str:
        return (
            f"scanned={self.rows_scanned} found={self.links_found} "
            f"linked={self.links_written} remaining={self.candidates_remaining} "
            f"stopped_by={self.stopped_by} in={self.elapsed_s}s"
        )


def _ingest_defaults() -> tuple[int, int]:
    """(window hours, pool cap) that ingest is using right now.

    Imported lazily on purpose: this module lives *inside* the ingest package, so
    a package-level import at module scope would make importing the backfill
    depend on the package `__init__` already being finished. Mirroring ingest's
    numbers is the whole point, so they are read at call time, not copied.
    """
    from tgfarm import s1_sources as ingest

    return (
        int(ingest.INGEST_DEDUP_WINDOW_HOURS),
        int(ingest.INGEST_DEDUP_SCAN_LIMIT),
    )


def _moment() -> Any:
    """A document's own clock: `published_at`, else `fetched_at`.

    Typed explicitly because `coalesce()` over two DATETIME columns would
    otherwise hand SQLite's storage strings back to Python and every caller would
    have to sort timestamps by hand.
    """
    return func.coalesce(
        RawItemRow.published_at, RawItemRow.fetched_at, type_=DateTime(timezone=True)
    )


def _survivor_conditions() -> list[Any]:
    """Rows that are still their own document and are comparable at all.

    `duplicate_of IS NULL` does double duty: it is the candidate set (the links
    still to be worked out) *and* the set of documents allowed to act as an
    original — a reprint must not become the canonical copy of a third copy, the
    same rule the ingest registry applies.
    """
    return [
        RawItemRow.duplicate_of.is_(None),
        RawItemRow.simhash.isnot(None),
        RawItemRow.simhash != 0,
        _moment().isnot(None),
    ]


def _past(cursor: Cursor | None) -> Any:
    """Keyset predicate: strictly after the cursor in (moment, id) order."""
    if cursor is None:
        return None
    moment = _moment()
    return or_(moment > cursor.moment, and_(moment == cursor.moment, RawItemRow.id > cursor.row_id))


def _document(row_id: int, title: str, text: str, simhash_value: int, moment: datetime) -> _Document:
    return _Document(
        row_id=int(row_id),
        moment=ensure_utc(moment),
        # The stored fingerprint, never a recomputed one: these rows were written
        # with `fingerprint_of(title, text)`, and rehashing here would quietly
        # invent a second recipe — the one thing this module must not do.
        fingerprint=ContentFingerprint.of(
            int(row_id), title, text, simhash_value=int(simhash_value or 0)
        ),
    )


def _page_columns() -> tuple:
    """Only what the comparison needs — no embedding blob, no url."""
    return (RawItemRow.id, RawItemRow.title, RawItemRow.text, RawItemRow.simhash, _moment().label("moment"))


def _read_page(session, *, after: Cursor | None, limit: int) -> list[_Document]:
    """One chunk of unlinked rows, oldest first."""
    stmt = select(*_page_columns()).where(*_survivor_conditions())
    predicate = _past(after)
    if predicate is not None:
        stmt = stmt.where(predicate)
    stmt = stmt.order_by(_moment(), RawItemRow.id).limit(max(1, int(limit)))
    return [_document(*row) for row in session.execute(stmt)]


def _load_window(session, *, since: datetime, until: datetime, cap: int) -> list[_Document]:
    """Surviving originals in [since, until], oldest first, newest `cap` kept.

    Used when resuming mid-history: the rows before the cursor are already
    classified and never re-read, so the pool has to be rebuilt from the database
    rather than carried over. Keeping the newest `cap` mirrors the ingest
    registry, which likewise caps by taking the most recent rows of its window.
    """
    stmt = (
        select(*_page_columns())
        .where(*_survivor_conditions(), _moment() >= since, _moment() <= until)
        .order_by(_moment().desc(), RawItemRow.id.desc())
        .limit(max(1, int(cap)))
    )
    documents = [_document(*row) for row in session.execute(stmt)]
    documents.reverse()
    return documents


def _first_original(document: _Document, pool: deque[_Document]) -> _Document | None:
    """The earliest live original this document reprints, None when it is new.

    "Earliest" is recomputed from the entries rather than trusted from the pool's
    order: the sweep's ordering assumption lives in one place (`_read_page`), and a
    comparison rule that silently depended on a second one — the DB handing back
    timestamps whose text order differs from their datetime order — would lose the
    oldest copy and name a later one. The `best.key <= entry.key` test keeps a
    reprint storm cheap anyway: once the earliest match is in hand, the remaining
    entries are skipped without running the token comparison at all, which is where
    the cost actually is. Measured on a copy of the live store: 15 090 candidates
    over a 48-hour pool of ~3.9k originals in 22 s, identical to the ordered-pool
    version.

    `is_content_reprint` is the entire decision. No cheaper home-grown pre-filter
    here: the Hamming gate inside it already is the cheap one, and one shared
    implementation is what keeps the ingest path and this path from disagreeing.
    """
    best: _Document | None = None
    for entry in pool:
        if entry.key >= document.key:
            # Never point forward in time.
            continue
        if best is not None and best.key <= entry.key:
            continue
        if is_content_reprint(document.fingerprint, entry.fingerprint):
            best = entry
    return best


def _link(session, row_id: int, original_id: int) -> int:
    """Write one link, and only if the row is still unlinked.

    The NULL test inside the WHERE clause is the idempotency guard and the
    concurrency guard in one statement: a row that ingest has meanwhile pointed at
    its own original is left alone instead of re-pointed, and the rowcount says
    whether the link is this sweep's doing.
    """
    result = session.execute(
        update(RawItemRow)
        .where(RawItemRow.id == row_id, RawItemRow.duplicate_of.is_(None))
        .values(duplicate_of=original_id)
    )
    return int(result.rowcount or 0)


def _count_left(session, *, after: Cursor | None) -> int:
    """Candidates past the cut point — the backlog the next run inherits."""
    stmt = select(func.count(RawItemRow.id)).where(*_survivor_conditions())
    predicate = _past(after)
    if predicate is not None:
        stmt = stmt.where(predicate)
    return int(session.execute(stmt).scalar_one() or 0)


def _count_uncappable(session) -> tuple[int, int]:
    """Rows the sweep can never compare: (no fingerprint, no usable timestamp).

    Reported rather than silently dropped. On a corpus written before `simhash`
    existed, the first number is what tells the operator that the backfill is not
    finished — it is out of data.
    """
    stmt = select(
        func.sum(case((or_(RawItemRow.simhash.is_(None), RawItemRow.simhash == 0), 1), else_=0)),
        func.sum(case((_moment().is_(None), 1), else_=0)),
    ).where(RawItemRow.duplicate_of.is_(None))
    no_fingerprint, no_moment = session.execute(stmt).one()
    return int(no_fingerprint or 0), int(no_moment or 0)


def backfill_reprints(
    *,
    budget_seconds: float | None = DEFAULT_BUDGET_SECONDS,
    max_rows: int | None = None,
    window_hours: int | None = None,
    pool_limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    resume_after: Cursor | None = None,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> BackfillReport:
    """Link historical reprints to the earliest document they copy.

    Budgets: `budget_seconds` (wall clock) and `max_rows` (candidates examined),
    either or both; pass `None` for both to get an unbounded pass, which is what
    the tests and a one-off operator run want. `dry_run` runs the whole sweep and
    reports the links it would write without writing any.

    `window_hours` defaults to the ingest window so that the backfill never links
    a pair ingest would not have linked. Widening it makes the backfill *stricter
    than ingest* — a policy decision, not a bug to be smuggled in.
    """
    ingest_window, ingest_pool_limit = _ingest_defaults()
    window = int(ingest_window if window_hours is None else window_hours)
    if window <= 0:
        raise ValueError("window_hours must be positive; a zero window could link nothing")
    cap = int(ingest_pool_limit if pool_limit is None else pool_limit)
    if cap <= 0:
        raise ValueError("pool_limit must be positive")
    rows_cap = None if max_rows is None else max(0, int(max_rows))
    lookback = timedelta(hours=window)

    started = clock()
    deadline = None if budget_seconds is None else started + float(budget_seconds)
    report = BackfillReport(window_hours=window, dry_run=bool(dry_run))

    def _out_of_budget() -> str | None:
        """Why the sweep has to stop right now, or None to keep going.

        Tested per row, not per page: one page of history can itself be seconds of
        comparison work, and a stage that only notices its budget at page
        boundaries overshoots by however unlucky the last page was.
        """
        if deadline is not None and clock() >= deadline:
            return BUDGET_STOPPED
        if rows_cap is not None and report.rows_scanned >= rows_cap:
            return ROW_CAP_STOPPED
        return None

    pool: deque[_Document] = deque()
    with session_scope() as session:
        cursor = resume_after
        if cursor is not None:
            pool.extend(
                _load_window(session, since=cursor.key[0] - lookback, until=cursor.key[0], cap=cap)
            )

        stopped_by: str | None = None
        while stopped_by is None:
            stopped_by = _out_of_budget()
            if stopped_by is not None:
                break
            page = _read_page(session, after=cursor, limit=page_size)
            if not page:
                break
            report.pages_read += 1

            for document in page:
                stopped_by = _out_of_budget()
                if stopped_by is not None:
                    break

                # The window belongs to the candidate, not to the run.
                cutoff = document.key[0] - lookback
                while pool and pool[0].key[0] < cutoff:
                    pool.popleft()

                report.rows_scanned += 1
                original = _first_original(document, pool)
                if original is None:
                    # A row that found no original is itself an original, so it
                    # joins the pool for every row after it. One that did get
                    # linked must not: it is evidence of copying, never of an
                    # independent document — the ingest registry's rule.
                    pool.append(document)
                    report.pool_peak = max(report.pool_peak, len(pool))
                    while len(pool) > cap:
                        pool.popleft()
                else:
                    # `links_found` is the answer dry_run is asking for: what a
                    # real pass would write. `links_written` is what this statement
                    # actually changed, which can be lower if ingest linked the row
                    # underneath us — see `_link`.
                    report.links_found += 1
                    if not dry_run:
                        report.links_written += _link(session, document.row_id, original.row_id)

                cursor = Cursor(moment=document.key[0], row_id=document.row_id)

            # Durable per page: a run killed mid-sweep keeps the links it already
            # wrote, so the next one neither repeats nor undoes them.
            session.commit()
            if stopped_by is not None:
                break

        report.unfingerprinted_rows, report.undated_rows = _count_uncappable(session)
        report.candidates_remaining = _count_left(session, after=cursor)
        report.stopped_at = cursor
        report.stopped_by = stopped_by or COMPLETE
        report.elapsed_s = round(clock() - started, 3)

    log.info(
        "backfill reprints: %s (window %dh, pool peak %d, dry_run=%s)",
        report,
        report.window_hours,
        report.pool_peak,
        dry_run,
    )
    return report


# ---------------------------------------------------------------- panel memory
# Where the last pass left the corpus is a fact the operator needs and the
# log line does not survive a page refresh. This mirrors exactly how S3 keeps
# the router's skip report: one module-level reference, no schema, no file
# format, and the API says out loud that a restart clears it. Nothing is
# invented here — either a report was recorded in this process or it was not,
# and `get_last_backfill_report()` answers None when it was not.

_LAST_BACKFILL_REPORT: BackfillReport | None = None
_LAST_BACKFILL_RECORDED_AT: datetime | None = None


def note_backfill_report(report: BackfillReport | None) -> None:
    """Remember the last backfill pass so the panel can serve its remainder.

    One wiring line in the maintenance step is all this needs:
    `note_backfill_report(report)` right after `backfill_reprints(...)`
    returns. A failed pass has no report, so `None` is ignored and the
    previously remembered pass stays as it was — forgetting what ran because
    something else failed would be its own lie. A copy is stored, so a caller
    that later mutates its own report object cannot rewrite operator history.
    """
    global _LAST_BACKFILL_REPORT, _LAST_BACKFILL_RECORDED_AT
    if report is None:
        return
    _LAST_BACKFILL_REPORT = replace(report)
    _LAST_BACKFILL_RECORDED_AT = datetime.now(UTC)


def get_last_backfill_report() -> BackfillReport | None:
    """The report of the last pass recorded in *this* process, or None.

    None is the honest answer when nothing has been recorded yet — the caller
    must present it as "never ran here", never as "nothing remains".
    """
    return _LAST_BACKFILL_REPORT


def get_last_backfill_recorded_at() -> datetime | None:
    """UTC instant `note_backfill_report` accepted the stored report, or None."""
    return _LAST_BACKFILL_RECORDED_AT


__all__ = [
    "BackfillReport",
    "Cursor",
    "backfill_reprints",
    "get_last_backfill_recorded_at",
    "get_last_backfill_report",
    "note_backfill_report",
]

"""Repair the `duplicate_of` links that were stored before the writer was fixed.

`raw_items.duplicate_of` was written by two conflicting contracts until commit
`83e51d2` landed: ingest linked to the earliest matching row, while the old S2
dedup pass walked its window newest-first and linked to whatever it met. What
that leaves in the store is not a duplicate flag but a broken map — chains where
a one-hop pointer to the root belongs, mutual pairs, and links that point at a
later document. P29 made the *writer* correct: `_dedup_pass` now scans
oldest-first and only ever links to what `resolve_duplicate_root` says survives.
The writer only sees the 48-hour window, though, so the links already stored in
the corpus never heal on their own.

This module is that repair: a chronological, bounded, resumable and idempotent
sweep over the flagged rows, in the shape `tgfarm.s1_sources.backfill_reprints`
proved out — keyset pages ordered by `COALESCE(published_at, fetched_at)` then
`id`, commits per page, a wall-clock budget that stops between rows, and a report
that carries the resume cursor and the remainder for the next run.

The contract is the writer's contract, nothing looser:

* a row links to its resolved root — the surviving document at the end of its
  chain — and to nothing else;
* a link whose root appears *later* than the row under the same
  `(moment, id)` ordering the writer uses is dropped to NULL: the row is treated
  as its own document, because a repair may not invent a pointer the writer would
  refuse (bulk imports share one second, so the id tie-break is load-bearing and
  is kept);
* a self-link is dropped, and a link that resolves to nothing because its target
  is missing (dangling) is dropped too — `resolve_duplicate_root` documents that
  a dangling chain resolves to nothing, and the writer's own root test requires
  the target to exist;
* a cycle has no root, and `resolve_duplicate_root` deliberately refuses it, so
  following that refusal would leave cycle rows untouched forever. A cycle is
  instead broken in favour of its earliest member under the same `(moment, id)`
  contract: the earliest member becomes a root (`duplicate_of = NULL`) and every
  other member points at it. That is the minimal write that satisfies the
  contract — a member already pointing at the earliest is left exactly as it is.

Only rows whose stored value actually changes are written, so a second pass over
a repaired corpus is a provable no-op, and a chain the repair can neither resolve
nor classify as cycle/dangling (longer than `MAX_DUP_CHAIN_HOPS`, which the
walker from `tgfarm.s2_trends` refuses by construction) is counted and left
alone. Nothing here re-implements the root walk or the hop cap: the walk is
imported, and the one extra classification walk only says *why* the walker
refused — it never names a root.

Rows that were not damaged are not touched, and rows S2 will re-derive on the
next cycle (the 48-hour window) are repaired the same way: the stored contract is
identical to the writer's, so the two cannot disagree about the same row.

This module is deliberately *not* registered anywhere: running it is an operator
decision against a measured store, and the live database must never be opened
read-write without explicit consent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, and_, func, or_, select, update

from tgfarm.core.logging import get_logger
from tgfarm.core.run import ensure_utc
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.s2_trends import MAX_DUP_CHAIN_HOPS, resolve_duplicate_root

log = get_logger(__name__)

# A pipeline cycle is killed by the watchdog at 2700 s; this is hygiene, not the
# news. One slice per maintenance step, mirroring the reprint backfill.
DEFAULT_BUDGET_SECONDS = 60.0
# Rows per read. A row here is id + stored parent + one timestamp, never the
# embedding blob — that is what actually makes the table large.
DEFAULT_PAGE_SIZE = 200

COMPLETE = "complete"
BUDGET_STOPPED = "budget"
ROW_CAP_STOPPED = "row_cap"


@dataclass(frozen=True, slots=True)
class Cursor:
    """The last row the repair looked at, addressed by (its moment, its id).

    Returned in the report and accepted again as `resume_after`, so a bounded run
    picks up where the previous one stopped instead of re-examining history.
    """

    moment: datetime
    row_id: int

    @property
    def key(self) -> tuple[datetime, int]:
        return (ensure_utc(self.moment), self.row_id)


@dataclass(slots=True)
class RepairReport:
    """What one bounded repair pass did, and what it left behind.

    The `*_found` counters answer the dry-run question ("what would a real pass
    write"); the `links_rewritten` / `links_dropped` / `cycles_broken` counters
    only ever move when a statement actually changed a row. A field nobody
    measured is not printed as zero of something else: `links_left` is exactly
    "the walker refused this row and the repair could not name a safe root for
    it", nothing more.
    """

    rows_scanned: int = 0
    changes_found: int = 0
    links_rewritten: int = 0
    links_dropped: int = 0
    dropped_forward: int = 0
    dropped_self: int = 0
    dropped_dangling: int = 0
    dropped_cycle_earliest: int = 0
    cycles_found: int = 0
    cycles_broken: int = 0
    links_left: int = 0
    candidates_remaining: int = 0
    pages_read: int = 0
    undated_rows: int = 0
    stopped_by: str = COMPLETE
    elapsed_s: float = 0.0
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
            "changes_found": self.changes_found,
            "links_rewritten": self.links_rewritten,
            "links_dropped": self.links_dropped,
            "dropped_forward": self.dropped_forward,
            "dropped_self": self.dropped_self,
            "dropped_dangling": self.dropped_dangling,
            "dropped_cycle_earliest": self.dropped_cycle_earliest,
            "cycles_found": self.cycles_found,
            "cycles_broken": self.cycles_broken,
            "links_left": self.links_left,
            "candidates_remaining": self.candidates_remaining,
            "pages_read": self.pages_read,
            "undated_rows": self.undated_rows,
            "stopped_by": self.stopped_by,
            "budget_exhausted": self.budget_exhausted,
            "elapsed_s": self.elapsed_s,
            "dry_run": self.dry_run,
        }

    def __str__(self) -> str:
        return (
            f"scanned={self.rows_scanned} found={self.changes_found} "
            f"rewritten={self.links_rewritten} dropped={self.links_dropped} "
            f"cycles_broken={self.cycles_broken} left={self.links_left} "
            f"remaining={self.candidates_remaining} stopped_by={self.stopped_by} "
            f"in={self.elapsed_s}s"
        )


def _moment() -> Any:
    """A document's own clock: `published_at`, else `fetched_at`.

    Typed explicitly because `coalesce()` over two DATETIME columns would
    otherwise hand SQLite's storage strings back to Python. This is the same
    expression `_dedup_pass` orders by — one contract, read from one place.
    """
    return func.coalesce(
        RawItemRow.published_at, RawItemRow.fetched_at, type_=DateTime(timezone=True)
    )


def _candidate_conditions() -> list[Any]:
    """Flagged rows the sweep can place in time — its own candidate set.

    Unlike the backfill, a candidate here is a row that is *already linked*;
    whether its link needs changing is decided per row against the resolved root,
    not in SQL. A row with neither `published_at` nor `fetched_at` cannot be
    ordered and is counted in `undated_rows` rather than sorted to the front.
    """
    return [RawItemRow.duplicate_of.isnot(None), _moment().isnot(None)]


def _past(cursor: Cursor | None) -> Any:
    """Keyset predicate: strictly after the cursor in (moment, id) order."""
    if cursor is None:
        return None
    moment = _moment()
    return or_(moment > cursor.moment, and_(moment == cursor.moment, RawItemRow.id > cursor.row_id))


class _LinkStore:
    """`duplicate_of` and moment per row id, fetched lazily, cached per pass.

    Same role as the `parents` dict inside `_dedup_pass`: `resolve_duplicate_root`
    is a pure walker and needs a callable that answers "who does this row point
    at" for arbitrary ancestors. Rows are fetched by primary key with the two
    cheap columns only — never the ORM entity, whose embedding blob is the reason
    this table is hundreds of megabytes. A write made by this pass updates the
    cache too, so a later row that resolves *through* a repaired ancestor sees the
    repair, not the chain it just replaced.
    """

    def __init__(self, session) -> None:
        self._session = session
        self._parent: dict[int, int | None] = {}
        self._moment: dict[int, datetime | None] = {}

    def remember(self, row_id: int, parent: int | None, moment: datetime | None) -> None:
        self._parent[row_id] = parent
        self._moment[row_id] = moment

    def knows(self, row_id: int) -> bool:
        """True once this pass has taken a position on the row.

        The page read is a snapshot from before the writes that followed it: a
        cycle broken in an earlier row of the same page has already moved these
        links in the store *and* in the cache, and re-feeding the stale page
        value here would resurrect the cycle the pass itself just deleted.
        """
        return row_id in self._parent

    def _ensure(self, row_id: int) -> None:
        if row_id in self._parent:
            return
        row = self._session.execute(
            select(RawItemRow.id, RawItemRow.duplicate_of, _moment().label("moment")).where(
                RawItemRow.id == row_id
            )
        ).first()
        if row is None:
            # The store does not have this row: `resolve_duplicate_root` reads a
            # LookupError as "the chain ends in nothing" and refuses it, which is
            # exactly the signal this repair needs to count a dangling link.
            raise LookupError(row_id)
        self.remember(int(row.id), row.duplicate_of, ensure_utc(row.moment))

    def parent_of(self, row_id: int) -> int | None:
        self._ensure(row_id)
        return self._parent[row_id]

    def key_of(self, row_id: int) -> tuple[datetime, int] | None:
        """The row's place in the writer's ordering, or None if it has no clock."""
        self._ensure(row_id)
        moment = self._moment[row_id]
        return None if moment is None else (moment, row_id)

    def set_parent(self, row_id: int, parent: int | None) -> None:
        self._parent[row_id] = parent


def _classify_failure(start_id: int, parent_of: Callable[[int], int | None]) -> tuple[str, list[int]]:
    """Say *why* `resolve_duplicate_root` refused this chain.

    Returns `("cycle", members)` — the members of the cycle the chain enters,
    which includes `start_id` itself when the row sits in the cycle — or
    `("dangling", [])` when some ancestor is missing from the store, or
    `("overlong", [])` when the chain is longer than `MAX_DUP_CHAIN_HOPS` without
    repeating or ending. `("root", [id])` is a defensive branch that mirrors the
    walker's termination and should be unreachable once the walker has refused:
    if this walk finds a root, the caller applies the normal root decision rather
    than inventing a second one.

    This is not a second root resolver. It runs only on the refusal path, it
    borrows the walker's own hop bound by import, and its sole product is the
    membership list of a cycle — a structure that provably has no root.
    """
    seq = [start_id]
    index = {start_id: 0}
    current = start_id
    for _ in range(MAX_DUP_CHAIN_HOPS):
        try:
            parent = parent_of(current)
        except LookupError:
            return "dangling", []
        if parent is None:
            return "root", [current]
        if parent in index:
            return "cycle", seq[index[parent] :]
        index[parent] = len(seq)
        seq.append(parent)
        current = parent
    return "overlong", []


def _write_link(session, row_id: int, expected_parent: int, new_parent: int | None) -> int:
    """Rewrite one link only if the row still holds the value being repaired.

    The expected-value guard inside the WHERE clause is the idempotency and the
    concurrency guard in one statement: if another path has meanwhile re-pointed
    or cleared the row, the rowcount says "this repair did not change that row"
    and the report does not claim it.
    """
    result = session.execute(
        update(RawItemRow)
        .where(RawItemRow.id == row_id, RawItemRow.duplicate_of == expected_parent)
        .values(duplicate_of=new_parent)
    )
    return int(result.rowcount or 0)


def _drop(
    session, store: _LinkStore, report: RepairReport, row_id: int, reason: str, *, dry_run: bool
) -> None:
    """Clear one link to NULL, counting it under the field that names its reason."""
    expected = store.parent_of(row_id)
    if expected is None:
        return
    report.changes_found += 1
    if not dry_run and _write_link(session, row_id, expected, None):
        report.links_dropped += 1
        setattr(report, reason, getattr(report, reason) + 1)
    store.set_parent(row_id, None)


def _rewrite(
    session, store: _LinkStore, report: RepairReport, row_id: int, new_parent: int, *, dry_run: bool
) -> None:
    """Point one row at a different root, if that is not already its stored value."""
    expected = store.parent_of(row_id)
    if expected is None or expected == new_parent:
        return
    report.changes_found += 1
    if not dry_run and _write_link(session, row_id, expected, new_parent):
        report.links_rewritten += 1
    store.set_parent(row_id, new_parent)


def _apply_root(
    session,
    store: _LinkStore,
    report: RepairReport,
    row_id: int,
    row_key: tuple[datetime, int],
    root: int,
    *,
    dry_run: bool,
) -> None:
    """The writer's decision, applied to a resolved root instead of a fresh match.

    The root must be the row itself or appear strictly before it under
    `(moment, id)` — the same comparison `surviving_root` makes inside
    `_dedup_pass`. Anything else is a forward pointer no rewrite can save: the
    row becomes its own document again, which is what the writer would have done
    with a pair it cannot order backwards.
    """
    root_key = store.key_of(root)
    if root_key is None or root_key >= row_key:
        _drop(session, store, report, row_id, "dropped_forward", dry_run=dry_run)
        return
    _rewrite(session, store, report, row_id, root, dry_run=dry_run)


def _break_cycle(
    session,
    store: _LinkStore,
    report: RepairReport,
    row_id: int,
    row_key: tuple[datetime, int],
    members: list[int],
    *,
    dry_run: bool,
) -> None:
    """Break one cycle in favour of its earliest member.

    The earliest member (by the writer's `(moment, id)` order, not by id alone)
    loses its pointer and becomes the root; every other member ends up pointing
    at it — a member that *already* points there is the minimal-write case and
    stays exactly as it is. After this the store holds one root and a fan of
    one-hop links where the walk refused before, so the next row that resolves
    through any of them gets a real answer instead of another refusal.
    """
    keys: dict[int, tuple[datetime, int]] = {}
    for member in members:
        key = store.key_of(member)
        if key is None:
            # A cycle member with no clock cannot be ordered, so "earliest" is
            # not a fact here — leave the cycle and count it, never guess.
            report.links_left += 1
            return
        keys[member] = key
    earliest = min(members, key=lambda member: keys[member])
    report.cycles_found += 1

    for member in members:
        if member == earliest:
            continue
        _rewrite(session, store, report, member, earliest, dry_run=dry_run)
    # The earliest member's own link is what makes the cycle a cycle; dropping it
    # is the break itself, and `cycles_broken` counts breaks the pass actually
    # wrote, not cycles it recognised.
    earliest_parent = store.parent_of(earliest)
    if earliest_parent is not None:
        report.changes_found += 1
        if not dry_run and _write_link(session, earliest, earliest_parent, None):
            report.links_dropped += 1
            report.dropped_cycle_earliest += 1
            report.cycles_broken += 1
        store.set_parent(earliest, None)

    if row_id not in members:
        _apply_root(session, store, report, row_id, row_key, earliest, dry_run=dry_run)


def _repair_row(
    session,
    store: _LinkStore,
    report: RepairReport,
    row_id: int,
    row_key: tuple[datetime, int],
    *,
    dry_run: bool,
) -> None:
    """Bring one flagged row's link into line with the stored contract.

    A self-link is dropped without consulting the walker (which would refuse it,
    but "self" is worth its own honest counter). Everything else follows the
    root: resolve first, apply the writer's decision; only when the walker
    refuses does the classification run, and only a cycle gets repaired beyond
    the refusal — everything else the walker rejects is a link that cannot name a
    root, and is dropped (dangling) or left (overlong) rather than guessed at.
    """
    stored = store.parent_of(row_id)
    if stored is None:
        # Already unflagged underneath us (a concurrent clear, or a page read
        # against a row that changed mid-pass): nothing to repair.
        return
    if stored == row_id:
        _drop(session, store, report, row_id, "dropped_self", dry_run=dry_run)
        return

    root = resolve_duplicate_root(row_id, store.parent_of)
    if root is not None:
        _apply_root(session, store, report, row_id, row_key, int(root), dry_run=dry_run)
        return

    kind, found = _classify_failure(row_id, store.parent_of)
    if kind == "cycle":
        _break_cycle(session, store, report, row_id, row_key, found, dry_run=dry_run)
    elif kind == "dangling":
        _drop(session, store, report, row_id, "dropped_dangling", dry_run=dry_run)
    elif kind == "root":
        _apply_root(session, store, report, row_id, row_key, found[0], dry_run=dry_run)
    else:
        report.links_left += 1


def _read_page(session, *, after: Cursor | None, limit: int) -> list[tuple[int, int, datetime]]:
    """One chunk of flagged rows, oldest first. No embedding, no text."""
    stmt = select(RawItemRow.id, RawItemRow.duplicate_of, _moment().label("moment")).where(
        *_candidate_conditions()
    )
    predicate = _past(after)
    if predicate is not None:
        stmt = stmt.where(predicate)
    stmt = stmt.order_by(_moment(), RawItemRow.id).limit(max(1, int(limit)))
    rows = []
    for row in session.execute(stmt):
        rows.append((int(row.id), int(row.duplicate_of), ensure_utc(row.moment)))
    return rows


def _count_left(session, *, after: Cursor | None) -> int:
    """Flagged rows past the cut point — the backlog the next run re-examines.

    Honest by construction: correct links past the cursor are included, because a
    resume re-reads them and only *writes* the ones that changed. This number is
    "rows the next slice will scan", not "rows still broken".
    """
    stmt = select(func.count(RawItemRow.id)).where(*_candidate_conditions())
    predicate = _past(after)
    if predicate is not None:
        stmt = stmt.where(predicate)
    return int(session.execute(stmt).scalar_one() or 0)


def _count_undated(session) -> int:
    """Flagged rows with neither timestamp — outside the sweep, counted not dropped."""
    stmt = select(func.count(RawItemRow.id)).where(
        RawItemRow.duplicate_of.isnot(None), _moment().is_(None)
    )
    return int(session.execute(stmt).scalar_one() or 0)


def repair_duplicate_links(
    *,
    budget_seconds: float | None = DEFAULT_BUDGET_SECONDS,
    max_rows: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    resume_after: Cursor | None = None,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> RepairReport:
    """Rewrite stored duplicate links to the contract the fixed writer applies.

    Budgets: `budget_seconds` (wall clock) and `max_rows` (flagged rows
    examined), either or both; pass `None` for both to get an unbounded pass,
    which is what the tests and a watched one-off operator run want. `dry_run`
    walks the whole history and reports what a real pass would write —
    `changes_found` and `cycles_found` — without touching a row.

    Resumable: feed `report.next_cursor` back as `resume_after`; a bounded slice
    plus its resumes must converge the store to the same state an unbounded pass
    would produce, because every decision is made against the stored chain, not
    against anything remembered from an earlier page.
    """
    started = clock()
    deadline = None if budget_seconds is None else started + float(budget_seconds)
    rows_cap = None if max_rows is None else max(0, int(max_rows))
    report = RepairReport(dry_run=bool(dry_run))

    def _out_of_budget() -> str | None:
        """Why the sweep has to stop right now, or None to keep going.

        Tested per row, not per page — the same discipline as the reprint
        backfill: one page can itself be seconds of chain walking, and a stage
        that only notices its budget at page boundaries overshoots by however
        unlucky the last page was.
        """
        if deadline is not None and clock() >= deadline:
            return BUDGET_STOPPED
        if rows_cap is not None and report.rows_scanned >= rows_cap:
            return ROW_CAP_STOPPED
        return None

    with session_scope() as session:
        store = _LinkStore(session)
        cursor = resume_after
        stopped_by: str | None = None
        while stopped_by is None:
            stopped_by = _out_of_budget()
            if stopped_by is not None:
                break
            page = _read_page(session, after=cursor, limit=page_size)
            if not page:
                break
            report.pages_read += 1

            for row_id, stored_parent, row_moment in page:
                stopped_by = _out_of_budget()
                if stopped_by is not None:
                    break
                report.rows_scanned += 1
                if not store.knows(row_id):
                    store.remember(row_id, stored_parent, row_moment)
                row_key = (row_moment, row_id)
                _repair_row(session, store, report, row_id, row_key, dry_run=dry_run)
                cursor = Cursor(moment=row_moment, row_id=row_id)

            # Durable per page: a run killed mid-sweep keeps the links it already
            # repaired, so the next one neither repeats nor undoes them.
            session.commit()
            if stopped_by is not None:
                break

        report.undated_rows = _count_undated(session)
        report.candidates_remaining = _count_left(session, after=cursor)
        report.stopped_at = cursor
        report.stopped_by = stopped_by or COMPLETE
        report.elapsed_s = round(clock() - started, 3)

    log.info("duplicate repair: %s (dry_run=%s)", report, dry_run)
    return report


__all__ = [
    "BUDGET_STOPPED",
    "COMPLETE",
    "Cursor",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_PAGE_SIZE",
    "RepairReport",
    "ROW_CAP_STOPPED",
    "repair_duplicate_links",
]

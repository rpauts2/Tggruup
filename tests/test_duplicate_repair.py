"""The duplicate-link repair: the stored contract, applied to links that predate it.

P29 fixed the *writer* (`_dedup_pass` links only to a resolved root, never later
than the row), but the corpus still holds what the old newest-first pass left
behind: chains where one-hop pointers belong, mutual pairs, and links pointing
forward in time. These tests pin the repair to the writer's contract rather than
to a similar one:

* every surviving link is one hop to a root that appears strictly earlier under
  the `(moment, id)` order — the same pair the writer's `surviving_root` accepts;
* a cycle is broken in favour of its earliest member, not by guessing a root;
* a second pass over a repaired store writes provably nothing, including over
  corpora that contained cycles;
* a bounded slice plus its resumes converges to what the unbounded pass writes;
* `dry_run` measures the damage without touching a row;
* links that cannot name a safe root (dangling targets) are dropped, and links
  that are already correct are left exactly where they are.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import test_trends
from sqlalchemy import select, update

from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.s2_trends.duplicate_repair import RepairReport, repair_duplicate_links

# The store-wiping fixture S2's tests already use, borrowed from its own module
# (`tests/` has no `__init__.py`, so it is importable by top-level name), exactly
# as the backfill tests borrow it.
clean_items = test_trends.clean_items


def _insert(
    source_id: str,
    *,
    minutes_ago: int,
    duplicate_of: int | None = None,
    fetched_minutes_ago: int | None = None,
) -> int:
    """One stored row at a chosen place in time, optionally pre-flagged.

    This is what the repair inherits: a `duplicate_of` written by someone else,
    at whatever angle. The fingerprint never matters here — the repair reads the
    links, not the text — so `simhash` stays at its schema default.
    """
    now = datetime.now(UTC)
    with session_scope() as session:
        row = RawItemRow(
            source_id=source_id,
            external_id=source_id,
            url=f"https://{source_id}.invalid/story",
            title=f"story by {source_id}",
            text="",
            lang="ru",
            published_at=now - timedelta(minutes=minutes_ago),
            fetched_at=now - timedelta(minutes=fetched_minutes_ago or minutes_ago),
            niche_hint=["tech"],
            source_weight=0.6,
            embedding=None,
            duplicate_of=duplicate_of,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def _flag(row_id: int, target_id: int) -> None:
    """Point a stored row at another, the way the broken writer did."""
    with session_scope() as session:
        session.execute(
            update(RawItemRow).where(RawItemRow.id == row_id).values(duplicate_of=target_id)
        )


def _links() -> dict[int, int | None]:
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.id, RawItemRow.duplicate_of).order_by(RawItemRow.id)
        ).all()
    return {int(row.id): (None if row.duplicate_of is None else int(row.duplicate_of)) for row in rows}


def _moments() -> dict[int, datetime]:
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.id, RawItemRow.published_at, RawItemRow.fetched_at).order_by(
                RawItemRow.id
            )
        ).all()
    return {int(row.id): (row.published_at or row.fetched_at) for row in rows}


def _assert_stored_contract() -> None:
    """After any complete repair: one hop back in time, to a row that survived."""
    links = _links()
    moments = _moments()
    for row_id, parent in links.items():
        if parent is None:
            continue
        assert links.get(parent) is None, f"{row_id} links to {parent}, which is itself flagged"
        assert (moments[parent], parent) < (moments[row_id], row_id), (
            f"{row_id} links forward in time to {parent}"
        )


def _delete_row(row_id: int) -> None:
    """Hard-delete a row leaving children's pointers behind.

    The app engine enforces `PRAGMA foreign_keys=ON`, so an ORM delete of a
    referenced row would fail — which is the point: a dangling target can only
    exist as legacy damage (the live audit opens the store without that pragma),
    and this is exactly how a repair sees one.
    """
    path = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DELETE FROM raw_items WHERE id = ?", (row_id,))
        con.commit()
    finally:
        con.close()


class _FakeClock:
    """A clock that jumps, so the budget is tested without sleeping on it."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


# --------------------------------------------------------------- chain collapse
def test_a_chain_collapses_to_a_one_hop_pointer_to_the_root(clean_items):
    oldest = _insert("repair_chain_root", minutes_ago=300)
    middle = _insert("repair_chain_mid", minutes_ago=200, duplicate_of=oldest)
    newest = _insert("repair_chain_top", minutes_ago=100, duplicate_of=middle)

    report = repair_duplicate_links()

    assert report.rows_scanned == 2, "only the two flagged rows are candidates"
    assert report.links_rewritten == 1
    assert report.links_dropped == 0
    links = _links()
    assert links[oldest] is None
    assert links[middle] == oldest, "a correct one-hop link is not rewritten"
    assert links[newest] == oldest, "the chain top skips the middle and names the root"
    _assert_stored_contract()


def test_a_link_to_a_later_row_is_dropped_not_repointed(clean_items):
    """The old pass linked newest-first: the *earlier* row can hold the pointer.

    Repairing it to "whatever the chain ends at" would keep the forward link, and
    rewriting the direction would be inventing a relationship the store never
    measured. The writer's contract has exactly one answer: no link.
    """
    earlier = _insert("repair_fwd_a", minutes_ago=300)
    later = _insert("repair_fwd_b", minutes_ago=100)
    _flag(earlier, later)

    report = repair_duplicate_links()

    assert report.links_dropped == 1
    assert report.dropped_forward == 1
    assert report.links_rewritten == 0
    assert _links() == {earlier: None, later: None}


def test_the_id_tiebreak_decides_links_written_in_the_same_second(clean_items):
    """Bulk imports share one second; the contract is `(moment, id)`, not the clock.

    A→B inside one second is a forward link only because B's id is larger; B→A
    inside that same second is the link the writer would have made. The audit's
    563-vs-13370 spread is precisely this case, so the tie-break must be kept.
    """
    same = datetime.now(UTC)
    first = _insert("repair_tie_a", minutes_ago=0, duplicate_of=None)
    second = _insert("repair_tie_b", minutes_ago=0, duplicate_of=None)
    with session_scope() as session:
        for row_id in (first, second):
            session.execute(
                update(RawItemRow)
                .where(RawItemRow.id == row_id)
                .values(published_at=same, fetched_at=same)
            )
    _flag(second, first)
    _flag(first, second)

    report = repair_duplicate_links()

    links = _links()
    assert links[first] is None, "the earlier id is the earliest member: it becomes the root"
    assert links[second] == first, "the later id keeps its (already correct) backwards link"
    assert report.cycles_broken == 1
    assert report.links_rewritten == 0, "breaking a mutual pair minimally is one write"
    _assert_stored_contract()


# ---------------------------------------------------------------- cycle breaking
def test_a_mutual_pair_breaks_in_favour_of_its_earliest_member(clean_items):
    older = _insert("repair_mutual_old", minutes_ago=300)
    newer = _insert("repair_mutual_new", minutes_ago=100)
    _flag(older, newer)
    _flag(newer, older)

    report = repair_duplicate_links()

    assert report.cycles_found == 1
    assert report.cycles_broken == 1
    links = _links()
    assert links[older] is None, "the earliest member becomes the root"
    assert links[newer] == older
    assert report.dropped_cycle_earliest == 1
    assert report.links_dropped == report.dropped_cycle_earliest + report.dropped_forward + (
        report.dropped_self + report.dropped_dangling
    )
    _assert_stored_contract()


def test_a_longer_cycle_breaks_with_the_earliest_at_the_centre(clean_items):
    """Three rows, A→B→C→A: only the break and the links that must move are written."""
    oldest = _insert("repair_cycle_a", minutes_ago=400)
    middle = _insert("repair_cycle_b", minutes_ago=300)
    newest = _insert("repair_cycle_c", minutes_ago=200)
    _flag(oldest, middle)
    _flag(middle, newest)
    _flag(newest, oldest)

    report = repair_duplicate_links()

    assert report.cycles_broken == 1
    links = _links()
    assert links[oldest] is None
    assert links[middle] == oldest, "B pointed at C: it moves to the new root"
    assert links[newest] == oldest, "C already pointed at the earliest member: untouched"
    assert report.links_rewritten == 1
    assert report.changes_found == report.links_rewritten + report.links_dropped, (
        "the page snapshot of B's old link must not resurrect the cycle the "
        "break itself just removed: found and written are the same rows"
    )
    _assert_stored_contract()

    again = repair_duplicate_links()
    assert again.changes_found == 0 and again.links_rewritten == 0 and again.links_dropped == 0


def test_earliest_is_earliest_in_time_not_in_id(clean_items):
    """Feeds backdate: the smaller id can hold the *later* document.

    Without this the break would pick by id and the repaired link would point
    backwards along ids but forwards along time — the exact damage the audit
    counts.
    """
    later_id = _insert("repair_time_a", minutes_ago=30)  # id smaller, time later
    earlier_id = _insert("repair_time_b", minutes_ago=300)  # id larger, time earlier
    assert later_id < earlier_id, "the fixture must keep id order contradicting time order"
    _flag(later_id, earlier_id)
    _flag(earlier_id, later_id)

    repair_duplicate_links()

    links = _links()
    assert links[earlier_id] is None, "the earliest *document* becomes the root, not row 1"
    assert links[later_id] == earlier_id
    _assert_stored_contract()


# ------------------------------------------------------------- second pass: no-op
def test_a_second_pass_writes_nothing_even_over_broken_cycles(clean_items):
    """Idempotence is the whole point of a repair that runs from a cron step."""
    a = _insert("repair_idem_a", minutes_ago=500)
    b = _insert("repair_idem_b", minutes_ago=400, duplicate_of=a)
    c = _insert("repair_idem_c", minutes_ago=300, duplicate_of=b)
    m1 = _insert("repair_idem_m1", minutes_ago=200)
    m2 = _insert("repair_idem_m2", minutes_ago=100)
    _flag(m1, m2)
    _flag(m2, m1)
    fwd = _insert("repair_idem_fwd", minutes_ago=50)
    _flag(fwd, c)

    first = repair_duplicate_links()
    assert first.changes_found > 0
    assert first.links_rewritten + first.links_dropped == first.changes_found

    before = _links()
    second = repair_duplicate_links()

    assert second.rows_scanned == len([row_id for row_id, parent in before.items() if parent])
    assert second.changes_found == 0
    assert second.links_rewritten == 0
    assert second.links_dropped == 0
    assert second.cycles_found == 0
    assert second.cycles_broken == 0
    assert _links() == before
    _assert_stored_contract()


# ------------------------------------------------------------------------ budget
def test_a_bounded_slice_and_its_resume_match_the_unbounded_pass(clean_items):
    """A stop mid-history must change when the repair runs, never what it decides."""

    def corpus() -> None:
        root1 = _insert("repair_budge_r1", minutes_ago=900)
        mid1 = _insert("repair_budge_m1", minutes_ago=800, duplicate_of=root1)
        _insert("repair_budge_c1", minutes_ago=700, duplicate_of=mid1)
        root2 = _insert("repair_budge_r2", minutes_ago=600)
        mid2 = _insert("repair_budge_m2", minutes_ago=500, duplicate_of=root2)
        _insert("repair_budge_c2", minutes_ago=400, duplicate_of=mid2)

    corpus()
    # step 3s against a 7s budget: the pass examines one row, then the deadline
    # passes it — the report says "budget", not "done".
    bounded = repair_duplicate_links(budget_seconds=7.0, page_size=2, clock=_FakeClock(step=3.0))
    assert bounded.stopped_by == "budget"
    assert bounded.budget_exhausted is True
    assert 0 < bounded.rows_scanned < 4
    assert bounded.candidates_remaining == 4 - bounded.rows_scanned
    assert bounded.next_cursor is not None

    resumed = repair_duplicate_links(resume_after=bounded.next_cursor)
    assert resumed.stopped_by == "complete"
    assert resumed.rows_scanned == bounded.candidates_remaining

    links = _links()
    assert sum(1 for parent in links.values() if parent is not None) == 4
    _assert_stored_contract()
    # The resume lands where the unbounded pass would have: each copy names its
    # own group's root directly — never the middle row it was stored against.
    with session_scope() as session:
        pairs = session.execute(
            select(RawItemRow.source_id, RawItemRow.duplicate_of, RawItemRow.id).order_by(
                RawItemRow.id
            )
        ).all()
    by_id = {int(row.id): row.source_id for row in pairs}
    named = {
        row.source_id: (None if row.duplicate_of is None else by_id[int(row.duplicate_of)])
        for row in pairs
    }
    assert named["repair_budge_c1"] == "repair_budge_r1"
    assert named["repair_budge_c2"] == "repair_budge_r2"

    again = repair_duplicate_links()
    assert again.links_rewritten == 0 and again.links_dropped == 0, "no double repair on resume"


def test_a_row_cap_stops_between_rows(clean_items):
    root = _insert("repair_cap_r", minutes_ago=300)
    mid = _insert("repair_cap_m", minutes_ago=200, duplicate_of=root)
    top = _insert("repair_cap_t", minutes_ago=100, duplicate_of=mid)

    capped = repair_duplicate_links(max_rows=1, budget_seconds=None)
    assert capped.stopped_by == "row_cap"
    assert capped.rows_scanned == 1
    assert capped.candidates_remaining == 1

    finished = repair_duplicate_links(resume_after=capped.next_cursor)
    assert finished.links_rewritten == 1
    assert _links()[top] == root
    assert _links()[mid] == root


# ----------------------------------------------------------------------- dry run
def test_dry_run_measures_the_damage_without_writing_it(clean_items):
    a = _insert("repair_dry_a", minutes_ago=500)
    b = _insert("repair_dry_b", minutes_ago=400, duplicate_of=a)
    _insert("repair_dry_c", minutes_ago=300, duplicate_of=b)
    m1 = _insert("repair_dry_m1", minutes_ago=200)
    m2 = _insert("repair_dry_m2", minutes_ago=100)
    _flag(m1, m2)
    _flag(m2, m1)

    before = _links()
    preview = repair_duplicate_links(dry_run=True)

    assert _links() == before, "a measurement pass must not touch the corpus"
    assert preview.links_rewritten == 0
    assert preview.links_dropped == 0
    assert preview.cycles_broken == 0
    assert preview.changes_found == 2, "the chain collapse and the cycle break"
    assert preview.cycles_found == 1

    real = repair_duplicate_links()
    assert real.links_rewritten + real.links_dropped == preview.changes_found, (
        "the dry run previews exactly what the real pass writes"
    )
    assert real.cycles_broken == preview.cycles_found
    _assert_stored_contract()


# ------------------------------------------------------------------ missing data
def test_a_link_to_a_missing_target_is_dropped(clean_items):
    """`resolve_duplicate_root` refuses a dangling chain; a refusal names no root."""
    gone = _insert("repair_gone", minutes_ago=400)
    flagged = _insert("repair_dangle", minutes_ago=300, duplicate_of=gone)
    chained = _insert("repair_dangle_top", minutes_ago=200, duplicate_of=flagged)
    _delete_row(gone)

    report = repair_duplicate_links()

    links = _links()
    assert links[flagged] is None, "a link into a missing row names no root and is dropped"
    assert links[chained] == flagged, (
        "the chain top resolves through a row that was repaired to a root itself: "
        "its link is only damaged by the missing row, not by the drop"
    )
    assert report.dropped_dangling == 1
    assert report.links_rewritten == 0
    _assert_stored_contract()


def test_a_self_link_is_dropped_without_calling_it_a_cycle(clean_items):
    row = _insert("repair_self", minutes_ago=100)
    _flag(row, row)

    report = repair_duplicate_links()

    assert _links()[row] is None
    assert report.dropped_self == 1
    assert report.cycles_broken == 0, "a 1-cycle is reported as a self link, not a cycle"


def test_correct_links_are_left_exactly_where_they_are(clean_items):
    root = _insert("repair_ok_root", minutes_ago=300)
    copy = _insert("repair_ok_copy", minutes_ago=100, duplicate_of=root)

    report = repair_duplicate_links()

    assert report.rows_scanned == 1
    assert report.changes_found == 0
    links = _links()
    assert links[root] is None
    assert links[copy] == root


# ------------------------------------------------------------------------ shape
def test_the_report_is_a_plain_dict_for_the_maintenance_journal(clean_items):
    a = _insert("repair_shape_a", minutes_ago=200)
    b = _insert("repair_shape_b", minutes_ago=100)
    _flag(a, b)

    payload = repair_duplicate_links().as_dict()

    assert payload["stopped_by"] == "complete"
    assert payload["budget_exhausted"] is False
    assert payload["links_dropped"] == 1
    assert payload["dropped_forward"] == 1
    assert set(payload) >= {"rows_scanned", "cycles_broken", "candidates_remaining", "elapsed_s"}
    assert "dry_run" in payload and "undated_rows" in payload
    assert isinstance(repair_duplicate_links(), RepairReport)


def test_an_empty_store_repairs_nothing(clean_items):
    report = repair_duplicate_links()
    assert report.rows_scanned == 0
    assert report.changes_found == 0
    assert report.candidates_remaining == 0
    assert report.stopped_by == "complete"
    assert report.next_cursor is None


def test_repairing_does_not_disturb_the_s2_writer(clean_items):
    """The repaired store must be exactly what a fresh `_dedup_pass` reads happily.

    Not a behaviour test of S2 — a guard that the repair's vocabulary is the
    writer's: after the repair, S2's own root walk over the same rows finds the
    links it would have written, so the two paths cannot drift.
    """
    from tgfarm.s2_trends import resolve_duplicate_root

    old = _insert("repair_drift_old", minutes_ago=9000)
    mid = _insert("repair_drift_mid", minutes_ago=8000, duplicate_of=old)
    top = _insert("repair_drift_top", minutes_ago=7000, duplicate_of=mid)
    repair_duplicate_links()

    stored = _links()
    parents = {row_id: parent for row_id, parent in stored.items()}

    def parent_of(target_id: int) -> int | None:
        if target_id not in parents:
            raise LookupError(target_id)
        return parents[target_id]

    assert resolve_duplicate_root(top, parent_of) == old
    assert resolve_duplicate_root(mid, parent_of) == old
    assert resolve_duplicate_root(old, parent_of) == old


# ----------------------------------------------------------- fixture self-check
def test_the_contract_guard_actually_detects_damage(clean_items):
    """Guards `_assert_stored_contract` against silently passing everything.

    A forward link is exactly what the guard is for: if it cannot see this one,
    every repair assertion that relies on it is worthless.
    """
    early = _insert("repair_seed_early", minutes_ago=300)
    late = _insert("repair_seed_late", minutes_ago=100)
    _flag(early, late)

    with pytest.raises(AssertionError):
        _assert_stored_contract()

    repair_duplicate_links()
    _assert_stored_contract()

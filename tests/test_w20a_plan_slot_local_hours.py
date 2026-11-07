"""W20-A: the editorial plan books only hours its channel may publish in.

Two rules met in the same column and disagreed. Wave 19 put the plan's slot on
the clock: `_next_free_slot` reserves an instant, S4 copies it into
``Post.plan_slot_at`` once at insert, and from `0d8fd6f` `schedule_ready_posts`
publishes exactly at that booking — while `d848eee` made it *refuse* a booking
outside the channel's legal local hours (`GATE_SLOT_OUTSIDE_LOCAL_HOURS`)
instead of quietly moving it. The refusal is the right rule: a scheduler that
re-slots the plan's decision makes the booking column meaningless.

What was wrong is the other half: `_next_free_slot` looked at occupancy only.
Measured on `main` at `d848eee` with the clock at 21:00 UTC, a UTC channel and a
`[9, 18]` window, it returned **21:30** — a slot the scheduler then refused on
every tick, forever: no ``scheduled_at`` ever appears, and the same gate keeps
being counted in the journal. The booking site is the only place that can settle
this without breaking either rule, so the check here is the same shared window
predicate the scheduler obeys: `plan_execution.local_hours_allow`.

The rules being pinned, each by its own test:

1. no window (24/7) → the pre-W20-A answer, bit for bit, including `None` when
   the horizon is full, and no record written — the shipped cadence is
   `active_hours: null`, so this is the live path and it must not shift by a
   second;
2. a window → the first instant that is free *and* legal, judged in the
   *channel's* timezone;
3. a horizon whose legal hours are all occupied → `None`, never the nearest
   illegal instant (that would recreate the permanent refusal this change exists
   to remove);
4. rows already in the database are not repaired: an old illegal ``slot_at``
   stays illegal until a newer snapshot supersedes it — stated as a residue,
   because silently turning an old forbidden slot into a legal one is the same
   lie as the scheduler re-slotting a live booking.

Determinism and shared-DB hygiene: the clock is a frozen literal everywhere;
rows are seeded under unique `w20a_` ids and deleted by those ids in an autouse
finisher; every count is a delta around one call, never an absolute table count
(`tests/conftest.py` gives the whole suite one SQLite file).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from tgfarm import plan_execution
from tgfarm.core.settings import get_settings, reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial_plan import (
    SLOT_START_OFFSET_MINUTES,
    SLOT_STEP_MINUTES,
    _next_free_slot,
    build_editorial_plan,
)
from tgfarm.plan_execution import local_hours_allow

#: `active_hours` window used by every cell here: legal local hours are 09:00
#: through 17:59 (`_legal_local_hours` reads the pair as `range(9, 18)`).
LEGAL_WINDOW = [9, 18]

#: The instant the builder clock is frozen to. A Wednesday, chosen because
#: 21:00 + SLOT_START_OFFSET_MINUTES = 21:30 — the exact illegal booking the
#: conflict produced, measured on `main` (see `GOLDEN_WINDOW_NONE`).
FROZEN_NOW = datetime(2026, 11, 4, 21, 0, 0, tzinfo=UTC)
#: Second frozen clock: its first candidate (09:30) *is* legal, so a cell can
#: show the window discarding candidates that are free but forbidden.
MORNING_NOW = datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)

#: The `main` answer for `FROZEN_NOW` on a UTC channel with nothing occupied.
MAIN_ILLEGAL_BOOKING = datetime(2026, 11, 4, 21, 30, tzinfo=UTC)
UTC_CHANNEL = "UTC"

_seeded_channel_ids: list[str] = []
_seeded_post_ids: list[str] = []
_seeded_cluster_ids: list[str] = []
_seeded_plan_public_ids: list[str] = []


@pytest.fixture(autouse=True)
def _leave_no_residue():
    """Delete the rows this file created, by the ids it remembered.

    One transaction per step and no absolute counters: the rest of the suite
    shares this database and owns its own rows.
    """
    yield
    with session_scope() as session:
        if _seeded_plan_public_ids:
            plan_ids = list(
                session.execute(
                    select(EditorialPlan.id).where(
                        EditorialPlan.public_id.in_(_seeded_plan_public_ids)
                    )
                ).scalars().all()
            )
            if plan_ids:
                session.execute(
                    delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
                )
                # A superseded snapshot keeps `superseded_by_id` pointing at the
                # row this test wrote, so the link is unhooked before the plan goes.
                session.execute(
                    delete(EditorialPlan).where(
                        EditorialPlan.id.in_(plan_ids) | EditorialPlan.superseded_by_id.in_(plan_ids)
                    )
                )
        if _seeded_post_ids:
            session.execute(delete(Post).where(Post.public_id.in_(_seeded_post_ids)))
        if _seeded_cluster_ids:
            session.execute(delete(Cluster).where(Cluster.public_id.in_(_seeded_cluster_ids)))
        if _seeded_channel_ids:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channel_ids)))
    _seeded_channel_ids.clear()
    _seeded_post_ids.clear()
    _seeded_cluster_ids.clear()
    _seeded_plan_public_ids.clear()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _window_config(window: list[int] | None) -> Any:
    """A cadence owner whose only interesting field is `active_hours`.

    `local_hours_allow` reads `config.schedule.cadence` and nothing else, so the
    stub carries exactly that and no more: a fake that also faked `hours()` or
    `hour_probability()` would be a second implementation of the window, which
    is the thing this change refuses to write.
    """
    return SimpleNamespace(
        schedule=SimpleNamespace(cadence={"mode": "interval", "active_hours": window})
    )


def _channel(*, timezone_name: str = UTC_CHANNEL) -> Channel:
    """An in-memory Channel: `_next_free_slot` reads `id` and `timezone` only."""
    return Channel(
        id=f"w20a_ch_{_uid()}",
        platform="telegram",
        title="w20a channel",
        niche="",
        enabled=True,
        timezone=timezone_name,
    )


def _candidate_grid(now: datetime, horizon_days: int) -> list[datetime]:
    """The slot grid the builder walks, for seeding an *occupied* set.

    Never used to predict an expected answer: a test that recomputed the slot
    from the production arithmetic would pass even if that arithmetic ignored
    the window.
    """
    step = timedelta(minutes=SLOT_STEP_MINUTES)
    out: list[datetime] = []
    candidate = now + timedelta(minutes=SLOT_START_OFFSET_MINUTES)
    limit = now + timedelta(days=horizon_days)
    for _ in range(int(horizon_days * 24 * 60 / SLOT_STEP_MINUTES) + 2):
        out.append(candidate.replace(second=0, microsecond=0))
        candidate = candidate + step
        if candidate > limit:
            break
    return out


# ------------------------------------------------------------------- rule 1: 24/7
#: Measured off `main` (`d848eee`) by calling its `_next_free_slot` directly, so
#: "the 24/7 answer did not move" is an assertion against recorded output and
#: not against a description of the algorithm.
GOLDEN_WINDOW_NONE = [
    (
        "nothing occupied",
        FROZEN_NOW,
        frozenset(),
        UTC_CHANNEL,
        MAIN_ILLEGAL_BOOKING,
    ),
    (
        "first candidate occupied",
        FROZEN_NOW,
        frozenset({MAIN_ILLEGAL_BOOKING}),
        UTC_CHANNEL,
        datetime(2026, 11, 4, 23, 0, tzinfo=UTC),
    ),
    (
        "horizon full",
        FROZEN_NOW,
        frozenset(_candidate_grid(FROZEN_NOW, 1)),
        UTC_CHANNEL,
        None,
    ),
    (
        "fractional clock is truncated, not rounded forward",
        datetime(2026, 11, 4, 21, 0, 37, 500000, tzinfo=UTC),
        frozenset(),
        UTC_CHANNEL,
        MAIN_ILLEGAL_BOOKING,
    ),
    (
        "non-UTC timezone is still ignored, because nothing is forbidden",
        FROZEN_NOW,
        frozenset(),
        "Europe/Moscow",
        MAIN_ILLEGAL_BOOKING,
    ),
]


@pytest.mark.parametrize(
    ("tag", "now", "occupied", "timezone_name", "expected"),
    GOLDEN_WINDOW_NONE,
    ids=[row[0] for row in GOLDEN_WINDOW_NONE],
)
def test_no_window_reproduces_the_pre_change_slot_bit_for_bit(
    tag: str,
    now: datetime,
    occupied: frozenset[datetime],
    timezone_name: str,
    expected: datetime | None,
) -> None:
    """`active_hours: null` is the shipped cadence, so this is the live path.

    Red under any change that shifts a 24/7 answer: a moved first slot, a
    different truncation, a window read that consults the channel timezone even
    though nothing is forbidden. The `horizon full` cell is the other half of the
    promise — a genuinely full 24/7 horizon still answers `None`.
    """
    channel = _channel(timezone_name=timezone_name)

    got = _next_free_slot(channel, {channel.id: set(occupied)}, now, 1, config=_window_config(None))

    assert got == expected, f"{tag}: main produces {expected}, this tree produces {got}"
    if expected is None:
        assert got is None, f"{tag}: a full horizon must stay None, got {got!r}"


def test_the_builder_with_no_window_books_the_very_instant_main_booked(monkeypatch, db) -> None:
    """The same promise one level up: `slot_at` on the entry, in ISO.

    The unit cell above proves the arithmetic; this one proves nothing else in
    the build path moved either — the entry the builder writes for a gated draft
    under a 24/7 cadence still carries 21:30, the instant `main` carried. If a
    later change makes the builder re-round, re-offset or re-read the timezone,
    this cell is the one that says so.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(None))
    channel_id, _public_id, cluster_id, _note = _seed_gated_draft(db, timezone_name=UTC_CHANNEL)

    plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=7, persist=False)
    entry = _schedule_entry(plan, cluster_id)

    assert datetime.fromisoformat(entry["slot_at"]) == MAIN_ILLEGAL_BOOKING, entry["slot_at"]
    assert entry["channel_id"] == channel_id


def test_a_no_window_build_writes_no_record_of_its_own(monkeypatch, db) -> None:
    """The other half of "identical behaviour": no extra row, no extra ledger.

    `persist=False` is what the HTTP preview calls, and the window check must not
    turn a preview into a write. Counts are deltas around one call, because the
    whole suite shares this database.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(None))
    _seed_gated_draft(db, timezone_name=UTC_CHANNEL)
    before_plans = _count(EditorialPlan)
    before_entries = _count(EditorialPlanEntry)
    before_posts = _count(Post)

    plan = build_editorial_plan(now=FROZEN_NOW, persist=False)

    assert plan["status"] == "transient"
    assert _count(EditorialPlan) == before_plans, plan["public_id"]
    assert _count(EditorialPlanEntry) == before_entries, plan["public_id"]
    assert _count(Post) == before_posts, plan["public_id"]


# ------------------------------------------------------------------- rule 2: window
def test_a_one_hour_legal_window_is_not_skipped_by_the_90_minute_grid() -> None:
    """The 08:30 → 10:00 grid misses [09:00, 10:00); the legal 09:30 does not."""
    channel = _channel()
    config = _window_config([9, 10])
    now = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)

    got = _next_free_slot(channel, {}, now, 1, config=config)

    assert got == datetime(2026, 11, 4, 9, 30, tzinfo=UTC), got
    assert local_hours_allow(got, channel.timezone, config=config) is True


def test_a_full_short_window_moves_to_the_next_day_not_an_illegal_grid_point() -> None:
    """An occupied legal candidate must not fall back to the next forbidden grid point."""
    channel = _channel()
    config = _window_config([9, 10])
    now = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    first_legal = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)
    next_legal = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)

    got = _next_free_slot(
        channel, {channel.id: {first_legal}}, now, 2, config=config
    )

    assert got == next_legal, got
    assert local_hours_allow(got, channel.timezone, config=config) is True


def test_a_legal_window_skips_the_illegal_instant_main_booked() -> None:
    """The headline cell: `main` books 21:30, this tree books 09:30 next day.

    Without the check the answer is `MAIN_ILLEGAL_BOOKING` (pinned by
    `GOLDEN_WINDOW_NONE[0]`), and 21:30 is exactly what `schedule_ready_posts`
    refuses for a `[9, 18]` channel: the post never gets `scheduled_at` and the
    same gate counts again on every tick. So the booking site has to answer the
    predicate the scheduler obeys — here the first free candidate whose local
    hour is inside the window is 09:30 UTC on 11-05 (11-04 is over: 21:30, 23:00,
    00:30, 02:00, 03:30, 05:00, 06:30, 08:00 are all forbidden by `[9, 18]`).
    """
    channel = _channel()
    config = _window_config(LEGAL_WINDOW)

    got = _next_free_slot(channel, {}, FROZEN_NOW, 1, config=config)

    assert got == datetime(2026, 11, 5, 9, 30, tzinfo=UTC), got
    assert got != MAIN_ILLEGAL_BOOKING, "the refused booking is back"
    assert local_hours_allow(got, channel.timezone, config=config) is True


def test_a_fractional_clock_is_booked_on_a_whole_minute_under_the_window_too() -> None:
    """The window check reads the instant that gets written, seconds and all.

    Measured on `main`: a clock at 21:00:37.5 booked 21:30:00 (the candidate is
    truncated, never rounded forward) and a clock at 08:59:40 booked 09:29:00.
    Under `[9, 18]` the first answer becomes 09:30:00 on 11-05 — still whole
    minutes — while the second stays 09:29:00, because hour 9 is legal. A filter
    that judged the untruncated candidate, or that wrote the raw candidate out,
    moves one of these two.
    """
    config = _window_config(LEGAL_WINDOW)
    no_window = _window_config(None)
    late_clock = datetime(2026, 11, 4, 21, 0, 37, 500000, tzinfo=UTC)

    late = _channel()
    assert _next_free_slot(late, {}, late_clock, 7, config=no_window) == MAIN_ILLEGAL_BOOKING
    got_late = _next_free_slot(late, {}, late_clock, 7, config=config)
    assert got_late == datetime(2026, 11, 5, 9, 30, tzinfo=UTC), got_late
    assert (got_late.second, got_late.microsecond) == (0, 0), got_late

    early = _channel()
    got_early = _next_free_slot(
        early, {}, datetime(2026, 11, 4, 8, 59, 40, 123456, tzinfo=UTC), 7, config=config
    )
    assert got_early == datetime(2026, 11, 4, 9, 29, tzinfo=UTC), got_early
    assert local_hours_allow(got_early, early.timezone, config=config) is True


def test_the_window_is_judged_in_the_channels_own_timezone() -> None:
    """A Moscow channel's 09:30 is a different UTC instant than a UTC one's.

    The cell that shows `channel.timezone` is really threaded through instead of
    the local hour being read off UTC: the same clock and the same window answer
    06:30 UTC for `Europe/Moscow` (09:30 local) and 09:30 UTC for `UTC`. Were the
    timezone dropped, both would answer 09:30 UTC and the Moscow booking would be
    12:30 local — legal by luck of the 90-minute grid, which is precisely the
    kind of agreement that stops holding when the step or the window moves.
    """
    config = _window_config(LEGAL_WINDOW)
    moscow = _channel(timezone_name="Europe/Moscow")
    utc = _channel()

    got_moscow = _next_free_slot(moscow, {}, FROZEN_NOW, 7, config=config)
    got_utc = _next_free_slot(utc, {}, FROZEN_NOW, 7, config=config)

    assert got_moscow == datetime(2026, 11, 5, 6, 30, tzinfo=UTC), got_moscow
    assert got_utc == datetime(2026, 11, 5, 9, 30, tzinfo=UTC), got_utc
    assert got_utc != got_moscow, "the channel timezone never reached the check"
    assert local_hours_allow(got_moscow, "Europe/Moscow", config=config) is True
    # The same instant read as UTC is outside the window: the two readings are
    # genuinely different, so passing the wrong column would be caught here.
    assert local_hours_allow(got_moscow, UTC_CHANNEL, config=config) is False


def test_every_booked_instant_is_legal_for_every_start_hour() -> None:
    """The property, not one lucky cell: whatever the clock, no forbidden slot.

    Twenty-four frozen clocks over a 7-day horizon, one fresh channel each. The
    last assert keeps this honest — a filter that refused *everything* would pass
    the loop, so at least one clock must still get a slot, and it must be legal.
    """
    config = _window_config(LEGAL_WINDOW)
    booked = 0
    for hour in range(24):
        channel = _channel()
        now = MORNING_NOW.replace(hour=hour)
        got = _next_free_slot(channel, {}, now, 7, config=config)
        if got is None:
            continue
        booked += 1
        assert local_hours_allow(got, channel.timezone, config=config) is True, (
            f"booked {got} for a start at {now}"
        )
        assert got > now, f"a booking in the past: {got} <= {now}"
    assert booked == 24, f"the window refused some horizon outright: {booked}/24 booked"


# ------------------------------------------------------------------- rule 3: full
def test_a_horizon_whose_legal_hours_are_full_answers_none_not_the_nearest_illegal() -> None:
    """Occupied legal hours + a window = no slot, exactly like a full 24/7 week.

    Measured on `main` these inputs answered 18:30 — the first free candidate
    after the six legal-and-occupied ones, and an instant a `[9, 18]` channel
    cannot publish in. The honest answer is `None`: the entry goes out as a
    `schedule` decision with no slot, which `plan_execution` already reports as
    `no_slot_planned` instead of publishing outside the window. Substituting the
    nearest forbidden instant would put back the permanent refusal this change
    exists to remove.
    """
    channel = _channel()
    config = _window_config(LEGAL_WINDOW)
    legal_hours_occupied = {
        datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
        datetime(2026, 11, 4, 11, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 12, 30, tzinfo=UTC),
        datetime(2026, 11, 4, 14, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 15, 30, tzinfo=UTC),
        datetime(2026, 11, 4, 17, 0, tzinfo=UTC),
    }
    main_answer = datetime(2026, 11, 4, 18, 30, tzinfo=UTC)
    assert local_hours_allow(main_answer, channel.timezone, config=config) is False

    got = _next_free_slot(
        channel, {channel.id: set(legal_hours_occupied)}, MORNING_NOW, 1, config=config
    )

    assert got is None, f"the horizon has no legal free instant, got {got!r}"
    assert got != main_answer, "the nearest illegal instant was substituted for no slot"

    # Same channel, one legal instant left over: the `None` above is the
    # horizon's fault, not a blanket refusal.
    one_left = set(legal_hours_occupied)
    one_left.remove(datetime(2026, 11, 4, 14, 0, tzinfo=UTC))
    assert _next_free_slot(channel, {channel.id: one_left}, MORNING_NOW, 1, config=config) == (
        datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
    )


# ------------------------------------------------------------------- rule 4: rows
def test_the_builder_books_a_slot_the_scheduler_will_accept(monkeypatch, db) -> None:
    """End of the build path: the entry's `slot_at` is an instant s7 accepts.

    `_next_free_slot` is reached from `build_editorial_plan` only for a gated
    draft, and the value it returns is the one S4 copies into
    ``Post.plan_slot_at``. The window is installed where the builder finds it —
    `plan_execution.get_config`, the one cadence owner the scheduler and the
    executor already read — and the assertion is made on the report's own ISO
    `slot_at`, which is what the panel and `plan_execution._slot_at` parse.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(LEGAL_WINDOW))
    channel_id, _public_id, cluster_id, _note = _seed_gated_draft(db, timezone_name=UTC_CHANNEL)

    plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=7, persist=False)
    entry = _schedule_entry(plan, cluster_id)

    slot = datetime.fromisoformat(entry["slot_at"])
    assert slot == datetime(2026, 11, 5, 9, 30, tzinfo=UTC), entry["slot_at"]
    assert local_hours_allow(slot, UTC_CHANNEL, config=_window_config(LEGAL_WINDOW)) is True
    assert entry["channel_id"] == channel_id


def test_an_old_illegal_slot_at_stays_illegal(monkeypatch, db) -> None:
    """The residue, named: this is the build path, not a migration.

    A plan entry written before W20-A can still carry 21:30 for a `[9, 18]`
    channel, and that post keeps being refused. Nothing here rewrites it — a
    snapshot is the record of a decision that was really made, and quietly
    turning an old forbidden slot into a legal one is the same lie as the
    scheduler re-slotting a live booking. What closes the hole is the next
    snapshot: the new entry books a legal instant and the old plan is superseded.
    Both halves are pinned, so the fix says what it fixes and what it leaves.

    (The other half of the residue is outside this file's write-area: s7 has no
    re-booking path for a post whose `plan_slot_at` is illegal, so such a post
    stays `gated` until a human or a later wave moves it. `d848eee` made that
    refusal visible in the ledger, which is the honest state of it.)
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(LEGAL_WINDOW))
    channel_id, _public_id, cluster_id, _note = _seed_gated_draft(db, timezone_name=UTC_CHANNEL)

    old_public_id = f"w20a_oldplan_{_uid()}"
    # Dated in the future on purpose: `build_editorial_plan` supersedes the
    # *newest* active snapshot, and the shared suite database may hold another
    # file's active plan. A future date makes mine the newest, so "the old
    # snapshot got superseded" is a fact about this rule and not about the order
    # the suite happened to run in. The finisher deletes it by its public id.
    old_created = datetime(2099, 1, 1, tzinfo=UTC)
    with session_scope() as session:
        old_plan = EditorialPlan(
            public_id=old_public_id,
            created_at=old_created,
            horizon_days=7,
            status="active",
            summary={},
        )
        session.add(old_plan)
        session.flush()
        session.add(
            EditorialPlanEntry(
                plan_id=old_plan.id,
                cluster_public_id=cluster_id,
                title="w20a residue",
                niche="",
                action="schedule",
                platform="telegram",
                channel_id=channel_id,
                priority=1,
                confidence=0.5,
                reasons=["запись плана, сделанная до W20-A"],
                slot_at=MAIN_ILLEGAL_BOOKING,
                payload={},
            )
        )
        old_entry_id = int(
            session.execute(
                select(EditorialPlanEntry.id).where(EditorialPlanEntry.plan_id == old_plan.id)
            ).scalar_one()
        )
    _seeded_plan_public_ids.append(old_public_id)

    assert _slot_of_entry(old_entry_id) == MAIN_ILLEGAL_BOOKING, (
        "the read path has already rewritten history"
    )

    plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=7, persist=True)
    _seeded_plan_public_ids.append(plan["public_id"])

    assert _slot_of_entry(old_entry_id) == MAIN_ILLEGAL_BOOKING, (
        "an existing booking was rewritten: the old snapshot is history, not input"
    )
    with session_scope() as session:
        old_row = session.execute(
            select(EditorialPlan.status).where(EditorialPlan.public_id == old_public_id)
        ).scalar_one()
    assert old_row == "superseded", old_row

    entry = _schedule_entry(plan, cluster_id)
    assert datetime.fromisoformat(entry["slot_at"]) == datetime(2026, 11, 5, 9, 30, tzinfo=UTC)


# ------------------------------------------------------------------- rule 5: flag off
def test_flag_off_reads_the_base_tree_with_the_window_installed(monkeypatch) -> None:
    """§6: `PLAN_EXECUTION_ENABLED` off must give the identical tree.

    Both consumers short-circuit before they touch the database when the flag is
    off, so a window installed at the booking site cannot reach them — the tree
    below is the base one, written out as a literal rather than recomputed, and
    the row counts around the calls say no plan leaked in through the back door.
    """
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "false")
    reset_settings_cache()
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(LEGAL_WINDOW))
    try:
        assert get_settings().plan_execution_enabled is False
        before_plans = _count(EditorialPlan)
        before_entries = _count(EditorialPlanEntry)

        report = plan_execution.apply_plan(now=FROZEN_NOW)
        assert report == {
            "flag_enabled": False,
            "plan_id": None,
            "applied": 0,
            "held": 0,
            "no_plan": False,
            # The refusal names itself even in the base tree, which is the shape
            # `s7_publish.empty_gate_report` argues for (P34).
            "reasons": {"plan_execution_disabled": 1},
            "entries": [],
        }, report
        assert plan_execution.generation_brief("w20a-no-such-topic") is None
        assert _count(EditorialPlan) == before_plans
        assert _count(EditorialPlanEntry) == before_entries
    finally:
        reset_settings_cache()


def test_an_empty_plan_builds_the_identical_tree_under_both_windows(monkeypatch) -> None:
    """§6, other half: an empty plan is the same tree, byte for byte.

    With nothing in the ranked queue the builder never reaches a `schedule`
    decision, so `_next_free_slot` is never called and the window cannot show up
    anywhere in the report. The comparison is a whole-dict equality — the
    strongest form available here — with the queue pinned empty rather than
    hoping the shared database happens to hold no clusters.
    """
    import tgfarm.editorial_plan as editorial_plan

    monkeypatch.setattr(editorial_plan, "editorial_queue", lambda **_kwargs: [])
    before_plans = _count(EditorialPlan)
    before_entries = _count(EditorialPlanEntry)

    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(None))
    wide = build_editorial_plan(now=FROZEN_NOW, horizon_days=7, persist=False)
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(LEGAL_WINDOW))
    narrow = build_editorial_plan(now=FROZEN_NOW, horizon_days=7, persist=False)

    assert wide["entries"] == []
    assert narrow == wide, "an empty plan is not the same tree under a window"
    assert _count(EditorialPlan) == before_plans
    assert _count(EditorialPlanEntry) == before_entries


# ------------------------------------------------------------------ test plumbing
def _seed_gated_draft(db, *, timezone_name: str) -> tuple[str, str, str, str]:
    """One cluster + one gated post on one channel: a `schedule` entry's input.

    Everything is named `w20a_` and remembered for the finisher. The post is
    3 hours old and the cluster 2 hours: both inside `STALE_HOURS`, and the
    cluster hot enough to rank first, so the builder routes it to the draft.
    """
    channel_id = f"w20a_ch_{_uid()}"
    public_id = f"w20a_p_{_uid()}"
    cluster_id = f"w20a_topic_{_uid()}"
    note = f"w20a_seed_{_uid()}"
    now = datetime.now(UTC)
    db.merge(
        Channel(
            id=channel_id,
            platform="telegram",
            title="w20a channel",
            niche="",
            enabled=True,
            timezone=timezone_name,
            posts_per_day=6,
        )
    )
    db.merge(
        Cluster(
            public_id=cluster_id,
            label=f"Тема w20a {_uid()}",
            niche="",
            status="ready",
            heat=0.99,
            last_seen_at=now - timedelta(hours=2),
        )
    )
    db.merge(
        Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Хук легального слота.",
            body="Тело поста; план бронирует его в легальный локальный час.",
            status="gated",
            cluster_public_id=cluster_id,
            created_at=now - timedelta(hours=3),
            generation_provider="online-model",
        )
    )
    _seeded_channel_ids.append(channel_id)
    _seeded_cluster_ids.append(cluster_id)
    _seeded_post_ids.append(public_id)
    # The builder opens its own session, so the seed has to be committed before
    # it reads — the `db` fixture's transaction is not visible to it.
    db.commit()
    _seeded_post_ids.append(public_id)
    return channel_id, public_id, cluster_id, note


def _schedule_entry(plan: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    matches = [e for e in plan["entries"] if e["cluster_public_id"] == cluster_id]
    assert len(matches) == 1, matches
    entry = matches[0]
    assert entry["action"] == "schedule", entry
    assert entry["slot_at"] is not None, entry
    return entry


def _slot_of_entry(entry_id: int) -> datetime | None:
    with session_scope() as session:
        return _as_utc(session.get(EditorialPlanEntry, entry_id).slot_at)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _count(model: Any) -> int:
    with session_scope() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())

"""W22-C: a booking the channel may never publish in must not be a life sentence.

The defect, measured on `dd75c42` and named as a residue by accepted W20-A
(`tgfarm/editorial_plan.py:126`-`:130`, `tests/test_w20a_plan_slot_local_hours.py:458`-`:461`):
`schedule_ready_posts` reads `posts.plan_slot_at`, judges it with the channel's
legal local hours and, when the minute falls in a closed hour, returns
`_blocked(GATE_SLOT_OUTSIDE_LOCAL_HOURS)` and leaves the row exactly as it found
it — `gated`, `scheduled_at` NULL. The selection that found it is
`status == "gated" AND scheduled_at IS NULL` ordered by `created_at`, so the very
next tick finds the same row and refuses it again, forever. Nothing in the tree
can repair it: the plan's own writer (`s4_generation.apply_plan_booking`) claims
rows only `WHERE plan_slot_at IS NULL`, so a newer, legal snapshot cannot reach a
post that already carries an illegal booking, and the booking column is pinned as
written-once (registry P100). Since accepted W21-B the booking really does reach
`posts.scheduled_at`, so the refusal is now a live path rather than a
hypothetical one.

That is not a documented behaviour, it is a stuck post: the plan booking is
neither honoured nor replaced, and the same gate is counted on tick 1, tick 2 and
tick 5 with nothing in between changing. The tests below state what a reasonable
engineer would demand instead, and each names the safety it must not cost:

1. `test_a_post_booked_into_a_closed_hour_is_not_left_refused_forever` — the bug
   itself: by the fifth tick the row must carry an instant the channel may
   publish in. RED on `dd75c42`.
2. `test_the_corrected_instant_is_the_one_the_plan_builder_would_have_booked` —
   the correction is the planner's own arithmetic (`editorial_plan._next_free_slot`
   walked from the refused minute), not a new clock.
3. `test_the_rebind_never_rewrites_the_booking_column` — registry P100: the
   booking stays byte-identical; the correction is written where the queue owns
   the value. Green before the fix and after it; that is the point.
4. `test_a_rebind_is_ledgered_under_a_registered_code` — registry P95: a named
   outcome lives in `empty_gate_report()`, not as a bare literal, and one fact
   has one spelling in both modules.
5. `test_a_rebind_writes_one_journal_entry_and_a_second_rebind_needs_a_new_one` —
   the boundedness: no rebind without a journal entry, and the corrected instant
   is a stable point, so a row cannot oscillate.
6. `test_a_horizon_with_no_legal_instant_still_refuses_under_the_old_code` — the
   W20-A rule survives: never substitute an illegal instant.
7. `test_flag_off_leaves_the_base_refusal_byte_for_byte` — the standing rule:
   `PLAN_EXECUTION_ENABLED=False` changes nothing, refusal included.
8. `test_the_rebind_reaches_the_ledger_the_booking_reader_reads` — the state must
   be readable by `/api/editorial/plan/booking`'s own source, not only in a
   column: `pipeline._publish_decision` → `api._booking_refusal_ledger`.
9. `test_the_executor_and_the_queue_rebound_one_booking_to_one_instant` — the
   W21-B property, restated for the corrected instant: two consumers of one
   booking cannot write two numbers.

Database hygiene: `tests/conftest.py` gives the suite one SQLite file, so every
row here is `w22c`-prefixed, remembered by id and deleted again by the fixture
teardown — journal rows included — and every clock is a literal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

from tgfarm import pipeline, plan_execution, s7_publish
from tgfarm.core.settings import get_settings, reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, PipelineRun, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial_plan import PLAN_HORIZON_DAYS, SLOT_STEP_MINUTES
from tgfarm.plan_execution import local_hours_allow

#: The channel's legal local hours, in the shape `_legal_local_hours` reads:
#: 09:00-17:59 local. The shipped cadence window is not the subject.
LEGAL_WINDOW = [9, 18]
#: The refused booking: 21:30 local on a UTC channel — an hour the window closed.
#: The same instant W20-A measured as `MAIN_ILLEGAL_BOOKING`, the one the
#: scheduler has been refusing ever since.
ILLEGAL_BOOKING = datetime(2026, 11, 4, 21, 30, tzinfo=UTC)
#: The instant the plan builder books instead, measured in W20-A
#: (`tests/test_w20a_plan_slot_local_hours.py:301`): every candidate between the
#: refused minute and this one (23:00, 00:30, 02:00, 03:30, 05:00, 06:30, 08:00)
#: is outside [9, 18).
CORRECTED_INSTANT = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
#: The first tick. After the refused booking, and in a legal local hour, so the
#: queue's own tick-hour can never be the reason.
TICK_1 = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
#: The tick the plan consumer is run at. It needs three things at once, and no
#: single instant on the refused day has them: past the refused booking (or the
#: entry is held as `slot_not_due`), inside the window (the executor asks the
#: tick's hour too, since W21-B, and holds it under `REASON_LOCAL_HOURS`), and
#: still a tick where the corrected minute is the answer. 10:00 on the next day
#: is the first one, and it corrects to the same 09:30 the queue writes.
EXEC_TICK = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
TICK_STEP = timedelta(minutes=10)
#: Older than anything the rest of the suite can leave behind, so `limit=1` is
#: provably about the row seeded below.
OLDEST = datetime(1971, 1, 1, tzinfo=UTC)

#: The clock `s7_publish` is allowed to see. A dict, not a constant, because the
#: defect is about what happens on the fifth tick and a frozen clock would make
#: the repetition an artifact of the harness instead of of the rule.
_CLOCK: dict[str, datetime] = {"now": TICK_1}

_seeded: dict[str, list[str]] = {"channels": [], "posts": [], "clusters": [], "plans": [], "runs": []}


class _SteppingClock(datetime):
    """`datetime` whose `now()` is this file's movable clock."""

    @classmethod
    def now(cls, tz=None):
        moment = _CLOCK["now"]
        return moment.astimezone(tz) if tz is not None else moment


class _DelegatingSchedule:
    """The shipped schedule object with only `cadence` swapped."""

    def __init__(self, schedule: Any, cadence: dict) -> None:
        self._schedule = schedule
        self.cadence = cadence

    def __getattr__(self, name: str) -> Any:
        return getattr(self._schedule, name)


def _shared_config(active_hours: list[int] | None) -> SimpleNamespace:
    """One cadence owner that both the queue and the plan consumer read.

    The persona's daily cap is deliberately enormous. `_posts_today` counts every
    row whose day bucket is at or after midnight, including the future slots this
    file fills in `test_a_horizon_with_no_legal_instant_still_refuses_under_the_old_code`,
    and a cap of six — the honest-looking value — would answer that cell with
    `daily_cap` before the hour gate is ever reached. The subject here is which
    minutes the window admits, so the cap is taken out of the running instead of
    being tuned around.
    """
    real = s7_publish.get_config()
    return SimpleNamespace(
        persona=lambda _channel_id: SimpleNamespace(enabled=True, posts_per_day=1000),
        schedule=_DelegatingSchedule(
            real.schedule,
            {"mode": "interval", "min_gap_s": 300, "max_gap_s": 620,
             "active_hours": active_hours},
        ),
        personas=real.personas,
    )


def _slug() -> str:
    return uuid.uuid4().hex[:8]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ------------------------------------------------------------------ the world
@pytest.fixture()
def hours(monkeypatch):
    """The window installed at the one place both schedulers read it."""
    config = _shared_config(LEGAL_WINDOW)
    monkeypatch.setattr(plan_execution, "get_config", lambda: config)
    monkeypatch.setattr(s7_publish, "get_config", lambda: config)
    return config


@pytest.fixture()
def queue_clock(monkeypatch):
    """`s7_publish`'s wall clock, movable by one tick at a time.

    The settings stub only replaces the four fields this path reads for the
    *publisher's* own gates; `plan_execution_enabled` is forwarded from the real
    settings object, because the rebind transition is supposed to obey exactly
    that switch and a stub that pinned it either way would make the flag-off test
    a tautology instead of a measurement.
    """
    _CLOCK["now"] = TICK_1
    monkeypatch.setattr(s7_publish, "datetime", _SteppingClock)
    monkeypatch.setattr(s7_publish, "_posts_platform_today", lambda *args: 0)

    def _settings() -> SimpleNamespace:
        return SimpleNamespace(
            approval_required=False,
            dzen_requires_llm=False,
            vk_prime_hours="",
            vk_prime_share=0.0,
            plan_execution_enabled=bool(
                getattr(get_settings(), "plan_execution_enabled", False)
            ),
        )

    monkeypatch.setattr(s7_publish, "get_settings", _settings)
    yield _CLOCK


@pytest.fixture()
def flag(monkeypatch):
    """`PLAN_EXECUTION_ENABLED` set one way, and put back the other."""
    def _set(enabled: bool) -> None:
        monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if enabled else "false")
        reset_settings_cache()

    _set(True)
    yield _set
    reset_settings_cache()


@pytest.fixture()
def switches(monkeypatch):
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 0)
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda platform: True)
    yield


@pytest.fixture(autouse=True)
def only_my_plan_is_active():
    """Foreign snapshots parked for one test and put back: one reader, one plan."""
    with session_scope() as session:
        foreign_ids = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.status == "active")
            ).scalars().all()
        )
        for plan_id in foreign_ids:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in foreign_ids:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


@pytest.fixture(autouse=True)
def _leave_no_residue():
    yield
    with session_scope() as session:
        plan_ids = (
            list(
                session.execute(
                    select(EditorialPlan.id).where(
                        EditorialPlan.public_id.in_(_seeded["plans"])
                    )
                ).scalars().all()
            )
            if _seeded["plans"]
            else []
        )
        if plan_ids:
            session.execute(delete(EditorialPlanEntry).where(
                EditorialPlanEntry.plan_id.in_(plan_ids)
            ))
            session.execute(
                delete(EditorialPlan).where(
                    EditorialPlan.id.in_(plan_ids) | EditorialPlan.superseded_by_id.in_(plan_ids)
                )
            )
        if _seeded["posts"]:
            session.execute(delete(Post).where(Post.public_id.in_(_seeded["posts"])))
        if _seeded["clusters"]:
            session.execute(delete(Cluster).where(Cluster.public_id.in_(_seeded["clusters"])))
        if _seeded["channels"]:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded["channels"])))
        if _seeded["runs"]:
            session.execute(
                delete(PipelineRun).where(PipelineRun.run_id.in_(_seeded["runs"]))
            )
        _sweep_my_rebind_rows(session)
    for bucket in _seeded.values():
        bucket.clear()


# ---------------------------------------------------------------- row builders
def _channel(*, tz: str = "UTC", booked: datetime | None = None,
             status: str = "gated", created_at: datetime = OLDEST) -> tuple[str, str, str]:
    """One enabled channel and one gated draft carrying `booked` as its booking."""
    channel_id = f"w22c-ch-{_slug()}"
    public_id = f"w22c-post-{_slug()}"
    cluster_id = f"w22c-cl-{_slug()}"
    _seeded["channels"].append(channel_id)
    _seeded["posts"].append(public_id)
    _seeded["clusters"].append(cluster_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title=channel_id,
                niche="",
                enabled=True,
                timezone=tz,
                posts_per_day=6,
            )
        )
        session.add(
            Cluster(
                public_id=cluster_id,
                label=f"Тема w22c {_slug()}",
                niche="",
                status="ready",
                heat=0.5,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                cluster_public_id=cluster_id,
                hook="Заголовок поста с закрытой для канала минутой.",
                body="Тело поста; план забронировал час, который канал не публикует.",
                status=status,
                generation_provider="online-model",
                plan_slot_at=booked,
                created_at=created_at,
            )
        )
    return channel_id, public_id, cluster_id


def _plan_entry(channel_id: str, cluster_id: str, slot_at: datetime) -> str:
    """An ACTIVE snapshot with one `schedule` entry for this topic."""
    public_id = f"w22c-plan-{_slug()}"
    _seeded["plans"].append(public_id)
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=public_id,
            created_at=datetime.now(UTC),
            horizon_days=7,
            status="active",
            summary={"total": 1},
        )
        session.add(plan)
        session.flush()
        session.add(
            EditorialPlanEntry(
                plan_id=plan.id,
                cluster_public_id=cluster_id,
                title="Тема",
                niche="",
                action="schedule",
                platform="telegram",
                channel_id=channel_id,
                format="news",
                priority=1,
                confidence=0.5,
                reasons=["запись плана с нелегальной минутой"],
                slot_at=slot_at,
                payload={},
            )
        )
    return public_id


def _read(public_id: str) -> SimpleNamespace:
    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        session.expunge_all()
    return SimpleNamespace(
        status=str(row.status),
        scheduled_at=_aware(row.scheduled_at),
        plan_slot_at=_aware(row.plan_slot_at),
    )


def _release(public_id: str) -> None:
    """Put a scheduled row back the way a requeue puts it back: gated, no slot."""
    with session_scope() as session:
        session.execute(
            Post.__table__.update()
            .where(Post.public_id == public_id)
            .values(status="gated", scheduled_at=None)
        )


def _rebind_journal() -> list[dict]:
    """Every journal row this file's rule wrote, oldest first, with its meta.

    Selected by the stage this transition owns and then narrowed to the records
    naming this file's own posts. Both halves matter: the suite shares one
    database, so a stage invented here can carry a row from a package that runs
    the same transition on its own rows, and a count polluted by it would be a
    measurement of somebody else's world. The rows kept here are remembered for
    the teardown, which is what lets `test_a_rebind_writes_one_journal_entry_…`
    count journal entries rather than ticks.
    """
    with session_scope() as session:
        rows = session.execute(
            select(PipelineRun)
            .where(PipelineRun.stage == plan_execution.STAGE_SLOT_REBIND)
            .order_by(PipelineRun.id.asc())
        ).scalars().all()
        out = []
        for row in rows:
            meta = dict(row.meta or {})
            records = [
                dict(record)
                for record in (meta.get("rebinds") or [])
                if str(record.get("post_public_id") or "").startswith("w22c-post-")
            ]
            if not records:
                continue
            _seeded["runs"].append(row.run_id)
            meta["rebinds"] = records
            out.append(meta)
    return out


def _sweep_my_rebind_rows(session) -> None:
    """Delete this file's journal rows, whether or not a test read them.

    A cell that never asks for the ledger must still leave nothing behind, or the
    next cell's count is a count of two cells.
    """
    rows = session.execute(
        select(PipelineRun).where(PipelineRun.stage == plan_execution.STAGE_SLOT_REBIND)
    ).scalars().all()
    for row in rows:
        records = (row.meta or {}).get("rebinds") or []
        if records and all(
            str(record.get("post_public_id") or "").startswith("w22c-post-")
            for record in records
        ):
            session.delete(row)


def _rebind_count(meta: dict, public_id: str) -> int:
    hits = 0
    for record in meta.get("rebinds") or []:
        if str(record.get("post_public_id")) == public_id:
            hits += 1
    return int(hits)


def _tick() -> dict[str, int]:
    """One publish-tick scheduling pass over the queue this file seeded."""
    gates = s7_publish.empty_gate_report()
    s7_publish.schedule_ready_posts(limit=1, blocked=gates)
    return gates


# --------------------------------------------------------------------- the bug
def test_a_post_booked_into_a_closed_hour_is_not_left_refused_forever(
    hours, queue_clock, flag, switches
) -> None:
    """The defect, as a bug and not as a description.

    Five scheduling ticks over one gated post whose booked minute is 21:30 on a
    channel that may only publish 09:00-17:59. On `dd75c42` the row is in the
    same place at tick 5 as at tick 1: `gated`, `scheduled_at` NULL, and the same
    gate counted again — the booking is neither honoured nor replaced, and no
    later path can repair it, because the plan's booking writer only claims rows
    with `plan_slot_at IS NULL` and the booking column is written-once. A post
    stuck that way is not a policy, it is a dead letter in the queue, and the
    tick that keeps re-counting it says so in its own ledger.

    What is demanded instead: by the fifth tick the row carries an instant the
    channel may publish in. What is NOT allowed as the way to get there: the
    booking column rewritten (`plan_slot_at` stays the refused minute, registry
    P100), or an illegal instant stamped anyway.
    """
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    seen: list[tuple[str, Any, int]] = []
    for tick in range(1, 6):
        _CLOCK["now"] = TICK_1 + (tick - 1) * TICK_STEP
        gates = _tick()
        row = _read(public_id)
        seen.append((row.status, row.scheduled_at, gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS]))

    first, _second, fifth = seen[0], seen[1], seen[4]
    final = _read(public_id)
    assert final.scheduled_at is not None, (
        "a booking the channel may never publish in left the post unscheduled on "
        f"every one of five ticks: {seen}. The plan said {ILLEGAL_BOOKING}, the "
        "refusal has no rebind path, and nothing in the tree can move this row — "
        "the post is stuck in `gated` forever."
    )
    assert final.status == "scheduled", final
    assert local_hours_allow(final.scheduled_at, "UTC", config=hours) is True, (
        f"the queue escaped the refusal by stamping an hour the window closed: {final}"
    )
    assert final.plan_slot_at == ILLEGAL_BOOKING, (
        "the repair was paid for by rewriting the booking column: the refused "
        f"booking read {ILLEGAL_BOOKING} and now reads {final.plan_slot_at}"
    )
    # The refusal must stop being a per-tick refrain: one tick of news, not five.
    assert first[2] + seen[1][2] + seen[2][2] + seen[3][2] + fifth[2] <= 1, (
        f"the same refusal counted on several ticks: {seen}"
    )


def test_the_corrected_instant_is_the_one_the_plan_builder_would_have_booked(
    hours, queue_clock, flag, switches
) -> None:
    """No new clock: the correction is `editorial_plan`'s own slot arithmetic.

    The planner, since accepted W20-A, only books minutes the window admits. The
    corrected instant therefore has to be the minute that builder would have
    returned for the same refused start — 09:30 on the next day, because 23:00,
    00:30, 02:00, 03:30, 05:00, 06:30 and 08:00 are all closed hours. A second
    implementation of the walk is how two schedulers start disagreeing about one
    plan, which is the thing W21-B spent a whole test removing.
    """
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    gates = _tick()

    row = _read(public_id)
    assert row.scheduled_at == CORRECTED_INSTANT, (
        f"expected the planner's own next legal minute {CORRECTED_INSTANT}, got {row}"
    )
    assert row.scheduled_at != ILLEGAL_BOOKING, gates
    assert row.status == "scheduled", row


def test_the_rebind_never_rewrites_the_booking_column(
    hours, queue_clock, flag, switches
) -> None:
    """Registry P100, restated as a constraint on the fix and not on the bug.

    `plan_slot_at` is the plan's own durable statement of what was ordered, and
    three readers depend on it being written once: `/api/editorial/plan/booking`
    subtracts it from `scheduled_at`, `app.js` prints it, and
    `editorial_plan`'s booking-vs-fact read treats a difference as the queue
    having used another instant. A rebind that moved that column would fix the
    stuck post by destroying the only record of why it was stuck. The corrected
    instant belongs in `scheduled_at`, which is the column the queue owns and
    which `tests/test_autonomy_chain_offline.py` and W20-C already expect to move.
    """
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    _tick()

    row = _read(public_id)
    assert row.plan_slot_at == ILLEGAL_BOOKING, "the booking column was rewritten"
    assert row.scheduled_at == CORRECTED_INSTANT, row


def test_a_rebind_is_ledgered_under_a_registered_code(
    hours, queue_clock, flag, switches
) -> None:
    """Registry P95: a named outcome is a key of the closed ledger, not a literal.

    Before this change a tick that refused a booking said so with
    `slot_outside_local_hours`. A tick that repaired one has to say that too, in
    a code the ledger already knows — otherwise `scheduled N` arrives without an
    explanation of how many bookings were replaced on the way, and the wave-19
    trap ("a reason that is not in `empty_gate_report()` is invisible in the
    journal") is back. One fact, two modules, one string: the equality the gate
    matrix already demands of `GATE_SLOT_OUTSIDE_LOCAL_HOURS` is reproduced here.
    """
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    gates = _tick()

    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED in gates, (
        f"the outcome is not a registered ledger key: {sorted(gates)}"
    )
    assert gates[s7_publish.GATE_SLOT_BOOKING_REBOUNDED] == 1, gates
    assert gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS] == 0, (
        f"a repaired booking is still reported as a refusal: {gates}"
    )
    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED == plan_execution.REASON_SLOT_REBOUND, (
        "two modules, two spellings of one outcome"
    )
    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED != s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS


def test_a_rebind_writes_one_journal_entry_and_a_second_rebind_needs_a_new_one(
    hours, queue_clock, flag, switches
) -> None:
    """Bounded: no rebind without its own journal entry, and no oscillation.

    The fear behind a rebind is a row that gets re-slotted on every tick — a
    booking that becomes a suggestion with a new number each time. Two properties
    close it. First, the queue only selects `gated AND scheduled_at IS NULL`, so
    a rebound row leaves the selection: five ticks produce one journal entry
    naming it, not five. Second, when a release does put the row back (a requeue,
    a retry), the new rebind is a new decision and has to bring its own journal
    entry — and it lands on the *same* corrected instant, because the correction
    is a function of the refused booking, not of the clock.
    """
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    for tick in range(1, 6):
        _CLOCK["now"] = TICK_1 + (tick - 1) * TICK_STEP
        _tick()

    entries = _rebind_journal()
    named = sum(_rebind_count(meta, public_id) for meta in entries)
    assert named == 1, f"five ticks, {named} journal entries naming this post: {entries}"

    _release(public_id)
    _CLOCK["now"] = TICK_1 + 5 * TICK_STEP
    _tick()

    entries = _rebind_journal()
    named = sum(_rebind_count(meta, public_id) for meta in entries)
    assert named == 2, f"a second rebind without a second journal entry: {entries}"
    row = _read(public_id)
    assert row.scheduled_at == CORRECTED_INSTANT, (
        f"the corrected instant moved between two identical rebinds: {row}"
    )


def test_a_horizon_with_no_legal_instant_still_refuses_under_the_old_code(
    hours, queue_clock, flag, switches
) -> None:
    """The W20-A rule is not traded away: no legal instant, no substitution.

    A window this scheduler cannot satisfy must not be escaped by stamping an
    instant the channel may not publish in — that was the lie W20-A refused when
    it made `_next_free_slot` answer `None` instead of the nearest illegal
    candidate. Here every grid minute the window admits inside the whole plan
    horizon is already taken by this channel's own queued posts, so the honest
    answer is still the refusal this file starts with, counted under the code
    that already exists and journalled under none of the new ones.
    """
    channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)
    # Occupy the horizon exactly the way the correction reads it: the 90-minute
    # grid walked from the refused minute, up to the horizon limit, keeping the
    # instants the window admits. Anything the walk never reaches is not seeded —
    # a shorter list would leave a legal minute free and the cell would pass for
    # the wrong reason.
    limit = ILLEGAL_BOOKING + timedelta(days=PLAN_HORIZON_DAYS)
    steps = int(PLAN_HORIZON_DAYS * 24 * 60 / SLOT_STEP_MINUTES) + 2
    busy = 0
    for step in range(steps):
        candidate = ILLEGAL_BOOKING + timedelta(minutes=SLOT_STEP_MINUTES * step)
        if candidate > limit:
            break
        if not local_hours_allow(candidate, "UTC", config=hours):
            continue
        busy += 1
        _seeded["posts"].append(f"w22c-busy-{_slug()}")
        with session_scope() as session:
            session.add(
                Post(
                    public_id=_seeded["posts"][-1],
                    channel_id=channel_id,
                    hook="Заголовок занятого слота.",
                    body="Тело поста, который уже стоит в этом же легальном слоте.",
                    status="scheduled",
                    generation_provider="online-model",
                    scheduled_at=candidate,
                    created_at=OLDEST,
                )
            )

    _CLOCK["now"] = TICK_1
    gates = _tick()

    assert busy > 1, f"the cell seeded nothing to occupy: {busy}"
    row = _read(public_id)
    assert row.status == "gated", f"a full legal horizon was escaped by moving the post: {row}"
    assert row.scheduled_at is None, row
    assert row.plan_slot_at == ILLEGAL_BOOKING, row
    assert gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS] == 1, gates
    assert gates[s7_publish.GATE_SLOT_BOOKING_REBOUNDED] == 0, gates


def test_flag_off_leaves_the_base_refusal_byte_for_byte(
    hours, queue_clock, flag, switches
) -> None:
    """`PLAN_EXECUTION_ENABLED=False` must not gain a new transition.

    The standing rule of this project: the switch gates the plan consumer's
    writes, and nothing that happens behind it may leak into the base tree. The
    rebind is the plan layer's judgement — it reads the plan's window and the
    plan's slot arithmetic and it is journalled by the plan consumer — so with
    the switch off the queue keeps base behaviour exactly: `gated`, no slot, one
    refusal counted, no journal row, and no new code in the ledger.
    """
    flag(False)
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    gates = _tick()

    row = _read(public_id)
    assert row.status == "gated", row
    assert row.scheduled_at is None, row
    assert row.plan_slot_at == ILLEGAL_BOOKING, row
    assert gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS] == 1, gates
    assert gates.get(s7_publish.GATE_SLOT_BOOKING_REBOUNDED, 0) == 0, gates
    assert _rebind_journal() == [], "the rebind was journalled with the consumer off"


def test_the_rebind_reaches_the_ledger_the_booking_reader_reads(
    hours, queue_clock, flag, switches
) -> None:
    """Readable, not merely stored: the reader's own two hops carry the state.

    `/api/editorial/plan/booking` classifies a booked row from its columns and
    from exactly one other source — `pipeline.last_publish_decision()`, the
    publish tick's gate ledger. For the corrected booking to be readable there
    rather than only in `posts`, the outcome has to survive
    `pipeline._publish_decision` (which drops a zero-count gate and keeps a
    non-zero one) and arrive in `api._booking_refusal_ledger` as a count with a
    code. Both hops are run here against the real tick output; the label the
    panel prints for the code is a different seam and is not asserted.
    """
    from tgfarm import api

    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    gates = _tick()
    decision = pipeline._publish_decision(
        {"scheduled": 1, "published": 0, "due": 0, "failed": 0, "gates": gates}
    )

    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED in (decision.get("gates") or {}), decision
    ledger = api._booking_refusal_ledger(
        decision, s7_publish.GATE_SLOT_BOOKING_REBOUNDED
    )
    assert ledger == {"count": 1, "tick_reason": "gated"}, ledger
    # The refusal code is not the rebind code: the reader must be able to tell a
    # repaired booking from one that is still held, and both are in the ledger.
    assert s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS not in (decision.get("gates") or {}), decision


def test_the_executor_and_the_queue_rebound_one_booking_to_one_instant(
    hours, flag, switches, queue_clock
) -> None:
    """The W21-B property, one level up: two consumers, one corrected instant.

    The plan consumer judges the entry's `slot_at`; the queue judges the row's
    `plan_slot_at`. Since W21-B both write the instant they judged, so both have
    to correct it the same way or the operator gets two different answers about
    one booking — which is the exact disagreement W21-B spent a test removing.
    Here the same refused minute goes through both paths on two channels, and the
    two stamped instants must be equal, legal, and never the refused one. The
    two ticks differ, and they have to: the queue judges only the booked minute,
    while the executor — since W21-B, and unchanged here — also judges the hour
    it runs in, so its tick is `EXEC_TICK`, an hour the window admits.
    """
    queue_channel, queue_post, _cluster = _channel(booked=ILLEGAL_BOOKING)
    # One minute later on purpose: both rows would otherwise carry `OLDEST`, and
    # a `limit=1` tick that has to break that tie is not provably about the queue
    # row this leg is measuring.
    plan_channel, plan_post, plan_cluster = _channel(
        booked=None, created_at=OLDEST + timedelta(minutes=1)
    )
    _plan_entry(plan_channel, plan_cluster, ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    _tick()
    plan_execution.apply_plan(now=EXEC_TICK)

    from_queue = _read(queue_post)
    from_plan = _read(plan_post)
    assert from_queue.scheduled_at == CORRECTED_INSTANT, from_queue
    assert from_plan.scheduled_at == CORRECTED_INSTANT, from_plan
    assert from_queue.scheduled_at == from_plan.scheduled_at, (
        "the queue and the plan consumer corrected one booking to two instants"
    )
    assert from_plan.plan_slot_at is None, (
        "the plan consumer wrote the booking column: the correction belongs in "
        "`scheduled_at`, which the queue owns"
    )
    assert local_hours_allow(from_plan.scheduled_at, "UTC", config=hours) is True


def test_the_plan_consumer_names_the_correction_in_its_own_reasons(
    hours, flag, switches
) -> None:
    """The executor's side of the same registered vocabulary.

    `apply_plan` refuses loudly — `applied: 0` has to name itself (P34). An entry
    it *repaired* also has to name itself, in the reason dictionary that already
    travels into the apply tick's journal row, and it must not be counted as a
    held entry: the post moved. This is the same outcome string the queue ledger
    carries, so one deviation has one spelling across the two modules.
    """
    channel_id, public_id, cluster_id = _channel(booked=None)
    _plan_entry(channel_id, cluster_id, ILLEGAL_BOOKING)

    report = plan_execution.apply_plan(now=EXEC_TICK)

    assert report["applied"] == 1, report
    assert report["held"] == 0, report
    assert report["reasons"].get(plan_execution.REASON_SLOT_REBOUND) == 1, report
    assert report["reasons"].get(plan_execution.REASON_LOCAL_HOURS, 0) == 0, report
    assert _read(public_id).scheduled_at == CORRECTED_INSTANT, report


def test_a_legal_booking_is_still_honoured_exactly(hours, queue_clock, flag, switches) -> None:
    """Positive control for every cell above: an unbroken booking is not touched.

    A rule that passes by rebinding everything would pass the tests above and
    destroy the plan. 14:15 local is inside the window, so the queue must still
    publish at the booked instant, count neither code, and write no journal row.
    """
    legal = datetime(2026, 11, 4, 14, 15, tzinfo=UTC)
    assert local_hours_allow(legal, "UTC", config=hours) is True
    _channel_id, public_id, _cluster = _channel(booked=legal)

    _CLOCK["now"] = TICK_1
    gates = _tick()

    row = _read(public_id)
    assert row.scheduled_at == legal, row
    assert row.status == "scheduled", row
    assert gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS] == 0, gates
    assert gates[s7_publish.GATE_SLOT_BOOKING_REBOUNDED] == 0, gates
    assert _rebind_journal() == [], "a legal booking was journalled as a correction"


# ------------------------------------------------- the two readings, decided once
def test_the_ledger_counts_a_correction_as_an_outcome_and_the_post_is_scheduled(
    hours, queue_clock, flag, switches
) -> None:
    """`blocked` names this pass's gate OUTCOMES, not only the posts it left behind.

    The out-parameter used to be documented as "the gates that returned a post to
    the queue unscheduled", and a corrected booking counted into it anyway made
    that sentence false in the file that wrote it. Two readings were possible, and
    this package picks the first one and pins it:

    1. the correction IS a gate outcome — the slot gate decided something about
       this post — so it belongs in the same ledger, and the ledger's own
       documentation says a key can mean a post this pass *scheduled*;
    2. or the correction is not a blocked post at all, so it must not be counted
       here, and the documentation has to name the other place it goes.

    Reading 2 is what fails below, in both of its shapes. A pass that moved the
    count out of `blocked` leaves `slot_booking_rebounded` at zero while still
    scheduling the post — the number asserted first, and the ledger would then
    carry no trace of a booking the queue replaced, which is the invisible
    transition registry P95 exists to forbid. A pass that took the old sentence
    literally — "returned to the queue unscheduled" — would leave the row `gated`
    and return `scheduled 0`, and the second assertion is the one that catches it
    (the same mistake from the other side: a correction that reports itself and
    does not release the post is a refusal with new paperwork). What is *not*
    asserted anywhere is that the parameter is renamed: `pipeline`, `demo` and the
    gate tests all pass `blocked=` by keyword in files this package does not own,
    so the name stays and the meaning is carried by the documentation and by this
    test.
    """
    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)

    _CLOCK["now"] = TICK_1
    gates = s7_publish.empty_gate_report()
    scheduled = s7_publish.schedule_ready_posts(limit=1, blocked=gates)

    # The outcome is in the ledger, and the post left the queue on the same pass.
    assert gates[s7_publish.GATE_SLOT_BOOKING_REBOUNDED] == 1, gates
    assert scheduled == 1, f"counted a correction and did not release the post: {scheduled}"
    row = _read(public_id)
    assert row.status == "scheduled", row
    assert row.scheduled_at == CORRECTED_INSTANT, row
    # It is not a refusal, and it is the only thing this pass has to say.
    assert gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS] == 0, gates
    assert {code: n for code, n in gates.items() if n} == {
        s7_publish.GATE_SLOT_BOOKING_REBOUNDED: 1
    }, gates
    # The sentence that made the ledger refusals-only is gone, and both places
    # that now describe it call a code an outcome and name the correction.
    for fn in (s7_publish.schedule_ready_posts, s7_publish.empty_gate_report):
        doc = (fn.__doc__ or "").lower()
        assert "outcome" in doc, fn.__name__
        assert "gate_slot_booking_rebounded" in doc, fn.__name__
    assert "naming the gates that returned a post to the queue unscheduled" not in (
        (s7_publish.schedule_ready_posts.__doc__ or "").lower()
    ), "the parameter is documented as refusals-only again while it still counts corrections"


def test_the_gate_vocabulary_stays_closed_without_anyone_counting_it() -> None:
    """The invariant the matrix's size literal is a proxy for, owned here.

    `tests/test_publish_gate_matrix.py` pins the closed vocabulary twice: once as
    a relationship ("every `GATE_*` the module declares is a key of
    `empty_gate_report()`") and once as a number (`len(VOCABULARY) == 11`). The
    number is a proxy, and registering a code in a closed vocabulary makes it one
    too small by construction; the literal is in a file this package does not own
    and its owner has the diff. What this file CAN own is the relationship,
    restated with no count anywhere, so that a package which registers a code and
    forgets the ledger fails here first and the arithmetic never has to be
    re-derived from a green test. The positive half is the new code itself, in the
    dictionary before any tick has run, which is what makes it a registered
    outcome instead of a string that appears only when something goes wrong (P95).
    """
    declared = {
        value
        for name, value in vars(s7_publish).items()
        if name.startswith("GATE_") and isinstance(value, str)
    }
    ledger = s7_publish.empty_gate_report()

    assert declared == set(ledger), declared ^ set(ledger)
    assert all(count == 0 for count in ledger.values()), ledger
    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED in ledger
    # The plan layer's own name for the same fact is a key of the same ledger,
    # exactly as `REASON_LOCAL_HOURS` already is.
    assert plan_execution.REASON_SLOT_REBOUND in ledger
    assert plan_execution.REASON_SLOT_REBOUND == s7_publish.GATE_SLOT_BOOKING_REBOUNDED


def test_the_booking_reader_can_be_asked_about_a_correction_in_one_line(
    hours, queue_clock, flag, switches
) -> None:
    """The contract W22-C needs from `tgfarm/api.py`, stated as an assertion.

    `/api/editorial/plan/booking` classifies a booked row from its two columns and
    from `pipeline.last_publish_decision()`, and it asks that ledger exactly one
    question — `api._booking_refusal_ledger(decision, GATE_SLOT_OUTSIDE_LOCAL_HOURS)`.
    That is the whole of the gap: after a tick that repaired a booking the reader
    answers `count: 0` for the refusal, which is true, and has no field that says
    "one booking was replaced on the way", which is the other half of the fact.
    The helper it already calls is generic over the code, so the fix is one more
    call with the rebind code and nothing else — no new reader, no new query, no
    changed key set, which matters because W22-B renders against this payload
    right now. Both halves are asserted here against the real tick output, so the
    seam is a measurement and not a wish, and the key set this file's change must
    NOT move is asserted on the payload itself.

    `tgfarm/api.py` is not this package's file, so the requested diff is in the
    package report instead of in the tree; the line it depends on is the one
    asserted below.
    """
    from tgfarm import api

    _channel_id, public_id, _cluster = _channel(booked=ILLEGAL_BOOKING)
    _CLOCK["now"] = TICK_1
    gates = _tick()
    decision = pipeline._publish_decision(
        {"scheduled": 1, "published": 0, "due": 0, "failed": 0, "gates": gates}
    )

    # (a) what the reader can say today, unchanged: no refusal was counted…
    refusal = api._booking_refusal_ledger(
        decision, s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS
    )
    assert refusal == {"count": 0, "tick_reason": "gated"}, refusal
    # …while the same helper, asked the other registered code, already knows.
    correction = api._booking_refusal_ledger(
        decision, s7_publish.GATE_SLOT_BOOKING_REBOUNDED
    )
    assert correction == {"count": 1, "tick_reason": "gated"}, correction
    assert correction["count"] != refusal["count"], (
        "a repaired booking and a still-held one answer the reader identically"
    )
    # (b) the row itself carries both numbers, which is what lets any reader tell
    # the two states apart without the ledger at all.
    row = _read(public_id)
    assert row.plan_slot_at == ILLEGAL_BOOKING, row
    assert row.scheduled_at == CORRECTED_INSTANT, row
    # (c) the payload contract W22-B renders against: ten keys, same names.
    payload = api._booking_fidelity()
    assert set(payload) == {
        "readable", "reason", "reason_detail", "precision", "gate",
        "population", "states", "shift", "samples", "note",
    }, sorted(payload)
    assert payload["gate"]["code"] == s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS

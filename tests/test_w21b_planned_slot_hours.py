"""W21-B: the hours gate names two instants — and since W22-C corrects one of them.

Since W21-B the plan's consumer no longer queues an entry at its own tick: it
hands the entry's reserved minute to `s7_publish.unpublish`, which stamps it as
`posts.scheduled_at`. That makes the stamped minute a claim about when the post
lands, so the channel's legal local hours have to be answered about it — the
same question `schedule_ready_posts` has answered about a booked slot since
W19-D, under the registered gate `GATE_SLOT_OUTSIDE_LOCAL_HOURS`, through the
same `plan_execution.local_hours_allow`.

What did not move is the tick. A release makes the post due on the next publish
tick, so the hour this loop runs in is the hour the farm actually posts in, and
the base already refused that; the first cell below still proves it. What DID
move, in W22-C, is the verdict on a reserved minute the window closed. This file
used to claim the plan consumer was *at most* as permissive as the base on every
entry and that both consumers refused an illegal minute identically. The refusal
was the dead letter W22-C removed: the entry is now corrected to the first
instant this channel may publish in (`REASON_SLOT_REBOUND`), so the consumer is
one step more permissive than the base on that one branch — never into a closed
hour, because the corrected instant is itself gated by the same predicate. The
boundary the refusal still guards (a horizon with nothing legal left in it) is
owned by `tests/test_w22c_booked_slot_rebinding.py`; what these five cells keep
owning is that both paths reach one number for one booking, and that the tick is
still asked.

Window, timezone and rows are this file's: the cadence is a fake of the shape
`_legal_local_hours` reads (the real YAML's window is not the subject), every
channel and post is `w21bhours`-prefixed, and teardown deletes exactly those ids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

from tgfarm import editorial_plan, plan_execution, s7_publish
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope

#: The instant the executor is asked about, and the local hour it is in for a
#: channel on UTC-3: 20:30 — outside the window below, so the tick is closed.
LATE_TICK_UTC = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
#: The reserved minute the same channel would be allowed to post at: 10:15 local
#: the same day. It has already arrived at the tick above, so `slot_not_due` is
#: not what holds the entry: only the hour gate can be.
LEGAL_SLOT_UTC = datetime(2026, 11, 4, 13, 15, tzinfo=UTC)
#: 01:40 on the same local clock — a minute the window refuses, at a tick it
#: would allow. Both halves of the gate are needed to tell these two apart.
ILLEGAL_SLOT_UTC = datetime(2026, 11, 4, 4, 40, tzinfo=UTC)
#: The instant W22-C gives `ILLEGAL_SLOT_UTC` instead of refusing it: the
#: 90-minute grid walked from the refused minute is 01:40, 03:10, 04:40, 06:10,
#: 07:40 local — all closed — and then 09:10 local, which is 12:10 UTC: the
#: first minute the [9,18) window admits at or after the refused one. A literal
#: on purpose: the tests below state the number the planner owes rather than
#: re-deriving it from the walk they are checking.
REBOUNDED_SLOT_UTC = datetime(2026, 11, 4, 12, 10, tzinfo=UTC)
LEGAL_TICK_UTC = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
#: 11:30 for a channel on UTC+5, inside the same window, for the queue path.
CHANNEL_TZ = "America/Sao_Paulo"
LEGAL_WINDOW = [9, 18]
#: Older than every row any other file in the suite can leave behind.
OLDEST = datetime(1971, 1, 1, tzinfo=UTC)


def _slug() -> str:
    return uuid.uuid4().hex[:8]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _DelegatingSchedule:
    """The shipped schedule with only `cadence` swapped.

    A booking is gated by the cadence owner's window, so a test that narrows the
    window has to change `cadence` and nothing else — hours, weights, jitter and
    the fleet capacity stay the shipped ones, or the two consumers this file
    compares would be reading two different config objects for a third reason.
    """

    def __init__(self, schedule: Any, cadence: dict) -> None:
        self._schedule = schedule
        self.cadence = cadence

    def __getattr__(self, name: str) -> Any:
        return getattr(self._schedule, name)


def _shared_config(active_hours: list[int]) -> SimpleNamespace:
    """One config object that satisfies `plan_execution` and `s7_publish` alike."""
    real = s7_publish.get_config()
    return SimpleNamespace(
        persona=lambda _channel_id: SimpleNamespace(enabled=True, posts_per_day=6),
        schedule=_DelegatingSchedule(real.schedule, {"mode": "interval",
                                                     "min_gap_s": 300,
                                                     "max_gap_s": 620,
                                                     "active_hours": active_hours}),
        personas=real.personas,
    )


@pytest.fixture()
def shared_hours(monkeypatch):
    """Both schedulers read one window, and only `get_config` says so."""
    config = _shared_config(LEGAL_WINDOW)
    monkeypatch.setattr(plan_execution, "get_config", lambda: config)
    monkeypatch.setattr(s7_publish, "get_config", lambda: config)
    yield config


class _Owned:
    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.cluster_ids: list[str] = []
        self.post_ids: list[str] = []
        self.plan_public_ids: list[str] = []

    def forget(self) -> None:
        with session_scope() as session:
            plan_ids = (
                list(
                    session.execute(
                        select(EditorialPlan.id).where(
                            EditorialPlan.public_id.in_(self.plan_public_ids)
                        )
                    ).scalars().all()
                )
                if self.plan_public_ids
                else []
            )
            if plan_ids:
                session.execute(
                    delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
                )
                session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.cluster_ids:
                session.execute(delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))


@pytest.fixture()
def rows() -> Any:
    owned = _Owned()
    yield owned
    owned.forget()


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def only_my_plan_is_active():
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


@pytest.fixture()
def quiet_budget(monkeypatch):
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 0)
    yield


@pytest.fixture()
def switches_on(monkeypatch):
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda platform: True)
    yield


# ------------------------------------------------------------------- row builders
def _channel(owned: _Owned, *, tz: str = CHANNEL_TZ, booked: datetime | None = None,
             status: str = "gated",
             created_at: datetime | None = None) -> tuple[str, str]:
    """A channel with one gated draft, optionally carrying a booking.

    `created_at` is how the queue-path half of the last test wins its batch:
    `schedule_ready_posts` has no per-post filter, it takes the oldest gated rows
    up to `limit`, so a row older than anything the suite can leave behind makes
    the window this tick processed this test's instead of the table's.
    """
    channel_id = f"w21bhours-ch-{_slug()}"
    public_id = f"w21bhours-post-{_slug()}"
    cluster_id = f"w21bhours-cl-{_slug()}"
    owned.channel_ids.append(channel_id)
    owned.post_ids.append(public_id)
    owned.cluster_ids.append(cluster_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title=channel_id,
                niche="",
                enabled=True,
                timezone=tz,
                posts_per_day=5,
            )
        )
        session.add(
            Cluster(
                public_id=cluster_id,
                label=f"Тема w21bhours {_slug()}",
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
                hook="Заголовок поста с забронированной минутой.",
                body="Тело поста; план забронировал минуту, пост в черновиках.",
                status=status,
                generation_provider="online-model",
                plan_slot_at=booked,
                created_at=created_at or datetime.now(UTC),
            )
        )
    return channel_id, public_id


def _plan(owned: _Owned, entries: list[dict]) -> str:
    public_id = f"w21bhours-plan-{_slug()}"
    owned.plan_public_ids.append(public_id)
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=public_id,
            created_at=datetime.now(UTC),
            horizon_days=7,
            status="active",
            summary={"total": len(entries)},
        )
        session.add(plan)
        session.flush()
        for entry in entries:
            session.add(
                EditorialPlanEntry(
                    plan_id=plan.id,
                    cluster_public_id=entry["cluster_public_id"],
                    title="Тема",
                    niche="",
                    action=entry["action"],
                    platform="telegram",
                    channel_id=entry["channel_id"],
                    format="news",
                    priority=1,
                    confidence=0.5,
                    reasons=["тест W21-B"],
                    slot_at=entry["slot_at"],
                    payload={},
                )
            )
    return public_id


def _entry(owned: _Owned, channel_id: str, cluster_id: str, slot: datetime) -> dict[str, Any]:
    return {
        "cluster_public_id": cluster_id,
        "action": "schedule",
        "channel_id": channel_id,
        "slot_at": slot,
    }


def _read(public_id: str) -> SimpleNamespace:
    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        session.expunge_all()
    return SimpleNamespace(
        status=row.status,
        scheduled_at=_aware(row.scheduled_at),
        plan_slot_at=_aware(row.plan_slot_at),
    )


def _grid_from(moment: datetime) -> list[datetime]:
    """The instants a correction walks, in the order it walks them.

    Stepping the planner's own `SLOT_STEP_MINUTES` here, rather than importing
    its answer, is what lets the cells below say the stamped minute is not just
    legal but the FIRST legal one at or after the refused instant — the property
    `editorial_plan.next_legal_slot_after` owes and a bare literal cannot show.
    """
    step = timedelta(minutes=editorial_plan.SLOT_STEP_MINUTES)
    out: list[datetime] = []
    at = moment
    for _ in range(8):
        out.append(at.replace(second=0, microsecond=0))
        at = at + step
    return out


# ------------------------------------------------------- the tick still refuses
def test_a_legal_minute_is_still_held_while_the_tick_is_closed(
    shared_hours, flag_on, rows, quiet_budget, switches_on
):
    """The plan consumer did not become the scheduler that posts at night.

    10:15 local is inside the window and already past; 20:30 local, the tick, is
    outside it. Releasing here would put a live post in an hour the operator
    closed, which is exactly what the base refused and what W21-B keeps refusing
    — the hour gate grew a term, it did not lose one.
    """
    channel_id, public_id = _channel(rows)
    cluster_id = _read_cluster(public_id)
    entry = _entry(rows, channel_id, cluster_id, LEGAL_SLOT_UTC)
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=LATE_TICK_UTC)

    assert report["applied"] == 0, report
    row = [item for item in report["entries"] if item["cluster_public_id"] == cluster_id]
    assert row and row[0]["reason"] == plan_execution.REASON_LOCAL_HOURS, row
    after = _read(public_id)
    assert after.status == "gated", "закрытый местный час не имеет права двигать пост"
    assert after.scheduled_at is None


def test_a_legal_minute_and_a_legal_tick_still_hand_the_row_over(
    shared_hours, flag_on, rows, quiet_budget, switches_on
):
    """The positive control: the refusal above is the gate, not a broken loop."""
    channel_id, public_id = _channel(rows)
    cluster_id = _read_cluster(public_id)
    entry = _entry(rows, channel_id, cluster_id, LEGAL_SLOT_UTC)
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=LEGAL_TICK_UTC)

    assert report["applied"] == 1, report
    after = _read(public_id)
    assert after.status == "scheduled"
    assert after.scheduled_at == LEGAL_SLOT_UTC, (
        f"ряд должен нести забронированную минуту, а не {after.scheduled_at}"
    )


def test_an_illegal_minute_is_rebound_to_the_first_legal_one_at_a_legal_tick(
    shared_hours, flag_on, rows, quiet_budget, switches_on
):
    """The term W21-B grew, corrected by W22-C: 01:40 local is not a minute this
    channel may be posted at — and the executor owes an answer to "then which one".

    At base this entry went through, because the tick's hour was legal and the
    tick's hour was the only hour the transition could write. W21-B stopped that
    and *refused* the entry instead: the minute it would have stamped was the
    plan's, and the window was asked about the plan's minute. W22-C supersedes the
    refusal, not the asking. A held entry with an illegal reserved minute is not
    a policy but a dead letter — the row can never be re-booked, because
    `s4_generation.apply_plan_booking` claims rows whose `plan_slot_at` is still
    NULL and the booking column is written once — so the minute being stamped is
    now corrected with the planner's own arithmetic before the gate answers.

    What did NOT move is the direction of the test: the hours gate still judges
    the instant the row will carry rather than the tick that ran, and it is still
    the identical judgement `schedule_ready_posts` makes about the same booking
    through the identical `local_hours_allow`. Only the verdict grew a second
    branch. The cell above this one is the half that proves the tick is still
    gated too — a correction of the stamped minute is not a licence to post at
    night.
    """
    channel_id, public_id = _channel(rows)
    cluster_id = _read_cluster(public_id)
    entry = _entry(rows, channel_id, cluster_id, ILLEGAL_SLOT_UTC)
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=LEGAL_TICK_UTC)

    assert report["applied"] == 1, report
    row = [item for item in report["entries"] if item["cluster_public_id"] == cluster_id]
    assert row and row[0]["outcome"] == "scheduled", row
    assert row[0]["reason"] == plan_execution.REASON_SLOT_REBOUND, row
    assert row[0]["reason"] != plan_execution.REASON_LOCAL_HOURS

    after = _read(public_id)
    assert after.status == "scheduled", "закрытая минута оставила пост в очереди"
    assert after.scheduled_at == REBOUNDED_SLOT_UTC, after
    assert after.scheduled_at != ILLEGAL_SLOT_UTC, "нелегальная минута проставлена как есть"
    assert plan_execution.local_hours_allow(after.scheduled_at, CHANNEL_TZ, config=shared_hours), (
        f"the correction landed outside the window: {after.scheduled_at}"
    )
    # …and it is the FIRST legal minute at or after the refused one, not merely a
    # legal one: every earlier step of the grid this channel may not post in.
    grid = _grid_from(ILLEGAL_SLOT_UTC)
    earlier = grid[: grid.index(REBOUNDED_SLOT_UTC)]
    assert earlier, "the corrected instant is not even on the planner's grid"
    assert not any(
        plan_execution.local_hours_allow(moment, CHANNEL_TZ, config=shared_hours)
        for moment in earlier
    ), f"an open minute was skipped on the way to {REBOUNDED_SLOT_UTC}: {earlier}"
    assert report["held"] == 0, report
    assert report["reasons"].get(plan_execution.REASON_LOCAL_HOURS, 0) == 0, report["reasons"]


def test_a_rebound_booking_keeps_the_instant_the_plan_wrote(
    shared_hours, flag_on, rows, quiet_budget, switches_on
):
    """`plan_slot_at` is written once and never touched — least of all by a correction.

    The refusal this cell used to pin must not quietly re-slot the plan to make
    itself go away, and neither may the correction that replaced the refusal:
    that is how a booking becomes a suggestion. What changed with W22-C is the
    other column. The old version of this test asserted `scheduled_at IS NULL`
    after the closed minute was refused, which was the *whole* pin — a row with
    no slot and no way to get one. The correction now writes the queue's own
    column, so the invariant has to be stated as a pair: the booking keeps the
    refused instant and the slot carries the replacement. Both halves are
    asserted, and together they are the reason `/api/editorial/plan/booking` can
    still subtract one from the other and call the difference a rebound instead
    of a lie about what the plan ordered.
    """
    channel_id, public_id = _channel(rows, booked=ILLEGAL_SLOT_UTC)
    cluster_id = _read_cluster(public_id)
    entry = _entry(rows, channel_id, cluster_id, ILLEGAL_SLOT_UTC)
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=LEGAL_TICK_UTC)

    after = _read(public_id)
    assert after.plan_slot_at == ILLEGAL_SLOT_UTC, "исправление переписало бронь плана"
    assert after.scheduled_at == REBOUNDED_SLOT_UTC, after
    assert after.scheduled_at != after.plan_slot_at, (
        "the correction moved the booking instead of the slot: "
        f"{after.plan_slot_at} / {after.scheduled_at}"
    )
    assert after.status == "scheduled", after
    assert report["applied"] == 1, report
    # The plan's own snapshot still reserves the refused minute too: what moved is
    # the instant on the row, never the record of the decision.
    with session_scope() as session:
        reserved = session.execute(
            select(EditorialPlanEntry.slot_at)
            .join(EditorialPlan, EditorialPlanEntry.plan_id == EditorialPlan.id)
            .where(EditorialPlan.public_id.in_(rows.plan_public_ids))
        ).scalars().all()
    assert [_aware(moment) for moment in reserved] == [ILLEGAL_SLOT_UTC], reserved


# ------------------------------------------------------ one number, two schedulers
def test_both_consumers_of_one_booking_write_one_instant_and_rebound_one_too(
    shared_hours, flag_on, rows, quiet_budget, switches_on, monkeypatch
):
    """The executor and the queue cannot disagree, on either verdict.

    Same window, same channel timezone, same instants, two paths. The queue reads
    `posts.plan_slot_at`; the plan consumer reads the plan entry and hands the
    same minute to the same transition. A legal instant is written identically by
    both. W21-B pinned the illegal one as refused identically by both, under one
    registered string; W22-C supersedes the refusal and keeps the property, so
    what this cell asserts now is that both paths *correct* the same booking to
    the same instant — one arithmetic in `editorial_plan`, two callers, one
    number on the row. Two consumers that corrected one booking to two instants
    would be the disagreement this file was written to remove, one level up.

    The refusal did not disappear, it moved: a horizon with nothing legal left in
    it is still refused by both paths under `GATE_SLOT_OUTSIDE_LOCAL_HOURS`, and
    `tests/test_w22c_booked_slot_rebinding.py` owns that boundary. What is pinned
    here is that a refusal the queue could repair is *not* counted as a refusal
    by either ledger, and that both modules name the correction with the same
    registered code.
    """
    # The queue path: a booked draft each, one legal minute and one illegal,
    # seeded older than the whole suite so `limit=2` is this test's batch.
    _queue_channel, queue_legal = _channel(rows, booked=LEGAL_SLOT_UTC, created_at=OLDEST)
    _queue_channel_2, queue_illegal = _channel(
        rows, booked=ILLEGAL_SLOT_UTC, created_at=OLDEST + timedelta(minutes=1)
    )

    monkeypatch.setattr(s7_publish, "_posts_platform_today", lambda *args: 0)
    gates = s7_publish.empty_gate_report()
    scheduled = s7_publish.schedule_ready_posts(limit=2, blocked=gates)

    assert scheduled == 2, gates
    assert _read(queue_legal).scheduled_at == LEGAL_SLOT_UTC, gates
    assert _read(queue_illegal).scheduled_at == REBOUNDED_SLOT_UTC, (
        "очередь передвинула нелегальную бронь иначе, чем планировщик"
    )
    assert _read(queue_illegal).status == "scheduled", gates
    # The repair is an outcome, not a refusal: one rebound, zero refusals, and
    # both codes are keys of the closed ledger rather than new strings.
    assert gates[s7_publish.GATE_SLOT_BOOKING_REBOUNDED] == 1, gates
    assert gates[s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS] == 0, gates
    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED in gates

    # The plan path over the same two instants, on rows the queue did not touch.
    plan_channel, plan_legal = _channel(rows)
    plan_channel_2, plan_illegal = _channel(rows)
    _plan(rows, [
        _entry(rows, plan_channel, _read_cluster(plan_legal), LEGAL_SLOT_UTC),
        _entry(rows, plan_channel_2, _read_cluster(plan_illegal), ILLEGAL_SLOT_UTC),
    ])
    report = plan_execution.apply_plan(now=LEGAL_TICK_UTC)

    assert report["applied"] == 2, report
    by_cluster = {item["cluster_public_id"]: item for item in report["entries"]}
    assert by_cluster[_read_cluster(plan_illegal)]["reason"] == (
        plan_execution.REASON_SLOT_REBOUND
    ), report
    assert by_cluster[_read_cluster(plan_legal)]["outcome"] == "scheduled", report
    assert by_cluster[_read_cluster(plan_legal)]["reason"] == "", report

    # One instant on both paths, one correction string on both paths, and the
    # refusal string still shared by both for the boundary this cell does not run.
    assert _read(plan_legal).scheduled_at == _read(queue_legal).scheduled_at
    assert _read(plan_illegal).scheduled_at == _read(queue_illegal).scheduled_at
    assert _read(plan_illegal).scheduled_at == REBOUNDED_SLOT_UTC
    assert _read(plan_illegal).status == _read(queue_illegal).status == "scheduled"
    assert s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS == plan_execution.REASON_LOCAL_HOURS
    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED == plan_execution.REASON_SLOT_REBOUND
    assert s7_publish.GATE_SLOT_BOOKING_REBOUNDED in gates
    assert plan_execution.REASON_SLOT_REBOUND in gates


def _read_cluster(public_id: str) -> str:
    with session_scope() as session:
        return str(session.execute(
            select(Post.cluster_public_id).where(Post.public_id == public_id)
        ).scalar_one())

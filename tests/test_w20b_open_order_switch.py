"""W20-B seam: the open editorial order exists only while a consumer can answer it.

`tgfarm/editorial.py` grew a second half in `15afbe1`: besides ready-and-unconsumed
clusters, the queue now also re-shows *open orders* — topics whose draft the plan
itself ordered (`plan_entry_id IS NOT NULL`), that are gated, and that still have
no booked slot and no scheduler. That is what lets a self-running walk reach a
`schedule` verdict at all, and it retired the fifth strict `xfail`.

Why this file exists rather than a paragraph in the report: at intake the mutant
`_plan_consumer_enabled() -> True` — that is, the open-order half ignoring the
executor switch — **survived** the whole inherited set (48 passed, 4 xfailed in
`test_autonomy_chain_offline.py`, `test_w19b_plan_into_generation.py`,
`test_w20a_plan_slot_local_hours.py`, `test_plan_consumer_seam.py`). The queue's
own docstring asserted "byte-identical to main's until an operator turns it on",
and nothing tested that sentence. A claim about flag-off behaviour without a
falsification is the defect class this registry has been paying for since wave 14.

The second hole was the same decision told twice. `s4_generation.run` calls
`awaiting_booking()` *without* the switch, while `queue()` calls
`open_order_cluster_ids()` *with* it — so after an operator turned the executor
off, generation would keep refusing to draft topics main drafts. `awaiting_booking`
now answers the switch itself, and one predicate serves both halves.

Database hygiene: the suite shares one session database (`tests/conftest.py:242`),
so every channel, cluster, raw item and post here is `w20bo_`-prefixed, remembered
by the `owned` holder and deleted again in teardown; foreign ACTIVE plans are
superseded for the duration of a test because `get_active_plan()` reads the whole
table and a same-second `created_at` tie in the shared database is not the fact
under test.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from tgfarm import editorial, s3_router, s4_generation, s11_video
from tgfarm.db.models import Channel, EditorialPlan, EditorialPlanEntry, Post, RawItem
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.session import session_scope

#: The queue is asked for more topics than exist, so a missing row is a decision
#: and not a truncation.
_QUEUE_LIMIT = 25


class _Owned:
    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.cluster_ids: list[str] = []
        self.post_public_ids: list[str] = []
        self.plan_public_ids: list[str] = []


@pytest.fixture()
def owned():
    holder = _Owned()
    yield holder
    with session_scope() as session:
        plan_ids = (
            list(
                session.execute(
                    select(EditorialPlan.id).where(
                        EditorialPlan.public_id.in_(holder.plan_public_ids)
                    )
                ).scalars().all()
            )
            if holder.plan_public_ids
            else []
        )
        if plan_ids:
            session.execute(
                delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
            )
            session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
        if holder.post_public_ids:
            session.execute(delete(Post).where(Post.public_id.in_(holder.post_public_ids)))
        if holder.cluster_ids:
            session.execute(
                delete(RawItem).where(
                    RawItem.cluster_id.in_(
                        select(ClusterRow.id).where(
                            ClusterRow.public_id.in_(holder.cluster_ids)
                        )
                    )
                )
            )
            session.execute(delete(ClusterRow).where(ClusterRow.public_id.in_(holder.cluster_ids)))
        if holder.channel_ids:
            session.execute(delete(Channel).where(Channel.id.in_(holder.channel_ids)))


@pytest.fixture()
def switch(monkeypatch):
    """Set `PLAN_EXECUTION_ENABLED` for one test and put the cache back."""

    def _set(enabled: bool) -> None:
        monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if enabled else "false")
        from tgfarm.core.settings import reset_settings_cache

        reset_settings_cache()

    from tgfarm.core.settings import reset_settings_cache

    yield _set
    reset_settings_cache()


@pytest.fixture()
def no_foreign_active_plan():
    """`get_active_plan()` reads the table; a neighbour's plan must not win."""
    with session_scope() as session:
        foreign = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.status == "active")
            ).scalars().all()
        )
        for plan_id in foreign:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in foreign:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _channel(holder: _Owned) -> str:
    channel_id = f"w20bo_ch_{_uid()}"
    holder.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title="Канал W20-B",
                niche="business",
                enabled=True,
                posts_per_day=5,
            )
        )
    return channel_id


def _consumed_cluster(holder: _Owned, *, heat: float = 0.7) -> str:
    """A cluster S4 has already drafted: invisible to main's queue, forever.

    `consumed_at` is what `mark_consumed` stamps and what the ready branch
    filters on, so this row can only reappear through the open-order half.
    """
    public_id = f"w20bo_cl_{_uid()}"
    holder.cluster_ids.append(public_id)
    with session_scope() as session:
        cluster = ClusterRow(
            public_id=public_id,
            label="ЦБ сохранил ставку на уровне 19 процентов",
            niche="business",
            status="consumed",
            heat=heat,
            last_seen_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(cluster)
        session.flush()
        session.add(
            RawItem(
                cluster_id=cluster.id,
                source_id=f"w20bo_src_{_uid()}",
                external_id=f"w20bo_ext_{_uid()}",
                title="ЦБ сохранил ставку на уровне 19 процентов",
                text="Банк России оставил ключевую ставку на уровне 19 процентов.",
                url=f"https://example.test/w20bo/{_uid()}",
                fetched_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )
    return public_id


def _gated_draft_of(holder: _Owned, cluster_public_id: str, channel_id: str, *, booked=None,
                    scheduled=None, status: str = "gated", plan_ordered: bool = True) -> str:
    """The draft the plan itself ordered, in the state the order is judged by.

    `plan_ordered=False` writes the row with `plan_entry_id` NULL, which is how
    every draft the farm produced before a plan existed looks — the case the
    predicate has to exclude, or the queue would re-offer the whole history.
    """
    public_id = f"w20bo_po_{_uid()}"
    holder.post_public_ids.append(public_id)
    with session_scope() as session:
        entry_id = 1 if plan_ordered else None
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                cluster_public_id=cluster_public_id,
                hook="ЦБ сохранил ставку",
                body="Регулятор оставил ключевую ставку на уровне 19 процентов.",
                status=status,
                plan_entry_id=entry_id,
                plan_slot_at=booked,
                scheduled_at=scheduled,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    return public_id


def _entry(holder: _Owned, cluster_public_id: str, channel_id: str, *, action: str,
           slot_at: datetime | None) -> int:
    """One ACTIVE plan entry — the thing `apply_plan_booking` reads back."""
    with session_scope() as session:
        plan_public_id = f"w20bo_pl_{_uid()}"
        holder.plan_public_ids.append(plan_public_id)
        plan = EditorialPlan(
            public_id=plan_public_id,
            status="active",
            horizon_days=3,
            summary={},
            created_at=datetime.now(UTC),
        )
        session.add(plan)
        session.flush()
        entry = EditorialPlanEntry(
            plan_id=plan.id,
            cluster_public_id=cluster_public_id,
            channel_id=channel_id,
            action=action,
            title="ЦБ сохранил ставку на уровне 19 процентов",
            niche="business",
            platform="telegram",
            format="news",
            slot_at=slot_at,
            priority=5,
            confidence=0.6,
            reasons=["w20bo seam"],
        )
        session.add(entry)
        session.flush()
        return int(entry.id)


def _queue_ids() -> list[str]:
    return [row["cluster_public_id"] for row in editorial.queue(limit=_QUEUE_LIMIT)]


def _queue_row(cluster_public_id: str) -> dict | None:
    for row in editorial.queue(limit=_QUEUE_LIMIT):
        if row["cluster_public_id"] == cluster_public_id:
            return row
    return None


def _run_refuses_reDraft(monkeypatch, cluster_public_id: str) -> tuple[bool, list[str]]:
    """Did `s4_generation.run` short-circuit before touching routing?

    Routing and video discovery are the only stages stubbed; the guard under
    test sits before both, so reaching `plan_cluster` is the observable
    difference between "this is an open order" and "this is material".
    """
    calls: list[str] = []

    async def fake_plan_cluster(cid):
        calls.append(cid)
        return []

    async def fake_fetch_video(_item):
        return None

    monkeypatch.setattr(s3_router, "plan_cluster", fake_plan_cluster)
    monkeypatch.setattr(s11_video, "fetch_video_for", fake_fetch_video)
    created = asyncio.run(s4_generation.run(cluster_public_id))
    return (cluster_public_id in calls, created)


def test_off_the_switch_an_open_order_is_invisible_to_the_queue(owned, switch):
    """The measured baseline: main's queue is ready-and-unconsumed rows only.

    This is the assertion the intake mutant walked straight through.
    """
    switch(False)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(owned, cluster_id, channel_id)

    assert editorial._plan_consumer_enabled() is False
    assert cluster_id not in _queue_ids()
    assert owned_post_count() >= 1  # the seed is really there; the filter is doing the work


def test_off_the_switch_generation_still_drafts_the_topic(owned, switch, monkeypatch):
    """The half the package left untold: `run()` must obey the same switch.

    Before this seam `awaiting_booking` read the row set without asking the
    executor, so a farm that switched `PLAN_EXECUTION_ENABLED` back off kept
    refusing drafts `main` writes — a live behaviour change with no test.
    """
    switch(False)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(owned, cluster_id, channel_id)

    assert editorial.awaiting_booking(cluster_id) is False
    reached_routing, created = _run_refuses_reDraft(monkeypatch, cluster_id)
    assert reached_routing is True, "flag off must leave `run` on the pre-W20-B path"
    assert created == []  # routing refused, which is the stub's answer, not the guard's


def test_on_the_switch_the_queue_carries_the_order_and_says_which_half_it_is(
    owned, switch, no_foreign_active_plan
):
    """The other side of the same gate, and the additive key that names it.

    `awaiting_booking` on the row is what keeps the two halves of the answer from
    looking alike: a topic with no draft yet and a topic whose draft the plan
    ordered are different debts, and a caller that spends generation budget has
    to be able to tell them apart.
    """
    switch(True)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(owned, cluster_id, channel_id)
    _entry(owned, cluster_id, channel_id, action="schedule", slot_at=None)

    assert editorial._plan_consumer_enabled() is True
    row = _queue_row(cluster_id)
    assert row is not None, f"an open order has to reach the plan: {_queue_ids()}"
    assert row["awaiting_booking"] is True
    assert editorial.awaiting_booking(cluster_id) is True


def test_on_the_switch_generation_refuses_to_draft_an_open_order_twice(
    owned, switch, monkeypatch, no_foreign_active_plan
):
    """One story, one draft, one budget — and it never reaches routing."""
    switch(True)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    draft_id = _gated_draft_of(owned, cluster_id, channel_id)
    _entry(owned, cluster_id, channel_id, action="schedule", slot_at=None)

    reached_routing, created = _run_refuses_reDraft(monkeypatch, cluster_id)
    assert reached_routing is False
    assert created == []
    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id).where(Post.cluster_public_id == cluster_id)
        ).scalars().all()
    assert list(rows) == [draft_id], f"a second draft for one order: {list(rows)}"


def test_a_booked_order_stops_haunting_the_queue(owned, switch, no_foreign_active_plan):
    """Self-limiting by construction: the answer closes the debt.

    `plan_slot_at IS NOT NULL` is part of the predicate, so the instant the
    booking is written the topic leaves the open-order half again. Without this
    the queue would re-offer every scheduled topic forever.
    """
    switch(True)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(
        owned, cluster_id, channel_id, booked=datetime.now(UTC) + timedelta(hours=2)
    )

    assert cluster_id not in _queue_ids()
    assert editorial.awaiting_booking(cluster_id) is False


def test_a_scheduler_moved_row_is_answered_too(owned, switch, no_foreign_active_plan):
    """The same fact from the other side: `scheduled_at` is an answer as well.

    A row the publisher already slotted is not an order the plan still owes,
    even when the booking column itself was never written.
    """
    switch(True)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(
        owned, cluster_id, channel_id, scheduled=datetime.now(UTC) - timedelta(minutes=1)
    )

    assert cluster_id not in _queue_ids()
    assert editorial.awaiting_booking(cluster_id) is False


def test_a_draft_the_plan_never_ordered_is_material_not_an_order(owned, switch):
    """`plan_entry_id IS NOT NULL` is what keeps the wider view honest.

    Without it every gated draft of a consumed cluster — including all the rows
    the farm wrote before any plan existed — would return to the queue and be
    offered to generation again.
    """
    switch(True)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(owned, cluster_id, channel_id, plan_ordered=False)

    assert cluster_id not in _queue_ids()
    assert editorial.awaiting_booking(cluster_id) is False


def test_a_produce_entry_is_not_a_booking_and_says_so(owned, switch, no_foreign_active_plan):
    """The order is open, so the queue shows it — and nothing is written.

    A `produce` entry carries no slot, so `apply_plan_booking` must report
    `no_schedule_entry` instead of inventing an instant: booking `when` is the
    plan's decision to make, not the writer's to guess.
    """
    from tgfarm.s4_generation import PLAN_BOOKING_NO_ENTRY

    switch(True)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(owned, cluster_id, channel_id)
    _entry(owned, cluster_id, channel_id, action="produce", slot_at=None)

    decision = s4_generation.apply_plan_booking(cluster_id)
    assert decision["state"] == PLAN_BOOKING_NO_ENTRY, decision
    assert decision["action"] == "produce"
    with session_scope() as session:
        still_null = session.execute(
            select(func.count(Post.public_id)).where(
                Post.cluster_public_id == cluster_id, Post.plan_slot_at.is_(None)
            )
        ).scalar_one()
    assert still_null == 1


def test_off_the_switch_a_booking_attempt_refuses_to_write(owned, switch):
    """No consumer, no claim: the writer says `execution_disabled`.

    A booked instant that nobody may honour is a promise the farm cannot keep,
    so the refusal is a named state and not a silent zero.
    """
    from tgfarm.s4_generation import PLAN_BOOKING_DISABLED

    switch(False)
    channel_id = _channel(owned)
    cluster_id = _consumed_cluster(owned)
    _gated_draft_of(owned, cluster_id, channel_id)

    decision = s4_generation.apply_plan_booking(cluster_id)
    assert decision["state"] == PLAN_BOOKING_DISABLED, decision
    assert decision["slot_at"] is None


def owned_post_count() -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count(Post.public_id)).where(Post.public_id.like("w20bo_%"))
            ).scalar_one()
        )

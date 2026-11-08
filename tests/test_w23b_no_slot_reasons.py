"""W23-B: the builder names *why* it found no slot, and the report keeps it.

One `REASON_NO_SLOT` used to cover two different failures the operator acts on
differently: the horizon is full of taken minutes («поставить позже») and the
horizon holds no legal minute at all («настроить часы канала»). The builder
(`tgfarm.editorial_plan`) now writes that judgement into each schedule entry's
payload as `no_slot_reason` — `"horizon_occupied"` when an empty-taken probe
still finds a legal minute, `"no_legal_hours"` when even an empty-taken probe
finds none — and the pass report (`tgfarm.plan_execution.apply_plan`) carries
the label through as its own reason, falling back to the generic
`no_slot_planned` only for plans persisted before the label existed.

These tests pin the seam between the two halves one way an operator can read it:
the *report* of a pass distinguishes the two causes, the *selection* of slots
did not move one minute to make room for the label, and a disabled flag never
pronounces it. Each one is written so the mutant that collapses it back into
the single reason goes red; that is measured, not asserted, in the journal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

from tgfarm import plan_execution
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial_plan import build_editorial_plan

#: Legal local hours are 09:00 through 17:59: the pair is read as `range(9, 18)`.
LEGAL_WINDOW = [9, 18]

#: 21:00 UTC: afterclose for the window above, so the first legal candidate is
#: the next morning's 09:30 — the instant the three golden families record.
FROZEN_NOW = datetime(2026, 11, 4, 21, 0, 0, tzinfo=UTC)

#: The next morning's first legal minute on a UTC channel, written out because
#: the control case must state the number the planner owes, not recompute it.
NEXT_MORNING = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)

#: The apply-pass clock used by its own suite: 04:30 UTC.
MORNING_UTC = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _window_config(window: list[int] | None) -> Any:
    """A cadence owner whose only interesting field is `active_hours`."""
    return SimpleNamespace(
        schedule=SimpleNamespace(cadence={"mode": "interval", "active_hours": window})
    )


class _Rows:
    """Every row this file creates, so teardown deletes exactly these."""

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
                    delete(EditorialPlanEntry).where(
                        EditorialPlanEntry.plan_id.in_(plan_ids)
                    )
                )
                session.execute(
                    delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids))
                )
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.cluster_ids:
                session.execute(
                    delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids))
                )
            if self.channel_ids:
                session.execute(
                    delete(Channel).where(Channel.id.in_(self.channel_ids))
                )


@pytest.fixture()
def rows():
    owned = _Rows()
    yield owned
    owned.forget()


@pytest.fixture()
def windowed(monkeypatch):
    """The cadence owner and the whole window the builder reads it through."""
    monkeypatch.setattr(plan_execution, "get_config", lambda: _window_config(LEGAL_WINDOW))
    yield


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def only_my_plan_is_active():
    """One reader, one snapshot: this file's own (see `test_plan_execution_apply`)."""
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
    """The AI ledger as here: nothing charged today."""
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 0)
    yield


# ----------------------------------------------------------------------- seeding
def _channel(owned: _Rows, *, timezone: str = "UTC", posts_per_day: int = 6) -> str:
    channel_id = f"w23b_ch_{_uid()}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title="w23b channel",
                niche="",
                enabled=True,
                timezone=timezone,
                posts_per_day=posts_per_day,
            )
        )
    return channel_id


def _seed_gated_draft(owned: _Rows, channel_id: str) -> str:
    """One cluster + one gated post: a `schedule` entry's input."""
    cluster_id = f"w23b_topic_{_uid()}"
    public_id = f"w23b_p_{_uid()}"
    owned.cluster_ids.append(cluster_id)
    owned.post_ids.append(public_id)
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            Cluster(
                public_id=cluster_id,
                label=f"Тема w23b {_uid()}",
                niche="",
                status="ready",
                heat=0.99,
                last_seen_at=now - timedelta(hours=2),
            )
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок без слота.",
                body="Тело поста; план бронирует его в легальный локальный час.",
                status="gated",
                cluster_public_id=cluster_id,
                created_at=now - timedelta(hours=3),
                generation_provider="online-model",
            )
        )
    return cluster_id


def _seed_occupancy(owned: _Rows, channel_id: str) -> None:
    """Every half-hour of two horizons stands taken, on this channel.

    The walk only ever returns a minute `local_hours_allow` admits, and the
    legal ones are 09:00–17:59 every thirty minutes on the 90-minute lattice —
    so thirty-minute seeds make the horizon full no matter which grid phase the
    builder walks, and the *probe* (empty taken) still finds a legal minute.
    """
    with session_scope() as session:
        moment = FROZEN_NOW
        while moment < FROZEN_NOW + timedelta(days=2):
            public_id = f"w23b_taken_{_uid()}"
            owned.post_ids.append(public_id)
            session.add(
                Post(
                    public_id=public_id,
                    channel_id=channel_id,
                    hook="Заполняет горизонт.",
                    body="Тело поста; занимает минутную ячейку.",
                    status="scheduled",
                    scheduled_at=moment,
                )
            )
            moment += timedelta(minutes=30)


def _entry(plan: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    matches = [e for e in plan["entries"] if e["cluster_public_id"] == cluster_id]
    assert len(matches) == 1, matches
    return matches[0]


def _status(public_id: str) -> str:
    with session_scope() as session:
        return session.get(Post, public_id).status if False else _status_of(public_id)


def _status_of(public_id: str) -> str:
    with session_scope() as session:
        return session.execute(
            select(Post.status).where(Post.public_id == public_id)
        ).scalar_one()


# --------------------------------------------------------------- builder labels
def test_an_occupied_horizon_is_named_horizon_occupied(rows, windowed):
    """All legal minutes taken: the reason is «horizon occupied», not «no hours».

    The control is the same scene with nothing taken: the slot lands on the
    next morning's 09:30, proving the failure below is occupancy and not a
    broken scenario, and that no nearest-illegal minute was substituted.
    """
    channel_id = _channel(rows)
    cluster_id = _seed_gated_draft(rows, channel_id)

    # The control is built first, while the horizon is still free.
    empty_plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=1, persist=False)
    free = _entry(empty_plan, cluster_id)
    assert free["action"] == "schedule"
    assert free["slot_at"] is not None, free
    assert free["slot_at"].startswith("2026-11-05T09:30"), free["slot_at"]

    _seed_occupancy(rows, channel_id)
    full_plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=1, persist=False)
    stuck = _entry(full_plan, cluster_id)
    assert stuck["action"] == "schedule", stuck
    assert stuck["slot_at"] is None, stuck
    assert stuck["payload"]["no_slot_reason"] == "horizon_occupied", stuck


def test_an_empty_horizon_is_named_no_legal_hours(rows, windowed):
    """An empty horizon holds no legal minute: the reason is «no legal hours».

    The control is the same channel, the same draft, the same clock, one day
    of horizon — the morning slot is found, so «no legal hours» is about the
    *horizon*'s emptiness and not about the channel; and a nearest illegal
    minute is still not substituted for the empty one.
    """
    channel_id = _channel(rows)
    cluster_id = _seed_gated_draft(rows, channel_id)

    empty_plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=1, persist=False)
    free = _entry(empty_plan, cluster_id)
    assert free["slot_at"] is not None, free

    dry_plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=0, persist=False)
    stuck = _entry(dry_plan, cluster_id)
    assert stuck["action"] == "schedule", stuck
    assert stuck["slot_at"] is None, stuck
    assert stuck["payload"]["no_slot_reason"] == "no_legal_hours", stuck


def test_the_division_adds_a_label_and_nothing_to_the_selection(rows, windowed):
    """The two failure plans select the same rows; only the label differs.

    `horizon_occupied` must not change which entries the builder made, what
    their actions are or what the summary counts — an operator-facing label
    that also moved the selection would be the old bug in new words.
    """
    channel_id = _channel(rows)
    cluster_id = _seed_gated_draft(rows, channel_id)

    free_plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=1, persist=False)
    free = _entry(free_plan, cluster_id)
    assert free["slot_at"] is not None, free

    _seed_occupancy(rows, channel_id)
    full_plan = build_editorial_plan(now=FROZEN_NOW, horizon_days=1, persist=False)

    assert full_plan["summary"] == free_plan["summary"], (
        full_plan["summary"],
        free_plan["summary"],
    )
    assert [
        (e["cluster_public_id"], e["action"]) for e in full_plan["entries"]
    ] == [
        (e["cluster_public_id"], e["action"]) for e in free_plan["entries"]
    ]

    stuck = _entry(full_plan, cluster_id)
    assert stuck["slot_at"] is None
    assert stuck["payload"]["no_slot_reason"] == "horizon_occupied", stuck


# --------------------------------------------------------------- report reasons
def _plan(owned: _Rows, entries: list[dict]) -> str:
    public_id = f"w23bplan-{_uid()}"
    owned.plan_public_ids.append(public_id)
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=public_id,
            created_at=MORNING_UTC,
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
                    action="schedule",
                    platform=entry.get("platform", "telegram"),
                    channel_id=entry["channel_id"],
                    format="news",
                    priority=1,
                    confidence=0.5,
                    reasons=[],
                    slot_at=entry.get("slot_at"),
                    payload=entry.get("payload", {}),
                )
            )
    return public_id


def _reasoned_entry(rows: _Rows, channel_id: str, reason: str) -> dict:
    cluster_id = f"w23b_topic_{_uid()}"
    public_id = f"w23b_p_{_uid()}"
    rows.cluster_ids.append(cluster_id)
    rows.post_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Cluster(
                public_id=cluster_id,
                label=f"Тема w23b {_uid()}",
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
                hook="hook",
                body="body",
                status="gated",
                generation_provider="rule",
            )
        )
    return {
        "cluster_public_id": cluster_id,
        "channel_id": channel_id,
        "platform": "telegram",
        "slot_at": None,
        "payload": {"no_slot_reason": reason},
        "post_public_id": public_id,
    }


def test_the_report_names_the_two_reasons_separately(rows, flag_on, quiet_budget):
    """Both causes land in the pass report under their own keys.

    The mutant that collapses every no-slot entry back into `no_slot_planned`
    keeps the report flat and goes red here: the two reasons must appear, and
    the generic one must not.
    """
    channel_id = _channel(rows)
    occupied = _reasoned_entry(rows, channel_id, "horizon_occupied")
    illegal = _reasoned_entry(rows, channel_id, "no_legal_hours")
    _plan(rows, [occupied, illegal])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["reasons"]["horizon_occupied"] == 1, report["reasons"]
    assert report["reasons"]["no_legal_hours"] == 1, report["reasons"]
    assert "no_slot_planned" not in report["reasons"], report["reasons"]
    by_cluster = {e["cluster_public_id"]: e for e in report["entries"]}
    assert by_cluster[occupied["cluster_public_id"]]["reason"] == "horizon_occupied"
    assert by_cluster[illegal["cluster_public_id"]]["reason"] == "no_legal_hours"
    # Held, not scheduled: a label is a report, not a schedule.
    assert _status_of(occupied["post_public_id"]) == "gated"
    assert _status_of(illegal["post_public_id"]) == "gated"


def test_the_flag_off_report_never_names_a_no_slot_reason(rows, monkeypatch, quiet_budget):
    """The divide is for a pass that actually judges; disabled means disabled.

    With the flag off the pass ends before any entry is judged, so neither new
    reason — nor the old generic one — may appear. Turning the flag on in the
    same scene shows the contrast: the two reasons are there.
    """
    channel_id = _channel(rows)
    occupied = _reasoned_entry(rows, channel_id, "horizon_occupied")
    illegal = _reasoned_entry(rows, channel_id, "no_legal_hours")
    _plan(rows, [occupied, illegal])

    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "false")
    reset_settings_cache()
    report = plan_execution.apply_plan(now=MORNING_UTC)
    reset_settings_cache()

    assert report["reasons"] == {"plan_execution_disabled": 1}, report["reasons"]

    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    live = plan_execution.apply_plan(now=MORNING_UTC)
    reset_settings_cache()

    assert live["reasons"].get("horizon_occupied") == 1, live["reasons"]
    assert live["reasons"].get("no_legal_hours") == 1, live["reasons"]

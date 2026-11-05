"""W17-B: the editorial plan must tell the truth about its own outcomes.

Marker for every row this file writes: ``w17plan``. The suite shares one
session database (``tests/conftest.py``), so nothing here uses absolute
counters and the autouse teardown deletes exactly the rows tagged ``w17plan``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import SQLAlchemyError

from tgfarm import editorial_plan as editorial_plan_module
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial_plan import (
    _measure_plan,
    _plan_dict,
    build_editorial_plan,
    get_active_plan,
)

_TAG = "w17plan"
_H = timedelta(hours=1)

# The frozen contract: every key that existed before W17-B, verbatim.
ENTRY_KEYS = {
    "cluster_public_id",
    "title",
    "niche",
    "action",
    "platform",
    "channel_id",
    "channel",
    "format",
    "priority",
    "confidence",
    "reasons",
    "slot_at",
    "payload",
}
PAYLOAD_KEYS = {
    "score",
    "heat",
    "sources",
    "age_hours",
    "is_news",
    "needs_media",
    "channel_title",
}
PLAN_KEYS = {"public_id", "created_at", "horizon_days", "status", "summary", "entries"}
SUMMARY_KEYS = {
    "total",
    "produce",
    "schedule",
    "hold",
    "by_platform",
    "published_last_24h",
    "diff",
}
OUTCOME_KEYS = {"state", "post_status", "scheduled_at", "checked_at"}
COVERAGE_KEYS = {"measured", "materialized", "not_yet", "abandoned", "unknown"}


@pytest.fixture(autouse=True)
def _w17plan_rows_are_cleaned_up():
    """Delete only this file's rows; the session DB is shared by the suite."""

    yield
    with session_scope() as session:
        plan_ids = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.public_id.like(f"{_TAG}-%"))
            ).scalars()
        )
        if plan_ids:
            session.execute(
                delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
            )
            session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
        session.execute(delete(Post).where(Post.public_id.like(f"{_TAG}-%")))
        session.execute(delete(Cluster).where(Cluster.public_id.like(f"{_TAG}-%")))
        session.execute(delete(Channel).where(Channel.id.like(f"{_TAG}-%")))


def _new(tag: str) -> str:
    return f"{_TAG}-{tag}-{uuid.uuid4().hex[:8]}"


def _seed_channel(db, channel_id: str, *, platform: str = "vk") -> None:
    db.merge(Channel(id=channel_id, title=channel_id, niche="", platform=platform))


def _seed_cluster(db, public_id: str, *, heat: float = 0.9, age_hours: float = 2.0) -> None:
    now = datetime.now(UTC)
    db.merge(
        Cluster(
            public_id=public_id,
            label=public_id,
            niche="",
            status="ready",
            heat=heat,
            last_seen_at=now - timedelta(hours=age_hours),
        )
    )


def _seed_post(
    db,
    public_id: str,
    channel_id: str,
    *,
    cluster_public_id: str,
    status: str,
    created_ago: timedelta = _H,
    scheduled_in: timedelta | None = None,
) -> None:
    now = datetime.now(UTC)
    db.merge(
        Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="hook",
            body="body",
            status=status,
            cluster_public_id=cluster_public_id,
            created_at=now - created_ago,
            updated_at=now - created_ago,
            scheduled_at=now + scheduled_in if scheduled_in is not None else None,
        )
    )


def _seed_plan(db, entries_spec, *, created_ago: timedelta = _H, horizon_days: int = 7):
    """Persist an active plan snapshot without superseding anyone else's."""

    created_at = datetime.now(UTC) - created_ago
    summary = {
        "total": len(entries_spec),
        "produce": sum(1 for spec in entries_spec if spec["action"] == "produce"),
        "schedule": sum(1 for spec in entries_spec if spec["action"] == "schedule"),
        "hold": sum(1 for spec in entries_spec if spec["action"] == "hold"),
        "by_platform": {},
        "published_last_24h": {},
        "diff": {"added": [], "removed": [], "previous_plan_id": None},
    }
    plan = EditorialPlan(
        public_id=_new("plan"),
        created_at=created_at,
        horizon_days=horizon_days,
        status="active",
        summary=summary,
    )
    db.add(plan)
    db.flush()
    rows = []
    for spec in entries_spec:
        row = EditorialPlanEntry(
            plan_id=plan.id,
            cluster_public_id=spec["cluster"],
            title=spec.get("title", spec["cluster"]),
            niche="",
            action=spec["action"],
            platform=spec.get("platform", "vk"),
            channel_id=spec.get("channel_id"),
            format=None,
            priority=1,
            confidence=0.5,
            reasons=["тест"],
            slot_at=None,
            payload={
                "score": 1.0,
                "heat": 0.5,
                "sources": 1,
                "age_hours": 2.0,
                "is_news": False,
                "needs_media": True,
                "channel_title": spec.get("channel_id"),
            },
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return plan, rows


def _entry_for(plan_dict: dict, cluster_public_id: str) -> dict:
    matches = [e for e in plan_dict["entries"] if e["cluster_public_id"] == cluster_public_id]
    assert matches, f"no entry for {cluster_public_id}"
    return matches[0]


def _read_plan(public_id: str) -> dict:
    """The exact read-time composition ``get_active_plan`` uses, for our plan.

    ``get_active_plan`` returns the newest active snapshot and the suite shares
    one session database, so another file's plan can be newer than ours — every
    assertion below names the snapshot it reads instead of trusting recency.
    """

    with session_scope() as session:
        plan = session.execute(
            select(EditorialPlan).where(EditorialPlan.public_id == public_id)
        ).scalar_one()
        entries = list(
            session.execute(
                select(EditorialPlanEntry)
                .where(EditorialPlanEntry.plan_id == plan.id)
                .order_by(EditorialPlanEntry.priority, EditorialPlanEntry.id)
            ).scalars()
        )
        return _plan_dict(
            plan, entries, _measure_plan(session, plan, entries, now=datetime.now(UTC))
        )


def test_w17plan_materialized_for_scheduled_post_on_the_planned_channel(db):
    cluster = _new("cl")
    ch_a, ch_b = _new("cha"), _new("chb")
    _seed_channel(db, ch_a)
    _seed_channel(db, ch_b)
    _seed_cluster(db, cluster)
    db.commit()

    plan_row, _ = _seed_plan(
        db,
        [
            {"cluster": cluster, "action": "schedule", "channel_id": ch_a},
            {"cluster": cluster, "action": "schedule", "channel_id": ch_b},
        ],
    )
    _seed_post(
        db, _new("post"), ch_a, cluster_public_id=cluster, status="scheduled", scheduled_in=_H
    )
    db.commit()

    plan = _read_plan(plan_row.public_id)
    entry = _entry_for(plan, cluster)
    outcome = entry["outcome"]
    assert outcome["state"] == "materialized"
    assert outcome["post_status"] == "scheduled"
    assert outcome["scheduled_at"] is not None
    assert outcome["checked_at"]
    # The plan-picked channel is part of the answer: the same topic published
    # to another channel did not carry out *this* decision.
    other = [e for e in plan["entries"] if e["channel_id"] == ch_b][0]
    assert other["outcome"]["state"] == "not_yet"
    # A real count, not a placeholder zero.
    assert plan["summary"]["coverage"]["materialized"] == 1


def test_w17plan_not_yet_while_the_horizon_is_still_open(db):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()

    plan_row, _ = _seed_plan(
        db, [{"cluster": cluster, "action": "produce", "channel_id": channel}]
    )

    plan = _read_plan(plan_row.public_id)
    entry = _entry_for(plan, cluster)
    assert entry["outcome"]["state"] == "not_yet"
    assert entry["outcome"]["post_status"] is None
    coverage = plan["summary"]["coverage"]
    assert coverage["not_yet"] == 1
    assert coverage["measured"] == 1
    assert "1" in plan["summary"]["coverage_note"]


def test_w17plan_abandoned_when_the_post_was_rejected(db):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()

    plan_row, _ = _seed_plan(
        db, [{"cluster": cluster, "action": "produce", "channel_id": channel}]
    )
    _seed_post(db, _new("post"), channel, cluster_public_id=cluster, status="rejected")
    db.commit()

    plan = _read_plan(plan_row.public_id)
    outcome = _entry_for(plan, cluster)["outcome"]
    assert outcome["state"] == "abandoned"
    assert outcome["post_status"] == "rejected"
    assert plan["summary"]["coverage"]["abandoned"] == 1


def test_w17plan_abandoned_when_the_cluster_vanished(db):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    db.commit()

    # Deliberately no Cluster row: the topic left the queue on its own.
    plan_row, _ = _seed_plan(
        db, [{"cluster": cluster, "action": "produce", "channel_id": channel}]
    )

    plan = _read_plan(plan_row.public_id)
    outcome = _entry_for(plan, cluster)["outcome"]
    assert outcome["state"] == "abandoned"
    assert outcome["post_status"] is None


def test_w17plan_abandoned_when_a_gated_draft_stops_moving(db):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()

    plan_row, _ = _seed_plan(
        db, [{"cluster": cluster, "action": "schedule", "channel_id": channel}]
    )
    _seed_post(
        db,
        _new("post"),
        channel,
        cluster_public_id=cluster,
        status="gated",
        created_ago=timedelta(hours=editorial_plan_module.ABANDON_GATED_HOURS + 24),
    )
    db.commit()

    plan = _read_plan(plan_row.public_id)
    outcome = _entry_for(plan, cluster)["outcome"]
    assert outcome["state"] == "abandoned"
    assert outcome["post_status"] == "gated"


def test_w17plan_unknown_when_the_plan_was_never_persisted(db):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()

    plan = build_editorial_plan(persist=False)

    assert plan["status"] == "transient"
    assert plan["entries"], "the preview must still list its decisions"
    for entry in plan["entries"]:
        assert entry["outcome"]["state"] == "unknown"
    coverage = plan["summary"]["coverage"]
    assert coverage["measured"] == 0
    assert coverage["materialized"] == 0
    assert coverage["unknown"] == len(plan["entries"])
    note = plan["summary"]["coverage_note"]
    # "Nothing ran" must not be dressed up as a failed run.
    assert "не сохранён" in note and "ни одного" in note
    assert "%" not in note


def test_w17plan_unknown_when_the_read_fails(db, monkeypatch):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()
    plan_row, _ = _seed_plan(
        db, [{"cluster": cluster, "action": "produce", "channel_id": channel}]
    )

    def _boom(*args, **kwargs):
        raise SQLAlchemyError("w17plan: simulated read failure")

    monkeypatch.setattr(editorial_plan_module, "_load_outcome_rows", _boom)

    plan = _read_plan(plan_row.public_id)
    assert _entry_for(plan, cluster)["outcome"]["state"] == "unknown"
    coverage = plan["summary"]["coverage"]
    assert coverage["measured"] == 0
    assert coverage["unknown"] == len(plan["entries"])
    note = plan["summary"]["coverage_note"]
    assert "Чтение базы не удалось" in note
    assert "сбой" in note


def test_w17plan_coverage_counts_sum_to_the_entries(db):
    ch_a, ch_b, ch_c, ch_d = (_new("ch") for _ in range(4))
    cl_mat, cl_wait, cl_fail, cl_gone = (_new("cl") for _ in range(4))
    for channel in (ch_a, ch_b, ch_c, ch_d):
        _seed_channel(db, channel)
    _seed_cluster(db, cl_mat)
    _seed_cluster(db, cl_wait)
    _seed_cluster(db, cl_fail)
    db.commit()

    plan_row, _ = _seed_plan(
        db,
        [
            {"cluster": cl_mat, "action": "produce", "channel_id": ch_a},
            {"cluster": cl_wait, "action": "produce", "channel_id": ch_b},
            {"cluster": cl_fail, "action": "produce", "channel_id": ch_c},
            {"cluster": cl_gone, "action": "produce", "channel_id": ch_d},
        ],
    )
    _seed_post(
        db, _new("post"), ch_a, cluster_public_id=cl_mat, status="scheduled", scheduled_in=_H
    )
    _seed_post(db, _new("post"), ch_c, cluster_public_id=cl_fail, status="failed")
    db.commit()

    plan = _read_plan(plan_row.public_id)
    coverage = plan["summary"]["coverage"]
    total = len(plan["entries"])
    assert coverage["materialized"] == 1
    assert coverage["not_yet"] == 1
    assert coverage["abandoned"] == 2
    assert coverage["unknown"] == 0
    assert (
        coverage["materialized"] + coverage["not_yet"] + coverage["abandoned"] + coverage["unknown"]
        == total
    )
    assert coverage["measured"] == total - coverage["unknown"]
    assert f"{total}" in plan["summary"]["coverage_note"]


def test_w17plan_key_sets_are_unchanged_and_outcome_is_additive(db):
    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()
    plan_row, _ = _seed_plan(
        db, [{"cluster": cluster, "action": "produce", "channel_id": channel}]
    )

    plan = _read_plan(plan_row.public_id)
    assert set(plan) == PLAN_KEYS
    assert set(plan["summary"]) == SUMMARY_KEYS | {"coverage", "coverage_note"}
    assert isinstance(plan["summary"]["coverage_note"], str)
    entry = _entry_for(plan, cluster)
    assert set(entry) == ENTRY_KEYS | {"outcome"}
    assert set(entry["payload"]) == PAYLOAD_KEYS
    assert set(entry["outcome"]) == OUTCOME_KEYS
    assert set(plan["summary"]["coverage"]) == COVERAGE_KEYS
    assert isinstance(entry["outcome"]["state"], str)
    assert isinstance(entry["outcome"]["post_status"], (str, type(None)))
    assert isinstance(entry["outcome"]["scheduled_at"], (str, type(None)))
    assert isinstance(entry["outcome"]["checked_at"], str)

    # The persisted summary must not have been rewritten by a derived value.
    with session_scope() as session:
        stored = session.execute(
            select(EditorialPlan.summary).where(EditorialPlan.public_id == plan["public_id"])
        ).scalar_one()
    assert set(stored) == SUMMARY_KEYS


def test_w17plan_public_read_always_answers_with_a_state(db):
    """Whatever snapshot is newest, the public read must stay well-formed."""

    cluster = _new("cl")
    channel = _new("ch")
    _seed_channel(db, channel)
    _seed_cluster(db, cluster)
    db.commit()
    _seed_plan(db, [{"cluster": cluster, "action": "produce", "channel_id": channel}])

    plan = get_active_plan()
    assert plan is not None
    coverage = plan["summary"]["coverage"]
    assert set(coverage) == COVERAGE_KEYS
    assert (
        coverage["materialized"] + coverage["not_yet"] + coverage["abandoned"] + coverage["unknown"]
        == len(plan["entries"])
    )
    assert coverage["measured"] == len(plan["entries"]) - coverage["unknown"]
    for entry in plan["entries"]:
        assert entry["outcome"]["state"] in {"materialized", "not_yet", "abandoned", "unknown"}
        assert entry["outcome"]["checked_at"]


def test_w17plan_outcome_statements_do_not_grow_with_entries(db):
    engine = db.get_bind()
    counted = {"n": 0, "on": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        if counted["on"]:
            counted["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    now = datetime.now(UTC)
    try:
        small_channel = _new("ch")
        _seed_channel(db, small_channel)
        small_cluster = _new("cl")
        _seed_cluster(db, small_cluster)
        db.commit()
        small_plan, small_entries = _seed_plan(
            db, [{"cluster": small_cluster, "action": "produce", "channel_id": small_channel}]
        )

        big_channel = _new("ch")
        _seed_channel(db, big_channel)
        big_specs = []
        for _ in range(20):
            cluster = _new("cl")
            _seed_cluster(db, cluster)
            big_specs.append({"cluster": cluster, "action": "produce", "channel_id": big_channel})
        db.commit()
        big_plan, big_entries = _seed_plan(db, big_specs, created_ago=timedelta(minutes=5))

        counted["on"] = True
        counted["n"] = 0
        _measure_plan(db, small_plan, small_entries, now=now)
        one_entry = counted["n"]
        counted["n"] = 0
        _measure_plan(db, big_plan, big_entries, now=now)
        twenty_entries = counted["n"]
    finally:
        counted["on"] = False
        event.remove(engine, "before_cursor_execute", _before)

    assert len(small_entries) == 1 and len(big_entries) == 20
    assert one_entry > 0
    assert twenty_entries == one_entry

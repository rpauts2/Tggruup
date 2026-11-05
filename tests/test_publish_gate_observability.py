"""The journal has to say *why* a publish tick took nothing.

`publish_tick ok 0/0` was the only thing the run journal said about publication,
and it said it identically whether the queue was empty, a platform was switched
off, posts waited on a human approval, or Dzen refused offline text. The tests
here pin the replacement: every gate that drops an eligible post counts itself
by name, and the tick's journal row carries that reason.

Nothing in this file reaches a publisher: the posts under test are always
stopped by a gate *before* `_publish_one`, which is the behaviour being measured.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tgfarm.db.session import session_scope

CHANNEL_ARGS = {"title": "gate channel", "niche": "tech", "enabled": True}


def _channel(channel_id: str, platform: str) -> None:
    from tgfarm.db.models import Channel

    with session_scope() as session:
        session.merge(Channel(id=channel_id, platform=platform, **CHANNEL_ARGS))


def _post(public_id: str, channel_id: str, **kwargs) -> int:
    from tgfarm.db.models import Post

    row = Post(
        public_id=public_id,
        channel_id=channel_id,
        hook="Хук гейта.",
        body="Тело поста, прошедшее проверку качества.",
        **kwargs,
    )
    with session_scope() as session:
        session.add(row)
        session.flush()
        return row.id


def _due_post(public_id: str, channel_id: str) -> int:
    return _post(
        public_id,
        channel_id,
        status="scheduled",
        scheduled_at=datetime.now(UTC) - timedelta(hours=2),
    )


def _gated_post(public_id: str, channel_id: str) -> int:
    return _post(public_id, channel_id, status="gated", scheduled_at=None)


def _status_of(pk: int) -> str:
    from tgfarm.db.models import Post

    with session_scope() as session:
        return session.get(Post, pk).status


@pytest.fixture()
def _no_dzen_spacing(monkeypatch):
    """Free the Dzen gap so the only gate left standing is the one under test."""
    from tgfarm import s7_publish

    monkeypatch.setattr(s7_publish.publish_due, "_last_dzen", None, raising=False)
    monkeypatch.setattr(s7_publish, "_last_dzen_publish_time", lambda: None)


@pytest.fixture()
def _offline_generation(monkeypatch):
    """Reproduce the live condition: drafts come from an offline model."""
    from tgfarm import s7_publish

    settings = s7_publish.get_settings()
    monkeypatch.setattr(settings, "approval_required", False, raising=False)
    monkeypatch.setattr(settings, "dzen_requires_llm", True, raising=False)
    monkeypatch.setattr(s7_publish, "_llm_recently_working", lambda: False)


# ------------------------------------------------------------------ selection
def test_disabled_platform_is_named_instead_of_silently_skipped(monkeypatch):
    from tgfarm import s7_publish

    _channel("gate_tg_channel", "telegram")
    post_id = _due_post("gate_tg_disabled", "gate_tg_channel")
    monkeypatch.setattr(
        "tgfarm.platforms.is_platform_enabled",
        lambda platform: platform != "telegram",
    )

    gates = s7_publish.empty_gate_report()
    rows = s7_publish.due_posts(
        limit=200, post_public_ids=["gate_tg_disabled"], blocked=gates
    )

    assert rows == []
    assert gates[s7_publish.GATE_PLATFORM_DISABLED] == 1
    assert _status_of(post_id) == "scheduled", (
        "a paused platform must not mutate the queued post"
    )


def test_crosspost_only_platform_is_named(monkeypatch):
    from tgfarm import s7_publish

    _channel("gate_bs_channel", "bluesky")
    _due_post("gate_bs_undeliverable", "gate_bs_channel")
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda platform: True)

    gates = s7_publish.empty_gate_report()
    rows = s7_publish.due_posts(
        limit=200, post_public_ids=["gate_bs_undeliverable"], blocked=gates
    )

    assert rows == []
    assert gates[s7_publish.GATE_PLATFORM_NOT_DELIVERABLE] == 1


def test_due_posts_keeps_its_old_single_argument_contract():
    """The ledger is optional: every existing caller keeps working unchanged."""
    from tgfarm import s7_publish

    _channel("gate_compat_channel", "telegram")
    _due_post("gate_compat_post", "gate_compat_channel")

    rows = s7_publish.due_posts(20)
    assert isinstance(rows, list)
    assert all(len(row) == 4 for row in rows)


def test_a_deliverable_due_post_is_not_counted_as_blocked():
    from tgfarm import s7_publish

    _channel("gate_ok_channel", "telegram")
    post_id = _due_post("gate_ok_post", "gate_ok_channel")

    gates = s7_publish.empty_gate_report()
    rows = s7_publish.due_posts(limit=200, post_public_ids=["gate_ok_post"], blocked=gates)

    assert [row[0] for row in rows] == [post_id]
    assert sum(gates.values()) == 0, "an eligible post must not invent a blocker"


# ------------------------------------------------------------------ scheduling
def test_approval_mode_reports_the_number_of_posts_it_holds(monkeypatch):
    from tgfarm import s7_publish

    _channel("gate_appr_channel", "telegram")
    _gated_post("gate_appr_held", "gate_appr_channel")
    monkeypatch.setattr(
        "tgfarm.s7_publish.get_settings",
        lambda: SimpleNamespace(approval_required=True),
    )

    gates = s7_publish.empty_gate_report()
    scheduled = s7_publish.schedule_ready_posts(blocked=gates)

    assert scheduled == 0
    assert gates[s7_publish.GATE_APPROVAL_REQUIRED] >= 1, (
        "the hold must be counted, not only written to the log"
    )


def test_unknown_persona_is_named_instead_of_dropped(monkeypatch):
    from tgfarm import s7_publish

    monkeypatch.setattr(
        "tgfarm.s7_publish.get_settings",
        lambda: SimpleNamespace(approval_required=False),
    )
    _channel("gate_no_persona", "telegram")
    _gated_post("gate_no_persona_post", "gate_no_persona")

    gates = s7_publish.empty_gate_report()
    s7_publish.schedule_ready_posts(limit=200, blocked=gates)

    assert gates[s7_publish.GATE_PERSONA_DISABLED] >= 1


def test_schedule_ready_posts_still_returns_a_plain_int(monkeypatch):
    from tgfarm import s7_publish

    monkeypatch.setattr(
        "tgfarm.s7_publish.get_settings",
        lambda: SimpleNamespace(approval_required=True),
    )
    assert s7_publish.schedule_ready_posts() == 0


# ------------------------------------------------------------------- decisions
def test_publish_decision_names_the_dzen_model_gate():
    from tgfarm.pipeline import _publish_decision

    decision = _publish_decision(
        {
            "scheduled": 0,
            "due": 3,
            "published": 0,
            "failed": 0,
            "gates": {"dzen_needs_online_model": 3},
        }
    )

    assert decision["reason"] == "gated"
    assert decision["gates"] == {"dzen_needs_online_model": 3}
    assert decision["published"] == 0


def test_publish_decision_calls_a_quiet_queue_quiet():
    from tgfarm.pipeline import _publish_decision

    decision = _publish_decision(
        {"scheduled": 0, "due": 0, "published": 0, "failed": 0, "gates": {}}
    )
    assert decision["reason"] == "nothing_due"
    assert "gates" not in decision, "an empty ledger is noise, not an explanation"


def test_a_held_queue_outranks_an_empty_one():
    """`due: 0` because approval holds it is not the same fact as an empty queue."""
    from tgfarm.pipeline import _publish_decision

    held = _publish_decision(
        {
            "scheduled": 0,
            "due": 0,
            "published": 0,
            "failed": 0,
            "gates": {"approval_required": 47},
        }
    )
    assert held["reason"] == "gated"
    assert held["gates"] == {"approval_required": 47}

    empty = _publish_decision(
        {"scheduled": 0, "due": 0, "published": 0, "failed": 0, "gates": {}}
    )
    assert empty["reason"] == "nothing_due"


def test_publish_decision_reports_an_actual_success():
    from tgfarm.pipeline import _publish_decision

    decision = _publish_decision(
        {"scheduled": 4, "due": 4, "published": 2, "failed": 0, "gates": {}}
    )
    assert decision["reason"] == "published"
    assert decision["scheduled"] == 4


def test_publish_decision_separates_failure_from_nothing_to_do():
    from tgfarm.pipeline import _publish_decision

    decision = _publish_decision(
        {"scheduled": 0, "due": 2, "published": 0, "failed": 2, "gates": {}}
    )
    assert decision["reason"] == "publish_failed"


def test_publish_decision_does_not_claim_a_reason_it_cannot_see():
    from tgfarm.pipeline import _publish_decision

    assert _publish_decision(None) == {"reason": "stage_error", "measured": False}
    assert _publish_decision("garbage")["reason"] == "nothing_due"
    assert (
        _publish_decision({"due": 2, "published": 0, "failed": 0, "gates": {}})["reason"]
        == "attempted_not_sent"
    )


def test_publish_decision_survives_a_missing_gates_block():
    """A stage payload from an older build must not raise inside telemetry."""
    from tgfarm.pipeline import _publish_decision

    assert _publish_decision({"published": 1})["reason"] == "published"
    assert _publish_decision({})["reason"] == "nothing_due"


# ------------------------------------------------------- reported vs invented
def test_an_unreported_counter_stays_unreported_instead_of_becoming_a_zero():
    """`int(payload.get("due") or 0)` printed a zero the tick never counted.

    "0 due" is a claim about the queue. A tick that said nothing is not entitled
    to make it, so the field travels as `None` with the journal's existing
    three-state marker beside it, exactly as `items_in` / `items_out` already do.
    """
    from tgfarm.pipeline import _publish_decision

    decision = _publish_decision({"published": 0})

    assert decision["published"] == 0, "an actual zero stays an actual zero"
    assert decision["due"] is None
    assert decision["failed"] is None
    assert decision["scheduled"] is None
    assert decision["counters"] == "partial"
    assert decision["unmeasured"] == ["due", "failed", "scheduled"]


def test_a_fully_reported_tick_is_marked_measured_even_when_it_was_empty():
    from tgfarm.pipeline import _publish_decision

    decision = _publish_decision(
        {"due": 0, "published": 0, "failed": 0, "scheduled": 0, "gates": {}}
    )

    assert decision["counters"] == "measured"
    assert "unmeasured" not in decision
    assert decision["reason"] == "nothing_due", (
        "a quiet queue is an observation; only the invented zero was the lie"
    )


def test_a_silent_tick_reports_nothing_rather_than_four_nothings():
    from tgfarm.pipeline import PUBLISH_DECISION_FIELDS, _publish_decision

    decision = _publish_decision({})

    assert decision["counters"] == "not_reported"
    assert all(decision[name] is None for name in PUBLISH_DECISION_FIELDS)
    assert decision["unmeasured"] == list(PUBLISH_DECISION_FIELDS)


def test_an_unusable_counter_is_read_as_unreported_not_as_zero():
    """Coercion must not turn junk into a measurement, and must not raise."""
    from tgfarm.pipeline import _publish_decision

    assert _publish_decision({"due": "three"})["due"] is None
    assert _publish_decision({"due": True})["due"] is None, "a flag is not a count"
    assert _publish_decision({"due": "4"})["due"] == 4, "a numeric string was counted"


def test_the_unreported_shape_reaches_the_wire_as_null_not_as_zero():
    """The panel distinguishes "0 шт" from "нет отчёта" by type, so `None` has to
    survive the JSON hop as `null` — a `0` in its place is the same lie one layer
    further out, and `items()` ends up rendering it as a count."""
    import json

    from tgfarm.pipeline import _publish_decision

    wire = json.dumps(_publish_decision({"published": 0}))

    assert '"due": null' in wire
    assert '"due": 0' not in wire
    assert '"counters": "partial"' in wire


def test_a_tick_that_never_reported_its_numbers_is_journalled_as_unmeasured(monkeypatch):
    """The defect end-to-end: a stage that ran but said nothing about the queue."""
    from tgfarm import s7_publish
    from tgfarm.pipeline import _publish_tick

    async def _silent() -> dict:
        return {"gates": {}}

    monkeypatch.setattr(s7_publish, "run", _silent)

    asyncio.run(_publish_tick())

    decision = _latest_publish_tick()["decision"]
    assert decision["reason"] == "nothing_due"
    assert decision["counters"] == "not_reported", (
        "журнал не вправе показывать ноль там, где этап ничего не сообщил"
    )
    assert "unmeasured" in decision


# ---------------------------------------------------------------- integration
def _latest_publish_tick() -> dict:
    from tgfarm.db.models import PipelineRun

    with session_scope() as session:
        meta = session.execute(
            select(PipelineRun.meta)
            .where(PipelineRun.stage == "publish_tick")
            .order_by(PipelineRun.id.desc())
            .limit(1)
        ).scalar_one()
    return meta if isinstance(meta, dict) else json.loads(meta or "{}")


def _publish_only(public_id: str, limit: int = 200):
    """Let the real gate logic run against one post, not the shared test queue."""
    from tgfarm import s7_publish

    async def _scoped() -> dict:
        return await s7_publish._publish_due_once(limit=limit, post_public_ids=[public_id])

    return _scoped


def test_publish_tick_journal_carries_the_reason(monkeypatch, _no_dzen_spacing, _offline_generation):
    from tgfarm import s7_publish
    from tgfarm.pipeline import _publish_tick
    from tgfarm.s7_publish import GATE_DZEN_NEEDS_ONLINE_MODEL

    _channel("gate_tick_channel", "dzen")
    _due_post("gate_tick_dzen", "gate_tick_channel")
    monkeypatch.setattr(s7_publish, "publish_due", _publish_only("gate_tick_dzen"))

    asyncio.run(_publish_tick())

    meta = _latest_publish_tick()
    decision = meta["decision"]
    assert decision["reason"] == "gated"
    assert decision["gates"][GATE_DZEN_NEEDS_ONLINE_MODEL] == 1
    assert decision["due"] == 1
    assert decision["published"] == 0
    assert "errors" in meta, "the existing meta shape must keep working"


def test_the_blocked_post_stays_queued_and_no_publisher_is_called(
    monkeypatch, _no_dzen_spacing, _offline_generation
):
    """A gate holds a post without consuming it, and never opens a publisher."""
    from tgfarm import s7_publish
    from tgfarm.pipeline import _publish_tick

    async def _forbidden_publisher(*_a, **_k):
        raise AssertionError("a gated post must never reach the publisher")

    _channel("gate_safe_channel", "dzen")
    post_id = _due_post("gate_safe_dzen", "gate_safe_channel")
    monkeypatch.setattr(s7_publish, "publish_due", _publish_only("gate_safe_dzen"))
    monkeypatch.setattr(s7_publish, "_publish_one", _forbidden_publisher)

    asyncio.run(_publish_tick())

    assert _status_of(post_id) == "scheduled"


def test_publish_tick_records_nothing_due_when_the_queue_is_empty(
    monkeypatch, _no_dzen_spacing
):
    from tgfarm import s7_publish
    from tgfarm.pipeline import _publish_tick

    # Both halves empty: no post reaches the delivery window and no gate holds
    # one, which is the only state that honestly earns "nothing_due".
    def _schedule_nothing(_limit: int = 200, blocked: dict | None = None) -> int:
        return 0

    monkeypatch.setattr(s7_publish, "due_posts", lambda *a, **k: [])
    monkeypatch.setattr(s7_publish, "schedule_ready_posts", _schedule_nothing)

    asyncio.run(_publish_tick())

    decision = _latest_publish_tick()["decision"]
    assert decision["reason"] == "nothing_due"
    assert decision["due"] == 0
    assert "gates" not in decision


def test_status_exposes_the_last_publish_decision(
    monkeypatch, _no_dzen_spacing, _offline_generation
):
    from tgfarm import pipeline, s7_publish
    from tgfarm.pipeline import _publish_tick

    _channel("gate_status_channel", "dzen")
    _due_post("gate_status_dzen", "gate_status_channel")
    monkeypatch.setattr(s7_publish, "publish_due", _publish_only("gate_status_dzen"))

    asyncio.run(_publish_tick())

    status = pipeline.status()
    assert status["publication"]["reason"] == "gated"
    assert status["publication"]["gates"][s7_publish.GATE_DZEN_NEEDS_ONLINE_MODEL] == 1


def test_last_publish_decision_survives_without_any_journal(monkeypatch):
    """An unreadable journal must not take `/api/status` down with it."""
    from tgfarm import pipeline

    def boom(*_a, **_k):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(pipeline, "session_scope", boom)
    assert pipeline.last_publish_decision() is None

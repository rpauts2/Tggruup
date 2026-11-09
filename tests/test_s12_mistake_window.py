"""The bounded mistake window must read recent failures, with newest values winning."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from tgfarm import s12_learning
from tgfarm.db.models import Channel, CrosspostLog, LearningMemory, PipelineRun
from tgfarm.db.session import session_scope

CHANNEL_ID = "w35s12-window-channel"
PREFIX = "w35win"
SEEDED = 25
WINDOW = 20


def _clean() -> None:
    with session_scope() as session:
        session.execute(delete(LearningMemory).where(LearningMemory.channel_id == CHANNEL_ID))
        session.execute(delete(PipelineRun).where(PipelineRun.run_id.like(PREFIX + "%")))
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.like(PREFIX + "%")))
        session.execute(delete(Channel).where(Channel.id == CHANNEL_ID))


@pytest.fixture(autouse=True)
def _no_residue():
    _clean()
    yield
    _clean()


def _seed_channel() -> None:
    with session_scope() as session:
        session.add(Channel(
            id=CHANNEL_ID, platform="telegram", title="window", niche="", enabled=True, timezone="UTC",
        ))


def _stub_other_lessons(monkeypatch) -> None:
    monkeypatch.setattr(s12_learning, "angle_performance", lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "hook_style_performance", lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: [])
    monkeypatch.setattr(s12_learning, "learn_format_lessons", lambda channel_id: {"written": 0})


def _base() -> datetime:
    # Future-dated rows own the global newest window independently of neighboring tests.
    return (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)


def _seed_pipeline_errors() -> list[str]:
    base = _base()
    stages = [f"w35s{i:02d}" for i in range(SEEDED)]
    with session_scope() as session:
        for i, stage in enumerate(stages):
            session.add(PipelineRun(
                run_id=f"{PREFIX}_run_{i:02d}", stage=stage, status="error", error=f"failure {i}",
                created_at=base + timedelta(minutes=i),
            ))
        session.add(PipelineRun(
            run_id=f"{PREFIX}_run_dup", stage=stages[-1], status="error",
            error="older failure of the same stage",
            created_at=base + timedelta(minutes=SEEDED - 1, seconds=-30),
        ))
    return stages


def _seed_crosspost_failures() -> list[str]:
    base = _base()
    platforms = [f"w35p{i:02d}" for i in range(SEEDED)]
    with session_scope() as session:
        for i, platform in enumerate(platforms):
            session.add(CrosspostLog(
                post_public_id=f"{PREFIX}_log_{i:02d}", platform=platform, status="failed",
                error=f"crosspost failure {i}", created_at=base + timedelta(minutes=i),
            ))
        session.add(CrosspostLog(
            post_public_id=f"{PREFIX}_log_dup", platform=platforms[-1], status="failed",
            error="older crosspost failure of the same platform",
            created_at=base + timedelta(minutes=SEEDED - 1, seconds=-30),
        ))
    return platforms


def _mistakes() -> dict[str, dict]:
    return {row["key"]: row for row in s12_learning.mistakes(CHANNEL_ID)}


def test_the_newest_pipeline_errors_are_the_window(monkeypatch):
    _seed_channel()
    stages = _seed_pipeline_errors()
    _stub_other_lessons(monkeypatch)
    s12_learning._learn_channel(CHANNEL_ID)
    rows = _mistakes()
    newest_key = "pipeline:" + stages[-1]
    assert newest_key in rows, f"newest error missing; stored keys: {sorted(rows)}"
    assert rows[newest_key]["value"]["error"] == "failure 24"
    for i in range(WINDOW, SEEDED):
        key = "pipeline:" + stages[i]
        assert key in rows, f"recent failure {key} was never read"
        assert rows[key]["value"]["error"] == f"failure {i}"
    for i in range(SEEDED - WINDOW + 1):
        key = "pipeline:" + stages[i]
        assert key not in rows, f"{key} is older than the newest {WINDOW} rows"
    assert {k for k in rows if k.startswith("pipeline:w35s")} == {
        "pipeline:" + stage for stage in stages[6:]
    }


def test_the_newest_crosspost_failures_are_the_window(monkeypatch):
    _seed_channel()
    platforms = _seed_crosspost_failures()
    _stub_other_lessons(monkeypatch)
    s12_learning._learn_channel(CHANNEL_ID)
    rows = _mistakes()
    newest_key = "crosspost:" + platforms[-1]
    assert newest_key in rows, f"newest crosspost failure missing; stored keys: {sorted(rows)}"
    assert rows[newest_key]["value"]["error"] == "crosspost failure 24"
    for i in range(WINDOW, SEEDED):
        key = "crosspost:" + platforms[i]
        assert key in rows, f"recent failure {key} was never read"
        assert rows[key]["value"]["error"] == f"crosspost failure {i}"
    for i in range(SEEDED - WINDOW + 1):
        key = "crosspost:" + platforms[i]
        assert key not in rows, f"{key} is older than the newest {WINDOW} rows"
    assert {k for k in rows if k.startswith("crosspost:w35p")} == {
        "crosspost:" + platform for platform in platforms[6:]
    }


@pytest.mark.parametrize("model,kind", [(PipelineRun, "pipeline"), (CrosspostLog, "crosspost")])
def test_equal_timestamps_use_insertion_order(monkeypatch, model, kind):
    _seed_channel()
    _stub_other_lessons(monkeypatch)
    stamp = _base()
    with session_scope() as session:
        for error in ("older", "newer"):
            if model is PipelineRun:
                row = PipelineRun(
                    run_id=f"{PREFIX}_tie_{error}", stage="w35tie", status="error",
                    error=error, created_at=stamp,
                )
            else:
                row = CrosspostLog(
                    post_public_id=f"{PREFIX}_tie_{error}", platform="w35tie", status="failed",
                    error=error, created_at=stamp,
                )
            session.add(row)
            session.flush()
    s12_learning._learn_channel(CHANNEL_ID)
    assert _mistakes()[kind + ":w35tie"]["value"]["error"] == "newer"


def test_a_broken_mistake_read_warns_and_keeps_the_other_lessons(monkeypatch, caplog):
    writes: list[dict] = []

    def capture(channel_id, kind, key, value, score, sample_size, confidence, note=None):
        writes.append({"kind": kind, "key": key, "value": value})

    def broken_session():
        raise RuntimeError("too many SQL variables")

    _stub_other_lessons(monkeypatch)
    monkeypatch.setattr(s12_learning, "angle_performance", lambda days, channel_id=None: {
        "news": {"n": 9, "quality": 0.9}, "explainer": {"n": 6, "quality": 0.4},
    })
    monkeypatch.setattr(s12_learning, "_upsert", capture)
    monkeypatch.setattr(s12_learning, "session_scope", broken_session)
    with caplog.at_level(logging.WARNING, logger="tgfarm.s12_learning"):
        result = s12_learning._learn_channel(CHANNEL_ID)
    assert any(
        rec.levelno == logging.WARNING and "mistake collection" in rec.getMessage()
        for rec in caplog.records
    ), f"lost mistake sweep emitted no WARNING: {[r.getMessage() for r in caplog.records]}"
    assert result["lessons"] > 0
    assert {row["key"] for row in writes} >= {"angle_weights", "best_angle", "worst_angle"}

"""S12 keeps the newest failure, and never stores a self-contradicting lesson.

`pipeline:<stage>` and `crosspost:<platform>` are keyed upserts, so the order the
source rows are read decides which error survives. Reading newest-first meant the
oldest row of the window overwrote every newer one. Separately, a
`mistake/worst_angle` written by an earlier cycle survived a newer cycle in which
that same angle became the best one, so S4 was told both to prefer and to avoid
it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from tgfarm import s12_learning
from tgfarm.db.models import Channel, LearningMemory, PipelineRun
from tgfarm.db.session import session_scope

CHANNEL_ID = "w29s12-recency-channel"


def _clean() -> None:
    with session_scope() as session:
        session.execute(delete(LearningMemory).where(LearningMemory.channel_id == CHANNEL_ID))
        session.execute(delete(PipelineRun).where(PipelineRun.run_id.like("w29rec%")))
        session.execute(delete(Channel).where(Channel.id == CHANNEL_ID))


@pytest.fixture(autouse=True)
def _no_residue():
    _clean()
    yield
    _clean()


def _seed_channel() -> None:
    with session_scope() as session:
        session.add(
            Channel(
                id=CHANNEL_ID,
                platform="telegram",
                title="recency",
                niche="",
                enabled=True,
                timezone="UTC",
            )
        )


def _stub_other_lessons(monkeypatch) -> None:
    monkeypatch.setattr(s12_learning, "angle_performance", lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "hook_style_performance", lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: [])
    monkeypatch.setattr(s12_learning, "learn_format_lessons", lambda channel_id: {"written": 0})


def test_the_newest_pipeline_error_is_the_one_remembered(monkeypatch):
    _seed_channel()
    base = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    with session_scope() as session:
        session.add(
            PipelineRun(
                run_id="w29rec_old",
                stage="s4",
                status="error",
                error="old failure",
                created_at=base,
            )
        )
        session.add(
            PipelineRun(
                run_id="w29rec_new",
                stage="s4",
                status="error",
                error="new failure",
                created_at=base + timedelta(minutes=5),
            )
        )
    _stub_other_lessons(monkeypatch)

    s12_learning._learn_channel(CHANNEL_ID)

    rows = {row["key"]: row for row in s12_learning.mistakes(CHANNEL_ID)}
    assert rows["pipeline:s4"]["value"]["error"] == "new failure"


def test_a_stale_worst_angle_is_removed_when_it_becomes_the_best(monkeypatch):
    _seed_channel()
    with session_scope() as session:
        session.add(
            LearningMemory(
                channel_id=CHANNEL_ID,
                kind="mistake",
                key="worst_angle",
                value={"angle": "news"},
                score=0.0,
                sample_size=5,
                confidence=0.5,
            )
        )
    _stub_other_lessons(monkeypatch)
    monkeypatch.setattr(
        s12_learning,
        "angle_performance",
        lambda days, channel_id=None: {"news": {"n": 9, "quality": 0.9}},
    )

    s12_learning._learn_channel(CHANNEL_ID)

    mistakes = {row["key"] for row in s12_learning.lessons(CHANNEL_ID, kind="mistake")}
    tactics = {row["key"]: row for row in s12_learning.lessons(CHANNEL_ID, kind="tactic")}

    assert "worst_angle" not in mistakes
    assert tactics["best_angle"]["value"]["angle"] == "news"

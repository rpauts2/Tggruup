"""Repeated gate rejections must accumulate into a lesson the writer actually sees.

S5 calls `record_gate_failure` on every rejected draft. While `_upsert` overwrote
the row, each lesson carried `sample_size=1` and a fixed confidence no matter how
many times the same gate had fired, and S4 took `mistakes[:5]` in whatever order
the database returned. A gate that had rejected 300 drafts was indistinguishable
from one that rejected a single draft — and could be cut out of the prompt
entirely by the five-slot budget.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from tgfarm.db.models import LearningMemory
from tgfarm.db.session import session_scope
from tgfarm.s12_learning import _sample_confidence, lessons, record_gate_failure


@pytest.fixture
def channel():
    name = f"lesson_{uuid.uuid4().hex[:8]}"
    yield name
    with session_scope() as session:
        session.execute(
            delete(LearningMemory).where(LearningMemory.channel_id == name)
        )


@pytest.fixture
def failure_entities():
    state = {"channel_ids": set(), "post_ids": set(), "run_ids": set()}
    yield state
    from tgfarm.db.models import Channel, CrosspostLog, PipelineRun, Post

    with session_scope() as session:
        if state["post_ids"]:
            session.execute(
                delete(CrosspostLog).where(
                    CrosspostLog.post_public_id.in_(state["post_ids"])
                )
            )
            session.execute(delete(Post).where(Post.public_id.in_(state["post_ids"])))
        if state["run_ids"]:
            session.execute(
                delete(PipelineRun).where(PipelineRun.run_id.in_(state["run_ids"]))
            )
        if state["channel_ids"]:
            session.execute(delete(Channel).where(Channel.id.in_(state["channel_ids"])))


def _mistake(channel_id: str, gate: str) -> dict:
    rows = [row for row in lessons(channel_id, kind="mistake") if row["key"] == f"gate:{gate}"]
    assert len(rows) == 1, rows
    return rows[0]




def _seed_channel(channel_id: str, title: str) -> None:
    from tgfarm.db.models import Channel

    with session_scope() as session:
        session.add(Channel(
            id=channel_id,
            platform="telegram",
            title=title,
            niche="tech",
            enabled=True,
            timezone="UTC",
        ))


def test_pipeline_failure_lessons_aggregate_and_keep_the_newest_error(channel, failure_entities):
    from datetime import UTC, datetime, timedelta

    from tgfarm.db.models import PipelineRun
    from tgfarm.s12_learning import _record_recent_failures

    now = datetime.now(UTC)
    stage = f"s12gen{uuid.uuid4().hex[:8]}"
    run_ids = [f"lesson-run-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    failure_entities["run_ids"].update(run_ids)
    with session_scope() as session:
        session.add_all([
            PipelineRun(
                run_id=run_ids[0],
                stage=stage,
                status="error",
                error="older generation error",
                created_at=now - timedelta(hours=2),
            ),
            PipelineRun(
                run_id=run_ids[1],
                stage=stage,
                status="error",
                error="newest generation error",
                created_at=now,
            ),
            PipelineRun(
                run_id=run_ids[2],
                stage=f"{stage[:20]}qc",
                status="error",
                error="factcheck error",
                created_at=now - timedelta(minutes=30),
            ),
        ])

    _record_recent_failures(channel)
    rows = {row["key"]: row for row in lessons(channel, kind="mistake")}

    generation = rows[f"pipeline:{stage}"]
    assert generation["sample_size"] == 2
    assert generation["value"]["occurrences"] == 2
    assert generation["value"]["error"] == "newest generation error"
    assert generation["value"]["recent_errors"] == [
        "newest generation error",
        "older generation error",
    ]
    assert generation["confidence"] == pytest.approx(_sample_confidence(2))
    assert "2 failures" in (generation["note"] or "")
    assert rows[f"pipeline:{stage[:20]}qc"]["sample_size"] == 1


def test_crosspost_failure_lessons_are_channel_scoped_and_redacted(channel, failure_entities):
    from datetime import UTC, datetime, timedelta

    from tgfarm.db.models import CrosspostLog, Post
    from tgfarm.s12_learning import _record_recent_failures

    other_channel = f"{channel}_other"
    _seed_channel(channel, "Primary learning channel")
    _seed_channel(other_channel, "Unrelated learning channel")
    failure_entities["channel_ids"].update({channel, other_channel})
    now = datetime.now(UTC)
    own_ids = [f"{channel}_post_{index}" for index in range(2)]
    other_id = f"{other_channel}_post"
    failure_entities["post_ids"].update({*own_ids, other_id})

    with session_scope() as session:
        session.add_all([
            Post(
                public_id=own_ids[0],
                channel_id=channel,
                hook="Hook",
                body="Body",
                status="failed",
            ),
            Post(
                public_id=own_ids[1],
                channel_id=channel,
                hook="Hook",
                body="Body",
                status="failed",
            ),
            Post(
                public_id=other_id,
                channel_id=other_channel,
                hook="Hook",
                body="Body",
                status="failed",
            ),
        ])
        session.flush()
        session.add_all([
            CrosspostLog(
                post_public_id=own_ids[0],
                platform="reddit",
                status="failed",
                error="older own-channel failure",
                created_at=now - timedelta(hours=1),
            ),
            CrosspostLog(
                post_public_id=own_ids[1],
                platform="reddit",
                status="failed",
                error="Authorization: Bearer TEST_SECRET_SENTINEL_7f9c2e",
                created_at=now,
            ),
            CrosspostLog(
                post_public_id=other_id,
                platform="reddit",
                status="failed",
                error="unrelated newer channel failure",
                created_at=now + timedelta(minutes=1),
            ),
        ])

    written = _record_recent_failures(channel)
    row = next(item for item in lessons(channel, kind="mistake") if item["key"] == "crosspost:reddit")

    assert written == 1
    assert row["sample_size"] == 2
    assert row["value"]["occurrences"] == 2
    assert row["value"]["error"] == "Authorization: Bearer [REDACTED]"
    assert "TEST_SECRET_SENTINEL_7f9c2e" not in str(row["value"])
    assert row["value"]["recent_errors"][1] == "older own-channel failure"
    assert row["confidence"] == pytest.approx(_sample_confidence(2))

def test_repeated_rejections_of_one_gate_accumulate(channel):
    for attempt in range(3):
        record_gate_failure(channel, "cliches", f"штамп #{attempt}")

    row = _mistake(channel, "cliches")

    assert row["sample_size"] == 3
    assert row["value"]["occurrences"] == 3
    assert row["confidence"] == pytest.approx(_sample_confidence(3))
    assert "3 отклонений" in (row["note"] or "")


def test_different_gates_stay_separate_lessons(channel):
    record_gate_failure(channel, "cliches", "штамп")
    record_gate_failure(channel, "facts", "нет источника")
    record_gate_failure(channel, "facts", "нет ссылки")

    assert _mistake(channel, "cliches")["sample_size"] == 1
    assert _mistake(channel, "facts")["sample_size"] == 2


def test_the_reason_tail_stays_bounded(channel):
    for attempt in range(9):
        record_gate_failure(channel, "tone", f"пример #{attempt}")

    row = _mistake(channel, "tone")

    assert row["sample_size"] == 9
    assert row["value"]["recent_reasons"][-1] == "пример #8"
    assert len(row["value"]["recent_reasons"]) == 5


def test_a_channel_less_failure_records_nothing():
    before = len(lessons("", kind="mistake"))

    record_gate_failure("", "cliches", "штамп")

    assert len(lessons("", kind="mistake")) == before


def test_ranks_by_frequency_and_breaks_ties_by_recency():
    from tgfarm.s4_generation import _rank_learning_mistakes

    ranked = _rank_learning_mistakes([
        {"key": "rare", "sample_size": 1, "updated_at": "2026-11-04T10:00:00"},
        {"key": "common", "sample_size": 40, "updated_at": "2026-10-01T10:00:00"},
        {"key": "older_tie", "sample_size": 7, "updated_at": "2026-11-01T10:00:00"},
        {"key": "newer_tie", "sample_size": 7, "updated_at": "2026-11-03T10:00:00"},
    ])

    assert [item["key"] for item in ranked] == ["common", "newer_tie", "older_tie", "rare"]


def test_ranking_survives_junk_counts_and_honours_the_limit():
    from tgfarm.s4_generation import _rank_learning_mistakes

    ranked = _rank_learning_mistakes([
        {"key": "broken", "sample_size": None},
        {"key": "text", "sample_size": "many"},
        {"key": "negative", "sample_size": -4},
        {"key": "real", "sample_size": 2},
    ], limit=2)

    assert [item["key"] for item in ranked] == ["real", "broken"]


def test_the_frequent_lesson_reaches_the_prompt_even_when_it_was_written_last(channel):
    """The two fixes together: five one-off gates must not squeeze out a recurring one."""
    from tgfarm.config import Persona
    from tgfarm.s4_generation import _persona_block

    for gate in ("cliches", "facts", "tone", "length", "links"):
        record_gate_failure(channel, gate, "один раз")
    for _ in range(3):
        record_gate_failure(channel, "redundancy", "повтор")

    block = _persona_block(Persona(id=channel, title="Урок", niche="tech"), "news")

    avoid = [line for line in block.splitlines() if line.startswith("- Избегать:")]

    # Six lessons exist but five fit the prompt, so ranking is what keeps this one visible.
    assert len(avoid) == 5
    assert "redundancy" in avoid[0]

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


def _mistake(channel_id: str, gate: str) -> dict:
    rows = [row for row in lessons(channel_id, kind="mistake") if row["key"] == f"gate:{gate}"]
    assert len(rows) == 1, rows
    return rows[0]


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

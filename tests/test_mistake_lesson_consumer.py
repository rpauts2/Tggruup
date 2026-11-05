"""The `mistake` lesson must move the pen, not just the database.

`kind='mistake'` is the only kind the live farm stores: measured 2026-11-05 against
`tgfarm.db` read-only, `learning_memory` holds 24 rows, all 24 of them
`kind='mistake'`, none of any other kind — 16 keyed `gate:*`, 6 keyed `pipeline:*`,
2 keyed `crosspost:*`, for exactly the two enabled personas (`vk_misc_ru` 13 rows,
`zen_misc_ru` 11). Every one of them carries `score=0.0`, `sample_size=1` and
`confidence` 0.2 or 0.3.

So the question `tests/test_lesson_readers.py` cannot answer — it works at kind
granularity and stops at "some file compares `LearningMemory.kind == 'mistake'`" —
is whether those 24 rows can change what S4 writes. A SELECT is not a consumer: a
reader that filtered `kind == 'format'`, or demanded a confidence these rows never
reach, or looked up a `channel_id` shape no writer produces, would keep the guard
green while the signal stayed dead. This file tests the row, not the kind.

What is pinned here, in order of how each could silently die:

1. the end-to-end line — a draft rejected by an S5 gate changes the prompt string
   the next draft is actually sent to the LLM (not merely the output of a helper);
2. the filters — the reader must accept a row with the live shape
   (`score=0.0, sample_size=1, confidence=0.2`), so a future confidence threshold
   cannot kill all 24 production rows in silence;
3. the channel shape — rows are stored under the persona id `_persona_block` looks
   up, and `_learn_channel` writes the `pipeline:*` / `crosspost:*` / `worst_angle`
   keys under the same id;
4. the budget and the ranking — at most five lines reach a prompt, ordered by
   measured frequency, never by the inert `score` column: an unmeasured quantity
   must not out-rank a measured one (operator rule, mirrored by
   `tgfarm/s4_generation/_rank_learning_mistakes`).

Deliberately NOT claimed: that the mistake `score` or `confidence` means anything.
Every writer stores `score=0.0` for this kind and the reader never reads `score` or
`confidence` — `sample_size` (how many times the event happened) is the only
quantity with a consumer. `tests/test_scores_of_every_kind_are_read.py`, if anyone
wants the next step, is the file that should exist for the other columns.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from tgfarm.config import Persona
from tgfarm.core.schemas import DraftPost
from tgfarm.db.models import CrosspostLog, LearningMemory, PipelineRun
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import _persona_block, _rank_learning_mistakes, generate
from tgfarm.s12_learning import lessons, record_gate_failure

#: Rows seeded by these tests; the autouse fixture deletes exactly these channels.
SEEDED_CHANNELS: list[str] = []
SEEDED_RUN_IDS: list[str] = []
SEEDED_LOG_KEYS: list[str] = []

#: The shape the live table actually holds for this kind (2026-11-05, read-only).
LIVE_MISTAKE_SCORE = 0.0
LIVE_MISTAKE_SAMPLE_SIZE = 1
LIVE_MISTAKE_CONFIDENCE = 0.2

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    """Every row these tests write belongs to a key they invented."""
    yield
    with session_scope() as session:
        if SEEDED_CHANNELS:
            session.execute(delete(LearningMemory).where(
                LearningMemory.channel_id.in_(SEEDED_CHANNELS)))
        if SEEDED_RUN_IDS:
            session.execute(delete(PipelineRun).where(PipelineRun.run_id.in_(SEEDED_RUN_IDS)))
        if SEEDED_LOG_KEYS:
            session.execute(delete(CrosspostLog).where(
                CrosspostLog.post_public_id.in_(SEEDED_LOG_KEYS)))
    SEEDED_CHANNELS.clear()
    SEEDED_RUN_IDS.clear()
    SEEDED_LOG_KEYS.clear()


def _channel(prefix: str = "mlc") -> str:
    """A channel id nothing else in the farm owns, so counts inside it are exact."""
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    SEEDED_CHANNELS.append(name)
    return name


def _avoid_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- Избегать:")]


def _persona(channel_id: str) -> Persona:
    return replace(
        Persona(id=channel_id, title="Канал урока", niche="technology"),
        tone=["спокойный"],
        style_notes=[],
        banned_words=[],
    )


def _seed_live_shaped_mistake(channel_id: str, key: str, note: str, *,
                              sample_size: int = LIVE_MISTAKE_SAMPLE_SIZE,
                              confidence: float = LIVE_MISTAKE_CONFIDENCE,
                              score: float = LIVE_MISTAKE_SCORE) -> None:
    """One row exactly as today's production writers leave it in the table."""
    with session_scope() as session:
        session.add(
            LearningMemory(
                channel_id=channel_id,
                kind="mistake",
                key=key,
                value={"stage": key.split(":", 1)[1], "error": note},
                score=score,
                sample_size=sample_size,
                confidence=confidence,
                note=note,
                updated_at=NOW,
            )
        )


# ------------------------------------------------------------------ 1. end to end
def test_a_rejected_draft_changes_the_prompt_the_llm_is_given(monkeypatch):
    """The claim under test: a `mistake` row reaches the model request, not a helper.

    `_persona_block` returning a nice string proves nothing on its own — S4 could
    build the prompt from another block, or drop the lesson before the call. So the
    assertion is made on the text captured inside `complete_json`, the argument the
    provider actually receives.
    """
    from tgfarm import s4_generation
    from tgfarm.s4_generation import topic_memory

    channel = _channel()
    for attempt in range(6):
        record_gate_failure(channel, "length", f"177 chars < min 200 #{attempt}")

    captured: dict[str, str] = {}

    class FakeLLM:
        async def complete_json(self, _system, prompt, **_kwargs):
            captured["prompt"] = prompt
            return (
                {"hook": "Подтверждённый факт.", "body": "Абзац с проверяемым фактом.",
                 "hashtags": ["тех"], "cta": None},
                SimpleNamespace(provider="fake", model="fake-1", latency_ms=5),
            )

    plan = SimpleNamespace(
        persona=_persona(channel), angle="news", channel_id=channel,
        cluster_public_id="mlc-cluster",
    )
    item = SimpleNamespace(source_id="src-1", title="Заголовок",
                           text="Тело источника.", url="https://example.org/a")

    async def no_claims(*_args, **_kwargs):
        return []

    monkeypatch.setattr(s4_generation, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(s4_generation, "get_settings",
                        lambda: SimpleNamespace(holdout_share=0.0))
    monkeypatch.setattr(s4_generation, "_to_scored", lambda rows: list(rows))
    monkeypatch.setattr(s4_generation, "compose_offline_post",
                        lambda *_a: {"hook": "Хук", "body": "Тело"})
    monkeypatch.setattr(s4_generation, "_source_block", lambda _items: "ИСТОЧНИКИ")
    monkeypatch.setattr(s4_generation, "_is_somber_topic", lambda _items: False)
    monkeypatch.setattr(s4_generation, "_pick_hook_style", lambda *_a: "fact")
    monkeypatch.setattr(s4_generation, "verify", no_claims)
    monkeypatch.setattr(
        s4_generation, "normalise",
        lambda payload, *_a: DraftPost(hook=payload["hook"], body=payload["body"],
                                       sources=[], angle="news"),
    )
    monkeypatch.setattr(topic_memory, "build_topic_context", lambda *_a: "")
    monkeypatch.setattr(s4_generation.policy, "should_use_ai",
                        lambda *_a, **_k: (True, "test"))

    def reserve(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, usage_id=1)

    monkeypatch.setattr(s4_generation.policy, "reserve", reserve)
    monkeypatch.setattr(s4_generation.policy, "finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(s4_generation.policy, "record_skip", lambda *_a, **_k: None)

    asyncio.run(generate(plan, [item], format_plan=None, strategy_enabled=False,
                        holdout_override=False, examples_override=[],
                        usage_scope="mlcusage"))

    prompt = captured["prompt"]
    assert "ОПЫТ И ОШИБКИ (самообучение):" in prompt, (
        "the stored lesson never reached the request at all"
    )
    avoid = _avoid_lines(prompt)
    assert len(avoid) == 1, avoid
    assert "length" in avoid[0] and "6 отклонений" in avoid[0], avoid[0]
    # The key shape is part of the contract: a writer renaming it would stop being
    # found by anything that selects on `gate:`.
    stored = lessons(channel, kind="mistake")
    assert [row["key"] for row in stored] == ["gate:length"]


def test_the_same_lesson_absent_leaves_the_prompt_without_the_avoid_block():
    """The prompt is a function of the rows, not a constant block that always prints.

    Without this half, a hardcoded "Избегать:" line in the prompt would satisfy the
    test above forever, dead rows or not.
    """
    empty = _persona(_channel())
    before = _persona_block(empty, "news")
    assert "Избегать" not in before

    _seed_live_shaped_mistake(empty.id, "pipeline:s7_publish",
                              "stage s7_publish failed: database or disk is full")
    after = _persona_block(empty, "news")
    assert "ОПЫТ И ОШИБКИ (самообучение):" in after
    assert _avoid_lines(after) == [
        "- Избегать: stage s7_publish failed: database or disk is full"
    ]
    assert after != before


# -------------------------------------------------------------------- 2. filters
def test_a_production_shaped_row_passes_the_readers_filters():
    """No confidence or sample gate may hide all 24 live rows from the writer.

    Every row in production has `score=0.0`, `sample_size=1`, `confidence<=0.3`.
    A reader that demanded more would keep the kind-level guard green and make the
    signal dead in practice; this test is the row-level tripwire for that.
    """
    channel = _channel()
    notes = []
    for index in range(3):
        note = f"гейт gate{index} отклоняет посты: причина {index}"
        notes.append(note)
        _seed_live_shaped_mistake(channel, f"gate:gate{index}", note)
    for stage, confidence in (("s6_visual", 0.2), ("platform_auth", 0.2)):
        _seed_live_shaped_mistake(channel, f"pipeline:{stage}",
                                  f"stage {stage} failed: см. value.error",
                                  confidence=confidence)

    block = _persona_block(_persona(channel), "news")
    avoid = _avoid_lines(block)

    assert len(avoid) == 5, avoid
    for note in notes:
        assert any(note in line for line in avoid), note
    assert any("s6_visual" in line for line in avoid)
    assert any("platform_auth" in line for line in avoid)


def test_a_mistake_row_written_for_another_channel_is_not_rendered():
    """The reader selects by channel; a fleet-wide leak would steer every channel."""
    mine = _channel()
    other = _channel()
    _seed_live_shaped_mistake(mine, "gate:mine", "только мой канал")
    _seed_live_shaped_mistake(other, "gate:other", "чужой канал")

    block = _persona_block(_persona(mine), "news")

    assert _avoid_lines(block) == ["- Избегать: только мой канал"]


# ------------------------------------------------------------- 3. producer shape
def _fake_s8(monkeypatch) -> None:
    """Learning input faked the way a measured channel looks, as in the kind guard."""
    from tgfarm import s12_learning

    perf = {
        angle: {"n": 8, "er": 0.04, "ctr": 0.1, "quality": 0.5 + index / 10}
        for index, angle in enumerate(
            ("news", "analysis", "checklist", "why_it_matters", "history")
        )
    }
    monkeypatch.setattr(s12_learning, "angle_performance",
                        lambda days, channel_id=None: dict(perf))
    monkeypatch.setattr(s12_learning, "hook_style_performance",
                        lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: [])
    monkeypatch.setattr(s12_learning, "learn_format_lessons", lambda *a, **k: {"written": 0})


def test_the_learning_cycle_stores_the_keys_the_writer_looks_up(monkeypatch):
    """`_learn_channel` writes `pipeline:*`, `crosspost:*`, `worst_angle` — S4 reads them.

    The prompt budget is five lines and the stage's error sweep is global, so other
    tests' rows can share the channel; the expectation is therefore recomputed from
    the stored rows by the documented rule (frequency, then recency) instead of
    assuming this channel is empty.
    """
    from tgfarm import s12_learning

    channel = _channel()
    stage = f"x{uuid.uuid4().hex[:6]}"
    platform = f"x{uuid.uuid4().hex[:5]}"
    run_id = uuid.uuid4().hex[:12]
    post_key = uuid.uuid4().hex[:12]
    SEEDED_RUN_IDS.append(run_id)
    SEEDED_LOG_KEYS.append(post_key)

    with session_scope() as session:
        session.add(PipelineRun(run_id=run_id, stage=stage, status="error",
                                error="OperationalError: disk is full"))
        session.add(CrosspostLog(post_public_id=post_key, platform=platform,
                                 status="failed", error="getaddrinfo failed"))

    _fake_s8(monkeypatch)
    s12_learning._learn_channel(channel)  # noqa: SLF001 - the stage's own entry point

    rows = lessons(channel, kind="mistake")
    by_key = {row["key"]: row for row in rows}
    assert {f"pipeline:{stage}", f"crosspost:{platform}", "worst_angle"} <= set(by_key), (
        f"the cycle stored {sorted(by_key)}; a key the writer invented and the "
        "reader never queries is a dead lesson under another name"
    )
    assert {row["channel_id"] for row in rows} == {channel}, (
        "lessons must be stored under the persona id _persona_block queries by"
    )

    expected = _expected_top_lines(rows)
    block = _persona_block(_persona(channel), "news")
    assert _avoid_lines(block) == expected


def _expected_top_lines(rows: list[dict]) -> list[str]:
    """The documented budget rule, recomputed in the test rather than imported."""

    def rank(row: dict) -> tuple[int, str]:
        try:
            count = max(0, int(row.get("sample_size") or 0))
        except (TypeError, ValueError):
            count = 0
        return count, str(row.get("updated_at") or "")

    return [f"- Избегать: {row['note'] or row['key']}"
            for row in sorted(rows, key=rank, reverse=True)[:5]]


# ------------------------------------------------------------------ 4. the budget
def test_the_budget_is_five_lines_and_frequency_wins_the_last_seat():
    """At most five lessons steer one prompt, and the recurring gate keeps a seat.

    `vk_misc_ru` has 13 rows and `zen_misc_ru` 11 in production, so the cap is what
    keeps the prompt bounded; ranking by measured repetitions is what keeps the
    lesson that actually happens most visible inside it.
    """
    channel = _channel()
    for gate in range(8):
        _seed_live_shaped_mistake(channel, f"gate:rare{gate}", f"редкий отказ {gate}")
    _seed_live_shaped_mistake(channel, "gate:common", "гейт common: 40 отклонений",
                              sample_size=40)

    block = _persona_block(_persona(channel), "news")
    avoid = _avoid_lines(block)

    assert len(avoid) == 5
    assert "40 отклонений" in avoid[0]
    assert len(lessons(channel, kind="mistake")) == 9


def test_an_unmeasured_score_never_out_ranks_a_measured_repetition():
    """For this kind the score column is inert; only counted events may rank.

    Every writer stores `score=0.0` for a mistake, so a reader that ranked on score
    would rank on nothing — and a row whose `sample_size` was never measured must
    not pull a prompt on its own.
    """
    channel = _channel()
    _seed_live_shaped_mistake(channel, "gate:loud", "шумная одна штука",
                              sample_size=1, confidence=0.99, score=0.99)
    _seed_live_shaped_mistake(channel, "gate:quiet", "измеренная часто",
                              sample_size=12, confidence=0.0, score=0.0)

    ranked = _rank_learning_mistakes(lessons(channel, kind="mistake"), limit=2)
    assert [row["key"] for row in ranked] == ["gate:quiet", "gate:loud"]

    avoid = _avoid_lines(_persona_block(_persona(channel), "news"))
    assert "измеренная часто" in avoid[0]
    assert "шумная одна штука" in avoid[1]


def test_a_lesson_with_no_note_still_names_its_key():
    """`note` is Text and nullable; falling back to the key keeps the row visible."""
    channel = _channel()
    with session_scope() as session:
        session.add(LearningMemory(channel_id=channel, kind="mistake",
                                   key="crosspost:vk", value={"platform": "vk"},
                                   score=0.0, sample_size=1, confidence=0.2,
                                   note=None, updated_at=NOW))

    assert _avoid_lines(_persona_block(_persona(channel), "news")) == ["- Избегать: crosspost:vk"]

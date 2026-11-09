"""An unreadable critic verdict is not a judgement, in either direction.

W29-S closed the skipped and offline paths: a critic that never ran could no
longer store ``review_board.status = "passed"``. One path was left open. When a
live provider answers with a JSON object whose ``score`` cannot be read, the
gates coerced it to the *offline default* — 8.0 / 7.0 / 7.5, all at or above
their thresholds — and then labelled the outcome ``provenance="online"``. A
model that answered ``{"score": "excellent"}`` therefore produced a recorded
collegial approval with a number nobody gave.

The mirror image is just as dishonest: a missing score became ``0.0`` and was
reported as ``critic 0.0/10`` — a rejection invented by the parser, attributed
to a critic that never scored the draft.

Both are now ``fallback``: no opinion, no invented number, and the deterministic
gates alone decide publishability, exactly as for the offline provider.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from tgfarm.config import Persona
from tgfarm.core.schemas import Claim, DraftPost
from tgfarm.s5_quality import gate_quality, gate_relevance, gate_tone

PERSONA = Persona(id="verdict-test", title="Verdict test", niche="tech")
SOURCE_URL = "https://verdict.invalid/source"

GATES = [gate_tone, gate_relevance, gate_quality]

#: Payloads a live model can really return. `LLMClient.complete_json` already
#: refuses a non-dict (`expected a JSON object` -> offline), so a list or a bare
#: string never reaches a gate and is deliberately not tested here.
UNREADABLE = [
    pytest.param({"score": "excellent"}, id="string-score"),
    pytest.param({"score": None}, id="null-score"),
    pytest.param({"score": float("nan")}, id="nan-score"),
    pytest.param({"score": float("inf")}, id="inf-score"),
    pytest.param({"score": 99}, id="above-the-critics-own-scale"),
    pytest.param({"score": -3}, id="below-the-critics-own-scale"),
    pytest.param({"score": True}, id="boolean-score"),
    pytest.param({"issues": ["нет оценки"]}, id="no-score-key"),
    pytest.param({}, id="empty-object"),
]


def _draft() -> DraftPost:
    return DraftPost(
        hook="Технологическая компания обновила свой продукт. Новые функции уже доступны.",
        body=(
            "Изменения затрагивают несколько ключевых функций сервиса. "
            "Пользователям необходимо проверить новые условия работы приложения. "
            "Подробности и аналитика опубликованы в первоисточниках по ссылкам ниже. "
            "Эксперты отмечают, что обновление может повлиять на рынок в ближайшие месяцы. "
            "Ожидается, что конкуренты также представят свои обновления в течение квартала."
        ),
        sources=[{"title": "Source", "url": SOURCE_URL, "source": "test"}],
        claims=[Claim(claim="продукт обновлён", source_url=SOURCE_URL, verified=True)],
        angle="news",
    )


class _PayloadJudge:
    """A live provider whose payload the test chooses verbatim."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def complete_json(self, _system, _prompt, **_kwargs):
        return self.payload, SimpleNamespace(
            provider="compatible", model="test-model", latency_ms=1, billed=True
        )


def _wire(monkeypatch, payload: dict) -> None:
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(quality, "get_llm", lambda: _PayloadJudge(payload))
    monkeypatch.setattr(
        quality,
        "policy",
        SimpleNamespace(
            should_use_ai=lambda *_a, **_k: (True, ""),
            reserve=lambda *_a, **_k: SimpleNamespace(allowed=True, usage_id=1, reason=""),
            finalize=lambda *_a, **_k: None,
            record_skip=lambda *_a, **_k: None,
        ),
    )
    monkeypatch.setattr(
        quality,
        "get_settings",
        lambda: SimpleNamespace(
            llm_critic_model="", ai_relevance_threshold=7.0, ai_quality_threshold=7.5
        ),
    )


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("payload", UNREADABLE)
def test_an_unreadable_verdict_is_a_fallback_not_an_online_approval(monkeypatch, gate, payload):
    _wire(monkeypatch, payload)

    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))

    assert outcome.provenance == "fallback", (
        f"{gate.__name__} called an unreadable verdict online: {outcome!r}"
    )
    assert outcome.passed is False, "a verdict nobody can read has no opinion"
    assert outcome.scores is None, "no invented number may reach the average"


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("payload", UNREADABLE)
def test_the_reason_never_attributes_a_fabricated_score_to_the_critic(monkeypatch, gate, payload):
    """`critic 0.0/10` reads as a real rejection. It was the parser's default."""
    _wire(monkeypatch, payload)

    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))

    assert outcome.reason, "the operator must be told why there is no verdict"
    assert "critic" not in outcome.reason.lower(), (
        f"{gate.__name__} attributed a number to the critic: {outcome.reason!r}"
    )


@pytest.mark.parametrize("gate", GATES)
def test_a_readable_approval_still_passes_as_online(monkeypatch, gate):
    """The fix must not make the gates refuse every real verdict."""
    _wire(monkeypatch, {"score": 9.0, "issues": [], "fix": ""})

    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))

    assert outcome.passed is True
    assert outcome.provenance == "online"
    assert outcome.scores, "a real score still reaches the average"
    assert all(math.isfinite(v) for v in outcome.scores.values())


@pytest.mark.parametrize("gate", GATES)
def test_a_readable_rejection_still_rejects_as_online(monkeypatch, gate):
    _wire(monkeypatch, {"score": 2.0, "issues": ["слабый тон"], "fix": ""})

    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))

    assert outcome.passed is False
    assert outcome.provenance == "online"
    assert outcome.gate == gate.__name__.removeprefix("gate_")
    assert "2.0" in (outcome.reason or ""), "a real score is still reported"


@pytest.mark.parametrize("gate", GATES)
def test_a_numeric_string_inside_the_scale_is_still_a_verdict(monkeypatch, gate):
    """Providers do answer `"8"`. Readable and in range is readable."""
    _wire(monkeypatch, {"score": "9", "issues": []})

    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))

    assert outcome.passed is True
    assert outcome.provenance == "online"


def test_evaluate_records_the_unreadable_review_as_fallback(monkeypatch):
    """End to end: the panel must not read `passed` for a review that failed to
    produce a verdict, and the offline pipeline must still ship the draft."""
    import tgfarm.s5_quality as quality

    _wire(monkeypatch, {"score": "excellent"})

    report = asyncio.run(
        quality.evaluate(_draft(), PERSONA, generation_provider="online")
    )

    assert report.review_status == "fallback"
    assert set(report.review_provenance) == {"tone", "relevance", "quality"}
    assert all(v == "fallback" for v in report.review_provenance.values())
    assert report.passed is True, "deterministic gates alone decide publishability"
    assert report.scores, "the deterministic gates still score the draft"
    assert all(math.isfinite(v) for v in report.scores.values()), (
        f"a NaN leaked into the stored average: {report.scores!r}"
    )
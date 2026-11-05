"""AI_RELEVANCE_THRESHOLD / AI_QUALITY_THRESHOLD must actually move the S5 gates.

A `Settings` field that no code reads is a lie told in `.env.example`: the
operator turns the dial and nothing happens. Both thresholds used to be module
constants, so the knobs could only be moved by editing Python.

These tests pin both directions: the code default reproduces exactly the
behaviour the hard-coded constants produced, and a different value measurably
moves the gate. They also pin the call-time read — a value frozen at import
would satisfy the first tests and still be dead for a running process.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgfarm.config import Persona
from tgfarm.core.schemas import Claim, DraftPost
from tgfarm.core.settings import Settings
from tgfarm.s5_quality import (
    DEFAULT_QUALITY_THRESHOLD,
    DEFAULT_RELEVANCE_THRESHOLD,
    quality_threshold,
    relevance_threshold,
)

PERSONA = Persona(id="threshold-test", title="Threshold test", niche="tech")

SOURCE_URL = "https://threshold.invalid/source"


def _draft() -> DraftPost:
    return DraftPost(
        hook="Технологическая компания обновила продукт",
        body=(
            "Изменения затрагивают несколько функций сервиса. "
            "Пользователям нужно проверить новые условия работы. "
            "Подробности опубликованы в первоисточниках."
        ),
        sources=[{"title": "Source", "url": SOURCE_URL, "source": "test"}],
        claims=[Claim(claim="продукт обновлён", source_url=SOURCE_URL, verified=True)],
        angle="news",
    )


class _Critic:
    """A judge that always returns one fixed score, so the threshold is the only variable."""

    def __init__(self, score: float) -> None:
        self.score = score

    async def complete_json(self, _system, _prompt, **_kwargs):
        return (
            {"score": self.score, "issues": ["нет аргумента"], "fix": ""},
            SimpleNamespace(provider="online", model="test-model", latency_ms=1),
        )


@pytest.fixture
def critic(monkeypatch):
    """Install a fixed-score judge plus an explicit settings object for the gate."""

    def _install(score: float, **settings_fields: object):
        import tgfarm.s5_quality as quality

        # llm_critic_model is read by the gate before the threshold, so a stub
        # that omits it would fail inside _critic_model() rather than the gate.
        fields: dict[str, object] = {"llm_critic_model": ""}
        fields.update(settings_fields)
        monkeypatch.setattr(quality, "get_llm", lambda: _Critic(score))
        monkeypatch.setattr(
            quality,
            "policy",
            SimpleNamespace(
                should_use_ai=lambda *_a, **_k: (True, ""),
                reserve=lambda *_a, **_k: SimpleNamespace(allowed=True, usage_id="usage-1"),
                finalize=lambda *_a, **_k: None,
                record_skip=lambda *_a, **_k: None,
            ),
        )
        monkeypatch.setattr(quality, "get_settings", lambda: SimpleNamespace(**fields))

    return _install


def _relevance():
    import tgfarm.s5_quality as quality

    return asyncio.run(quality.gate_relevance(_draft(), PERSONA, generation_provider="online"))


def _quality():
    import tgfarm.s5_quality as quality

    return asyncio.run(quality.gate_quality(_draft(), PERSONA, generation_provider="online"))


def test_settings_defaults_match_the_fallback_literals():
    """The module fallback and the Settings default may never drift apart, or the
    documented number and the enforced one silently disagree."""
    declared = Settings.model_fields
    assert declared["ai_relevance_threshold"].default == DEFAULT_RELEVANCE_THRESHOLD == 7.0
    assert declared["ai_quality_threshold"].default == DEFAULT_QUALITY_THRESHOLD == 7.5


def test_no_import_time_constant_is_left_behind():
    """A frozen module constant would let a knob look wired while staying dead."""
    import tgfarm.s5_quality as quality

    for stale in ("RELEVANCE_THRESHOLD", "QUALITY_THRESHOLD"):
        assert not hasattr(quality, stale), f"{stale} is frozen at import time"


def test_tone_threshold_is_unchanged():
    """TONE_THRESHOLD has no Settings field; this wave must not invent one."""
    import tgfarm.s5_quality as quality

    assert quality.TONE_THRESHOLD == 7.5


def test_relevance_default_reproduces_the_hard_coded_behaviour(critic):
    critic(6.9, ai_relevance_threshold=DEFAULT_RELEVANCE_THRESHOLD)
    assert _relevance().passed is False
    critic(7.0, ai_relevance_threshold=DEFAULT_RELEVANCE_THRESHOLD)
    assert _relevance().passed is True


def test_relevance_gate_follows_the_live_setting(critic):
    """The same draft and the same judge score; only the env-backed knob moves."""
    critic(7.2, ai_relevance_threshold=DEFAULT_RELEVANCE_THRESHOLD)
    assert _relevance().passed is True

    critic(7.2, ai_relevance_threshold=8.0)
    outcome = _relevance()
    assert outcome.passed is False
    assert outcome.gate == "relevance"
    assert "7.2/10" in outcome.reason

    critic(7.2, ai_relevance_threshold=5.0)
    assert _relevance().passed is True


def test_quality_default_reproduces_the_hard_coded_behaviour(critic):
    critic(7.4, ai_quality_threshold=DEFAULT_QUALITY_THRESHOLD)
    assert _quality().passed is False
    critic(7.5, ai_quality_threshold=DEFAULT_QUALITY_THRESHOLD)
    assert _quality().passed is True


def test_quality_gate_follows_the_live_setting(critic):
    critic(7.6, ai_quality_threshold=DEFAULT_QUALITY_THRESHOLD)
    assert _quality().passed is True

    critic(7.6, ai_quality_threshold=8.5)
    outcome = _quality()
    assert outcome.passed is False
    assert outcome.gate == "quality"
    assert "7.6/10" in outcome.reason


def test_threshold_is_read_on_every_call_not_at_import(critic):
    """One process, one import, two settings values: the second must take effect."""
    critic(7.4, ai_relevance_threshold=7.0)
    assert relevance_threshold() == 7.0
    assert _relevance().passed is True

    critic(7.4, ai_relevance_threshold=7.5)
    assert relevance_threshold() == 7.5
    assert _relevance().passed is False

    critic(6.0, ai_quality_threshold=7.5)
    assert quality_threshold() == 7.5
    assert _quality().passed is False
    critic(6.0, ai_quality_threshold=5.5)
    assert quality_threshold() == 5.5
    assert _quality().passed is True


def test_real_settings_are_read_without_any_stub():
    """conftest pins AI_RELEVANCE_THRESHOLD / AI_QUALITY_THRESHOLD to the code
    defaults, so the unstubbed accessor must report the number the constants held."""
    import tgfarm.s5_quality as quality

    assert quality.relevance_threshold() == float(Settings.model_fields["ai_relevance_threshold"].default)
    assert quality.quality_threshold() == float(Settings.model_fields["ai_quality_threshold"].default)


def test_stubbed_settings_without_the_field_fall_back(critic):
    """A partial `get_settings` stub — the shape older tests use — must neither
    crash the gate nor silently disable it."""
    critic(6.9)
    assert relevance_threshold() == DEFAULT_RELEVANCE_THRESHOLD
    assert quality_threshold() == DEFAULT_QUALITY_THRESHOLD
    assert _relevance().passed is False


def test_a_non_finite_threshold_cannot_silently_open_the_gate(critic):
    """`score < nan` is always false, so NaN would mean "reject nothing"."""
    critic(6.9, ai_relevance_threshold=float("nan"))
    assert relevance_threshold() == DEFAULT_RELEVANCE_THRESHOLD
    assert _relevance().passed is False

    critic(6.9, ai_relevance_threshold=float("inf"))
    assert relevance_threshold() == DEFAULT_RELEVANCE_THRESHOLD

    critic(6.9, ai_relevance_threshold="не число")
    assert relevance_threshold() == DEFAULT_RELEVANCE_THRESHOLD
    assert _relevance().passed is False

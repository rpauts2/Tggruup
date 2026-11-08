"""W29-S: a review that never happened must never read as an approval.

The defect this file exists to keep dead: ``gate_tone``/``gate_relevance``/
``gate_quality`` returned ``GateOutcome(True)`` on the skipped and offline
fallback paths, so a draft with no AI critic behind it was stored as
``review_board.status = "passed"``. An operator reading the panel saw a
collegial approval that had not taken place.

Two properties are pinned here, and they are deliberately in tension:

1. **Honesty.** ``review_board.status`` is ``skipped``/``fallback``/``rejected``
   whenever no online critic approved the draft — never ``passed``.
2. **The offline pipeline still ships.** A skipped critic does not reject the
   post; the deterministic gates alone decide publishability.

Each test is written so it fails against the old code, not against a mock.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tgfarm.config import Persona
from tgfarm.core.schemas import Claim, DraftPost, QualityReport
from tgfarm.db.models import Channel
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.s5_quality import gate_quality, gate_relevance, gate_tone, save_outcome

PERSONA = Persona(id="integrity-test", title="Integrity test", niche="tech")
SOURCE_URL = "https://integrity.invalid/source"

#: Every gate outcome in this file is one of the five words the directive names.
HONEST_STATUSES = frozenset({"passed", "skipped", "fallback", "rejected", "failed"})


def _draft() -> DraftPost:
    """A draft that passes every cheap/medium gate so AI provenance is the variable.

    `gate_structure` requires a full-sentence hook (ends with ``[.!?…]``);
    `gate_cliches` rejects stock phrases; `gate_redundancy` rejects 6-gram
    overlap between hook and body.  The body must also exceed ``min_chars``
    (150 by default) and the sources must survive ``gate_links``.
    """
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


class _Judge:
    """A critic whose origin is fixed by the test, so provenance is the variable."""

    def __init__(self, *, provider: str, score: float = 9.0) -> None:
        self.provider = provider
        self.score = score

    async def complete_json(self, _system, _prompt, **_kwargs):
        if self.provider == "offline":
            # The real client hands back the caller's own fallback payload on this
            # path; the gate must not mistake that echo for a judgement.
            return {}, SimpleNamespace(provider="offline", model="", latency_ms=0)
        return (
            {"score": self.score, "issues": [], "fix": ""},
            SimpleNamespace(provider=self.provider, model="test-model", latency_ms=1),
        )


def _wire(
    monkeypatch,
    *,
    use_ai: bool,
    allowed: bool = True,
    provider: str = "online",
    score: float = 9.0,
):
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(quality, "get_llm", lambda: _Judge(provider=provider, score=score))
    monkeypatch.setattr(
        quality,
        "policy",
        SimpleNamespace(
            should_use_ai=lambda *_a, **_k: (use_ai, "no online provider" if not use_ai else ""),
            reserve=lambda *_a, **_k: SimpleNamespace(
                allowed=allowed,
                usage_id=None if not allowed else 1,
                reason="budget exhausted",
            ),
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


# --------------------------------------------------------------- gate level
@pytest.mark.parametrize("gate", [gate_tone, gate_relevance, gate_quality])
def test_a_policy_skipped_critic_is_not_a_pass(monkeypatch, gate):
    """The old code returned passed=True with a synthetic 0.8 score here."""
    _wire(monkeypatch, use_ai=False)
    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="offline"))
    assert outcome.passed is False, "a critic that never ran cannot approve"
    assert outcome.provenance == "skipped"
    assert outcome.scores is None, "no invented score may reach the average"


@pytest.mark.parametrize("gate", [gate_tone, gate_relevance, gate_quality])
def test_an_exhausted_budget_is_not_a_pass(monkeypatch, gate):
    _wire(monkeypatch, use_ai=True, allowed=False)
    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))
    assert outcome.passed is False
    assert outcome.provenance == "skipped"
    assert "budget" in (outcome.reason or "")


@pytest.mark.parametrize("gate", [gate_tone, gate_relevance, gate_quality])
def test_an_offline_fallback_is_not_a_pass(monkeypatch, gate):
    """The provider answered with our own fallback literal, not with a review."""
    _wire(monkeypatch, use_ai=True, provider="offline")
    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))
    assert outcome.passed is False
    assert outcome.provenance == "fallback"
    assert outcome.scores is None


@pytest.mark.parametrize("gate", [gate_tone, gate_relevance, gate_quality])
def test_a_real_critic_approval_is_a_pass(monkeypatch, gate):
    _wire(monkeypatch, use_ai=True, provider="online", score=9.0)
    outcome = asyncio.run(gate(_draft(), PERSONA, generation_provider="online"))
    assert outcome.passed is True
    assert outcome.provenance == "online"


def test_a_real_critic_rejection_keeps_online_provenance(monkeypatch):
    """A low score is a genuine rejection: it must say so, not hide behind a skip."""
    _wire(monkeypatch, use_ai=True, provider="online", score=2.0)
    outcome = asyncio.run(gate_tone(_draft(), PERSONA, generation_provider="online"))
    assert outcome.passed is False
    assert outcome.provenance == "online"


# ------------------------------------------------------------ report level
def test_review_status_is_passed_only_for_a_real_online_review():
    online = {"tone": "online", "relevance": "online", "quality": "online"}
    assert QualityReport(passed=True, review_provenance=online).review_status == "passed"
    assert (
        QualityReport(passed=False, gate="tone", review_provenance=online).review_status
        == "rejected"
    )


def test_review_status_reports_skipped_rather_than_passed():
    report = QualityReport(
        passed=True,
        review_provenance={"tone": "skipped", "relevance": "skipped", "quality": "skipped"},
    )
    assert report.passed is True, "the offline pipeline still ships"
    assert report.review_status == "skipped", "but nobody may call that an approval"


def test_review_status_reports_fallback_rather_than_passed():
    report = QualityReport(
        passed=True,
        review_provenance={"tone": "fallback", "relevance": "fallback", "quality": "fallback"},
    )
    assert report.passed is True
    assert report.review_status == "fallback"


def test_a_partially_reviewed_draft_is_not_reported_as_reviewed():
    """One online critic plus two skips is not a collegial approval."""
    report = QualityReport(
        passed=True,
        review_provenance={"tone": "online", "relevance": "skipped", "quality": "skipped"},
    )
    assert report.review_status == "skipped"
    assert report.review_status in HONEST_STATUSES


def test_a_directly_stated_verdict_keeps_the_old_wording():
    """An operator or test that writes the verdict by hand has no provenance to
    consult; the boolean alone must keep answering, or every existing consumer
    of QualityReport starts reporting a review that was never run at all."""
    assert QualityReport(passed=True).review_status == "passed"
    assert QualityReport(passed=False, gate="tone").review_status == "rejected"


# ------------------------------------------------------------- evaluate glue
def test_evaluate_passes_the_draft_but_records_the_missing_review(monkeypatch):
    import tgfarm.s5_quality as quality

    _wire(monkeypatch, use_ai=False)
    report = asyncio.run(quality.evaluate(_draft(), PERSONA, generation_provider="offline"))
    assert report.passed is True
    assert set(report.review_provenance) == {"tone", "relevance", "quality"}
    assert report.review_status == "skipped"
    assert report.scores, "the deterministic gates still score the draft"


def test_evaluate_still_rejects_on_a_real_online_rejection(monkeypatch):
    import tgfarm.s5_quality as quality

    _wire(monkeypatch, use_ai=True, provider="online", score=2.0)
    report = asyncio.run(quality.evaluate(_draft(), PERSONA, generation_provider="online"))
    assert report.passed is False
    assert report.gate == "tone"
    assert report.review_status == "rejected"


def test_evaluate_carries_provenance_into_a_deterministic_rejection(monkeypatch):
    """A draft a real critic approved and a deterministic gate rejected must
    still say the review happened; otherwise the panel hides a completed one."""
    import tgfarm.s5_quality as quality

    _wire(monkeypatch, use_ai=True, provider="online", score=9.0)
    draft = _draft()
    draft.body = "Слишком коротко."
    report = asyncio.run(quality.evaluate(draft, PERSONA, generation_provider="online"))
    assert report.passed is False
    assert report.gate == "length"
    assert report.review_provenance == {}


# ----------------------------------------------------------------- storage
@pytest.fixture
def post(db):
    """A fresh post per test: the session-scoped test store is never rolled back,
    so a fixed public_id would collide on the second storage test."""
    del db  # the shared fixture has already pointed the session at the test store
    public_id = f"integrity-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        session.merge(
            Channel(id="integrity_channel", title="Integrity", niche="tech", platform="vk")
        )
        session.add(
            PostRow(
                public_id=public_id,
                channel_id="integrity_channel",
                hook="Проверка честности результата рецензии.",
                body="Тело поста, достаточное для записи вердикта в базу.",
                status="draft",
            )
        )
    return public_id


def _stored_board(public_id: str) -> dict:
    with session_scope() as session:
        row = session.execute(
            select(PostRow).where(PostRow.public_id == public_id)
        ).scalar_one()
        return row.quality["agent_team"]["roles"]["review_board"]


def test_a_skipped_review_is_stored_as_skipped_not_passed(post):
    save_outcome(
        post,
        QualityReport(
            passed=True,
            scores={"structure": 1.0},
            reason="mean=1.00",
            review_provenance={
                "tone": "skipped",
                "relevance": "skipped",
                "quality": "skipped",
            },
        ),
    )
    board = _stored_board(post)
    assert board["status"] == "skipped"
    assert board["status"] in HONEST_STATUSES
    assert board["provenance"] == {
        "tone": "skipped",
        "relevance": "skipped",
        "quality": "skipped",
    }
    with session_scope() as session:
        row = session.execute(select(PostRow).where(PostRow.public_id == post)).scalar_one()
        assert row.status == "gated", "publishability is decided apart from the review"


def test_a_fallback_review_is_stored_as_fallback(post):
    save_outcome(
        post,
        QualityReport(
            passed=True,
            scores={"structure": 1.0},
            review_provenance={"tone": "fallback", "relevance": "fallback", "quality": "fallback"},
        ),
    )
    assert _stored_board(post)["status"] == "fallback"


def test_a_real_online_approval_is_stored_as_passed(post):
    save_outcome(
        post,
        QualityReport(
            passed=True,
            scores={"tone": 0.9},
            review_provenance={"tone": "online", "relevance": "online", "quality": "online"},
        ),
    )
    assert _stored_board(post)["status"] == "passed"


def test_a_rejection_never_stores_an_approval(post):
    save_outcome(
        post,
        QualityReport(
            passed=False,
            gate="tone",
            reason="critic 3.0/10",
            review_provenance={"tone": "online"},
        ),
    )
    board = _stored_board(post)
    assert board["status"] == "rejected"
    with session_scope() as session:
        row = session.execute(select(PostRow).where(PostRow.public_id == post)).scalar_one()
        assert row.status == "rejected"

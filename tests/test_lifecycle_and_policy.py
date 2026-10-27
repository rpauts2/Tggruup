"""Lifecycle and AI-policy regressions: generation must not skip the gates."""

from __future__ import annotations

from sqlalchemy import select

from tgfarm.config import get_config
from tgfarm.core.schemas import DraftPost, QualityReport
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.s3_router import ChannelPlan
from tgfarm.s4_generation import persist_draft
from tgfarm.s5_quality import _pending_ids, save_outcome


def _plan() -> ChannelPlan:
    persona = get_config().persona("zen_misc_ru")
    assert persona is not None
    return ChannelPlan(
        cluster_public_id="cluster-test",
        channel_id=persona.id,
        persona=persona,
        angle="news",
        score=0.9,
    )


def _draft() -> DraftPost:
    return DraftPost(
        hook="Тестовая история для проверки жизненного цикла.",
        body="Это достаточно длинный текст, чтобы пройти базовые проверки качества. "
        "Он нужен только для теста и не содержит реальных утверждений.",
        sources=[{"title": "Тест", "url": "https://example.invalid/story", "source": "test"}],
    )


def test_persist_draft_starts_as_draft_and_is_pending():
    public_id = persist_draft(_draft(), _plan())

    with session_scope() as session:
        row = session.execute(select(PostRow).where(PostRow.public_id == public_id)).scalar_one_or_none()
        assert row is not None
        assert row.status == "draft"

    assert public_id in _pending_ids()


def test_save_outcome_promotes_passed_draft_to_gated():
    public_id = persist_draft(_draft(), _plan())

    save_outcome(
        public_id,
        QualityReport(passed=True, scores={"structure": 1.0}, reason="mean=1.00"),
    )

    with session_scope() as session:
        row = session.execute(select(PostRow).where(PostRow.public_id == public_id)).scalar_one_or_none()
        assert row is not None
        assert row.status == "gated"
        assert row.reject_reason is None
    assert public_id not in _pending_ids()

    save_outcome(
        public_id,
        QualityReport(passed=False, gate="tone", reason="too dry"),
    )

    with session_scope() as session:
        row = session.execute(select(PostRow).where(PostRow.public_id == public_id)).scalar_one_or_none()
        assert row is not None
        assert row.status == "rejected"
        assert row.reject_reason == "tone: too dry"


def _policy_settings(**overrides):
    from types import SimpleNamespace

    base = {
        "online": True,
        "ai_policy": "selective",
        "ai_daily_budget": 2,
        "provider_chain": ["longcat", "offline"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_selective_ai_skips_good_offline_news(monkeypatch):
    from types import SimpleNamespace

    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "get_settings", lambda: _policy_settings())
    persona = SimpleNamespace(min_chars=350)

    assert policy.should_use_ai(
        "s4_draft", angle="news", source_count=1, offline_chars=900, persona=persona
    )[0] is False
    assert policy.should_use_ai(
        "s4_draft", angle="analysis", source_count=1, offline_chars=900, persona=persona
    )[0] is True
    assert policy.should_use_ai(
        "s4_draft", angle="checklist", source_count=2, offline_chars=900, persona=persona
    )[0] is True
    assert policy.should_use_ai(
        "s4_draft", angle="news", source_count=1, offline_chars=120, persona=persona
    )[0] is True


def test_selective_ai_skips_verify_and_tone_for_offline_draft(monkeypatch):
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "get_settings", lambda: _policy_settings())
    assert policy.should_use_ai("s4_verify", generation_provider="offline")[0] is False
    assert policy.should_use_ai("s5_tone", generation_provider="offline")[0] is False
    assert policy.should_use_ai("s5_rewrite", generation_provider="offline")[0] is True


def test_budget_reservations_release_on_offline_fallback(monkeypatch):
    from sqlalchemy import delete

    from tgfarm.db.models import AIUsage
    from tgfarm.llm import policy

    with session_scope() as session:
        session.execute(delete(AIUsage))

    monkeypatch.setattr(policy, "get_settings", lambda: _policy_settings(ai_daily_budget=2))

    first = policy.reserve("s4_draft")
    second = policy.reserve("s4_draft")
    blocked = policy.reserve("s4_draft")
    assert first.allowed and second.allowed
    assert not blocked.allowed
    assert policy.charged_today() == 2

    policy.finalize(first.usage_id, provider="offline", used=False)
    assert policy.charged_today() == 1

    third = policy.reserve("s4_draft")
    assert third.allowed

    policy.finalize(second.usage_id, provider="longcat", model="LongCat-2.5-Preview", used=True)
    assert policy.charged_today() == 2
    assert not policy.reserve("s4_draft").allowed


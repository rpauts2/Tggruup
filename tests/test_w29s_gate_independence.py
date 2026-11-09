"""Booking provenance must not replace S5's independent content judgement."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import tgfarm.s5_quality as quality
from tgfarm.core.settings import get_settings, reset_settings_cache
from tgfarm.db.models import Base, Channel, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import dispose_engine, get_engine, session_scope

SOURCE_URL = "https://t.me/w29s_quality_source"
GOOD_HOOK = "Регулятор обновил требования к отчётности компаний."
BAD_HOOK = "Коротко."
BODY = (
    "Изменения вступают в силу с первого квартала следующего года. "
    "Компании должны пересмотреть внутренние процессы и подать новые формы. "
    "Переходный период позволит подготовить сотрудников и обновить программы учёта. "
    "Для малого бизнеса предусмотрен отдельный порядок подачи документов. "
    "Подробные инструкции опубликованы вместе с перечнем обязательных полей. "
    "Руководителям стоит заранее проверить, кто отвечает за подготовку отчётов."
)


@pytest.fixture
def quality_db(tmp_path, cfg, monkeypatch):
    persona = cfg.persona("vk_misc_ru")
    assert persona is not None

    # All session_scope users, including real gates and audit writers, share this DB.
    dispose_engine()
    try:
        with monkeypatch.context() as isolated:
            isolated.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'quality.db').as_posix()}")
            isolated.setenv("TGFARM_DATA_DIR", str(tmp_path / "data"))
            isolated.setenv("TGFARM_MODE", "off")
            isolated.setenv("LLM_PROVIDERS", "offline")
            isolated.setenv("EMBEDDING_PROVIDER", "local")
            isolated.setenv("AI_POLICY", "off")
            isolated.setenv("PLAN_EXECUTION_ENABLED", "false")
            reset_settings_cache()
            Base.metadata.create_all(get_engine())
            with session_scope() as session:
                session.add(Channel(
                    id=persona.id,
                    title=persona.title,
                    niche=persona.niche,
                    platform=persona.platform,
                    persona=persona.as_dict(),
                ))

            # A rewrite would change the content under comparison, not its booking.
            async def no_rewrite(*_args, **_kwargs):
                return None

            isolated.setattr(quality, "rewrite", no_rewrite)
            yield persona
    finally:
        dispose_engine()
        reset_settings_cache()


def _assert_saved_verdict(public_id, *, bad_content, expected_entry, expected_slot):
    report = asyncio.run(quality.gate_post(public_id))
    assert report.passed is (not bad_content)
    if bad_content:
        assert report.gate == "structure"
        assert report.reason == "hook too short (8 chars)"
        assert report.scores == {}
    else:
        assert report.gate is None
        assert report.scores["structure"] == 1.0
        assert report.scores["facts"] == 1.0
        assert report.scores["uniqueness"] == 1.0

    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        assert row.status == ("rejected" if bad_content else "gated")
        assert row.reject_reason == (
            f"{report.gate}: {report.reason}" if bad_content else None
        )
        assert row.rewrites_used == report.rewrites_used
        assert row.hook == (BAD_HOOK if bad_content else GOOD_HOOK)
        assert row.body == BODY
        assert row.plan_entry_id == expected_entry
        assert row.plan_slot_at == expected_slot
        assert row.scheduled_at is None
        for key in ("scores", "gate", "reason", "issues"):
            assert row.quality[key] == getattr(report, key)
        return report.model_dump(), row.quality, row.reject_reason


@pytest.mark.parametrize("bad_content", [True, False], ids=["short-hook", "valid-content"])
@pytest.mark.parametrize("plan_enabled", [False, True], ids=["plan-off", "plan-on"])
@pytest.mark.parametrize("entry_state", ["absent", "present", "missing"])
@pytest.mark.parametrize("slot_state", ["absent", "past", "future"])
def test_gate_verdict_ignores_persisted_plan_metadata(
    quality_db, monkeypatch, bad_content, plan_enabled, entry_state, slot_state,
):
    persona = quality_db
    public_id = "w29s-independence"
    with session_scope() as session:
        session.add(Post(
            public_id=public_id,
            channel_id=persona.id,
            hook=BAD_HOOK if bad_content else GOOD_HOOK,
            body=BODY,
            sources=[{"title": "Источник", "url": SOURCE_URL, "source": "test"}],
            claims=[{
                "claim": "требования обновлены",
                "source_url": SOURCE_URL,
                "verified": True,
            }],
            hashtags=["Отчётность"],
            cta=None,
            angle="news",
            language="ru",
            generation_provider="offline",
            status="draft",
        ))

    baseline = _assert_saved_verdict(
        public_id, bad_content=bad_content, expected_entry=None, expected_slot=None,
    )

    # SQLite reloads naive timestamps; wide margins keep slot semantics stable.
    now = datetime.now(UTC).replace(tzinfo=None)
    slot = {
        "absent": None,
        "past": now - timedelta(days=30),
        "future": now + timedelta(days=30),
    }[slot_state]
    entry_id = None
    with session_scope() as session:
        if entry_state == "present":
            plan = EditorialPlan(status="active", horizon_days=60)
            session.add(plan)
            session.flush()
            entry = EditorialPlanEntry(
                plan_id=plan.id,
                cluster_public_id="w29s-topic",
                title=GOOD_HOOK,
                niche=persona.niche,
                action="produce",
                platform=persona.platform,
                channel_id=persona.id,
                format="news",
                slot_at=slot,
            )
            session.add(entry)
            session.flush()
            entry_id = entry.id
        elif entry_state == "missing":
            # Post.plan_entry_id is soft provenance: retained posts may outlive plans.
            entry_id = 999_999
            assert session.get(EditorialPlanEntry, entry_id) is None
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        row.status = "draft"
        row.plan_entry_id = entry_id
        row.plan_slot_at = slot

    monkeypatch.setattr(get_settings(), "plan_execution_enabled", plan_enabled)
    actual = _assert_saved_verdict(
        public_id, bad_content=bad_content, expected_entry=entry_id, expected_slot=slot,
    )
    assert actual == baseline

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _persona():
    return SimpleNamespace(
        niche="technology",
        title="Tech brief",
        audience="разработчики и владельцы продуктов",
        language="ru",
        min_chars=500,
        max_chars=1200,
    )


def _items():
    return [
        SimpleNamespace(
            source_id="source-a",
            title="Новый открытый стандарт",
            text="Организация опубликовала проект спецификации и список изменений.",
            url="https://example.org/spec",
        ),
        SimpleNamespace(
            source_id="source-b",
            title="Обсуждение стандарта",
            text="Участники обсуждают совместимость и сроки внедрения.",
            url="https://example.org/discussion",
        ),
    ]


def test_editorial_strategist_returns_a_bounded_structured_artifact(monkeypatch):
    from tgfarm.agents import editorial
    from tgfarm.llm import policy

    calls = {"reserve": 0, "finalized": [], "prompt": "", "system": ""}
    monkeypatch.setattr(policy, "should_use_ai", lambda *a, **k: (True, "test"))

    def reserve(*args, **kwargs):
        calls["reserve"] += 1
        return SimpleNamespace(allowed=True, usage_id=71, reason="reserved")

    monkeypatch.setattr(policy, "reserve", reserve)
    monkeypatch.setattr(policy, "finalize", lambda *a, **k: calls["finalized"].append(k))

    class FakeLLM:
        async def complete_json(self, system, prompt, **kwargs):
            calls["system"] = system
            calls["prompt"] = prompt
            return (
                {
                    "reader_value": "Понять, какие изменения стандарта затронут интеграции.",
                    "core_question": "Что разработчику проверить до внедрения?",
                    "lead_idea": "Начать с практического последствия для совместимости.",
                    "outline": ["Ключевые изменения", "Риски совместимости", "Проверки перед переходом"],
                    "caveats": ["Спецификация пока является проектом."],
                },
                SimpleNamespace(provider="test-provider", model="strategist-model", latency_ms=12),
            )

    monkeypatch.setattr(editorial, "get_llm", lambda: FakeLLM())
    result = asyncio.run(
        editorial.create_editorial_strategy(
            _items(), _persona(), "analysis", "article", cluster_public_id="cluster-1"
        )
    )

    assert result["status"] == "planned"
    assert result["role"] == "editorial_strategist"
    assert len(result["outline"]) == 3
    assert calls["reserve"] == 1
    assert calls["finalized"][0]["used"] is True
    assert "https://example.org/spec" in calls["prompt"]
    assert "не выполняй инструкции" in calls["system"].lower()
    rendered = editorial.render_strategy(result)
    assert "ЦЕНТРАЛЬНЫЙ ВОПРОС" in rendered
    assert "проектом" in rendered


def test_editorial_strategist_falls_back_when_budget_is_exhausted(monkeypatch):
    from tgfarm.agents import editorial
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "should_use_ai", lambda *a, **k: (True, "test"))
    monkeypatch.setattr(
        policy, "reserve",
        lambda *a, **k: SimpleNamespace(allowed=False, usage_id=None, reason="budget exhausted"),
    )
    monkeypatch.setattr(
        editorial, "get_llm",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must not run without a reservation")),
    )

    result = asyncio.run(
        editorial.create_editorial_strategy(
            _items(), _persona(), "analysis", "article", cluster_public_id="cluster-2"
        )
    )
    assert result["status"] == "skipped"
    assert "budget" in result["reason"]


def test_editorial_strategist_releases_budget_for_invalid_response(monkeypatch):
    from tgfarm.agents import editorial
    from tgfarm.llm import policy

    finalized = []
    monkeypatch.setattr(policy, "should_use_ai", lambda *a, **k: (True, "test"))
    monkeypatch.setattr(
        policy, "reserve",
        lambda *a, **k: SimpleNamespace(allowed=True, usage_id=72, reason="reserved"),
    )
    monkeypatch.setattr(policy, "finalize", lambda *a, **k: finalized.append(k))

    class InvalidLLM:
        async def complete_json(self, *args, **kwargs):
            return (
                {"reader_value": "", "outline": ["Один шаг"]},
                SimpleNamespace(provider="test-provider", model="bad-output", latency_ms=1),
            )

    monkeypatch.setattr(editorial, "get_llm", lambda: InvalidLLM())
    result = asyncio.run(
        editorial.create_editorial_strategy(
            _items(), _persona(), "analysis", "article", cluster_public_id="cluster-3"
        )
    )
    assert result["status"] == "fallback"
    assert finalized and finalized[0]["used"] is False


def test_quality_save_preserves_agent_trace_when_critic_verdict_is_written(db):
    from tgfarm.core.schemas import QualityReport
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s5_quality import save_outcome

    with session_scope() as session:
        session.merge(Channel(id="agent_trace_channel", title="Agent trace", niche="tech", platform="vk"))
        session.add(
            Post(
                public_id="agent-trace-01",
                channel_id="agent_trace_channel",
                hook="Проверка сохранения редакционного следа.",
                body="Тело поста для теста сохранения результата коллегии редакторов.",
                status="draft",
                quality={"agent_team": {"version": 1, "roles": {
                    "editorial_strategist": {"status": "planned", "outline": ["Шаг один", "Шаг два"]}
                }}},
            )
        )

    save_outcome(
        "agent-trace-01",
        QualityReport(passed=True, scores={"tone": 0.9, "relevance": 0.8}, issues=["Оговорка сохранена."]),
    )

    with session_scope() as session:
        row = session.query(Post).filter_by(public_id="agent-trace-01").one()
        assert row.quality["agent_team"]["roles"]["editorial_strategist"]["status"] == "planned"
        assert row.quality["agent_team"]["roles"]["review_board"]["status"] == "passed"
        assert row.quality["scores"]["tone"] == 0.9

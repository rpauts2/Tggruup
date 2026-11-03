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

    calls = {"reserve": 0, "finalized": [], "prompt": "", "system": "", "model": None}
    monkeypatch.setattr(policy, "should_use_ai", lambda *a, **k: (True, "test"))
    monkeypatch.setattr(editorial, "get_settings", lambda: SimpleNamespace(llm_strategist_model="reasoning-model"))

    def reserve(*args, **kwargs):
        calls["reserve"] += 1
        return SimpleNamespace(allowed=True, usage_id=71, reason="reserved")

    monkeypatch.setattr(policy, "reserve", reserve)
    monkeypatch.setattr(policy, "finalize", lambda *a, **k: calls["finalized"].append(k))

    class FakeLLM:
        async def complete_json(self, system, prompt, **kwargs):
            calls["system"] = system
            calls["prompt"] = prompt
            calls["model"] = kwargs.get("model")
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
    assert calls["model"] == "reasoning-model"
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


def test_generation_control_arm_skips_strategist_and_preserves_pair_inputs(monkeypatch):
    from tgfarm import s4_generation
    from tgfarm.s4_generation import topic_memory
    from tgfarm.core.schemas import DraftPost

    captured = {"prompt": "", "usage_scope": "", "examples": None}

    class FakeLLM:
        async def complete_json(self, _system, prompt, **_kwargs):
            captured["prompt"] = prompt
            return (
                {
                    "hook": "Практический результат уже подтверждён.",
                    "body": "Первый абзац содержит проверяемый факт из источника.\\n\\n"
                    "Второй абзац объясняет, что это значит для читателя.",
                    "hashtags": ["технологии"],
                    "cta": None,
                },
                SimpleNamespace(provider="test-provider", model="writer-v1", latency_ms=15),
            )

    persona = SimpleNamespace(
        id="benchmark-channel",
        title="Тестовый канал",
        niche="technology",
        audience="разработчики",
        language="ru",
        min_chars=40,
        max_chars=1200,
        tone=["спокойный"],
        banned_words=[],
        emoji_policy="none",
        disclaimer="",
        always_cite_sources=True,
        max_sources=3,
        as_dict=lambda: {"title": "Тестовый канал"},
    )
    plan = SimpleNamespace(
        persona=persona,
        angle="analysis",
        channel_id="benchmark-channel",
        cluster_public_id="cluster-001",
    )
    item = SimpleNamespace(
        source_id="source-1",
        title="Подтверждённое изменение",
        text="Источник подтверждает практическое изменение для разработчиков.",
        url="https://example.org/change",
    )
    monkeypatch.setattr(s4_generation, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(s4_generation, "get_settings", lambda: SimpleNamespace(holdout_share=0.1))
    monkeypatch.setattr(s4_generation, "_to_scored", lambda rows: list(rows))
    monkeypatch.setattr(s4_generation, "compose_offline_post", lambda *_args: {
        "hook": "Практический результат уже подтверждён.",
        "body": "Детерминированный базовый текст.",
    })
    monkeypatch.setattr(s4_generation, "_persona_block", lambda *_args: "PERSONA")
    monkeypatch.setattr(s4_generation, "_source_block", lambda _items: "SOURCES")

    def render_examples(examples):
        captured["examples"] = examples
        return "SHARED EXAMPLES"

    monkeypatch.setattr(s4_generation, "_examples_block", render_examples)
    monkeypatch.setattr(s4_generation, "_is_somber_topic", lambda _items: False)

    async def no_claims(*_args, **_kwargs):
        return []

    monkeypatch.setattr(s4_generation, "verify", no_claims)
    monkeypatch.setattr(
        s4_generation,
        "normalise",
        lambda payload, *_args: DraftPost(
            hook=payload["hook"],
            body=payload["body"],
            sources=[{"title": item.title, "url": item.url}],
            angle="analysis",
        ),
    )
    monkeypatch.setattr(
        s4_generation.policy,
        "should_use_ai",
        lambda *_args, **_kwargs: (True, "benchmark test"),
    )

    def reserve(*_args, **kwargs):
        captured["usage_scope"] = kwargs.get("cluster_public_id", "")
        return SimpleNamespace(allowed=True, usage_id=7)

    monkeypatch.setattr(s4_generation.policy, "reserve", reserve)
    monkeypatch.setattr(s4_generation.policy, "finalize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(s4_generation, "_pick_hook_style", lambda *_args, **_kwargs: "analysis")
    monkeypatch.setattr(topic_memory, "build_topic_context", lambda *_args: "")

    draft = asyncio.run(
        s4_generation.generate(
            plan,
            [item],
            format_plan=SimpleNamespace(fmt="article", min_chars=40, max_chars=1200, reason="test"),
            strategy_enabled=False,
            holdout_override=False,
            examples_override=[("Example hook", "Example body")],
            hook_style_override="analysis",
            usage_scope="ab1234567890control",
        )
    )

    assert draft.generation_provider == "test-provider"
    assert draft.generation_model == "writer-v1"
    assert draft.editorial_strategy["status"] == "disabled"
    assert "ОТДЕЛЬНЫЙ ПЛАН РЕДАКЦИОННОГО СТРАТЕГА" not in captured["prompt"]
    assert "SHARED EXAMPLES" in captured["prompt"]
    assert "СТИЛЬ ХУКА:" in captured["prompt"]
    assert captured["examples"] == [("Example hook", "Example body")]
    assert captured["usage_scope"] == "ab1234567890control"


def _post_id_for_trace(public_id: str) -> int:
    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        return int(session.query(Post.id).filter_by(public_id=public_id).scalar())


def test_platform_editor_trace_merges_with_existing_agent_team_and_keeps_metadata_only(db):
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s9_growth import record_platform_editor_trace

    with session_scope() as session:
        session.merge(Channel(
            id="platform_editor_trace_channel",
            title="Platform editor trace",
            niche="technology",
            platform="telegram",
        ))
        session.add(Post(
            public_id="platform-editor-trace-01",
            channel_id="platform_editor_trace_channel",
            hook="Заголовок материала.",
            body="Содержимое поста не должно копироваться в trace.",
            status="published",
            quality={
                "agent_team": {
                    "version": 1,
                    "roles": {
                        "editorial_strategist": {
                            "status": "planned",
                            "outline": ["Сохранить исходную мысль"],
                        }
                    },
                }
            },
        ))

    post_id = _post_id_for_trace("platform-editor-trace-01")
    assert record_platform_editor_trace(
        post_id,
        "bluesky",
        {
            "status": "ai_native",
            "used_ai": True,
            "provider": "test-provider",
            "model": "editor-model",
            "reason": "Создана нативная версия.",
        },
        {
            "format": "Короткий пост",
            "eligible": True,
            "target_type": "profile",
        },
        destination="connected Bluesky profile",
    )
    assert record_platform_editor_trace(
        post_id,
        "threads",
        {"status": "special_workflow", "used_ai": False},
        {"format": "Пост обсуждения", "eligible": True, "target_type": "profile"},
    )
    assert record_platform_editor_trace(
        post_id,
        "bluesky",
        {"status": "deterministic_guardrail", "used_ai": False, "model": "editor-model"},
        {"format": "Короткий пост", "eligible": True, "target_type": "profile"},
    )

    with session_scope() as session:
        row = session.query(Post).filter_by(public_id="platform-editor-trace-01").one()
        team = row.quality["agent_team"]
        assert team["roles"]["editorial_strategist"]["status"] == "planned"
        assert set(team["platform_edits"]) == {"bluesky", "threads"}
        assert team["platform_edits"]["bluesky"]["status"] == "deterministic_guardrail"
        assert team["platform_edits"]["bluesky"]["model"] == "editor-model"
        assert team["platform_edits"]["threads"]["status"] == "special_workflow"
        assert "Содержимое поста" not in repr(team["platform_edits"])

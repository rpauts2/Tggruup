"""Generation, routing, scheduling, crossposting and the safe-growth guard."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from tgfarm.config import Persona
from tgfarm.core.schemas import ItemScored, RawItem
from tgfarm.s3_router import _stems, plan_cluster, sameness
from tgfarm.s4_generation import _lexically_supported, _normalise_number, _to_scored, normalise
from tgfarm.s7_publish import plan_slot
from tgfarm.s9_growth import FORBIDDEN_GROWTH_PRIMITIVES, UnsafeGrowthPrimitive, assert_safe_growth


def scored(title: str, text: str = "", url: str = "", niche="tech", weight=0.6) -> ItemScored:
    return ItemScored(
        source_id="s",
        external_id="e",
        url=url or f"https://example.invalid/{abs(hash(title))}",
        title=title,
        text=text,
        niche_hint=[niche],
        source_weight=weight,
    )


@pytest.mark.asyncio
async def test_publish_tick_runs_only_the_publisher(monkeypatch):
    from tgfarm import pipeline, s7_publish

    calls = []

    async def fake_publish_run():
        calls.append("publish")
        return {"scheduled": 2, "published": 1, "failed": 0}

    async def fail_full_cycle(**kwargs):
        pytest.fail("the fast publish tick must not run the full content pipeline")

    monkeypatch.setattr(s7_publish, "run", fake_publish_run)
    monkeypatch.setattr(pipeline, "run_cycle", fail_full_cycle)

    result = await pipeline._publish_tick()

    assert calls == ["publish"]
    assert result["errors"] == []
    assert result["stages"]["s7_publish"]["scheduled"] == 2
    assert result["stages"]["s7_publish"]["published"] == 1


@pytest.mark.asyncio
async def test_publish_batches_are_serialized(monkeypatch):
    import asyncio

    from tgfarm import s7_publish

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_publish_once(limit=1000, post_public_ids=None):
        calls.append((limit, post_public_ids))
        started.set()
        await release.wait()
        return {"published": 1, "failed": 0, "errors": []}

    monkeypatch.setattr(s7_publish, "_active_publish_task", None)
    monkeypatch.setattr(s7_publish, "_publish_due_once", fake_publish_once)

    first = asyncio.create_task(s7_publish.publish_due(limit=7))
    await asyncio.wait_for(started.wait(), timeout=2)
    second = await s7_publish.publish_due(limit=4)

    assert second["skipped_concurrent"] == 1
    assert calls == [(7, None)]

    release.set()
    result = await asyncio.wait_for(first, timeout=2)
    assert result["published"] == 1


@pytest.mark.asyncio
async def test_guarded_cycle_skips_a_concurrent_full_cycle(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from tgfarm import pipeline

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_cycle(*, collect=True, max_clusters=1000):
        started.set()
        await release.wait()
        return pipeline.CycleResult(run_id="cycle-guard-test")

    monkeypatch.setattr(pipeline, "_active_cycle_task", None)
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(cycle_timeout_min=2),
    )
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)

    first = asyncio.create_task(pipeline._guarded_cycle())
    await asyncio.wait_for(started.wait(), timeout=2)
    second = await pipeline._guarded_cycle()
    assert second is None

    release.set()
    result = await asyncio.wait_for(first, timeout=2)
    assert result is not None
    assert result.run_id == "cycle-guard-test"


@pytest.mark.asyncio
async def test_watchdog_cancels_stale_cycle_before_catchup(monkeypatch):
    from tgfarm import pipeline

    class ActiveTask:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    active = ActiveTask()
    monkeypatch.setattr(pipeline, "_active_cycle_task", active)
    monkeypatch.setattr(pipeline, "_cycle_state", "running")
    monkeypatch.setattr(pipeline, "_cycle_finished_at", None)
    monkeypatch.setattr(
        pipeline,
        "cycle_health",
        lambda: {"state": "running", "stale": True, "age_s": 9999},
    )
    monkeypatch.setattr(pipeline, "collect_alerts", lambda: [])

    async def no_alerts():
        return 0

    monkeypatch.setattr(pipeline, "deliver_alerts", no_alerts)

    scheduled = []
    original_create_task = pipeline.asyncio.create_task

    def capture_task(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(pipeline.asyncio, "create_task", capture_task)
    try:
        await pipeline._watchdog()
    finally:
        monkeypatch.setattr(pipeline.asyncio, "create_task", original_create_task)

    assert active.cancelled is True
    assert pipeline._cycle_state == "failed"
    assert scheduled == [], "catch-up must wait until the cancelled task releases its slot"


def test_watchdog_recovery_backoff_is_exponential_and_bounded():
    from tgfarm.pipeline import _watchdog_recovery_delay_min

    assert _watchdog_recovery_delay_min(0, 5, 30) == 5
    assert _watchdog_recovery_delay_min(1, 5, 30) == 5
    assert _watchdog_recovery_delay_min(2, 5, 30) == 10
    assert _watchdog_recovery_delay_min(3, 5, 30) == 20
    assert _watchdog_recovery_delay_min(4, 5, 30) == 30
    assert _watchdog_recovery_delay_min(20, 5, 30) == 30


def test_cycle_health_exposes_recovery_backoff_window(monkeypatch):
    from types import SimpleNamespace

    from tgfarm import pipeline

    now = datetime.now(UTC)
    monkeypatch.setattr(pipeline, "_cycle_state", "failed")
    monkeypatch.setattr(pipeline, "_cycle_started_at", now - timedelta(minutes=3))
    monkeypatch.setattr(pipeline, "_cycle_finished_at", now)
    monkeypatch.setattr(pipeline, "_watchdog_recovery_attempts", 3)
    monkeypatch.setattr(
        pipeline,
        "_watchdog_last_recovery_at",
        now - timedelta(minutes=4),
    )
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            cycle_stale_min=45,
            watchdog_recovery_base_min=5,
            watchdog_recovery_max_min=60,
        ),
    )

    health = pipeline.cycle_health(now)

    assert health["state"] == "failed"
    assert health["stale"] is False
    assert health["recovery_attempts"] == 3
    assert health["recovery_delay_min"] == 20
    assert health["recovery_wait_s"] == 16 * 60
    assert health["recovery_next_at"] == (now + timedelta(minutes=16)).isoformat(timespec="seconds")


@pytest.mark.asyncio
async def test_watchdog_resets_recovery_backoff_after_healthy_cycle(monkeypatch):
    from datetime import UTC, datetime

    from tgfarm import pipeline

    monkeypatch.setattr(pipeline, "_watchdog_recovery_attempts", 4)
    monkeypatch.setattr(pipeline, "_watchdog_last_recovery_at", datetime.now(UTC))
    monkeypatch.setattr(pipeline, "collect_alerts", lambda: [])

    async def no_alerts():
        return 0

    monkeypatch.setattr(pipeline, "deliver_alerts", no_alerts)
    monkeypatch.setattr(
        pipeline,
        "cycle_health",
        lambda: {"state": "ok", "stale": False, "age_s": 1},
    )

    await pipeline._watchdog()

    assert pipeline._watchdog_recovery_attempts == 0
    assert pipeline._watchdog_last_recovery_at is None


def test_scheduler_uses_dedicated_publish_tick():
    from tgfarm import pipeline

    scheduler = pipeline._build_scheduler()
    try:
        assert scheduler.get_job("publish-tick").func is pipeline._guarded_publish_tick
        assert scheduler.get_job("cycle").func is pipeline._guarded_cycle
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


# --------------------------------------------------------------- number grounding
@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("Модель дешевле на 60%.", True),
        ("Комиссия упала с 4 до 0.02.", True),
        ("Прибыль выросла на 999%.", False),
        ("Стоимость 1 200 долларов.", True),
    ],
)
def test_lexical_grounding(claim, expected):
    item = scored(
        "Ethereum перешёл на L2",
        "Комиссия упала с 4 до 0,02 доллара. Модель дешевле на 60 процентов. "
        "Стоимость внедрения 1 200 долларов.",
    )
    assert _lexically_supported(claim, item) is expected


def test_lexical_grounding_rejects_numeric_substrings():
    item = scored(
        "Компания получила 312 млн рублей",
        "Сумма инвестиций составила 312 млн рублей.",
    )
    assert _lexically_supported("Сумма инвестиций составила 312 млн рублей.", item)
    assert not _lexically_supported("Сумма инвестиций составила 12 млн рублей.", item)


def test_qualitative_claims_always_pass():
    """No numbers means no corroboration problem — the tone critic covers those."""
    item = scored("Заголовок", "Текст без чисел вообще.")
    assert _lexically_supported("Это просто мнение.", item)


def test_numeric_audit_covers_body_sentences_and_detects_unsupported_values():
    from tgfarm.core.schemas import DraftPost
    from tgfarm.s4_generation import _fallback_claims, _numeric_audit_claims

    item = scored(
        "Компания привлекла 12 млн рублей",
        "Компания получила 12 млн рублей инвестиций в развитие.",
        url="https://example.invalid/funding",
    )
    draft = DraftPost(
        hook="Компания привлекла инвестиции.",
        body="Компания получила 12 млн рублей. Выручка выросла до 99 млн рублей.",
        sources=[{"title": item.title, "url": item.url}],
    )

    audit = _numeric_audit_claims(draft, [item])
    assert len(audit) == 2
    assert audit[0].verified is True
    assert audit[1].verified is False
    assert all(claim.note == "numeric_audit" for claim in audit)

    fallback = _fallback_claims(draft, [item])
    assert any(claim.note == "numeric_audit" and not claim.verified for claim in fallback)


def test_unverified_numeric_audit_cannot_be_diluted_by_other_verified_claims():
    from tgfarm.core.schemas import Claim, DraftPost
    from tgfarm.s5_quality import gate_facts

    source_url = "https://example.invalid/report"
    draft = DraftPost(
        hook="Компания продолжает работу.",
        body="Отчёт опубликован сегодня.",
        sources=[{"title": "Отчёт", "url": source_url}],
        claims=[
            Claim(claim="Факт один", source_url=source_url, verified=True),
            Claim(claim="Факт два", source_url=source_url, verified=True),
            Claim(
                claim="Выручка выросла до 99 млн.",
                source_url=source_url,
                verified=False,
                note="numeric_audit",
            ),
        ],
    )

    outcome = gate_facts(draft, Persona(id="x", title="X", niche="tech"))
    assert outcome.passed is False
    assert outcome.gate == "facts"
    assert "numeric claim" in (outcome.reason or "")


def test_number_normalisation_is_symmetric():
    assert _normalise_number("40%") == "40"
    assert _normalise_number("0,02") == "0.02"
    assert _normalise_number("1 200") == "1200"


# ------------------------------------------------------------------- stems
def test_stems_match_inflected_russian():
    """Exact set intersection scores ~0 on real Russian; prefix matching does not."""
    tokens = {"ставку", "экспортные", "контракты"}
    assert _stems(tokens, {"ставк", "экспорт", "контракт"}) == 3


def test_stems_ignore_short_tokens():
    assert _stems({"идёт", "в"}, {"ид"}) == 0


# ---------------------------------------------------------------- sameness
def test_sameness_detects_near_duplicate():
    from tgfarm.core.text import content_tokens

    history = {
        "recent_tokens": [set(content_tokens("регулятор обновил требования к отчётности компаний"))],
        "recent_embeds": [],
    }
    score = sameness("Регулятор обновил требования к отчётности компаний сегодня", history)
    assert score > 0.7


def test_sameness_low_for_different_text():
    from tgfarm.core.text import content_tokens

    history = {
        "recent_tokens": [set(content_tokens("регулятор обновил требования к отчётности компаний"))],
        "recent_embeds": [],
    }
    score = sameness("Ethereum перешёл на L2 и снизил комиссию для пользователей", history)
    assert score < 0.5


# ------------------------------------------------------------- normalise
def test_normalise_never_invents_sources():
    persona = Persona(id="x", title="T", niche="tech", max_sources=3)
    items = [
        scored("Первый заголовок", url="https://example.invalid/a"),
        scored("Второй заголовок", url="https://example.invalid/b"),
    ]
    result = normalise(
        {
            "hook": "X.",
            "body": "Y",
            "sources": [{"title": "fake", "url": "https://evil.invalid/z"}],
        },
        persona,
        "news",
        items,
    )
    allowed = {"https://example.invalid/a", "https://example.invalid/b"}
    assert result.sources
    assert all(s["url"] in allowed for s in result.sources)


def test_normalise_appends_mandatory_disclaimer():
    persona = Persona(
        id="x", title="T", niche="health", max_sources=2, disclaimer="Обратитесь к врачу."
    )
    result = normalise(
        {"hook": "X.", "body": "Y"}, persona, "news", [scored("Заголовок источника")]
    )
    assert "Обратитесь к врачу." in result.body


def test_normalise_respects_cta_policy():
    never = Persona(id="x", title="T", niche="tech", cta_policy="never", cta="Подпишись")
    result = normalise(
        {"hook": "X.", "body": "Y", "cta": "Подпишись"},
        never,
        "news",
        [scored("Заголовок источника")],
    )
    assert result.cta is None

    always = Persona(id="x", title="T", niche="tech", cta_policy="always", cta="Подпишись")
    result = normalise(
        {"hook": "X.", "body": "Y"}, always, "news", [scored("Заголовок источника")]
    )
    assert result.cta == "Подпишись"


def test_normalise_accepts_non_russian_language():
    persona = Persona(id="x", title="T", niche="tech", language="uz")
    result = normalise({"hook": "X.", "body": "Y"}, persona, "news", [scored("Заголовок источника")])
    assert result.language == "uz"


def test_to_scored_wraps_raw_items():
    raw = RawItem(
        source_id="s", external_id="e", url="https://x.invalid/1", title="Заголовок", text="b"
    )
    converted = _to_scored([raw])
    assert isinstance(converted[0], ItemScored)
    assert converted[0].title == "Заголовок"


def test_to_scored_passes_through_scored():
    item = scored("Заголовок")
    assert _to_scored([item])[0] is item


# ---------------------------------------------------------------- scheduling
def test_plan_slot_uses_configured_hours(cfg):
    allowed = set(cfg.schedule.hours())
    rng = random.Random(7)
    for _ in range(40):
        assert plan_slot(rng=rng).hour in allowed


def test_plan_slot_favours_the_peak_hours(cfg):
    """Weighted sampling must actually skew toward 17:00-18:00 local.

    `now` is pinned: without it the function samples relative to the real clock,
    so hours earlier than the current time are skipped for today and the same
    seed yields a different distribution depending on when the suite runs.
    """
    rng = random.Random(11)
    now = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    hours = [plan_slot(now=now, rng=rng).hour for _ in range(400)]
    peak = cfg.schedule.peak_hours()
    assert sum(1 for h in hours if h in peak) / len(hours) > 0.35
    dead = sum(1 for h in hours if cfg.schedule.hour_probability(h) == 0.0)
    assert dead == 0


def test_plan_slot_respects_minimum_lead():
    slot = plan_slot(rng=random.Random(1))
    assert slot > datetime.now(UTC) + timedelta(minutes=7)


def test_plan_slot_never_lands_in_dead_hours(cfg):
    rng = random.Random(99)
    for _ in range(80):
        slot = plan_slot(rng=rng)
        assert cfg.schedule.hour_probability(slot.hour) > 0


# ---------------------------------------------------------------- growth guard
def test_forbidden_growth_primitives_are_rejected():
    for primitive in FORBIDDEN_GROWTH_PRIMITIVES:
        with pytest.raises(UnsafeGrowthPrimitive):
            assert_safe_growth(primitive)


def test_safe_primitives_pass():
    assert_safe_growth("crosspost")


def test_forbidden_list_covers_the_usual_spam_suite():
    assert "mass_invite" in FORBIDDEN_GROWTH_PRIMITIVES
    assert "unsolicited_dm" in FORBIDDEN_GROWTH_PRIMITIVES
    assert "buy_subscribers" in FORBIDDEN_GROWTH_PRIMITIVES


# ---------------------------------------------------------------- adapters
def test_crosspost_adapters_self_disable_without_credentials():
    from tgfarm.s9_growth.adapters import ADAPTERS

    for name, adapter in ADAPTERS.items():
        assert adapter.configured() is False, f"{name} should be disabled with no creds"


@pytest.mark.asyncio
async def test_crosspost_adapter_returns_skipped():
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth.adapters import ADAPTERS

    payload = CrosspostPayload(platform="vk", title="t", text="b")
    result = await ADAPTERS["vk"].publish(payload)
    assert result.status == "skipped"


# ---------------------------------------------------------------- routing
@pytest.mark.asyncio
async def test_plan_cluster_respects_daily_quota():
    from tgfarm.db.models import Channel, Cluster
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.models import RawItem as RawItemRow
    from tgfarm.db.session import session_scope
    from tgfarm.db.vector import pack
    from tgfarm.llm.embedding import get_embedder

    vec = pack(get_embedder().embed("OpenAI выпустила GPT-5.5"))
    with session_scope() as session:
        channel = session.get(Channel, "ai_tech_ru")
        if channel is None:
            session.add(Channel(id="ai_tech_ru", title="AI", niche="tech"))
        for i in range(10):
            session.add(
                PostRow(
                    public_id=f"quota-{i}",
                    channel_id="ai_tech_ru",
                    hook="h",
                    body="b",
                    status="scheduled",
                )
            )
        cluster = Cluster(public_id="cl-quota", label="Тест", niche="tech", heat=0.9, status="ready")
        session.add(cluster)
        session.flush()
        session.add(
            RawItemRow(
                source_id="techcrunch_ai",
                external_id="q1",
                url="https://example.invalid/q1",
                title="OpenAI выпустила GPT-5.5",
                text="Модель выполняет 40 процентов задач инженеров.",
                niche_hint=["tech"],
                source_weight=0.7,
                embedding=vec,
                cluster_id=cluster.id,
            )
        )

    plans = await plan_cluster("cl-quota")
    assert all(p.channel_id != "ai_tech_ru" for p in plans), "quota-exhausted channel must not be planned"


def test_schedule_uses_configured_weights(cfg):
    hours = cfg.schedule.hours()
    assert 15 in hours and 17 in hours
    assert all(6 <= h <= 22 for h in hours)
    assert cfg.schedule.hour_probability(15) > cfg.schedule.hour_probability(4)


def test_persona_angle_sampling_respects_weights():
    from tgfarm.config import Persona

    persona = Persona(
        id="sampling_probe",
        title="Probe",
        niche="tech",
        angle_weights={"news": 0.7, "history": 0.05, "analysis": 0.25},
    )
    rng = random.Random(3)
    samples = [persona.sample_angle(rng) for _ in range(400)]
    assert samples.count("news") > samples.count("history")


def test_two_channel_mode_is_dzen_plus_vk(cfg):
    """Сейчас ферма работает ровно на двух каналах «обо всём»."""
    active = cfg.enabled_personas
    assert [p.id for p in active] == ["zen_misc_ru", "vk_misc_ru"]
    assert {p.platform for p in active} == {"dzen", "vk"}
    assert all(p.catch_all for p in active)
    # Нишевые персоны не удалены, а лежат рядом в архиве — вернуть их можно
    # копированием файла, без восстановления из истории.
    from tgfarm.core.settings import config_dir

    assert (config_dir() / "personas.archive.yaml").exists()


# ------------------------------------------------------------------- dzen
@pytest.mark.asyncio
async def test_catch_all_channel_receives_every_cluster():
    from tgfarm.db.models import Cluster
    from tgfarm.db.models import RawItem as RawItemRow
    from tgfarm.db.session import session_scope
    from tgfarm.db.vector import pack
    from tgfarm.llm.embedding import get_embedder

    vec = pack(get_embedder().embed("Марсоход нашёл следы воды"))
    with session_scope() as session:
        cluster = Cluster(
            public_id="cl-zen", label="Марсоход нашёл следы воды", niche="science", heat=0.9,
            status="ready",
        )
        session.add(cluster)
        session.flush()
        session.add(
            RawItemRow(
                source_id="science_daily",
                external_id="zen-1",
                url="https://example.invalid/zen-1",
                title="Марсоход нашёл следы воды",
                text="Находка требует дополнительных измерений.",
                niche_hint=["science"],
                source_weight=0.7,
                embedding=vec,
                cluster_id=cluster.id,
            )
        )

    plans = await plan_cluster("cl-zen")
    assert any(
        p.channel_id == "zen_misc_ru" and p.reason == "catch_all" for p in plans
    ), "the catch-all Dzen persona must receive clusters outside the niche classifier"


def test_dzen_length_limit_is_not_telegram_limit():
    from tgfarm.core.schemas import DraftPost
    from tgfarm.s5_quality import gate_length

    draft = DraftPost(hook="Заголовок", body="я" * 5000, sources=[])
    telegram = Persona(
        id="tg", title="T", niche="tech", min_chars=1, max_chars=20_000
    )
    dzen = Persona(
        id="dz", title="Z", niche="general", platform="dzen", min_chars=1, max_chars=20_000
    )
    assert gate_length(draft, telegram).passed is False
    assert gate_length(draft, dzen).passed is True


def test_dzen_typing_chunks_preserve_every_character():
    from tgfarm.dzen_web import chunk_text

    text = "Первый абзац.\n\nВторой абзац с цифрами 19% и ссылкой."
    chunks = chunk_text(text, random.Random(7))
    assert len(chunks) > 1
    assert "".join(chunks) == text


def test_dzen_publication_url_is_not_the_editor():
    from tgfarm.dzen_web import publication_url

    assert publication_url("https://dzen.ru/a/Zabc123") == "https://dzen.ru/a/Zabc123"
    assert publication_url("https://dzen.ru/media/id/1/story-2") == (
        "https://dzen.ru/media/id/1/story-2"
    )
    assert publication_url("https://dzen.ru/profile/editor/42") is None


def test_dzen_body_does_not_repeat_the_title():
    from types import SimpleNamespace

    from tgfarm.s7_publish import assemble_text

    post = SimpleNamespace(hook="Заголовок", body="Тело", sources=[], cta=None, hashtags=[])
    assert assemble_text(post, "dzen") == "Тело"
    assert assemble_text(post, "telegram").startswith("Заголовок")


def test_due_posts_preserves_dzen_platform():
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import due_posts

    now = datetime.now(UTC) - timedelta(minutes=1)
    with session_scope() as session:
        session.merge(Channel(id="tg_due", title="TG", niche="tech", platform="telegram"))
        session.merge(Channel(id="dz_due", title="DZ", niche="general", platform="dzen"))
        session.add(
            Post(
                public_id="due-tg",
                channel_id="tg_due",
                hook="TG",
                body="Telegram body",
                status="scheduled",
                scheduled_at=now,
            )
        )
        session.add(
            Post(
                public_id="due-dz",
                channel_id="dz_due",
                hook="DZ",
                body="Dzen body",
                status="scheduled",
                scheduled_at=now,
            )
        )

    pending = due_posts(limit=20)
    tg = next(row for row in pending if row[3] == "telegram" and "Telegram body" in row[1])
    dz = next(row for row in pending if row[3] == "dzen" and "Dzen body" in row[1])
    assert tg[2] != "dz_due"
    assert dz[2] == "dz_due"

    selected = due_posts(limit=20, post_public_ids=["due-dz"])
    assert len(selected) == 1
    assert selected[0][0] == dz[0]
    assert selected[0][3] == "dzen"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("read timed out"), True),
        (RuntimeError("connection reset by peer"), True),
        (RuntimeError("DNS getaddrinfo failed"), False),
        (RuntimeError("connection refused"), False),
        (ValueError("bad text before request"), True),
    ],
)
def test_publish_error_ambiguity_is_conservative(error, expected):
    from tgfarm.s7_publish import _is_ambiguous_publish_error

    assert _is_ambiguous_publish_error(error) is expected


def test_publish_server_error_is_ambiguous():
    from types import SimpleNamespace

    from tgfarm.s7_publish import _is_ambiguous_publish_error

    class ServerError(Exception):
        response = SimpleNamespace(status_code=503)

    class ClientError(Exception):
        response = SimpleNamespace(status_code=400)

    assert _is_ambiguous_publish_error(ServerError("503 Service Unavailable"))
    assert not _is_ambiguous_publish_error(ClientError("400 Bad Request"))


def test_publish_now_rejects_ineligible_statuses():
    from tgfarm.api import _blocked_publish_now_ids, _reject
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import unpublish

    channel_id = "force_publish_guard"
    ids = [
        ("force_pub_scheduled", "scheduled"),
        ("force_pub_gated", "gated"),
        ("force_pub_published", "published"),
        ("force_pub_draft", "draft"),
        ("force_pub_unknown", "publish_unknown"),
    ]
    with session_scope() as session:
        session.merge(Channel(id=channel_id, title="Guard", niche="tech", platform="telegram"))
        session.add_all(
            [
                Post(
                    public_id=public_id,
                    channel_id=channel_id,
                    hook=public_id,
                    body="Body",
                    status=status,
                    scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
                )
                for public_id, status in ids
            ]
        )

    blocked = _blocked_publish_now_ids([public_id for public_id, _ in ids] + ["force_pub_missing"])
    assert blocked == [
        {"id": "force_pub_published", "status": "published"},
        {"id": "force_pub_draft", "status": "draft"},
        {"id": "force_pub_unknown", "status": "publish_unknown"},
        {"id": "force_pub_missing", "status": "not_found"},
    ]

    # The low-level rescheduler is defensive too, even if called outside the API.
    assert unpublish([public_id for public_id, _ in ids]) == 2
    with session_scope() as session:
        assert session.query(Post).filter_by(public_id="force_pub_published").one().status == "published"
        assert session.query(Post).filter_by(public_id="force_pub_draft").one().status == "draft"
        assert session.query(Post).filter_by(public_id="force_pub_unknown").one().status == "publish_unknown"

    assert _reject(["force_pub_published", "force_pub_scheduled"], "test guard") == 1
    with session_scope() as session:
        assert session.query(Post).filter_by(public_id="force_pub_published").one().status == "published"
        assert session.query(Post).filter_by(public_id="force_pub_scheduled").one().status == "rejected"


def test_cli_publish_is_scoped_and_rejects_published_posts(monkeypatch):
    from types import SimpleNamespace

    from tgfarm import cli, s7_publish
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope

    channel_id = "cli_publish_guard"
    with session_scope() as session:
        session.merge(Channel(id=channel_id, title="CLI guard", niche="tech", platform="telegram"))
        session.add_all(
            [
                Post(
                    public_id="cli_publish_ready",
                    channel_id=channel_id,
                    hook="Ready",
                    body="Body",
                    status="scheduled",
                    scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
                ),
                Post(
                    public_id="cli_publish_already_published",
                    channel_id=channel_id,
                    hook="Already published",
                    body="Body",
                    status="published",
                    published_at=datetime.now(UTC),
                ),
            ]
        )

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    calls = []

    async def fake_publish_due(limit=1000, post_public_ids=None):
        calls.append((limit, post_public_ids))
        return {"published": 1, "failed": 0}

    monkeypatch.setattr(s7_publish, "publish_due", fake_publish_due)

    assert cli.cmd_publish(SimpleNamespace(post_id="cli_publish_already_published")) == 2
    assert calls == []
    assert cli.cmd_publish(SimpleNamespace(post_id="cli_publish_ready")) == 0
    assert calls == [(1, ["cli_publish_ready"])]


def test_reconcile_publish_outcomes_require_explicit_unknown_state():
    from tgfarm.api import _reconcile_publish_outcome
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        session.merge(Channel(id="reconcile_test", title="Reconcile", niche="tech", platform="telegram"))
        session.add_all(
            [
                Post(
                    public_id="reconcile_confirm",
                    channel_id="reconcile_test",
                    hook="Confirm",
                    body="Body",
                    status="publish_unknown",
                    publish_error="read timeout",
                ),
                Post(
                    public_id="reconcile_retry",
                    channel_id="reconcile_test",
                    hook="Retry",
                    body="Body",
                    status="publish_unknown",
                    publish_error="read timeout",
                ),
            ]
        )

    confirmed = _reconcile_publish_outcome("reconcile_confirm", "confirm_published")
    retried = _reconcile_publish_outcome("reconcile_retry", "retry_after_check")
    already_resolved = _reconcile_publish_outcome("reconcile_retry", "confirm_published")

    assert confirmed == {
        "ok": True,
        "status": "published",
        "detail": "operator confirmed post exists on destination",
    }
    assert retried["ok"] is True and retried["status"] == "scheduled"
    assert already_resolved["ok"] is False and already_resolved["error"] == "not_unknown"

    with session_scope() as session:
        assert session.query(Post).filter_by(public_id="reconcile_confirm").one().status == "published"
        assert session.query(Post).filter_by(public_id="reconcile_retry").one().status == "scheduled"


@pytest.mark.asyncio
async def test_dzen_dry_run_marks_published_and_logs_remote():
    from sqlalchemy import select

    from tgfarm.db.models import CrosspostLog, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _publish_one

    with session_scope() as session:
        post = Post(
            public_id="dzen-publish",
            channel_id="zen_misc_ru",
            hook="Заголовок",
            body="Тело статьи для Дзена.",
            status="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        session.add(post)
        session.flush()
        post_id = post.id

    result = await _publish_one(post_id, "zen_misc_ru", "Тело статьи для Дзена.", None, "dzen")
    assert result["ok"] is True

    with session_scope() as session:
        post = session.get(Post, post_id)
        assert post is not None and post.status == "published"
        log_row = session.execute(
            select(CrosspostLog).where(CrosspostLog.post_public_id == "dzen-publish")
        ).scalar_one()
        assert log_row.platform == "dzen"
        assert log_row.status == "dry_run"
        assert log_row.remote_id


@pytest.mark.asyncio
async def test_dzen_publish_passes_inline_photo_and_caption(monkeypatch):
    from types import SimpleNamespace

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _publish_dzen

    with session_scope() as session:
        post = Post(
            public_id="dzen-inline-live",
            channel_id="zen_misc_ru",
            hook="Заголовок",
            body="Тело.",
            status="scheduled",
            scheduled_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        post_id = post.id

    captured = {}

    async def fake_publish(
        self,
        title,
        body,
        cover_path=None,
        inline_image=None,
        inline_caption=None,
        inline_images=None,
    ):
        captured.update(
            title=title,
            body=body,
            cover_path=cover_path,
            inline_image=inline_image,
            inline_caption=inline_caption,
            inline_images=inline_images,
        )
        return SimpleNamespace(remote_id="r1", url="https://dzen.ru/a/test")

    monkeypatch.setattr("tgfarm.dzen_web.DzenWebPublisher.publish", fake_publish)
    monkeypatch.setattr(
        "tgfarm.s7_publish.get_settings", lambda: SimpleNamespace(live=True)
    )

    result = await _publish_dzen(
        post_id,
        "Первый.\n\nВторой.",
        "cover.jpg",
        "lead.jpg",
        "Фото: example.invalid",
    )

    assert result["ok"] is True
    assert captured == {
        "title": "Заголовок",
        "body": "Первый.\n\nВторой.",
        "cover_path": "cover.jpg",
        "inline_image": "lead.jpg",
        "inline_caption": "Фото: example.invalid",
        "inline_images": [("lead.jpg", "Фото: example.invalid")],
    }


def test_inline_media_is_chosen_separately_from_the_primary_cover(tmp_path):
    from tgfarm.db.models import Media, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _inline_media_for, _media_for

    cover = tmp_path / "cover.jpg"
    lead = tmp_path / "lead.jpg"
    cover.write_bytes(b"cover")
    lead.write_bytes(b"lead")

    with session_scope() as session:
        post = Post(
            public_id="dzen-inline",
            channel_id="zen_misc_ru",
            hook="Заголовок",
            body="Тело.",
            status="scheduled",
        )
        session.add(post)
        session.flush()
        post_id = post.id
        session.add_all(
            [
                Media(
                    post_id=post_id,
                    kind="template",
                    path=str(cover),
                    is_primary=True,
                ),
                Media(
                    post_id=post_id,
                    kind="article_image",
                    path=str(lead),
                    caption="Фото: example.invalid",
                    is_primary=False,
                ),
            ]
        )

    assert _media_for([post_id]) == {post_id: str(cover)}
    assert _inline_media_for([post_id]) == {
        post_id: [(str(lead), "Фото: example.invalid")]
    }


def test_missing_inline_media_does_not_suppress_the_cover(tmp_path):
    from tgfarm.db.models import Media, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _inline_media_for

    with session_scope() as session:
        post = Post(
            public_id="dzen-inline-missing",
            channel_id="zen_misc_ru",
            hook="Заголовок",
            body="Тело.",
            status="scheduled",
        )
        session.add(post)
        session.flush()
        post_id = post.id
        session.add(
            Media(
                post_id=post_id,
                kind="article_image",
                path=str(tmp_path / "gone.jpg"),
                is_primary=False,
            )
        )

    assert _inline_media_for([post_id]) == {}


def test_s9_only_crossposts_telegram_posts():
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s9_growth import _pending_crossposts

    now = datetime.now(UTC)
    with session_scope() as session:
        session.merge(Channel(id="tg_cross", title="TG", niche="tech", platform="telegram"))
        session.merge(Channel(id="dz_cross", title="DZ", niche="general", platform="dzen"))
        session.add(
            Post(
                public_id="cross-tg",
                channel_id="tg_cross",
                hook="TG",
                body="body",
                status="published",
                published_at=now,
            )
        )
        session.add(
            Post(
                public_id="cross-dz",
                channel_id="dz_cross",
                hook="DZ",
                body="body",
                status="published",
                published_at=now,
            )
        )

    found = {public_id for _, public_id in _pending_crossposts(1.0)}
    assert "cross-tg" in found
    assert "cross-dz" not in found




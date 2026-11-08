"""Tests for the IDEAS.md batch: gates, tone switcher, breaker, trace, audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4


def _draft(hook: str, body: str = "Тело поста с достаточной длиной для гейтов."):
    from tgfarm.core.schemas import DraftPost

    return DraftPost(
        hook=hook,
        body=body,
        sources=[{"title": "t", "url": "https://example.invalid/x"}],
    )


def _persona(platform: str = "vk", **kwargs):
    from tgfarm.config import Persona

    return Persona(id="ideas_probe", title="Probe", niche="general", platform=platform, **kwargs)


# ------------------------------------------------------------------ gates
def test_gate_cliches_catches_stock_phrases():
    from tgfarm.s5_quality import gate_cliches

    bad = _draft("Хук нормальный.", "Не секрет, что технология важна. Ещё текст.")
    assert gate_cliches(bad, _persona()).passed is False

    good = _draft("Дрон складывается за 0,3 секунды", "Инженеры из Цюриха показали механизм.")
    assert gate_cliches(good, _persona()).passed is True


def test_dzen_headline_limit_is_140():
    from tgfarm.s5_quality import gate_structure

    long_hook = ("Это очень длинный заголовок, который точно превышает лимит Дзена "
                 "в сто сорок символов и поэтому должен быть отклонён гейтом структуры "
                 "немедленно и без сожалений, потому что Дзен обрежет его сам.")
    assert len(long_hook) > 140
    outcome = gate_structure(_draft(long_hook), _persona("dzen"))
    assert outcome.passed is False and outcome.gate == "structure"

    short = "Дрон складывается за 0,3 секунды."
    short_body = "Первый абзац статьи.\n\nВторой абзац статьи."
    assert gate_structure(_draft(short, short_body), _persona("dzen")).passed is True


def test_somber_topic_switcher():
    from tgfarm.core.schemas import ItemScored
    from tgfarm.s4_generation import _is_somber_topic

    def item(title):
        return ItemScored(source_id="s", external_id="e", url="https://e.invalid/t",
                          title=title, text="текст", lang="ru")

    assert _is_somber_topic([item("При катастрофе погибли три человека")]) is True
    assert _is_somber_topic([item("Дрон научился складываться за 0,3 секунды")]) is False


def test_gate_failure_becomes_a_lesson(db):
    from tgfarm.core.schemas import QualityReport
    from tgfarm.db.models import Post
    from tgfarm.s12_learning import lessons, record_gate_failure

    record_gate_failure("zen_misc_ru", "cliches", "штампы: не секрет, что")
    rows = [r for r in lessons("zen_misc_ru", kind="mistake") if r["key"] == "gate:cliches"]
    assert rows, "отказ гейта должен попасть в уроки"

    # and the verdict persists its issues for the panel
    post = Post(public_id="issues-1", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="draft")
    db.add(post)
    db.commit()

    from tgfarm.s5_quality import save_outcome

    save_outcome("issues-1", QualityReport(
        passed=False, gate="tone", reason="critic 6.0/10",
        issues=["юмор в заголовке", "шаблонный зачин"],
    ))
    db.refresh(post)
    assert post.quality["issues"] == ["юмор в заголовке", "шаблонный зачин"]


# ---------------------------------------------------------- model cooldown
class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _ScriptedLLM:
    def __init__(self, plan):
        self.plan = list(plan)
        self.called_models = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        model = json["model"]
        self.called_models.append(model)
        status, body = self.plan.pop(0)
        return _Resp(status, body)


def test_model_cooldown_skips_hot_candidate(monkeypatch):
    import httpx

    from tgfarm.llm.client import _OpenAI
    
    script = _ScriptedLLM([
        (429, {"error": "slow down"}),
        (200, {"choices": [{"message": {"content": "ok"}}], "usage": {}}),
        (200, {"choices": [{"message": {"content": "ok2"}}], "usage": {}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: script)
    provider = _OpenAI("key", "hot:free,cold:free", 10.0, base_url="https://x.invalid/v1")

    import asyncio

    # первый вызов: hot ловит 429, ротация спасает через cold
    text = asyncio.run(provider.complete("s", "p", json_mode=False, max_tokens=8))
    assert text == "ok"
    assert provider._model_cooldown.get("hot:free", 0) > 0

    # второй вызов: горячая модель пропускается без запроса
    text = asyncio.run(provider.complete("s", "p", json_mode=False, max_tokens=8))
    assert text == "ok2"
    assert script.called_models == ["hot:free", "cold:free", "cold:free"]


# ---------------------------------------------------------------- breaker
def test_dzen_waits_when_llm_is_down(db, monkeypatch):
    """The Dzen gate, measured on rows this test owns.

    `conftest` gives the session one database, and this gate reads two global
    facts out of it: which platform the *five most recent drafts* came from
    (`tgfarm/core/llm_health.py`) and when the *last Dzen publication* happened.
    A neighbour that generated online therefore silently unlocks the gate, and a
    neighbour that published to Dzen holds our post for the wrong reason — both
    were observed as this test failing only in some run orders.

    The fix is to seed the facts, not to patch the verdict: the six rows below are
    the newest drafts in the table and every one of them carries the offline
    composer's stamp, so `llm_recently_working()` is False because of our rows; the
    batch is addressed by our own public id, so no other queued post can be
    counted; and the queue-gap clock is pinned to "nothing published to Dzen yet"
    — a gate that is not the one under test — with its own assertion proving it
    stayed at zero.
    """
    from sqlalchemy import delete, func, select

    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import (
        GATE_DZEN_NEEDS_ONLINE_MODEL,
        GATE_DZEN_QUEUE_GAP,
        publish_due,
    )

    stamp = uuid4().hex[:10]
    channel_id = f"brk-ch-{stamp}"
    subject_id = f"brk-due-{stamp}"
    filler_ids = [f"brk-draft-{index}-{stamp}" for index in range(5)]

    with session_scope() as session:
        newest = session.execute(select(func.max(Post.created_at))).scalar_one_or_none()
        base = max(
            ensure_tz(newest) if newest is not None else datetime.now(UTC),
            datetime.now(UTC),
        ) + timedelta(hours=1)
        session.merge(
            Channel(id=channel_id, title="Дзен (W26-OR)", niche="general", platform="dzen")
        )
        session.add(
            Post(
                public_id=subject_id,
                channel_id=channel_id,
                hook="Хук.",
                body="Тело.",
                status="scheduled",
                generation_provider="offline",
                scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
                created_at=base,
            )
        )
        # Five offline drafts newer than everything else in the table: they are
        # what the health signal reads, so the signal is a fact of this test.
        for index, public_id in enumerate(filler_ids):
            session.add(
                Post(
                    public_id=public_id,
                    channel_id=channel_id,
                    hook="Черновик.",
                    body="Тело офлайн-композера.",
                    status="draft",
                    generation_provider="offline",
                    created_at=base + timedelta(minutes=index + 1),
                )
            )

    # The Dzen pacing gate is not the subject here; pin it to "no Dzen post has
    # been sent recently" so the number below can only come from the model gate.
    monkeypatch.setattr("tgfarm.s7_publish._last_dzen_publish_time", lambda: None)
    monkeypatch.setattr(publish_due, "_last_dzen", None, raising=False)

    result = asyncio_run(publish_due(limit=10, post_public_ids=[subject_id]))

    assert result["due"] == 1, "the batch is exactly the post this test seeded"
    assert result["gates"][GATE_DZEN_NEEDS_ONLINE_MODEL] == 1
    assert result["gates"][GATE_DZEN_QUEUE_GAP] == 0, "the pacing gate must not be the reason"
    assert result["skipped_llm"] == 1, "офлайн-текст не должен идти в Дзен"
    assert result.get("published", 0) == 0
    assert "dry_run" not in result, "the post was held back, not sent as a dry run"

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == subject_id)
        ).scalar_one()
        assert row.status == "scheduled", "пост должен остаться в очереди"

    with session_scope() as session:
        own = filler_ids + [subject_id]
        session.execute(delete(Post).where(Post.public_id.in_(own)))
        session.execute(delete(Channel).where(Channel.id == channel_id))
    with session_scope() as session:
        left = session.execute(
            select(func.count()).select_from(Post).where(Post.public_id.in_(own))
        ).scalar_one()
        assert left == 0, "offline filler drafts must not outlive the test"


def ensure_tz(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# -------------------------------------------------------------- vk prime
def test_vk_prime_slot_snaps(monkeypatch):
    import random

    from tgfarm import s7_publish
    from tgfarm.core.settings import get_settings as real_get_settings

    monkeypatch.setattr(
        s7_publish, "get_settings",
        lambda: SimpleNamespace(vk_prime_hours="12,19", vk_prime_share=1.0,
                                **{k: getattr(real_get_settings(), k)
                                   for k in ()}),
    )
    from datetime import UTC, datetime

    slot = s7_publish._maybe_prime_slot(datetime.now(UTC).replace(hour=8, minute=5), random.Random(1))
    assert slot.hour in (12, 19)


# ------------------------------------------------- platform health + api
def test_platform_health_groups_failures(db):
    from tgfarm.db.models import CrosspostLog
    from tgfarm.s8_analytics import platform_health

    db.add(CrosspostLog(post_public_id="ph-1", platform="vk", status="failed",
                        error='{"error_code": 214}', created_at=datetime.now(UTC)))
    db.commit()
    health = platform_health(7)
    assert health.get("vk", {}).get("failed", 0) >= 1
    assert "214" in str(health.get("vk", {}).get("last_error"))


def _client():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    token = ensure_token()
    return TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1"), {
        "X-TGFarm-Token": token,
    }


def test_trace_endpoint_returns_events(db):
    from tgfarm.db.models import Post

    db.add(Post(public_id="trace-1", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="gated", created_at=datetime.now(UTC),
                quality={"gate": None, "reason": "mean=0.9", "issues": ["норм"]}))
    db.commit()

    client, headers = _client()
    resp = client.get("/api/posts/trace-1/trace", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    kinds = [e["kind"] for e in body["events"]]
    assert "draft" in kinds and "quality" in kinds


def test_polish_endpoint_offline_is_honest(db):
    from tgfarm.db.models import Post

    db.add(Post(public_id="pol-1", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="gated"))
    db.commit()

    client, headers = _client()
    resp = client.post("/api/posts/pol-1/polish", headers=headers, json={"mode": "shorter"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False, "без LLM полировка честно отказывается"


def test_reject_writes_audit_log(db):
    from tgfarm.db.models import Post

    db.add(Post(public_id="aud-1", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="gated"))
    db.commit()

    client, headers = _client()
    resp = client.post("/api/posts/reject", headers=headers,
                       json={"post_public_ids": ["aud-1"], "reason": "ручной тест"})
    assert resp.status_code == 200 and resp.json()["rejected"] == 1

    entries = client.get("/api/audit-log?limit=10", headers=headers).json()["entries"]
    assert any(e["action"] == "reject" and "aud-1" in e["target"] for e in entries)


def test_metrics_endpoint_exposes_llm_counters():
    client, headers = _client()
    body = client.get("/api/metrics", headers=headers).json()
    assert "posts_by_status" in body and "llm" in body

"""Horizon-2 tests: net guard, host middleware, styles, holdout, alerts, VK."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


# ------------------------------------------------------------------ net guard
def test_net_guard_blocks_private_hosts_and_bad_schemes():
    from tgfarm.core.net import UnsafeURLError, assert_public_http_url

    for url in (
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.5/admin",
        "http://10.0.0.3/",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ):
        with pytest.raises(UnsafeURLError):
            assert_public_http_url(url)
    assert assert_public_http_url("https://8.8.8.8/a") == "https://8.8.8.8/a"


@pytest.mark.asyncio
async def test_article_image_download_refuses_private_url():
    from tgfarm.s6_visual.article_images import _download

    assert await _download("http://127.0.0.1:8000/secret") is None


# ------------------------------------------------------------- host middleware
def test_untrusted_host_header_is_rejected():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        bad = client.get("/health", headers={"Host": "evil.example.com"})
        assert bad.status_code == 403


# ------------------------------------------------------------- hook styles
def test_hook_style_classification():
    from tgfarm.s4_generation import classify_hook_style

    assert classify_hook_style("Дрон складывается за 0,3 секунды") == "stat"
    assert classify_hook_style("Почему это важно?") == "question"
    assert classify_hook_style("Учёные представили квадрокоптер") == "fact"


def test_hook_style_performance_and_learning(db):
    from tgfarm.db.models import Post, PostStat
    from tgfarm.s8_analytics import hook_style_performance
    from tgfarm.s12_learning import _upsert, best_hook_style_weights

    post = Post(
        public_id="hs-1",
        channel_id="zen_misc_ru",
        hook="Дрон за 0,3 секунды",
        body="Тело",
        status="published",
        published_at=datetime.now(UTC),
        hook_style="stat",
    )
    db.add(post)
    db.flush()
    db.add(PostStat(post_id=post.id, views=120, reactions=3, comments=1, forwards=2,
                    impressions=200, opens=80, collected_at=datetime.now(UTC)))
    db.commit()

    perf = hook_style_performance(14)
    assert perf.get("stat", {}).get("n", 0) >= 1

    _upsert("zen_misc_ru", "strategy", "hook_style_weights",
            {"fact": 0.5, "stat": 0.3, "question": 0.2}, 0.8, 5, 0.5, "test")
    weights = best_hook_style_weights("zen_misc_ru")
    assert weights and abs(sum(weights.values()) - 1.0) < 0.01


# ------------------------------------------------------------- holdout
def test_holdout_report_and_style_diversity(db):
    from tgfarm.db.models import Post
    from tgfarm.s8_analytics import holdout_report, style_diversity

    long_hook = "Инженеры научили дрона складывать крылья за полсекунды после посадки"
    db.add(Post(public_id="ho-1", channel_id="zen_misc_ru", hook=long_hook,
                body="Тело", status="published", published_at=datetime.now(UTC),
                variant="holdout", quality={"scores": {"tone": 0.8}}))
    db.add(Post(public_id="dv-1", channel_id="zen_misc_ru", hook=long_hook,
                body="Тело", status="published", published_at=datetime.now(UTC)))
    db.add(Post(public_id="dv-2", channel_id="zen_misc_ru",
                hook="Клавиатура из полимера растворяется в воде за сорок минут целиком",
                body="Тело", status="published", published_at=datetime.now(UTC)))
    db.commit()

    report = holdout_report(30)
    assert report.get("holdout", {}).get("n", 0) >= 1

    guard = style_diversity(7)
    assert guard["posts"] >= 3
    assert guard["duplicate_gram_share"] > 0, "одинаковые хуки должны дать повтор 6-грамм"


def test_persist_draft_keeps_style_and_variant(db):
    from tgfarm.config import Persona
    from tgfarm.core.schemas import DraftPost
    from tgfarm.db.models import Channel, Post
    from tgfarm.s3_router import ChannelPlan
    from tgfarm.s4_generation import persist_draft

    db.merge(Channel(id="persist_chan", title="Persist", niche="general", platform="vk"))
    db.commit()

    plan = ChannelPlan(
        cluster_public_id="cl-persist",
        channel_id="persist_chan",
        persona=Persona(id="persist_chan", title="Persist", niche="general", platform="vk"),
        angle="news",
        score=0.5,
    )
    draft = DraftPost(
        hook="Дрон за 0,3 секунды",
        body="Тело поста для проверки сохранения.",
        sources=[{"title": "t", "url": "https://example.invalid/x"}],
    )
    draft.hook_style = "stat"
    draft.variant = "holdout"
    public_id = persist_draft(draft, plan)

    from sqlalchemy import select

    from tgfarm.db.session import session_scope

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one()
        assert row.hook_style == "stat"
        assert row.variant == "holdout"


# ------------------------------------------------------------ alert delivery
class _FakeTelegram:
    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self._calls.append((url, json))

        class _Resp:
            status_code = 200

        return _Resp()


def test_deliver_alerts_sends_once_per_cooldown(db, monkeypatch):
    import httpx

    from tgfarm import pipeline
    from tgfarm.db.models import Post

    monkeypatch.setattr(
        "tgfarm.pipeline.get_settings",
        lambda: SimpleNamespace(
            live=True,
            alert_no_publish_hours=3,
            cycle_timeout_min=60,
            cycle_stale_min=45,
            telegram_bot_token="tok",
            alert_chat_id="42",
            alert_cooldown_min=30,
        ),
    )
    now = datetime.now(UTC)
    db.query(Post).filter(Post.status == "published").update(
        {"published_at": now - timedelta(days=30)}, synchronize_session=False
    )
    db.add(Post(public_id="dl-old", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="published", published_at=now - timedelta(hours=6)))
    db.add(Post(public_id="dl-gated", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="gated"))
    db.commit()

    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeTelegram(calls))
    monkeypatch.setattr(pipeline, "_alert_last_sent", {})

    sent = __import__("asyncio").run(pipeline.deliver_alerts())
    assert sent >= 1
    assert any("api.telegram.org" in url for url, _ in calls)

    sent_again = __import__("asyncio").run(pipeline.deliver_alerts())
    assert sent_again == 0, "cooldown не должен слать повторно"


# --------------------------------------------------------------- index sync
def test_missing_index_is_recreated():
    from sqlalchemy import inspect

    from tgfarm.db.bootstrap import apply_additive_migrations
    from tgfarm.db.session import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_posts_status")
    assert "ix_posts_status" not in {i["name"] for i in inspect(engine).get_indexes("posts")}

    apply_additive_migrations(engine)
    assert "ix_posts_status" in {i["name"] for i in inspect(engine).get_indexes("posts")}


# ------------------------------------------------------------- vk images
class _VKResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _ScriptedVK:
    def __init__(self, responses):
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        return _VKResp(self.responses.pop(0))

    async def post(self, url, data=None, files=None):
        self.last_url = url
        self.last_data = data
        self.last_files = files
        return _VKResp(self.responses.pop(0))


def test_vk_upload_photo_chain(monkeypatch):
    import httpx

    import tgfarm.s9_growth.adapters as adapters
    from tgfarm.s9_growth.adapters import VkAdapter

    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(vk_access_token="tok", vk_group_id="-234474988"),
    )
    script = _ScriptedVK([
        {"response": {"upload_url": "http://upload"}},
        {"photo": "p", "server": "1", "hash": "h"},
        {"response": [{"owner_id": -234474988, "id": 42}]},
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: script)

    import asyncio
    import tempfile

    from PIL import Image

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        Image.new("RGB", (10, 10), color=(1, 2, 3)).save(handle, format="PNG")
        path = handle.name

    attachment = asyncio.run(VkAdapter()._upload_wall_photo(path))
    assert attachment == "photo-234474988_42"


def test_vk_publish_attaches_photo(monkeypatch):
    import httpx

    import tgfarm.s9_growth.adapters as adapters
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth.adapters import VkAdapter

    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(vk_access_token="tok", vk_group_id="-234474988"),
    )
    script = _ScriptedVK([
        {"response": {"upload_url": "http://upload"}},
        {"photo": "p", "server": "1", "hash": "h"},
        {"response": [{"owner_id": -234474988, "id": 42}]},
        {"response": {"post_id": 77}},
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: script)

    import asyncio
    import tempfile

    from PIL import Image

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        Image.new("RGB", (10, 10), color=(1, 2, 3)).save(handle, format="PNG")
        path = handle.name

    adapter = VkAdapter()
    payload = CrosspostPayload(platform="vk", title="t", text="b", meta={"image_path": path})
    result = asyncio.run(adapter.publish(payload))
    assert result.status == "posted"
    assert script.last_data.get("attachments") == "photo-234474988_42"

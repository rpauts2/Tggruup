from __future__ import annotations

import asyncio
import uuid

import pytest
from datetime import UTC, datetime

from sqlalchemy import select


def test_unconfigured_crosspost_does_not_accumulate_skipped_rows(monkeypatch):
    from tgfarm.db.models import Channel, CrosspostLog, Post
    from tgfarm.db.session import session_scope
    from tgfarm.platforms import is_platform_enabled
    from tgfarm.s9_growth import crosspost_post
    from tgfarm.s9_growth.adapters import ADAPTERS

    suffix = uuid.uuid4().hex[:8]
    channel_id = f"skip_{suffix}"
    public_id = f"skippost{suffix}"
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="skip test", niche="tech",
            platform="telegram", enabled=True,
        ))
        session.flush()
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Skip logging test",
            body="A body",
            status="published",
            published_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        post_id = post.id

    adapter = ADAPTERS["bluesky"]
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(adapter, "configured", lambda: False)

    first = asyncio.run(crosspost_post(post_id, ["bluesky"]))
    second = asyncio.run(crosspost_post(post_id, ["bluesky"]))

    assert first[0].status == "skipped"
    # The repeated call is a no-op: don't emit duplicate skipped results or log rows.
    assert second == []
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog).where(
                CrosspostLog.post_public_id == public_id,
                CrosspostLog.platform == "bluesky",
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "skipped"


def test_live_youtube_crosspost_renders_missing_short_on_demand(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from tgfarm import platforms, s9_growth
    from tgfarm.core.schemas import CrosspostResult
    from tgfarm.db.models import Channel, CrosspostLog, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s11_video import renderer

    suffix = uuid.uuid4().hex[:8]
    channel_id = f"youtube_auto_{suffix}"
    public_id = f"youtube_post_{suffix}"
    rendered_video = tmp_path / "generated-short.mp4"
    rendered_video.write_bytes(bytes.fromhex("000000186674797069736f6d") + b"mock-video")

    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="YouTube auto-render test", niche="tech",
            platform="telegram", enabled=True,
        ))
        session.flush()
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Automatic short title",
            body="A factual body from the saved post.",
            status="published",
            published_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        post_id = post.id

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda platform: platform == "youtube")
    monkeypatch.setattr(s9_growth, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(s9_growth, "primary_video_path", lambda _post_id: None)

    rendered = []

    def fake_render(post_id_value, **kwargs):
        rendered.append((post_id_value, kwargs))
        return SimpleNamespace(path=str(rendered_video))

    monkeypatch.setattr(renderer, "render_post_short", fake_render)
    adapter = s9_growth.ADAPTERS["youtube"]
    captured = []

    def configured():
        return True

    async def fake_publish(payload):
        captured.append(payload)
        return CrosspostResult(
            platform="youtube",
            status="posted",
            remote_id="yt-test-123",
            url="https://www.youtube.com/watch?v=yt-test-123",
        )

    monkeypatch.setattr(adapter, "configured", configured)
    monkeypatch.setattr(adapter, "publish", fake_publish)

    results = asyncio.run(s9_growth.crosspost_post(post_id, ["youtube"]))

    assert [result.status for result in results] == ["posted"]
    assert rendered == [(public_id, {})]
    assert len(captured) == 1
    assert captured[0].video_path == str(rendered_video)
    assert captured[0].meta["video_path"] == str(rendered_video)

    with session_scope() as session:
        row = session.query(CrosspostLog).filter_by(
            post_public_id=public_id,
            platform="youtube",
        ).order_by(CrosspostLog.id.desc()).first()
    assert row is not None and row.status == "posted"


def test_live_youtube_does_not_render_until_oauth_adapter_is_configured(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from tgfarm import platforms, s9_growth
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s11_video import renderer

    suffix = uuid.uuid4().hex[:8]
    channel_id = f"youtube_unconfigured_{suffix}"
    public_id = f"youtube_waiting_{suffix}"
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="YouTube unconfigured test", niche="tech",
            platform="telegram", enabled=True,
        ))
        session.flush()
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Wait for OAuth",
            body="A factual body from the saved post.",
            status="published",
            published_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        post_id = post.id

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda platform: platform == "youtube")
    monkeypatch.setattr(s9_growth, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(s9_growth, "primary_video_path", lambda _post_id: None)
    monkeypatch.setattr(
        s9_growth.ADAPTERS["youtube"],
        "configured",
        lambda: False,
    )
    monkeypatch.setattr(
        renderer,
        "render_post_short",
        lambda *_args, **_kwargs: pytest.fail("must not render until YouTube OAuth is configured"),
    )

    results = asyncio.run(s9_growth.crosspost_post(post_id, ["youtube"]))

    assert [result.status for result in results] == ["skipped"]



def test_reddit_is_delivered_through_the_automatic_crosspost_pipeline(monkeypatch):
    from types import SimpleNamespace

    from tgfarm import platforms, s9_growth
    from tgfarm.core.schemas import CrosspostResult
    from tgfarm.s9_growth import reddit_scout

    post = SimpleNamespace(
        id=321,
        public_id="reddit_auto_delivery_321",
        hook="A practical Python debugging workflow",
        body="A source-grounded explanation of a debugging workflow for Python developers.",
        cta="",
        hashtags=["python", "debugging"],
        sources=[{"title": "Reference", "url": "https://example.org/python-debugging"}],
    )
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "reddit")
    monkeypatch.setattr(s9_growth, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(s9_growth, "_load_post", lambda _post_id: post)
    monkeypatch.setattr(s9_growth, "already_done", lambda _public_id, _platform: False)
    monkeypatch.setattr(s9_growth, "begin_crosspost", lambda _public_id, _platform: 1)

    async def build_submission(_public_id):
        return {
            "ok": True,
            "subreddit": "learnpython",
            "title": "A practical Python debugging workflow",
            "body": "A native, source-grounded explanation for the Learn Python community.",
            "reddit_kind": "self",
        }

    monkeypatch.setattr(reddit_scout, "build_automatic_submission", build_submission)

    adapter = s9_growth.ADAPTERS["reddit"]
    monkeypatch.setattr(adapter, "configured", lambda: True)
    captured = []

    async def publish(payload):
        captured.append(payload)
        return CrosspostResult(
            platform="reddit",
            status="posted",
            remote_id="reddit-auto-1",
            url="https://www.reddit.com/r/learnpython/comments/reddit-auto-1/",
        )

    monkeypatch.setattr(adapter, "publish", publish)
    logged = []
    monkeypatch.setattr(s9_growth, "log_result", lambda _public_id, result, attempt=1: logged.append((result, attempt)))

    results = asyncio.run(s9_growth.crosspost_post(post.id, ["reddit"]))

    assert [result.status for result in results] == ["posted"]
    assert len(captured) == 1
    assert captured[0].title == "A practical Python debugging workflow"
    assert captured[0].text == "A native, source-grounded explanation for the Learn Python community."
    assert captured[0].meta["subreddit"] == "learnpython"
    assert captured[0].meta["reddit_kind"] == "self"
    assert len(logged) == 1
    assert logged[0][0].status == "posted"
    assert logged[0][1] == 1


def test_pikabu_is_scheduled_through_the_automatic_crosspost_pipeline(monkeypatch):
    from types import SimpleNamespace
    from tgfarm import platforms, s9_growth
    from tgfarm.core.schemas import CrosspostResult

    post = SimpleNamespace(
        id=654,
        public_id="pikabu_auto_delivery_654",
        hook="A practical guide to Python debugging",
        body="A source-grounded walkthrough of debugging Python applications.",
        cta="",
        hashtags=["python", "programming"],
        sources=[],
    )
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "pikabu")
    monkeypatch.setattr(s9_growth, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(s9_growth, "_load_post", lambda _post_id: post)
    monkeypatch.setattr(s9_growth, "already_done", lambda _public_id, _platform: False)
    monkeypatch.setattr(s9_growth, "begin_crosspost", lambda _public_id, _platform: 1)

    async def no_ai_adaptation(_payload, *, allow_ai):
        assert allow_ai is True

    monkeypatch.setattr("tgfarm.platform_editorial.adapt_payload_with_ai", no_ai_adaptation)
    adapter = s9_growth.ADAPTERS["pikabu"]
    monkeypatch.setattr(adapter, "configured", lambda: True)
    captured = []

    async def schedule(payload):
        captured.append(payload)
        return CrosspostResult(
            platform="pikabu",
            status="scheduled",
            error="Отложенная публикация сохранена в очереди Пикабу.",
        )

    monkeypatch.setattr(adapter, "publish", schedule)
    logged = []
    monkeypatch.setattr(
        s9_growth, "log_result",
        lambda _public_id, result, attempt=1: logged.append((result, attempt)),
    )

    results = asyncio.run(s9_growth.crosspost_post(post.id, ["pikabu"]))

    assert [result.status for result in results] == ["scheduled"]
    assert len(captured) == 1
    assert captured[0].meta["post_public_id"] == post.public_id
    assert len(logged) == 1
    assert logged[0][0].status == "scheduled"
    assert logged[0][0].remote_id is None
    assert logged[0][0].url is None


def test_pikabu_adapter_does_not_fabricate_remote_post_identity(monkeypatch):
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth import adapters

    adapter = adapters.ADAPTERS["pikabu"]
    monkeypatch.setattr(adapter, "configured", lambda: True)

    async def schedule(post_public_id, *, title, body):
        assert post_public_id == "pikabu_adapter_contract_123"
        assert title == "A useful native title"
        assert body == "A source-grounded article body."
        return {
            "ok": True,
            "status": "scheduled",
            "scheduled_at": "2026-11-04T12:30:00+03:00",
            "target_name": "Технологии",
            "url": "https://pikabu.ru/editor/draft/123",
            "detail": "Отложенная публикация сохранена в нативной очереди Пикабу.",
        }

    monkeypatch.setattr("tgfarm.community_drafts.publish_pikabu_scheduled", schedule)
    result = asyncio.run(
        adapter.publish(
            CrosspostPayload(
                platform="pikabu",
                title="A useful native title",
                text="A source-grounded article body.",
                meta={"post_public_id": "pikabu_adapter_contract_123"},
            )
        )
    )

    assert result.status == "scheduled"
    assert result.remote_id is None
    assert result.url is None
    assert "2026-11-04T12:30:00+03:00" in (result.error or "")
    assert "Технологии" in (result.error or "")


def test_scheduled_crosspost_is_terminal_and_not_retried(monkeypatch):
    from types import SimpleNamespace
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(
        adapters, "latest_crosspost",
        lambda _post_id, _platform: SimpleNamespace(status="scheduled"),
    )
    assert adapters.already_done("post-scheduled", "pikabu") is True


def test_wechat_crosspost_passes_selected_cover_image_to_official_account_adapter(monkeypatch):
    from types import SimpleNamespace

    from tgfarm import platforms, s9_growth
    from tgfarm.s9_growth import adapters
    from tgfarm.core.schemas import CrosspostResult

    post = SimpleNamespace(
        id=908,
        public_id="wechat_cover_route_908",
        hook="How an engineering team reduced deployment risk",
        body="A detailed analysis of release engineering, testing and safer deployments.",
        cta="",
        hashtags=["engineering", "software"],
        sources=[],
    )
    cover_path = "/tmp/tgfarm-wechat-cover.jpg"
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "wechat")
    monkeypatch.setattr(s9_growth, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(s9_growth, "_load_post", lambda _post_id: post)
    monkeypatch.setattr(s9_growth, "already_done", lambda _public_id, _platform: False)
    monkeypatch.setattr(s9_growth, "begin_crosspost", lambda _public_id, _platform: 1)
    monkeypatch.setattr(s9_growth, "primary_image_path", lambda _post_id: None)
    monkeypatch.setattr(adapters, "primary_wechat_cover_path", lambda _post_id: cover_path)

    async def no_ai_adaptation(_payload, *, allow_ai):
        assert allow_ai is True

    monkeypatch.setattr("tgfarm.platform_editorial.adapt_payload_with_ai", no_ai_adaptation)
    adapter = s9_growth.ADAPTERS["wechat"]
    monkeypatch.setattr(adapter, "configured", lambda: True)
    captured = []

    async def publish(payload):
        captured.append(payload)
        return CrosspostResult(
            platform="wechat",
            status="posted",
            remote_id="wechat-draft-908",
            url="https://mp.weixin.qq.com/s/wechat-draft-908",
        )

    monkeypatch.setattr(adapter, "publish", publish)
    monkeypatch.setattr(s9_growth, "log_result", lambda *_args, **_kwargs: None)

    results = asyncio.run(s9_growth.crosspost_post(post.id, ["wechat"]))

    assert [result.status for result in results] == ["posted"]
    assert len(captured) == 1
    assert captured[0].image_path == cover_path
    assert captured[0].meta["image_path"] == cover_path


def test_wechat_native_payload_respects_official_article_title_and_body_lengths():
    from types import SimpleNamespace

    from tgfarm.distribution import adapt_payload_for_platform

    payload = SimpleNamespace(
        title="標題" * 80,
        text="內容" * 15_000,
        link="",
        hashtags=[],
    )
    adapted = adapt_payload_for_platform(payload, "wechat")
    assert len(adapted.title) <= 64
    assert len(adapted.text) <= 20_000

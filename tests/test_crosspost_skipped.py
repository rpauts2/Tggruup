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


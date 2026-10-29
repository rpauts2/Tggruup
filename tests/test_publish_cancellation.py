from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _scheduled_post() -> int:
    suffix = uuid4().hex[:12]
    with session_scope() as session:
        session.add(
            Channel(
                id=f"cancel_{suffix}",
                title="Cancellation test",
                niche="test",
                platform="telegram",
            )
        )
        post = Post(
            public_id=f"cancelpost{suffix}",
            channel_id=f"cancel_{suffix}",
            status="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            hook="test",
            body="test body",
        )
        session.add(post)
        session.flush()
        return post.id


@pytest.mark.asyncio
async def test_cancelled_publish_stops_heartbeat_and_requires_reconciliation(monkeypatch):
    from tgfarm import s7_publish, platforms

    post_id = _scheduled_post()
    heartbeat_started = asyncio.Event()
    heartbeat_stopped = asyncio.Event()
    send_started = asyncio.Event()

    async def fake_heartbeat(_post_id: int, interval_s: float = 30.0):
        heartbeat_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_stopped.set()

    async def fake_send(_target: int, _text: str, _media: str | None):
        send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(live=True, telegram_bot_token="test-token"),
    )
    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", fake_heartbeat)
    monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=fake_send))

    task = asyncio.create_task(
        s7_publish._publish_one(post_id, 12345, "test message", None, "telegram")
    )
    await asyncio.wait_for(heartbeat_started.wait(), timeout=2)
    await asyncio.wait_for(send_started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert heartbeat_stopped.is_set()
    with session_scope() as session:
        post = session.get(Post, post_id)
        assert post is not None
        assert post.status == "publish_unknown"
        assert post.publish_heartbeat_at is None
        assert "verify platform before retry" in post.publish_error

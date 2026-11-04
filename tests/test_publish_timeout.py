"""Per-publication deadline: one hung adapter must not eat the whole tick.

Live incident (2026-11-04): a Dzen browser publication hung and the tick was
killed by the tick-wide timeout (``publish tick exceeded 660s``), so every other
due post starved. These tests pin the architecture: a per-post budget, a bounded
cancellation, a state that requires reconciliation, and a batch that continues.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _seed_post(platform: str, body: str, *, minutes_ago: int = 1) -> tuple[int, str]:
    suffix = uuid4().hex[:10]
    channel_id = f"deadline_{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Deadline test", niche="test", platform=platform)
        )
        post = Post(
            public_id=f"deadline{suffix}",
            channel_id=channel_id,
            status="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            hook="test",
            body=body,
        )
        session.add(post)
        session.flush()
        return post.id, post.public_id


def _snapshot(post_id: int) -> dict:
    with session_scope() as session:
        post = session.get(Post, post_id)
        assert post is not None
        return {
            "status": post.status,
            "error": post.publish_error or "",
            "attempts": int(post.attempts or 0),
            "heartbeat": post.publish_heartbeat_at,
        }


async def _noop_heartbeat(_post_id: int, interval_s: float = 30.0) -> None:
    await asyncio.Event().wait()


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        live=True,
        telegram_bot_token="token",
        dzen_min_gap_s=0,
        dzen_requires_llm=False,
        primary_publish_max_attempts=5,
        publish_cancel_grace_s=0.2,
        channel_ids=[-100123],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_hung_publisher_does_not_stop_the_batch(monkeypatch):
    from tgfarm import platforms, s7_publish

    hung_id, hung_public = _seed_post("telegram", "HUNG POST", minutes_ago=3)
    ok_id, ok_public = _seed_post("telegram", "OK POST", minutes_ago=2)
    ours = [hung_public, ok_public]
    calls: list[str] = []

    async def fake_send(_target, text, _media):
        calls.append(text)
        if "HUNG" in text:
            await asyncio.sleep(60)
        return 1001

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(s7_publish, "get_settings", lambda: _settings())
    monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=fake_send))
    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", _noop_heartbeat)
    # The production floor is 10s; the test compresses the budget only.
    monkeypatch.setattr(
        s7_publish, "publish_timeout_seconds", lambda platform, settings=None: 0.3
    )

    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(
        s7_publish._publish_due_once(limit=10, post_public_ids=ours), timeout=15
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert result["published"] == 1, result
    assert elapsed < 10  # the hung adapter did not own the tick
    assert len(calls) == 2
    assert "HUNG POST" in calls[0] and "OK POST" in calls[1]

    hung = _snapshot(hung_id)
    assert hung["status"] == "publish_unknown"
    assert "exceeded" in hung["error"]
    assert hung["heartbeat"] is None
    assert hung["attempts"] == 1

    ok = _snapshot(ok_id)
    assert ok["status"] == "published"

    # A second tick must not re-send the ambiguous post.
    again = await asyncio.wait_for(
        s7_publish._publish_due_once(limit=10, post_public_ids=ours), timeout=15
    )
    assert again["published"] == 0
    assert sum(1 for call in calls if "HUNG POST" in call) == 1


@pytest.mark.asyncio
async def test_deadline_is_enforced_when_adapter_swallows_cancellation(monkeypatch):
    from tgfarm import s7_publish

    async def stubborn():
        # Ignores the first cancellations, like a browser driver stuck in a call.
        for _ in range(40):
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                continue

    with pytest.raises(s7_publish.PublishTimeout) as caught:
        await s7_publish._await_publish(
            stubborn(),
            timeout_s=0.2,
            platform="dzen",
            post_id=42,
            cancel_grace_s=0.2,
        )
    assert "exceeded" in str(caught.value)
    await asyncio.sleep(1.0)  # let the abandoned task drain inside this loop


@pytest.mark.asyncio
async def test_timeout_is_recorded_as_ambiguous_not_as_failure(monkeypatch):
    from tgfarm import platforms, s7_publish

    post_id, _public = _seed_post("vk", "VK POST")
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(s7_publish, "get_settings", lambda: _settings())

    async def hang(_post_id, _text):
        await asyncio.sleep(60)

    monkeypatch.setattr(s7_publish, "_publish_vk", hang)
    monkeypatch.setattr(
        s7_publish, "publish_timeout_seconds", lambda platform, settings=None: 0.2
    )
    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", _noop_heartbeat)

    result = await s7_publish._publish_one(post_id, "target", "VK POST", None, "vk")

    assert result["ok"] is False
    row = _snapshot(post_id)
    # Ambiguous: the platform may have accepted the post, so no automatic retry.
    assert row["status"] == "publish_unknown"
    assert "deadline" in row["error"] or "exceeded" in row["error"]


def test_timeout_budget_override_and_floor():
    from tgfarm import s7_publish

    assert s7_publish.publish_timeout_seconds(
        "vk", SimpleNamespace(vk_publish_timeout_s=45)
    ) == 45
    # A too-small setting is raised to the floor instead of opening a race.
    assert s7_publish.publish_timeout_seconds(
        "telegram", SimpleNamespace(telegram_publish_timeout_s=1)
    ) == s7_publish.MIN_PUBLISH_TIMEOUT_S
    assert s7_publish.publish_timeout_seconds("dzen", SimpleNamespace()) == 600
    assert s7_publish.publish_timeout_seconds("unknown_platform", SimpleNamespace()) == 120


def test_cancel_grace_is_bounded():
    from tgfarm import s7_publish

    assert s7_publish.publish_cancel_grace_seconds(SimpleNamespace()) == s7_publish.DEFAULT_PUBLISH_CANCEL_GRACE_S
    assert s7_publish.publish_cancel_grace_seconds(
        SimpleNamespace(publish_cancel_grace_s=-5)
    ) == 0.0

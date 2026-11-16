"""Per-delivery preparation budget (W34-B remainder after the W36-A publish deadline).

One deadline per destination covers community selection, AI adaptation, the
rules recheck and the publish wait. The contract this file pins:

- A timeout BEFORE any publish request is safely retryable: the deterministic
  adaptation fallback, a ``skipped`` rules recheck, or a ``failed`` delivery
  whose error says no request was made. It is NEVER ``unknown`` — that token
  locks the pair for operator reconciliation and belongs only to a publish
  that may already have been accepted (W36-A, test_crosspost_publish_deadline.py).
- An exhausted budget must not fire the adapter at all.
- Cancelling the batch during preparation must resolve the claimed attempt
  with a terminal journal row instead of leaking a bare ``publishing`` claim.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from tgfarm import platform_editorial, platforms, s9_growth
from tgfarm.core.schemas import CrosspostResult
from tgfarm.db.models import Channel, CrosspostClaim, CrosspostLog, Post
from tgfarm.db.session import session_scope
from tgfarm.s9_growth import adapters


class RecordingAdapter:
    def __init__(self):
        self.calls = 0

    def configured(self):
        return True

    async def publish(self, payload):
        self.calls += 1
        return CrosspostResult(platform=payload.platform, status="posted", remote_id="stub")


@pytest.fixture()
def delivery(monkeypatch):
    suffix = uuid.uuid4().hex
    channel_id = f"delivery_budget_{suffix}"
    public_id = f"budget_{suffix}"
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="Budget test", niche="tech", platform="telegram", enabled=True
        ))
        session.flush()
        post = Post(
            public_id=public_id, channel_id=channel_id, hook="A useful engineering report",
            body="Verified details about software development.", status="published",
            published_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        post_pk = post.id

    stubs = {name: RecordingAdapter() for name in ("bluesky", "mastodon", "x", "reddit")}
    settings = SimpleNamespace(live=True, mode="live", cycle_timeout_min=5)
    monkeypatch.setattr(s9_growth, "get_settings", lambda: settings)
    monkeypatch.setattr(s9_growth, "ADAPTERS", stubs)
    monkeypatch.setattr(adapters, "ADAPTERS", stubs)
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _name: True)

    async def no_ai(_payload, *, allow_ai):
        return {"status": "deterministic", "used_ai": False}

    monkeypatch.setattr(platform_editorial, "adapt_payload_with_ai", no_ai)
    yield SimpleNamespace(id=post_pk, public_id=public_id, adapters=stubs, settings=settings)
    with session_scope() as session:
        session.execute(delete(CrosspostClaim).where(CrosspostClaim.post_public_id == public_id))
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id == public_id))
        session.execute(delete(Post).where(Post.id == post_pk))
        session.execute(delete(Channel).where(Channel.id == channel_id))


def journal(delivery, name="bluesky"):
    with session_scope() as session:
        rows = session.scalars(select(CrosspostLog).where(
            CrosspostLog.post_public_id == delivery.public_id,
            CrosspostLog.platform == name,
        ).order_by(CrosspostLog.id)).all()
        return [row.status for row in rows], [row.error for row in rows]


@pytest.mark.asyncio
async def test_ai_adaptation_timeout_keeps_delivery_on_the_deterministic_fallback(delivery, monkeypatch):
    """The optional LLM enhancement gets a share of the budget, not all of it."""
    token = s9_growth._publish_budget_seconds.set(0.6)
    started = asyncio.Event()

    async def hang_ai(_payload, *, allow_ai):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(platform_editorial, "adapt_payload_with_ai", hang_ai)
    try:
        results = await asyncio.wait_for(
            s9_growth.crosspost_post(delivery.id, ["bluesky"]), 5
        )
    finally:
        s9_growth._publish_budget_seconds.reset(token)
    assert started.is_set(), "the AI adaptation was never awaited"
    assert [r.status for r in results] == ["posted"]
    assert delivery.adapters["bluesky"].calls == 1
    statuses, _ = journal(delivery)
    assert statuses[-1] == "posted"
    assert "unknown" not in statuses


@pytest.mark.asyncio
async def test_exhausted_preparation_budget_fails_without_firing_the_adapter(delivery, monkeypatch):
    """No publish request was made, so the outcome is retryable, not unknown."""
    token = s9_growth._publish_budget_seconds.set(0.2)
    real_build = s9_growth.build_payload

    def slow_build(*args, **kwargs):
        time.sleep(0.4)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(s9_growth, "build_payload", slow_build)
    try:
        results = await asyncio.wait_for(
            s9_growth.crosspost_post(delivery.id, ["bluesky"]), 5
        )
    finally:
        s9_growth._publish_budget_seconds.reset(token)
    assert [r.status for r in results] == ["failed"]
    assert "budget" in results[0].error.lower()
    assert results[0].retry_after_seconds, "a retryable failure must carry a retry delay"
    assert delivery.adapters["bluesky"].calls == 0, "the adapter fired on an exhausted budget"
    statuses, _ = journal(delivery)
    assert statuses[-1] == "failed"
    assert "unknown" not in statuses


@pytest.mark.asyncio
async def test_rules_recheck_timeout_skips_without_submitting(delivery, monkeypatch):
    """The reddit preflight is read-only; its timeout cannot be an unknown publish."""
    from tgfarm.s9_growth import reddit_scout

    token = s9_growth._publish_budget_seconds.set(0.3)

    async def ok_submission(_public_id):
        return {"ok": True, "subreddit": "test", "title": "t", "body": "b", "reddit_kind": "self"}

    async def hang_preflight(_payload, _submission):
        await asyncio.Event().wait()

    monkeypatch.setattr(reddit_scout, "build_automatic_submission", ok_submission)
    monkeypatch.setattr(s9_growth, "_reddit_preflight", hang_preflight)
    try:
        results = await asyncio.wait_for(
            s9_growth.crosspost_post(delivery.id, ["reddit"]), 5
        )
    finally:
        s9_growth._publish_budget_seconds.reset(token)
    assert [r.status for r in results] == ["skipped"]
    assert "budget" in results[0].error.lower()
    assert delivery.adapters["reddit"].calls == 0
    statuses, _ = journal(delivery, "reddit")
    assert statuses[-1] == "skipped"
    assert "unknown" not in statuses


@pytest.mark.asyncio
async def test_cancellation_during_preparation_resolves_the_claim(delivery, monkeypatch):
    """A stopped batch must not leave a bare publishing claim behind."""
    token = s9_growth._publish_budget_seconds.set(5.0)
    started = asyncio.Event()

    async def hang_ai(_payload, *, allow_ai):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(platform_editorial, "adapt_payload_with_ai", hang_ai)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The terminal row is written through a shielded thread; give it a moment.
        for _ in range(40):
            statuses, errors = journal(delivery)
            if statuses and statuses[-1] == "failed":
                break
            await asyncio.sleep(0.05)
        assert statuses[-1] == "failed", f"claim leaked: {statuses}"
        assert "cancel" in (errors[-1] or "").lower()
        assert "unknown" not in statuses
        assert delivery.adapters["bluesky"].calls == 0
    finally:
        s9_growth._publish_budget_seconds.reset(token)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
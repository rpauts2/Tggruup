from __future__ import annotations

import asyncio
import gc
import threading
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from tgfarm import distribution, platform_editorial, platforms, s9_growth
from tgfarm.core.schemas import CrosspostResult
from tgfarm.db.models import Channel, CrosspostClaim, CrosspostLog, Post
from tgfarm.db.session import session_scope
from tgfarm.s9_growth import adapters


class DeadlineClock:
    def __init__(self, monkeypatch):
        self.calls = asyncio.Queue()
        self.real_wait = asyncio.wait
        monkeypatch.setattr(s9_growth.asyncio, "wait", self.wait)

    async def wait(self, tasks, *, timeout, return_when=asyncio.ALL_COMPLETED):
        assert 0 < timeout < float("inf")
        expired = asyncio.Event()
        self.calls.put_nowait((timeout, expired))
        alarm = asyncio.create_task(expired.wait())
        try:
            done, _ = await self.real_wait(
                {*tasks, alarm}, return_when=asyncio.FIRST_COMPLETED
            )
            return done.intersection(tasks), set(tasks).difference(done)
        finally:
            alarm.cancel()
            await asyncio.gather(alarm, return_exceptions=True)

    async def next(self):
        try:
            return await asyncio.wait_for(self.calls.get(), 2)
        except TimeoutError:
            pytest.fail("adapter.publish was awaited without a finite deadline")


class StubAdapter:
    def configured(self):
        return True

    async def publish(self, payload):
        return CrosspostResult(platform=payload.platform, status="posted", remote_id="stub")


@pytest.fixture()
def delivery(monkeypatch):
    suffix = uuid.uuid4().hex
    channel_id = f"publish_deadline_{suffix}"
    public_id = f"deadline_{suffix}"
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="Deadline test", niche="tech", platform="telegram", enabled=True
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

    stubs = {name: StubAdapter() for name in ("bluesky", "mastodon", "x")}
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
        claims = session.scalars(select(CrosspostClaim).where(
            CrosspostClaim.post_public_id == delivery.public_id,
            CrosspostClaim.platform == name,
        )).all()
        return [row.status for row in rows], len(claims), [row.error for row in rows]


def plan_for(monkeypatch, delivery, names):
    monkeypatch.setattr(distribution, "build_distribution_plan", lambda **_kwargs: {
        "platforms": [
            {"platform": name, "automation": "automatic", "eligible_count": 1,
             "selected": [{"post_id": delivery.id, "score": 100 - index}]}
            for index, name in enumerate(names)
        ]
    })


async def stop(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_first_target_does_not_monopolize_second_and_is_not_republished(delivery, monkeypatch):
    clock = DeadlineClock(monkeypatch)
    started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()
    publishes = []

    async def slow(payload):
        publishes.append(payload.platform)
        started.set()
        await release.wait()
        return CrosspostResult(platform=payload.platform, status="posted")

    async def fast(payload):
        second_started.set()
        return CrosspostResult(platform=payload.platform, status="posted")

    monkeypatch.setattr(delivery.adapters["bluesky"], "publish", slow)
    monkeypatch.setattr(delivery.adapters["mastodon"], "publish", fast)
    plan_for(monkeypatch, delivery, ["bluesky", "mastodon"])
    task = asyncio.create_task(s9_growth.run_crossposts(limit=2))
    try:
        await asyncio.wait_for(started.wait(), 2)
        budget, expired = await clock.next()
        assert budget == 150
        expired.set()
        await asyncio.wait_for(second_started.wait(), 2)
        summary = await asyncio.wait_for(asyncio.shield(task), 2)
        assert summary["unknown"] == 1
        assert summary["posted"] == 1
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
        assert await s9_growth.crosspost_post(delivery.id, ["bluesky"]) == []
        assert publishes == ["bluesky"]
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
    finally:
        release.set()
        await stop(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_deadline_returns_before_cancellation_cleanup_and_consumes_late_outcome(
    delivery, monkeypatch, late_error, caplog
):
    clock = DeadlineClock(monkeypatch)
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    adapter_tasks = []
    loop_errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def slow(payload):
        adapter_tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
        finally:
            finished.set()
        if late_error:
            raise RuntimeError("password=late-private-secret")
        return CrosspostResult(platform=payload.platform, status="posted")

    monkeypatch.setattr(delivery.adapters["bluesky"], "publish", slow)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        budget, expired = await clock.next()
        assert budget == 60
        expired.set()
        results = await asyncio.wait_for(asyncio.shield(task), 2)
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not finished.is_set()
        assert [result.status for result in results] == ["unknown"]
        assert adapter_tasks[0] in s9_growth._publish_tasks
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
        assert adapter_tasks[0] not in s9_growth._publish_tasks
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
        assert "late-private-secret" not in str(journal(delivery)[2])
        adapter_tasks.clear()
        gc.collect()
        assert "late-private-secret" not in caplog.text
        assert not loop_errors
    finally:
        release.set()
        await stop(task)
        await asyncio.gather(*adapter_tasks, return_exceptions=True)
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_late_sync_thread_completion_cannot_clear_unknown_or_claim(delivery, monkeypatch):
    clock = DeadlineClock(monkeypatch)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()

    def sync_publish():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        loop.call_soon_threadsafe(finished.set)
        return CrosspostResult(platform="bluesky", status="posted")

    async def publish(_payload):
        return await asyncio.to_thread(sync_publish)

    monkeypatch.setattr(delivery.adapters["bluesky"], "publish", publish)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        _, expired = await clock.next()
        expired.set()
        results = await asyncio.wait_for(asyncio.shield(task), 2)
        assert [result.status for result in results] == ["unknown"]
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
        assert await s9_growth.crosspost_post(delivery.id, ["bluesky"]) == []
    finally:
        release.set()
        await stop(task)
        await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
async def test_external_publish_cancellation_journals_unknown_once_then_propagates(delivery, monkeypatch):
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    adapter_tasks = []

    async def publish(payload):
        adapter_tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
        return CrosspostResult(platform=payload.platform, status="posted")

    monkeypatch.setattr(delivery.adapters["bluesky"], "publish", publish)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, "caller waited for adapter cancellation cleanup"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaning.is_set()
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
        assert await s9_growth.crosspost_post(delivery.id, ["bluesky"]) == []
        release.set()
        await asyncio.gather(*adapter_tasks, return_exceptions=True)
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
    finally:
        release.set()
        await stop(task)
        await asyncio.gather(*adapter_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claim", "posted_log", "unknown_log", "cancel_log"])
async def test_cancellation_during_threaded_claim_or_terminal_log_has_no_competing_writer(
    delivery, monkeypatch, phase
):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    writes = []
    real_begin = s9_growth.begin_crosspost
    real_log = s9_growth.log_result
    clock = DeadlineClock(monkeypatch) if phase == "unknown_log" else None
    publish_started = asyncio.Event()
    uncertain = phase in {"unknown_log", "cancel_log"}

    def begin(public_id, name):
        attempt = real_begin(public_id, name)
        if phase == "claim":
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5)
            finally:
                loop.call_soon_threadsafe(finished.set)
        return attempt

    def log_result(public_id, result, attempt=1):
        writes.append(result.status)
        if phase != "claim":
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5)
                real_log(public_id, result, attempt)
            finally:
                loop.call_soon_threadsafe(finished.set)
        else:
            real_log(public_id, result, attempt)

    async def stalled(_payload):
        publish_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(s9_growth, "begin_crosspost", begin)
    monkeypatch.setattr(s9_growth, "log_result", log_result)
    if uncertain:
        monkeypatch.setattr(delivery.adapters["bluesky"], "publish", stalled)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        if clock:
            _, expired = await clock.next()
            expired.set()
        elif phase == "cancel_log":
            await asyncio.wait_for(publish_started.wait(), 2)
            task.cancel()
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal(delivery)[:2] == (["publishing"], 1)
        assert writes == ([] if phase == "claim" else ["unknown" if uncertain else "posted"])
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        expected = (["publishing"], 1) if phase == "claim" else (
            ["publishing", "unknown" if uncertain else "posted"], 1 if uncertain else 0
        )
        assert journal(delivery)[:2] == expected
    finally:
        release.set()
        await stop(task)
        await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes,limit,want", [(0, 2, 30), (3, 3, 60), (60, 2, 450)])
async def test_batch_publish_budget_uses_clamped_outer_share_and_restores_direct_default(
    delivery, monkeypatch, minutes, limit, want
):
    clock = DeadlineClock(monkeypatch)
    delivery.settings.cycle_timeout_min = minutes
    plan_for(monkeypatch, delivery, ["bluesky", "mastodon"])
    summary = await s9_growth.run_crossposts(limit=limit)
    assert summary["posted"] == 2
    assert (await clock.next())[0] == want
    assert (await clock.next())[0] == want
    assert [result.status for result in await s9_growth.crosspost_post(delivery.id, ["x"])] == ["posted"]
    assert (await clock.next())[0] == 60


@pytest.mark.asyncio
async def test_batch_keeps_two_argument_crosspost_mock_compatibility(delivery, monkeypatch):
    plan_for(monkeypatch, delivery, ["bluesky"])

    async def crosspost(post_pk, names):
        assert post_pk == delivery.id
        return [CrosspostResult(platform=name, status="posted") for name in names]

    monkeypatch.setattr(s9_growth, "crosspost_post", crosspost)
    assert (await s9_growth.run_crossposts(limit=1))["posted"] == 1


@pytest.mark.asyncio
async def test_real_publish_timer_does_not_wait_for_adapter_cleanup(delivery, monkeypatch):
    cleaning = asyncio.Event()
    release = asyncio.Event()
    adapter_tasks = []

    async def slow(payload):
        adapter_tasks.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
        return CrosspostResult(platform=payload.platform, status="posted")

    monkeypatch.setattr(delivery.adapters["bluesky"], "publish", slow)
    token = s9_growth._publish_budget_seconds.set(0.02)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        results = await asyncio.wait_for(asyncio.shield(task), 2)
        assert cleaning.is_set()
        assert not release.is_set()
        assert [result.status for result in results] == ["unknown"]
        assert journal(delivery)[:2] == (["publishing", "unknown"], 1)
    finally:
        s9_growth._publish_budget_seconds.reset(token)
        release.set()
        await stop(task)
        await asyncio.gather(*adapter_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_reddit_preflight_cancellation_is_not_an_unknown_publish(delivery, monkeypatch):
    from tgfarm.s9_growth import reddit_scout

    clock = DeadlineClock(monkeypatch)
    preparing = asyncio.Event()
    release = asyncio.Event()
    delivery.adapters["reddit"] = StubAdapter()

    async def submission(_public_id):
        return {"ok": True, "subreddit": "learnpython", "title": "Engineering report",
                "body": "Verified details about software development.", "reddit_kind": "self"}

    async def preflight(_payload, _submission):
        preparing.set()
        await release.wait()
        return None

    monkeypatch.setattr(reddit_scout, "build_automatic_submission", submission)
    monkeypatch.setattr(s9_growth, "_reddit_preflight", preflight)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["reddit"]))
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        assert clock.calls.empty()
        assert journal(delivery, "reddit")[:2] == (["publishing"], 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal(delivery, "reddit")[:2] == (["publishing"], 1)
    finally:
        release.set()
        await stop(task)


@pytest.mark.asyncio
async def test_ai_preparation_is_not_wrapped_in_publish_deadline(delivery, monkeypatch):
    clock = DeadlineClock(monkeypatch)
    preparing = asyncio.Event()
    release = asyncio.Event()

    async def prepare(_payload, *, allow_ai):
        preparing.set()
        await release.wait()
        return {"status": "deterministic", "used_ai": False}

    monkeypatch.setattr(platform_editorial, "adapt_payload_with_ai", prepare)
    task = asyncio.create_task(s9_growth.crosspost_post(delivery.id, ["bluesky"]))
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        assert clock.calls.empty()
        assert journal(delivery)[:2] == (["publishing"], 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal(delivery)[:2] == (["publishing"], 1)
    finally:
        release.set()
        await stop(task)

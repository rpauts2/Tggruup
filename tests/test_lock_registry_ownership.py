"""Named lock ownership and elapsed hold time, without wall-clock sleeps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgfarm.core import ratelimit
from tgfarm.core.ratelimit import LockRegistry


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    # Replace only this module's time dependency, not asyncio's monotonic clock.
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def _watch_waiter(monkeypatch, lock):
    """Signal entry into the real lock's blocked acquisition; do not replace it."""
    queued = asyncio.Event()
    acquire = asyncio.Lock.acquire

    async def observed_acquire(self):
        if self is lock and self.locked():
            queued.set()
        return await acquire(self)

    monkeypatch.setattr(asyncio.Lock, "acquire", observed_acquire)
    return queued


async def test_same_name_excludes_waiter_until_owner_releases(monkeypatch):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    queued = _watch_waiter(monkeypatch, held)
    acquired = asyncio.Event()
    finish = asyncio.Event()

    async def contender():
        lock = await locks.acquire("channel")
        try:
            assert lock is held
            acquired.set()
            await finish.wait()
        finally:
            locks.release("channel", lock)

    task = asyncio.create_task(contender())
    try:
        await queued.wait()
        assert not acquired.is_set()
        assert held.locked()
        locks.release("channel", held)
        await acquired.wait()
        assert held.locked()
    finally:
        finish.set()
        if not acquired.is_set():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not held.locked()


@pytest.mark.parametrize("invalid", ["known_name", "unknown_name", "registered_lock", "foreign_lock"])
async def test_wrong_name_or_lock_rejected_without_changing_holders(invalid, clock):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    other = await locks.acquire("other")
    foreign = asyncio.Lock()
    await foreign.acquire()
    name, lock = {
        "known_name": ("other", held),
        "unknown_name": ("missing", held),
        "registered_lock": ("channel", other),
        "foreign_lock": ("channel", foreign),
    }[invalid]

    clock.now = 5.0
    for _ in range(2):
        with pytest.raises(RuntimeError):
            locks.release(name, lock)
        assert held.locked()
        assert other.locked()
        assert foreign.locked()
        assert locks.hold_seconds("channel") == 5.0
        assert locks.hold_seconds("other") == 5.0
        assert locks.hold_seconds("missing") == 0.0

    clock.now = 7.0
    locks.release("channel", held)
    locks.release("other", other)
    foreign.release()
    assert locks.hold_seconds("channel") == 7.0
    assert locks.hold_seconds("other") == 7.0


async def test_wrong_task_cannot_release_actual_holders_lock(clock):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    clock.now = 4.0

    async def stranger():
        for _ in range(2):
            with pytest.raises(RuntimeError):
                locks.release("channel", held)
            assert held.locked()
            assert locks.hold_seconds("channel") == 4.0

    await asyncio.create_task(stranger())
    clock.now = 6.0
    locks.release("channel", held)
    assert not held.locked()
    assert locks.hold_seconds("channel") == 6.0


async def test_stale_owner_cannot_release_later_owners_same_lock(clock):
    locks = LockRegistry()
    stale = await locks.acquire("channel")
    clock.now = 3.0
    locks.release("channel", stale)
    acquired = asyncio.Event()
    finish = asyncio.Event()

    async def next_owner():
        lock = await locks.acquire("channel")
        try:
            assert lock is stale
            acquired.set()
            await finish.wait()
        finally:
            locks.release("channel", lock)

    clock.now = 5.0
    task = asyncio.create_task(next_owner())
    try:
        await acquired.wait()
        clock.now = 8.0
        for _ in range(2):
            with pytest.raises(RuntimeError):
                locks.release("channel", stale)
            assert stale.locked()
            assert locks.hold_seconds("channel") == 6.0
        clock.now = 9.0
    finally:
        finish.set()
        await task
    assert not stale.locked()
    assert locks.hold_seconds("channel") == 7.0


async def test_duplicate_release_rejected_without_changing_completed_time(clock):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    clock.now = 2.0
    locks.release("channel", held)
    clock.now = 10.0
    for _ in range(2):
        with pytest.raises(RuntimeError):
            locks.release("channel", held)
        assert not held.locked()
        assert locks.hold_seconds("channel") == 2.0


@pytest.mark.parametrize("handoff", [False, True])
async def test_cancelled_waiter_cannot_steal_or_leak_lock(handoff, monkeypatch, clock):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    queued = _watch_waiter(monkeypatch, held)

    async def waiter():
        try:
            await locks.acquire("channel")
        except asyncio.CancelledError:
            for _ in range(2):
                with pytest.raises(RuntimeError):
                    locks.release("channel", held)
            raise

    task = asyncio.create_task(waiter())
    await queued.wait()
    clock.now = 4.0
    if handoff:
        locks.release("channel", held)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert held.locked() is (not handoff)
    assert locks.hold_seconds("channel") == 4.0
    if not handoff:
        locks.release("channel", held)

    clock.now = 20.0
    recovered = await locks.acquire("channel")
    assert recovered is held
    clock.now = 22.0
    locks.release("channel", recovered)
    assert locks.hold_seconds("channel") == 6.0
    assert not held.locked()


async def test_timed_out_queued_waiter_cannot_replace_holder_or_leak_lock(monkeypatch, clock):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    queued = _watch_waiter(monkeypatch, held)
    loop = asyncio.get_running_loop()
    call_at = loop.call_at
    deadlines = []

    def capture_deadline(when, callback, *args, context=None):
        timer = call_at(when, callback, *args, context=context)
        deadlines.append((timer, callback, args, context))
        return timer

    monkeypatch.setattr(loop, "call_at", capture_deadline)

    async def timed_out_waiter():
        with pytest.raises(TimeoutError):
            await locks.acquire("channel", timeout=60.0)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                locks.release("channel", held)

    task = asyncio.create_task(timed_out_waiter())
    await queued.wait()
    clock.now = 3.0
    # Fire the real timeout callback only after the real lock waiter is queued.
    (timer, callback, args, context), = deadlines
    timer.cancel()
    loop.call_soon(callback, *args, context=context)
    await task
    assert held.locked()
    assert locks.hold_seconds("channel") == 3.0
    locks.release("channel", held)
    recovered = await locks.acquire("channel")
    clock.now = 5.0
    locks.release("channel", recovered)
    assert locks.hold_seconds("channel") == 5.0
    assert not recovered.locked()


async def test_owner_is_caller_even_when_wait_for_uses_child_task(monkeypatch, clock):
    wait_for = asyncio.wait_for

    async def child_wait_for(awaitable, timeout):
        return await wait_for(asyncio.create_task(awaitable), timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", child_wait_for)
    locks = LockRegistry()
    held = await locks.acquire("channel")
    clock.now = 2.0
    locks.release("channel", held)
    assert not held.locked()
    assert locks.hold_seconds("channel") == 2.0


async def test_hold_seconds_adds_completed_and_current_holds_not_idle_time(clock):
    locks = LockRegistry()
    clock.now = 100.0
    assert locks.hold_seconds("channel") == 0.0
    held = await locks.acquire("channel")
    assert locks.hold_seconds("channel") == 0.0
    clock.now = 103.0
    assert locks.hold_seconds("channel") == 3.0
    locks.release("channel", held)
    clock.now = 150.0
    assert locks.hold_seconds("channel") == 3.0
    held = await locks.acquire("channel")
    clock.now = 154.0
    assert locks.hold_seconds("channel") == 7.0
    locks.release("channel", held)
    clock.now = 200.0
    assert locks.hold_seconds("channel") == 7.0


async def test_wait_and_handoff_gap_are_not_counted_as_hold_time(monkeypatch, clock):
    locks = LockRegistry()
    held = await locks.acquire("channel")
    queued = _watch_waiter(monkeypatch, held)
    acquired = asyncio.Event()
    finish = asyncio.Event()

    async def waiter():
        lock = await locks.acquire("channel")
        try:
            acquired.set()
            await finish.wait()
        finally:
            locks.release("channel", lock)

    clock.now = 2.0
    task = asyncio.create_task(waiter())
    try:
        await queued.wait()
        clock.now = 5.0
        locks.release("channel", held)
        clock.now = 10.0
        await acquired.wait()
        assert locks.hold_seconds("channel") == 5.0
        clock.now = 13.0
        assert locks.hold_seconds("channel") == 8.0
    finally:
        finish.set()
        if not acquired.is_set():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    clock.now = 100.0
    assert locks.hold_seconds("channel") == 8.0

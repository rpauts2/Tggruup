"""The token bucket must fail fast on an impossible request.

`take(n)` dropped any `n` above the bucket's capacity into an endless loop: the
token count is capped at capacity, so the loop could never satisfy the request
and the coroutine slept five seconds at a time forever. Every current caller
asks for the default single token, which is exactly why the trap was invisible —
the first caller to batch a request would hang the stage instead of getting an
error.
"""

from __future__ import annotations

import asyncio

import pytest

from tgfarm.core.ratelimit import DailyQuota, LockRegistry, TokenBucket


async def test_take_more_than_capacity_raises_instead_of_hanging():
    bucket = TokenBucket(rate_per_s=1.0, capacity=2.0)

    with pytest.raises(ValueError):
        await asyncio.wait_for(bucket.take(3.0), timeout=1.0)


async def test_take_spends_capacity_and_refills():
    bucket = TokenBucket(rate_per_s=1000.0, capacity=1.0)

    await asyncio.wait_for(bucket.take(), timeout=1.0)
    await asyncio.wait_for(bucket.take(), timeout=1.0)


def test_daily_quota_stops_at_the_limit():
    quota = DailyQuota(limit=2)

    assert quota.try_consume() is True
    assert quota.try_consume() is True
    assert quota.try_consume() is False
    assert quota.used() == 2
    assert quota.remaining() == 0


def test_daily_quota_rejects_a_batch_it_cannot_fit():
    quota = DailyQuota(limit=2)

    assert quota.try_consume(3) is False
    assert quota.used() == 0


async def test_lock_registry_times_out_and_recovers_after_release():
    locks = LockRegistry()

    held = await locks.acquire("channel", timeout=1.0)
    with pytest.raises(TimeoutError):
        await locks.acquire("channel", timeout=0.01)
    locks.release("channel", held)
    again = await locks.acquire("channel", timeout=1.0)
    locks.release("channel", again)


def test_a_bucket_with_no_capacity_is_refused():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_s=1.0, capacity=0.0)

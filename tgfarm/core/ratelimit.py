"""Token-bucket rate limiting and named async locks.

Used to respect provider quotas (LLM, image, Telegram Bot API) and to guarantee
one-at-a-time execution per channel so a crash loop cannot double-publish.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class TokenBucket:
    """Classic token bucket. `take()` sleeps until n tokens are available."""

    def __init__(self, rate_per_s: float, capacity: float | None = None) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be > 0")
        self.rate = float(rate_per_s)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_s))
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, tokens: float = 1.0) -> None:
        # The bucket can never hold more than `capacity` tokens, so a larger
        # request would spin here forever instead of reporting the mistake.
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        if tokens > self.capacity:
            raise ValueError(
                f"cannot take {tokens} tokens: capacity is {self.capacity}"
            )
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(min(wait, 5.0))


class DailyQuota:
    """Hard cap per calendar day — the last line of defence against runaway loops."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._hits: deque[str] = deque()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def used(self) -> int:
        today = self._today()
        while self._hits and self._hits[0] != today:
            self._hits.popleft()
        return len(self._hits)

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def try_consume(self, n: int = 1) -> bool:
        today = self._today()
        while self._hits and self._hits[0] != today:
            self._hits.popleft()
        if len(self._hits) + n > self.limit:
            return False
        self._hits.extend([today] * n)
        return True


class LockRegistry:
    """Named async locks. Two coroutines never hold the same name at once."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._spans: dict[str, float] = defaultdict(float)
        self._owners: dict[str, asyncio.Task | None] = {}
        self._held_since: dict[str, float] = {}

    async def acquire(self, name: str, timeout: float = 60.0) -> asyncio.Lock:
        owner = asyncio.current_task()
        async with self._guard:
            lock = self._locks.setdefault(name, asyncio.Lock())
        try:
            # Preserve wait_for's refusal to start even a free acquisition at <= 0.
            if timeout is not None and timeout <= 0:
                raise TimeoutError
            # Python 3.11 wait_for can swallow cancellation after a child grants
            # the lock. Acquire in the caller so Lock handles cancelled handoffs.
            async with asyncio.timeout(timeout):
                await lock.acquire()
        except TimeoutError as exc:
            raise TimeoutError(f"could not acquire lock {name!r} in {timeout}s") from exc
        self._owners[name] = owner
        self._held_since[name] = time.monotonic()
        return lock

    def release(self, name: str, lock: asyncio.Lock) -> None:
        if (
            self._locks.get(name) is not lock
            or name not in self._owners
            or self._owners[name] is not asyncio.current_task()
            or not lock.locked()
        ):
            raise RuntimeError(f"lock {name!r} is not held by the current task")
        self._spans[name] += time.monotonic() - self._held_since.pop(name)
        del self._owners[name]
        lock.release()

    def hold_seconds(self, name: str) -> float:
        elapsed = self._spans.get(name, 0.0)
        started = self._held_since.get(name)
        if started is not None:
            elapsed += time.monotonic() - started
        return elapsed


class RateLimiters:
    """Pre-built buckets matching real provider quotas."""

    def __init__(self) -> None:
        self.bot_api = TokenBucket(rate_per_s=25.0, capacity=25.0)
        self.llm = TokenBucket(rate_per_s=1.0, capacity=4.0)
        self.image = TokenBucket(rate_per_s=0.5, capacity=3.0)
        self.http = TokenBucket(rate_per_s=15.0, capacity=30.0)

    def telegram_quota(self) -> DailyQuota:
        # Bot API hard ceiling is 30 msg/s globally; stay well under it.
        return DailyQuota(limit=1500)


LOCKS = LockRegistry()
RATE = RateLimiters()
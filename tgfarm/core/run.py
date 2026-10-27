"""Async helper for running blocking work off the event loop."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from tgfarm.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


def ensure_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres hands back aware ones.

    Everything in the pipeline compares timestamps, so normalise at the boundary
    instead of guarding every comparison site.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def to_thread(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking callable in the default executor."""
    if kwargs:
        fn = functools.partial(fn, **kwargs)
    return await asyncio.to_thread(fn, *args)


class StageTimer:
    """Context manager that logs a pipeline stage duration."""

    def __init__(self, stage: str, items_in: int = 0) -> None:
        self.stage = stage
        self.items_in = items_in
        self.items_out = 0
        self._started = 0.0
        self.duration_ms = 0

    def __enter__(self) -> StageTimer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.duration_ms = int((time.perf_counter() - self._started) * 1000)
        if exc_type is None:
            log.info(
                "stage %s: %d -> %d in %dms", self.stage, self.items_in, self.items_out, self.duration_ms
            )
        else:
            log.error("stage %s failed after %dms: %s", self.stage, self.duration_ms, exc)


async def gather_limited(tasks: list[Awaitable[T]], limit: int = 6) -> list[T]:
    """Run awaitables with a concurrency cap, preserving input order."""
    semaphore = asyncio.Semaphore(limit)

    async def _run(task: Awaitable[T]) -> T:
        async with semaphore:
            return await task

    return await asyncio.gather(*(_run(t) for t in tasks), return_exceptions=True)
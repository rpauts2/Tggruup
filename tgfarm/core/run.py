"""Async helper for running blocking work off the event loop.

Also owns the nested-stage measurement handshake: a stage that measures its
work in an inner `StageTimer` publishes that measurement to the enclosing
stage's timer, so the run journal sees the numbers the stage actually produced
without every stage having to return them.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from tgfarm.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

#: The `StageTimer` chain of the current task / thread context, outermost first.
#: A contextvar rather than a module-level list: a stage that hops to a worker
#: thread through `asyncio.to_thread` receives a *copy* of the chain, so nested
#: timers still find their enclosing stage, while two stages running in parallel
#: never see each other's timers.
_stage_stack: contextvars.ContextVar[tuple[StageTimer, ...]] = contextvars.ContextVar(
    "tgfarm_stage_stack", default=()
)


def current_stage_timer() -> StageTimer | None:
    """The innermost stage currently being measured in this context, if any."""
    stack = _stage_stack.get()
    return stack[-1] if stack else None


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
    """Context manager that measures one pipeline stage.

    Every real stage measures its work in a private timer of its own, while the
    run journal row is written from the timer the orchestrator opened around it.
    So a timer that exits inside another stage's scope publishes its counters to
    the enclosing timer of the same stage family: the numbers a stage actually
    measured reach the journal row without any stage having to remember to
    report them.

    `items_in` / `items_out` are three-state on purpose. `None` means "nobody
    measured this", which is a different fact from "the stage processed zero
    items"; collapsing the first into the second is what made the journal
    useless. The per-sub-stage breakdown always travels, so a headline number
    can be read exactly.

    The handoff survives a thread hop because `asyncio.to_thread` copies the
    current context into the worker: the nested timer finds the enclosing timer
    *object* in that copy and mutates it, and a mutation of a shared object is
    visible in every context that holds it. Only the stack binding is per
    context, which is also what keeps two concurrently running stages from
    writing into each other's totals.
    """

    def __init__(self, stage: str, items_in: int | None = None) -> None:
        self.stage = stage
        self.items_in = _as_count(items_in)
        self.items_out: int | None = None
        self._started = 0.0
        self.duration_ms = 0
        self._enclosing: tuple[StageTimer, ...] = ()
        self._children: dict[str, dict[str, int | None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ nesting

    def __enter__(self) -> StageTimer:
        self._started = time.perf_counter()
        self._enclosing = _stage_stack.get()
        _stage_stack.set((*self._enclosing, self))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.duration_ms = int((time.perf_counter() - self._started) * 1000)
        _stage_stack.set(tuple(t for t in _stage_stack.get() if t is not self))
        # A stage that threw still hands over what it measured before it threw;
        # that is the whole point of recording it with status=error. Measurement is
        # telemetry, so a handoff that cannot happen must never break the stage.
        try:
            parent = _enclosing_of(self.stage, self._enclosing)
            if parent is not None and parent is not self:
                parent.adopt(self)
        except Exception as handoff_exc:  # noqa: BLE001 - never fail a stage over metrics
            log.debug("stage %s could not hand counters upward (%s)", self.stage, type(handoff_exc).__name__)
        if exc_type is None:
            log.info(
                "stage %s: %s -> %s in %dms",
                self.stage,
                _fmt_count(self.reported_in),
                _fmt_count(self.reported_out),
                self.duration_ms,
            )
        else:
            log.error(
                "stage %s failed after %dms with %s -> %s measured: %s",
                self.stage,
                self.duration_ms,
                _fmt_count(self.reported_in),
                _fmt_count(self.reported_out),
                describe_exception(exc_type, exc),
            )

    # ---------------------------------------------------------------- handoff

    def adopt(self, child: StageTimer) -> None:
        """Fold one nested stage's measurement into this stage.

        Same-named children are summed: `s4_generate` runs once per cluster, and
        the stage's total is the sum over its clusters. Distinct sub-stages stay
        distinct in `sub_stages`, so the breakdown is never lost. A grandchild is
        already folded into its own child's headline, so no measurement stops at
        the second level.
        """
        measurement = child.measurement()
        with self._lock:
            slot = self._children.setdefault(
                str(measurement["stage"]),
                {"items_in": None, "items_out": None, "duration_ms": 0, "runs": 0},
            )
            for key in ("items_in", "items_out"):
                value = _as_count(measurement[key])
                if value is not None:
                    slot[key] = (slot[key] or 0) + value
            slot["duration_ms"] = int(slot["duration_ms"]) + int(measurement["duration_ms"])
            slot["runs"] = int(slot["runs"]) + 1

    @property
    def sub_stages(self) -> list[dict[str, int | None]]:
        """Every nested stage that measured itself inside this one, in exit order."""
        return [
            {
                "stage": name,
                "runs": slot["runs"],
                "items_in": slot["items_in"],
                "items_out": slot["items_out"],
                "duration_ms": slot["duration_ms"],
            }
            for name, slot in self._children.items()
        ]

    def _headline(self, key: str) -> int | None:
        """The value a stage is best described by: its own, else its last sub-stage's.

        A stage that runs a sequence of passes (S2: dedup, cluster, score) has no
        single self-declared total; its output is what the last pass that measured
        one produced. Nothing is added across passes, because the passes count
        different things.
        """
        for slot in reversed(list(self._children.values())):
            value = slot.get(key)
            if value is not None:
                return int(value)
        return None

    @property
    def reported_in(self) -> int | None:
        """Items this stage consumed, or None when that was never measured.

        A stage's own number always wins; only a field it never touched is taken
        from a nested timer, so nothing is ever invented for a stage that said
        "zero".
        """
        if self.items_in is not None:
            return int(self.items_in)
        return self._headline("items_in")

    @property
    def reported_out(self) -> int | None:
        """Items this stage produced, or None when that was never measured."""
        if self.items_out is not None:
            return int(self.items_out)
        return self._headline("items_out")

    def measurement(self) -> dict[str, Any]:
        """This stage as recorded by whatever encloses it."""
        return {
            "stage": self.stage,
            "items_in": self.reported_in,
            "items_out": self.reported_out,
            "duration_ms": self.duration_ms,
            "sub_stages": self.sub_stages,
        }


def counters_state(items_in: int | None, items_out: int | None) -> str:
    """Whether a pair of stage counters is measurement, part-measurement, or nothing.

    `not_reported` is not a zero: it says nobody measured. Written to the journal
    row's `meta`, it is what lets an operator tell "the stage did no work" from
    "the counters never travelled".
    """
    measured = sum(1 for value in (items_in, items_out) if value is not None)
    return {0: "not_reported", 1: "partial", 2: "measured"}[measured]


def _as_count(value: object) -> int | None:
    """A counter, or None for "not measured". Never a silent 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt_count(value: int | None) -> str:
    """Log rendering: an unmeasured counter is `?`, a measured zero is `0`."""
    return "?" if value is None else str(int(value))


def describe_exception(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
) -> str:
    """`Type: message` — and always at least the type.

    Rendering an exception as its own `str` loses the name for the exceptions
    that carry no message, and the farm's dominant failure is exactly one of
    them: `str(asyncio.CancelledError())` is `''`, so the live log read as
    `stage s5_quality failed after 1234ms with 0 -> ? measured: ` and stopped.
    The type is known whenever `__exit__` is, so the line can never end bare.
    """
    name = str(getattr(exc_type, "__name__", "") or "") if exc_type is not None else ""
    if not name:
        name = type(exc).__name__ if exc is not None else "BaseException"
    message = str(exc).strip() if exc is not None else ""
    return f"{name}: {message}" if message else name


def _stage_family(stage: str) -> str:
    """`s4_generate` -> `s4`; anything else is its own family.

    A nested timer belongs to the enclosing stage only when it measures the same
    subsystem. `s3_route` runs inside S4 and must not be credited to S4's totals,
    and the stage name is the only place that relationship is written down.
    """
    head = str(stage).split("_", 1)[0].strip().lower()
    return head if len(head) > 1 and head[0] == "s" and head[1:].isdigit() else str(stage).strip().lower()


def _enclosing_of(stage: str, enclosing: tuple[StageTimer, ...]) -> StageTimer | None:
    """Nearest enclosing timer that measures the same subsystem, if any."""
    family = _stage_family(stage)
    for timer in reversed(enclosing):
        if _stage_family(timer.stage) == family:
            return timer
    return None


async def gather_limited(tasks: list[Awaitable[T]], limit: int = 6) -> list[T]:
    """Run awaitables with a concurrency cap, preserving input order."""
    semaphore = asyncio.Semaphore(limit)

    async def _run(task: Awaitable[T]) -> T:
        async with semaphore:
            return await task

    return await asyncio.gather(*(_run(t) for t in tasks), return_exceptions=True)
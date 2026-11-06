"""Resilience primitives for the LLM providers: clock, backoff, breaker, budget.

Why a module of its own instead of five more attributes on `client.py`:
every mechanism in here is a *state machine over time* — a cooldown, an open
breaker, a spent budget — and each one needs the same two seams to be testable
without a wall-clock or a network: an injected clock and an injected sleeper.
Keeping them in one owner means one fake clock drives all of them in the tests,
and it means `client.py` stays about talking to providers.

The honesty rule that runs through this module: none of these objects may turn a
refusal into a success. A breaker that is open, a budget that is spent and a
backoff that has run out of time all end in the same place — the caller must
take the deterministic offline path and say so. They must never end in "ok".
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

# ---------------------------------------------------------------- clocks ----
#: Injected seams. Tests set both; production never does.
_clock: Callable[[], float] = time.monotonic
_sleeper: Callable[[float], Any] | None = None
_rng: Callable[[], float] = random.random


def set_clock(fn: Callable[[], float] | None) -> None:
    """Install a monotonic clock (`time.monotonic` shape); None restores it."""
    global _clock
    _clock = fn or time.monotonic


def set_sleeper(fn: Callable[[float], Any] | None) -> None:
    """Install an awaitable sleep; None restores the real `asyncio.sleep`."""
    global _sleeper
    _sleeper = fn


def set_rng(fn: Callable[[], float] | None) -> None:
    """Install a `random.random` shape so jitter is reproducible; None restores."""
    global _rng
    _rng = fn or random.random


def now() -> float:
    return _clock()


async def sleep(seconds: float) -> None:
    """Await `seconds` through the injected sleeper.

    The default sleeper is `asyncio.sleep`, so production behaviour is
    unchanged. A test that installs a recording sleeper never waits at all,
    which is what lets the backoff paths be asserted on their *arguments*
    rather than on elapsed wall time.
    """
    global _sleeper
    if _sleeper is None:
        import asyncio

        _sleeper = asyncio.sleep
    waited = _sleeper(max(0.0, float(seconds)))
    if hasattr(waited, "__await__"):
        await waited


# ------------------------------------------------------------- backoff ------
#: Base delay of the exponential ladder, in seconds. The free-tier gateways that
#: this farm actually runs against answer a 503 in well under a second, so the
#: ladder starts where a retry could plausibly help, not at a coin flip.
BACKOFF_BASE_S = 0.4
#: Ceiling for one ladder step, in seconds. Chosen so a single wait cannot
#: exceed half of `llm_timeout_s` (60 by default): a wait longer than the
#: request timeout the operator configured is the provider saying "come back
#: later", and one call must not be the place where the cycle spends it.
BACKOFF_CAP_S = 20.0
#: Hard wall-clock budget for one logical generation call, from the first
#: request to the honest answer. 3 attempts over a 12-model catalogue cost ~24 s
#: in the live log (median `stage s4_generate` is 42 min for ~52 drafts at ~2
#: calls each), and an open breaker is what actually stops the stage. 20 s keeps
#: a healthy provider's fair chance (a real generation runs 3-15 s) while making
#: it arithmetically impossible for one call to outlive its cycle window.
CALL_BUDGET_S = 20.0
#: Ceiling on how long we ever agree to wait because a provider told us to.
#: `Retry-After` is an instruction from a stranger: honoured inside our budget,
#: never outside it. A 24 h `Retry-After` must not become a 24 h sleep.
MAX_WAIT_S = 20.0

_RETRY_AFTER_SECONDS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


def parse_retry_after(value: str | None, *, now_s: float | None = None) -> float | None:
    """`Retry-After` as seconds, or None when it is absent or unparseable.

    RFC 9110 allows two forms: delta-seconds and an HTTP-date. The date form is
    what OpenAI-compatible gateways answer with when they mean "until the quota
    window rolls over", and treating it as unparseable would silently retry
    early, which is exactly the hammering this function exists to stop.
    """
    if not value:
        return None
    text = str(value).strip()
    match = _RETRY_AFTER_SECONDS.match(text)
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except ValueError:
            return None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = datetime.now(UTC) if now_s is None else now_s
    if isinstance(current, datetime):
        delta = (when - current).total_seconds()
    else:
        # A caller that injected a monotonic clock still gets the date form:
        # compare against the wall clock it is standing in for.
        delta = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, delta)


def backoff_delay(
    attempt: int,
    *,
    base: float = BACKOFF_BASE_S,
    cap: float = BACKOFF_CAP_S,
    ceiling: float | None = None,
) -> float:
    """Exponential ladder with equal jitter, clamped to `cap` then `ceiling`.

    `attempt` is 0-based. Equal jitter (`d/2 + U(0, d/2)`) rather than full
    jitter: full jitter can hand back ~0 and re-create the tight retry loop the
    provider is complaining about, while no jitter at all makes a fleet of
    workers retry in lockstep. The floor of d/2 keeps the backoff a backoff.
    """
    step = min(cap, base * (2 ** max(0, int(attempt))))
    delayed = step / 2.0 + _rng() * (step / 2.0)
    if ceiling is not None:
        delayed = min(delayed, max(0.0, float(ceiling)))
    return max(0.0, delayed)


class TimeBudget:
    """A wall-clock allowance one logical call may not outrun."""

    __slots__ = ("limit_s", "started_at")

    def __init__(self, limit_s: float, started_at: float | None = None) -> None:
        self.limit_s = max(0.0, float(limit_s))
        self.started_at = now() if started_at is None else float(started_at)

    @classmethod
    def start(cls, limit_s: float = CALL_BUDGET_S) -> TimeBudget:
        return cls(limit_s)

    def elapsed(self) -> float:
        return max(0.0, now() - self.started_at)

    def remaining(self) -> float:
        return max(0.0, self.limit_s - self.elapsed())

    def expired(self) -> bool:
        return now() >= self.started_at + self.limit_s


# --------------------------------------------------------- breaker ----------
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

#: Consecutive failed calls that trip a provider. The live log answers 6 437
#: 503s against 31 429s: the failure mode is not a blip but a whole catalogue
#: being busy for minutes. Three attempts is the most one call may spend
#: (`llm_max_retries`), so four consecutive failed *calls* is one full
#: allowance of patience for a transient error and nothing more.
BREAKER_FAILURE_THRESHOLD = 4
#: First cool-off. One minute is the free-tier per-model quota window the
#: existing per-model cooldown already assumes; going below it re-opens onto a
#: window that has not rolled over yet.
BREAKER_COOLDOWN_S = 60.0
#: Ceiling for the doubling cool-off. A quarter of `cycle_stale_min` (45 min =
#: 2 700 s) is 15 min, and `watchdog_interval_min` is 5 min, so a provider that
#: stays down is probed at least twice per cycle instead of either being
#: hammered all day or being given up on entirely.
BREAKER_MAX_COOLDOWN_S = 900.0


class CircuitBreaker:
    """Per-provider key: stop calling after N failures, probe one call later.

    In-process on purpose. Persisting this across restarts would make a test
    run inherit yesterday's open breaker, and the state is cheap to relearn:
    the first failed call after a restart costs one request.

    `allow()` is the only gate. A key in `open` refuses; a key whose cool-off
    has elapsed moves to `half_open` and lets exactly one probe through, so a
    recovering provider is re-admitted by evidence rather than by a timer.
    """

    def __init__(
        self,
        *,
        threshold: int = BREAKER_FAILURE_THRESHOLD,
        cooldown_s: float = BREAKER_COOLDOWN_S,
        max_cooldown_s: float = BREAKER_MAX_COOLDOWN_S,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self.max_cooldown_s = max(self.cooldown_s, float(max_cooldown_s))
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._until: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}
        self._probing: dict[str, bool] = {}
        self.trips = 0

    def state(self, key: str) -> str:
        until = self._until.get(key)
        if until is None:
            return CLOSED
        if now() < until:
            return OPEN
        return HALF_OPEN

    def allow(self, key: str) -> bool:
        """True when a real request may be made for `key` right now.

        Half-open grants exactly one in-flight probe. That is not a detail: a
        half-open state that let a whole stage through would rediscover the
        outage twelve models at a time, which is the behaviour we are here to
        remove.
        """
        state = self.state(key)
        if state == CLOSED:
            return True
        if state == OPEN:
            return False
        if self._probing.get(key):
            return False
        self._probing[key] = True
        return True

    def release_probe(self, key: str) -> None:
        """Hand the probe slot back when the call never happened.

        A granted probe whose provider turned out unavailable (`available()`
        said no) would otherwise stay granted forever — a breaker that opens
        and never closes again, failing in the worse direction.
        """
        if self.state(key) == HALF_OPEN:
            self._probing.pop(key, None)

    def on_failure(self, key: str) -> bool:
        """Record a failed call. Returns True when this failure tripped it."""
        self._failures[key] = self._failures.get(key, 0) + 1
        self._probing.pop(key, None)
        if self._failures[key] < self.threshold:
            return False
        self.trips += 1
        current = self._failures[key] - self.threshold
        self._cooldown[key] = min(
            self.max_cooldown_s, self.cooldown_s * (2**max(0, current))
        )
        self._opened_at[key] = now()
        self._until[key] = now() + self._cooldown[key]
        return True

    def on_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._until.pop(key, None)
        self._opened_at.pop(key, None)
        self._cooldown.pop(key, None)
        self._probing.pop(key, None)

    def retry_in(self, key: str) -> float:
        """Seconds until `key` may be probed again; 0.0 when it is not open."""
        until = self._until.get(key)
        if until is None:
            return 0.0
        return max(0.0, until - now())

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key in set(self._failures) | set(self._until):
            out[key] = {
                "state": self.state(key),
                "failures": int(self._failures.get(key, 0)),
                "retry_in_s": round(self.retry_in(key), 3),
                "cooldown_s": float(self._cooldown.get(key, 0.0)),
            }
        return out

    def reset(self) -> None:
        self._failures.clear()
        self._until.clear()
        self._opened_at.clear()
        self._cooldown.clear()
        self._probing.clear()
        self.trips = 0


# ---------------------------------------------------------- budget ----------
#: One full sweep of the live `LLM_MODEL` catalogue is 12 candidates. That is
#: the default per-stage allowance for provider-level attempts: a stage may
#: prove every configured model unusable once, and then it must stop paying for
#: the proof. Not a new knob to remember — it is read from the model list.
STAGE_ATTEMPT_FLOOR = 4
MAX_STAGE_ATTEMPTS = 24

#: In-process projection of the online-call ledger, per UTC day.
#:
#: This deliberately is not a second authority. `llm/policy.py` + `ai_daily_budget`
#: remain the only thing that *charges* a call, and it charges only calls that
#: produced text (`finalize(used=provider != "offline")`). A doomed attempt is
#: never charged, which is precisely why the live log shows 8 506 offline
#: fallbacks against a 2 000/day budget that never filled. This counter is the
#: missing half of the same ledger: attempts spent. The allowance is the
#: *smaller* of the stage cap and the operator's own daily figure, so the one
#: knob an operator sets (`AI_DAILY_BUDGET`) bounds both halves and raising or
#: lowering it moves both together.
class AttemptBudget:
    def __init__(self) -> None:
        self._spent: dict[str, int] = {}
        self._day = self._today()
        self._stage_denied = 0

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y%m%d")

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._spent.clear()
            self._stage_denied = 0

    def stage_allowance(self, stage: str, *, daily_budget: int, model_count: int = 12) -> int:
        """Attempts this stage may still spend right now.

        Two ceilings compose, and the smaller wins: this stage's own headroom
        and the operator's `ai_daily_budget` minus everything already spent
        today. Same unit for both — one online call attempt — so there is one
        ledger with two views, not a second budget an operator has to remember.
        """
        self._roll()
        cap = min(MAX_STAGE_ATTEMPTS, max(STAGE_ATTEMPT_FLOOR, int(model_count)))
        stage_left = cap - self._spent.get(_stage_key(stage), 0)
        day_left = max(0, int(daily_budget)) - sum(self._spent.values())
        return max(0, min(stage_left, day_left))

    def try_spend(self, stage: str, *, daily_budget: int, model_count: int = 12) -> bool:
        """Consume one attempt. False means the caller must take the honest path."""
        self._roll()
        if self.stage_allowance(stage, daily_budget=daily_budget, model_count=model_count) <= 0:
            self._stage_denied += 1
            return False
        self._spent[_stage_key(stage)] = self._spent.get(_stage_key(stage), 0) + 1
        return True

    def spent(self, stage: str) -> int:
        self._roll()
        return int(self._spent.get(_stage_key(stage), 0))

    def spent_all(self) -> int:
        """Attempts spent today across every stage: the day-level view."""
        self._roll()
        return int(sum(self._spent.values()))

    def denied(self) -> int:
        return int(self._stage_denied)

    def snapshot(self) -> dict[str, Any]:
        self._roll()
        return {"day": self._day, "spent": dict(self._spent), "refusals": self._stage_denied}

    def reset(self) -> None:
        self._spent.clear()
        self._stage_denied = 0
        self._day = self._today()


def _stage_key(stage: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(stage or "unknown"))
    return cleaned[:48] or "unknown"


#: Process-wide state. In-process by design (see `CircuitBreaker`), and the
#: only place the breaker/budget singletons live so a test can reset all of it.
BREAKER = CircuitBreaker()
BUDGET = AttemptBudget()


def reset_resilience_state() -> None:
    """Drop breaker and budget state. Called by tests at file boundaries."""
    BREAKER.reset()
    BUDGET.reset()
    set_clock(None)
    set_sleeper(None)
    set_rng(None)

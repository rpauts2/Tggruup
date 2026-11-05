"""The consumer the editorial plan never had: build it, then act on its decisions.

``tgfarm.editorial_plan`` builds and persists a plan, and until this module
landed its only callers were three HTTP endpoints — so a live database held zero
plan rows, and the ``slot_at`` the planner computed for a gated draft never
reached ``Post.scheduled_at``. The decision died in the plan table. Behind
``Settings.plan_execution_enabled`` (off by default, so base behaviour is
untouched) this module is the missing consumer:

* :func:`build_plan_job` refreshes the persisted plan on the scheduler's clock —
  no HTTP call, no panel click.
* :func:`apply_plan` reads the ACTIVE plan and turns a qualifying ``schedule``
  entry into a real queued post through the existing scheduling path. It never
  assigns ``Post.status``/``Post.scheduled_at`` itself, never generates, never
  calls a model and never publishes: a handover only moves a post ``gated`` →
  ``scheduled``, which the publisher's own gates, mode and cadence still govern.

Why ``schedule_ready_posts`` is not the handover function
---------------------------------------------------------
It is the fleet scheduler: it selects the oldest gated posts across every channel
and re-derives eligibility from ``config/personas.yaml`` and a UTC day, so it
knows nothing about ``Channel.enabled``, ``Channel.timezone``,
``Channel.posts_per_day`` or the capability matrix. A per-entry decision made
here — hold this one, its channel is disabled, its local day is at cap — cannot
be enforced through a batch call that re-decides on its own terms: it would
publish a post this module just reported as held. Enforcing the plan honestly
needs a *per-post* transition, and ``s7_publish.unpublish`` is the only one at
base that is not a hand-written column write.

The trade-off is named plainly, because it is the whole design: ``unpublish``
stamps ``scheduled_at = now``, so an entry is handed over only once its planned
slot is due *and* ``now`` sits inside the channel's legal local hours. A slot in
an illegal local hour is not rewritten — it waits for the window to open and is
picked up by a later tick, which is the shift this module is allowed to make
without touching the schema.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from tgfarm import s7_publish
from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import ensure_utc, to_thread
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial_plan import build_editorial_plan, get_active_plan
from tgfarm.platform_capability import capability_of

log = get_logger(__name__)

#: Job ids the pipeline registers, and the stage names the journal is written
#: under. Named here so a registry test can read the same strings it asserts.
PLAN_BUILD_JOB_ID = "editorial-plan-build"
PLAN_APPLY_JOB_ID = "editorial-plan-apply"
STAGE_BUILD = "editorial_plan_build"
STAGE_APPLY = "editorial_plan_apply"
BUILD_INTERVAL_HOURS = 6
APPLY_INTERVAL_MINUTES = 10

#: Entries inspected per run. A plan snapshot holds at most `MAX_ENTRIES`
#: (40) rows, so this is the whole plan, not a slice of it.
MAX_ENTRIES_PER_RUN = 40

# ------------------------------------------------------------------ reason codes
#: Every refusal names itself, because `applied: 0` cannot tell a healthy queue
#: from a stall — the same rule `s7_publish.empty_gate_report` exists for (P34).
REASON_DISABLED = "plan_execution_disabled"
REASON_NO_PLAN = "no_active_plan"
REASON_PLAN_HOLD = "plan_action_hold"
REASON_NO_GENERATION = "no_generation_entry_point"
REASON_UNKNOWN_ACTION = "unknown_action"
REASON_POST_MISSING = "gated_post_missing"
REASON_ALREADY_SCHEDULED = "already_scheduled"
REASON_POST_NOT_GATED = "post_not_gated"
REASON_CHANNEL_MISSING = "channel_missing"
REASON_CHANNEL_DISABLED = "channel_disabled"
REASON_CAPABILITY = "capability_not_publishable"
REASON_DAILY_CAP = "channel_daily_cap"
REASON_PLATFORM_CAP = "platform_daily_cap"
REASON_BUDGET = "ai_daily_budget_exhausted"
REASON_NO_SLOT = "no_slot_planned"
REASON_SLOT_NOT_DUE = "slot_not_due"
REASON_LOCAL_HOURS = "slot_outside_local_hours"
REASON_HANDOVER_REFUSED = "handover_refused"

_OUTCOME_SCHEDULED = "scheduled"
_OUTCOME_HELD = "held"
_OUTCOME_SKIPPED = "skipped"


@dataclass
class _Target:
    """The row an entry would move, or why there is nothing to move."""

    post: Post | None = None
    channel: Channel | None = None
    reason: str | None = None


def _empty_result(*, flag_enabled: bool) -> dict[str, Any]:
    return {
        "flag_enabled": flag_enabled,
        "plan_id": None,
        "applied": 0,
        "held": 0,
        "no_plan": False,
        "reasons": {},
        "entries": [],
    }


def _count(reasons: dict[str, int], code: str) -> None:
    reasons[code] = reasons.get(code, 0) + 1


# ------------------------------------------------------------------- zone helpers
def _zone(timezone_name: str | None):
    """The channel's zone, or UTC when the stored name cannot be resolved.

    A bad tz string is an operator-fixable row, but it must not freeze the whole
    portfolio, so the day window falls back to UTC — the same reading the plan
    itself already used when it computed `slot_at`.
    """
    name = str(timezone_name or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("plan: channel timezone %r is unknown, using UTC", name[:64])
    return UTC


def _local_day_window(moment: datetime, timezone_name: str | None) -> tuple[datetime, datetime]:
    """The UTC instants that bound `moment`'s day in the channel's own timezone."""
    zone = _zone(timezone_name)
    local = moment.astimezone(zone)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC), (start_local + timedelta(days=1)).astimezone(UTC)


def _legal_local_hours(config) -> set[int] | None:
    """Hours of the day a post may land in, in the channel's local time.

    Read from the cadence owner, never copied: `interval` mode uses its
    `active_hours` window (24/7 when unset, which is what `_inside_active_hours`
    treats it as), `weights` mode uses the hours the measured distribution gives
    a non-zero weight — the dead zone that `plan_slot` refuses to sample.
    """
    cadence = config.schedule.cadence or {}
    if str(cadence.get("mode") or "weights").lower() == "interval":
        window = cadence.get("active_hours")
        if not window:
            return None
        try:
            start, end = int(window[0]), int(window[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not 0 <= start <= 23 or not 0 <= end <= 24:
            return None
        if end > start:
            return set(range(start, end))
        return set(range(start, 24)) | set(range(0, end))
    hours = {
        hour
        for hour in config.schedule.hours()
        if config.schedule.hour_probability(hour) > 0
    }
    return hours or None


def _slot_at(value: Any) -> datetime | None:
    """The plan's `slot_at` as an aware UTC datetime (`_plan_dict` returns ISO)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


# ---------------------------------------------------------------------- the plan
def build_plan_job(*, now: datetime | None = None) -> dict[str, Any]:
    """Refresh the persisted editorial plan; returns the plan dict it wrote.

    `build_editorial_plan(persist=True)` owns every part of that: the previous
    snapshot becomes `superseded` history, the new one becomes `active`.
    """
    plan = build_editorial_plan(now=ensure_utc(now), persist=True)
    summary = plan.get("summary") or {}
    log.info(
        "plan: rebuilt %s (total=%s schedule=%s produce=%s hold=%s)",
        plan.get("public_id"),
        summary.get("total"),
        summary.get("schedule"),
        summary.get("produce"),
        summary.get("hold"),
    )
    return plan


def _find_target(session, cluster_public_id: str) -> _Target:
    """The gated post for a planned topic, or the named reason there is none.

    The plan keys entries by `cluster_public_id`, so that is the join. A newest
    row that is *not* gated is reported as such instead of being counted again:
    a second `apply_plan` must not re-queue a post the first one already moved.
    """
    gated = session.execute(
        select(Post, Channel)
        .join(Channel, Post.channel_id == Channel.id)
        .where(
            Post.cluster_public_id == cluster_public_id,
            Post.status == s7_publish.STATUS_GATED,
        )
        .order_by(Post.created_at.desc())
        .limit(1)
    ).first()
    if gated is not None:
        return _Target(post=gated[0], channel=gated[1])

    newest = session.execute(
        select(Post, Channel)
        .join(Channel, Post.channel_id == Channel.id)
        .where(Post.cluster_public_id == cluster_public_id)
        .order_by(Post.created_at.desc())
        .limit(1)
    ).first()
    if newest is None:
        return _Target(reason=REASON_POST_MISSING)
    if newest[0].status in (
        s7_publish.STATUS_SCHEDULED,
        s7_publish.STATUS_PUBLISHING,
        s7_publish.STATUS_PUBLISHED,
    ):
        return _Target(channel=newest[1], reason=REASON_ALREADY_SCHEDULED)
    return _Target(channel=newest[1], reason=REASON_POST_NOT_GATED)


def _day_count(session, channel_ids: list[str], window: tuple[datetime, datetime]) -> int:
    """Posts of these channels already committed to this local day."""
    if not channel_ids:
        return 0
    bucket = func.coalesce(Post.published_at, Post.scheduled_at, Post.created_at)
    return int(
        session.execute(
            select(func.count())
            .select_from(Post)
            .where(
                Post.channel_id.in_(channel_ids),
                Post.status.in_(
                    (
                        s7_publish.STATUS_SCHEDULED,
                        s7_publish.STATUS_PUBLISHING,
                        s7_publish.STATUS_PUBLISHED,
                    )
                ),
                bucket >= window[0],
                bucket < window[1],
            )
        ).scalar_one()
    )


@dataclass
class _RunState:
    """Per-run caches: the limits are read once, then decremented as we hand over.

    Without the in-run `pending` counters, two entries on one channel could each
    pass a cap that only one slot still had room for.
    """

    moment: datetime
    settings: Any
    config: Any
    pending_channel: dict[str, int]
    pending_platform: dict[str, int]
    day_cache: dict[str, int]
    platform_cache: dict[str, int]

    def platform_enabled(self, platform: str) -> bool:
        from tgfarm.platforms import is_platform_enabled

        return bool(is_platform_enabled(platform))

    def local_day(self, channel: Channel) -> tuple[datetime, datetime]:
        return _local_day_window(self.moment, channel.timezone)

    def channel_day_count(self, session, channel: Channel) -> int:
        key = channel.id
        if key not in self.day_cache:
            self.day_cache[key] = _day_count(session, [key], self.local_day(channel))
        return self.day_cache[key]

    def platform_day_count(self, session, channel: Channel, platform: str) -> int:
        if platform not in self.platform_cache:
            ids = session.execute(
                select(Channel.id).where(
                    Channel.platform == platform, Channel.enabled.is_(True)
                )
            ).scalars().all()
            self.platform_cache[platform] = _day_count(session, list(ids), self.local_day(channel))
        return self.platform_cache[platform]


def _decide_schedule(
    session, state: _RunState, entry: dict[str, Any], target: _Target
) -> tuple[str, str]:
    """One `schedule` entry → (outcome, reason code). Never writes anything.

    The five limits the acceptance list names — channel limit, channel local
    time, platform capability, the provenance gate, the daily AI budget — each
    hold here with their own code, and each reuses its owner's constant when the
    owner already names the same fact.
    """
    if target.reason is not None:
        if target.reason == REASON_ALREADY_SCHEDULED:
            return _OUTCOME_SKIPPED, REASON_ALREADY_SCHEDULED
        return _OUTCOME_SKIPPED, target.reason

    post, channel = target.post, target.channel
    if channel is None:
        return _OUTCOME_SKIPPED, REASON_CHANNEL_MISSING

    platform = str(channel.platform or "telegram").lower()
    if not channel.enabled:
        return _OUTCOME_HELD, REASON_CHANNEL_DISABLED
    if capability_of(platform) != "publish":
        # Named before the operator's switch on purpose: a destination the farm
        # cannot deliver without a human (`draft_only`, `manual_upload`) is the
        # harder fact, and switching it off would not make the queueing safe.
        return _OUTCOME_HELD, REASON_CAPABILITY
    if not state.platform_enabled(platform):
        return _OUTCOME_HELD, s7_publish.GATE_PLATFORM_DISABLED
    if platform not in s7_publish.DELIVERABLE_PLATFORMS:
        # `due_posts` refuses it on every tick, so queueing it would be a rot
        # the plan caused; an adapter that crosspost owns is not the primary
        # queue, however capable the destination is.
        return _OUTCOME_HELD, s7_publish.GATE_PLATFORM_NOT_DELIVERABLE
    if state.settings.approval_required:
        # Human-in-the-loop mode: nothing may leave the gated state automatically,
        # whatever the plan decided.
        return _OUTCOME_HELD, s7_publish.GATE_APPROVAL_REQUIRED
    if (
        platform == "dzen"
        and getattr(state.settings, "dzen_requires_llm", False)
        and str(post.generation_provider or "") == "offline"
    ):
        # Same gate, same reason string as `schedule_ready_posts` and `review`.
        return _OUTCOME_HELD, s7_publish.GATE_DZEN_NEEDS_ONLINE_MODEL

    from tgfarm.distribution import clamp_channel_daily_cap
    from tgfarm.llm import policy

    budget = max(0, int(getattr(state.settings, "ai_daily_budget", 0) or 0))
    if policy.charged_today(state.moment) >= budget:
        return _OUTCOME_HELD, REASON_BUDGET

    cap = clamp_channel_daily_cap(max(1, int(channel.posts_per_day or 1)))
    used = state.channel_day_count(session, channel) + state.pending_channel.get(channel.id, 0)
    if used >= cap:
        return _OUTCOME_HELD, REASON_DAILY_CAP
    if platform in s7_publish.PRIMARY_PLATFORM_DAILY_CAPS:
        platform_cap = clamp_channel_daily_cap(s7_publish.PRIMARY_PLATFORM_DAILY_CAPS[platform])
        platform_used = (
            state.platform_day_count(session, channel, platform)
            + state.pending_platform.get(platform, 0)
        )
        if platform_used >= platform_cap:
            return _OUTCOME_HELD, REASON_PLATFORM_CAP

    slot = _slot_at(entry.get("slot_at"))
    if slot is None:
        # The planner returned a `schedule` decision with an empty horizon: the
        # slot it meant does not exist, so there is nothing to queue against.
        return _OUTCOME_HELD, REASON_NO_SLOT
    if slot > state.moment:
        return _OUTCOME_HELD, REASON_SLOT_NOT_DUE
    allowed = _legal_local_hours(state.config)
    if allowed is not None:
        zone = _zone(channel.timezone)
        # `unpublish` stamps `now`, so `now` is the hour this entry actually
        # claims and the only one this check can honestly gate. A `slot_at` in an
        # illegal local hour is not rewritten — that would be a column write — it
        # waits for the window to open and a later tick hands it over, which is
        # the shift this module is allowed to make. Checking the plan's own hour
        # as well would stall such an entry forever instead.
        if state.moment.astimezone(zone).hour not in allowed:
            return _OUTCOME_HELD, REASON_LOCAL_HOURS

    state.pending_channel[channel.id] = state.pending_channel.get(channel.id, 0) + 1
    if platform in s7_publish.PRIMARY_PLATFORM_DAILY_CAPS:
        state.pending_platform[platform] = state.pending_platform.get(platform, 0) + 1
    return _OUTCOME_SCHEDULED, ""


def apply_plan(*, now: datetime | None = None, limit: int = MAX_ENTRIES_PER_RUN) -> dict[str, Any]:
    """Act on the ACTIVE plan: qualified `schedule` entries become queued posts.

    Read the plan, decide per entry behind the limits, then hand the survivors to
    the existing scheduling transition in one call and report what the database
    actually says afterwards — never what the intent was.
    """
    moment = ensure_utc(now) or datetime.now(UTC)
    settings = get_settings()
    result = _empty_result(flag_enabled=bool(getattr(settings, "plan_execution_enabled", False)))
    if not result["flag_enabled"]:
        _count(result["reasons"], REASON_DISABLED)
        log.info("plan: apply skipped — PLAN_EXECUTION_ENABLED is off")
        return result

    plan = get_active_plan()
    if plan is None:
        result["no_plan"] = True
        _count(result["reasons"], REASON_NO_PLAN)
        log.info("plan: no active plan — nothing was applied, and no zeros are reported")
        return result
    result["plan_id"] = plan.get("public_id")

    entries = list(plan.get("entries") or [])[: max(0, int(limit))]
    state = _RunState(
        moment=moment,
        settings=settings,
        config=get_config(),
        pending_channel={},
        pending_platform={},
        day_cache={},
        platform_cache={},
    )
    handovers: list[tuple[dict[str, Any], str]] = []

    with session_scope() as session:
        for entry in entries:
            cluster_id = str(entry.get("cluster_public_id") or "")
            action = str(entry.get("action") or "")
            if action == "hold":
                # The plan already decided; this module never touches the row.
                outcome, reason = _OUTCOME_SKIPPED, REASON_PLAN_HOLD
            elif action == "produce":
                outcome, reason = _OUTCOME_SKIPPED, REASON_NO_GENERATION
            elif action == "schedule":
                target = _find_target(session, cluster_id)
                outcome, reason = _decide_schedule(session, state, entry, target)
                if outcome == _OUTCOME_SCHEDULED:
                    handovers.append((entry, str(target.post.public_id)))
            else:
                outcome, reason = _OUTCOME_SKIPPED, REASON_UNKNOWN_ACTION

            if outcome != _OUTCOME_SCHEDULED:
                _count(result["reasons"], reason)
                result["entries"].append(
                    {
                        "cluster_public_id": cluster_id,
                        "action": action,
                        "outcome": outcome,
                        "reason": reason,
                    }
                )
                if outcome == _OUTCOME_HELD:
                    result["held"] += 1

    if handovers:
        _hand_over(result, handovers)

    log.info(
        "plan: applied %d held %d (plan=%s, reasons=%s)",
        result["applied"],
        result["held"],
        result["plan_id"],
        dict(result["reasons"]),
    )
    return result


def _hand_over(result: dict[str, Any], handovers: list[tuple[dict[str, Any], str]]) -> None:
    """The one write this module makes, and it is not a column write.

    `s7_publish.unpublish` is the scheduling path: one conditional UPDATE that
    moves `gated` → `scheduled` with the same claim discipline the publish worker
    relies on. Each entry is then reported from the row as it now reads, so a
    handover that raced with another worker is counted as the other worker's,
    not as ours.
    """
    s7_publish.unpublish([public_id for _, public_id in handovers])
    with session_scope() as session:
        for entry, public_id in handovers:
            status = session.execute(
                select(Post.status).where(Post.public_id == public_id)
            ).scalar_one_or_none()
            if status == s7_publish.STATUS_SCHEDULED:
                result["applied"] += 1
                result["entries"].append(
                    {
                        "cluster_public_id": str(entry.get("cluster_public_id") or ""),
                        "action": "schedule",
                        "outcome": _OUTCOME_SCHEDULED,
                        "reason": "",
                    }
                )
                continue
            reason = REASON_ALREADY_SCHEDULED if status else REASON_HANDOVER_REFUSED
            _count(result["reasons"], reason)
            result["held"] += 1
            result["entries"].append(
                {
                    "cluster_public_id": str(entry.get("cluster_public_id") or ""),
                    "action": "schedule",
                    "outcome": _OUTCOME_HELD,
                    "reason": reason,
                }
            )


# ------------------------------------------------------------------- journaling
def _record(stage: str, status: str, items_in: int | None, items_out: int | None,
            duration_ms: int, meta: dict[str, Any], error: str | None = None) -> None:
    """Journal one tick with the pipeline's own writer.

    Imported lazily and deliberately: `pipeline` imports this module to register
    its jobs, and `_record` is the owner of the three-state counter rule
    (`None` is stored as 0 with the truth written beside it in `meta`), which is
    the thing P34 was filed for. Telemetry must never break a stage, so the
    writer swallows its own failures.
    """
    from tgfarm.pipeline import _record as pipeline_record

    pipeline_record(
        f"plan-{uuid.uuid4().hex[:8]}",
        stage,
        status,
        items_in=items_in,
        items_out=items_out,
        duration_ms=duration_ms,
        error=error,
        meta=meta,
    )


async def build_plan_tick(*, now: datetime | None = None) -> dict[str, Any]:
    """Scheduled plan refresh. Off flag ⇒ the job is not registered at all, and
    a flag flipped between registration and this tick still does nothing."""
    if not getattr(get_settings(), "plan_execution_enabled", False):
        return {"skipped": REASON_DISABLED}
    started = time.monotonic()
    try:
        plan = await to_thread(build_plan_job, now=now)
    except Exception as exc:  # noqa: BLE001 - a plan failure must not kill the scheduler
        message = f"{type(exc).__name__}: {exc}"
        log.warning("plan build tick failed: %s", message[:200])
        _record(
            STAGE_BUILD,
            "error",
            None,
            None,
            int((time.monotonic() - started) * 1000),
            {"job": PLAN_BUILD_JOB_ID},
            error=message,
        )
        return {"error": message}
    summary = plan.get("summary") or {}
    _record(
        STAGE_BUILD,
        "ok",
        None,
        int(summary.get("total") or 0),
        int((time.monotonic() - started) * 1000),
        {
            "job": PLAN_BUILD_JOB_ID,
            "plan_id": plan.get("public_id"),
            "schedule": summary.get("schedule"),
            "produce": summary.get("produce"),
            "hold": summary.get("hold"),
        },
    )
    return plan


async def apply_plan_tick(
    *, now: datetime | None = None, limit: int = MAX_ENTRIES_PER_RUN
) -> dict[str, Any]:
    """Turn the ACTIVE plan's decisions into queued posts, and journal the truth.

    A run with no plan is journalled as `skipped` with unmeasured counters: two
    zeros would read as "scanned nothing, queued nothing", which is a different
    fact from "there was no plan to read".
    """
    if not getattr(get_settings(), "plan_execution_enabled", False):
        return {"skipped": REASON_DISABLED}
    started = time.monotonic()
    try:
        report = await to_thread(apply_plan, now=now, limit=limit)
    except Exception as exc:  # noqa: BLE001 - a plan failure must not kill the scheduler
        message = f"{type(exc).__name__}: {exc}"
        log.warning("plan apply tick failed: %s", message[:200])
        _record(
            STAGE_APPLY,
            "error",
            None,
            None,
            int((time.monotonic() - started) * 1000),
            {"job": PLAN_APPLY_JOB_ID},
            error=message,
        )
        return {"error": message}
    scanned = len(report["entries"])
    _record(
        STAGE_APPLY,
        "skipped" if report["no_plan"] else "ok",
        None if report["no_plan"] else scanned,
        None if report["no_plan"] else int(report["applied"]),
        int((time.monotonic() - started) * 1000),
        {
            "job": PLAN_APPLY_JOB_ID,
            "plan_id": report["plan_id"],
            "held": report["held"],
            "reasons": dict(report["reasons"]),
        },
    )
    return report


__all__ = [
    "APPLY_INTERVAL_MINUTES",
    "BUILD_INTERVAL_HOURS",
    "MAX_ENTRIES_PER_RUN",
    "PLAN_APPLY_JOB_ID",
    "PLAN_BUILD_JOB_ID",
    "REASON_ALREADY_SCHEDULED",
    "REASON_BUDGET",
    "REASON_CAPABILITY",
    "REASON_CHANNEL_DISABLED",
    "REASON_CHANNEL_MISSING",
    "REASON_DAILY_CAP",
    "REASON_DISABLED",
    "REASON_HANDOVER_REFUSED",
    "REASON_LOCAL_HOURS",
    "REASON_NO_GENERATION",
    "REASON_NO_PLAN",
    "REASON_NO_SLOT",
    "REASON_PLAN_HOLD",
    "REASON_PLATFORM_CAP",
    "REASON_POST_MISSING",
    "REASON_POST_NOT_GATED",
    "REASON_SLOT_NOT_DUE",
    "REASON_UNKNOWN_ACTION",
    "STAGE_APPLY",
    "STAGE_BUILD",
    "apply_plan",
    "apply_plan_tick",
    "build_plan_job",
    "build_plan_tick",
]

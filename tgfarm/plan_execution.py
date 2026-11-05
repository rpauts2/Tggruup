"""The consumer the editorial plan never had: build it, then act on its decisions.

``tgfarm.editorial_plan`` builds and persists a plan, and until this module
landed its only callers were three HTTP endpoints — so a live database held zero
plan rows, and the ``slot_at`` the planner computed for a gated draft never
reached ``Post.scheduled_at``. The decision died in the plan table. Behind
``Settings.plan_execution_enabled`` (off by default, so base behaviour is
untouched) this module is the missing consumer:

* :func:`build_plan_job` refreshes the persisted plan on the scheduler's clock —
  no HTTP call, no panel click.
* :func:`generation_brief` is the reader S4 asks about a topic before it writes:
  the ACTIVE plan's `produce` booking — destination, format, slot, entry id — so
  the plan's decision survives the handoff into generation instead of being
  re-derived downstream and lost. Reading is all it does; this module still never
  generates and never calls a model.
* :func:`apply_plan` reads the ACTIVE plan and turns a qualifying ``schedule``
  entry into a real queued post through the existing scheduling path. It never
  assigns ``Post.status``/``Post.scheduled_at`` itself, never generates, never
  calls a model and never publishes: a handover only moves a post ``gated`` →
  ``scheduled``, which the publisher's own gates, mode and cadence still govern.

The instant a handover writes, and why it is the plan's
--------------------------------------------------------
An entry is released only once its planned slot is due, and since W21-B it is
released *at that slot*: ``unpublish`` takes the reserved minute as ``at`` and
stamps it as ``scheduled_at``, so ``posts.scheduled_at`` equals ``posts.plan_slot_at``
for a booked row and the booking-fidelity read has ``honoured`` to report instead
of a shift nobody asked for. Before that, ``unpublish`` could only say ``now``,
and the honest reading of the module's own design was that an entry's delivery
minute was this loop's tick, plus up to ``APPLY_INTERVAL_MINUTES`` of it — the
plan reserved a minute it never reached, and the panel said so.

The one limit that follows the stamped instant rather than the tick is the
channel's legal local hours: an entry whose reserved minute falls in an hour the
channel may not post in is now held under ``REASON_LOCAL_HOURS`` instead of being
queued at a later tick's hour. That is the same verdict ``schedule_ready_posts``
already returns for the same minute under the registered gate
``GATE_SLOT_OUTSIDE_LOCAL_HOURS``, through the same ``local_hours_allow`` — so the
two consumers of a booked slot still cannot disagree about which hours mean
anything, and a booking the cadence forbids stays a named refusal rather than
becoming a shift the plan never ordered. The planner only books minutes this
predicate admits (``editorial_plan`` asks it before reserving one), so the entries
that reach that hold are the config-drift ones: a cadence window narrowed after
the snapshot was built.

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

Dry run: the same verdicts, none of the writes
----------------------------------------------
``apply_plan(dry_run=True)`` (``evaluate_plan`` is the name the CLI uses) walks
the identical loop with the identical decision functions and withholds one thing:
the ``unpublish`` call. Nothing else branches. That is the whole guarantee, and
the reason it is worth having — the switch that lets the executor move rows also
hides what it would decide, so the only way to read the verdicts today was to
turn it on and restart the farm. A dry run therefore does *not* consult
``plan_execution_enabled``: the flag gates the writes, never the judgement, and a
report that answered ``plan_execution_disabled`` to every entry would tell an
operator nothing they could not already read off the ``.env``. The flag is still
reported, because the difference between "would queue three" and "would queue
three if it were allowed" is the decision the operator is about to make.
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
from tgfarm.db.models import Channel, EditorialPlan, EditorialPlanEntry, Post
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
REASON_NO_GENERATION = "produce_entry_not_generated_here"
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

#: A dry-run row is a prediction, not a result, so it says so in its own words:
#: `scheduled` would be read as "this happened". The three values map one-to-one
#: onto the outcomes above, and `tests/test_plan_execution_dry_run.py` pins that
#: a dry run run before a real one predicts exactly what the real one does.
VERDICT_WOULD_SCHEDULE = "would_schedule"
VERDICT_WOULD_HOLD = "would_hold"
VERDICT_WOULD_SKIP = "would_skip"
_VERDICTS = {
    _OUTCOME_SCHEDULED: VERDICT_WOULD_SCHEDULE,
    _OUTCOME_HELD: VERDICT_WOULD_HOLD,
    _OUTCOME_SKIPPED: VERDICT_WOULD_SKIP,
}


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


def _dry_run_extras(result: dict[str, Any], moment: datetime) -> dict[str, Any]:
    """Open a report that only judges. The keys are additive and only a dry run
    carries them, so the shape of every report the pipeline reads today is
    untouched.

    `applied` stays 0 and means 0 — nothing was applied. `would_schedule` is the
    number a real run at this same `judged_at` would hand over, and `judged_at`
    is stated because the verdict is a function of the clock: the slot-due and
    legal-local-hour checks read `now`, so a report without its instant would be
    an answer to a different question than the one asked.
    """
    result["dry_run"] = True
    result["judged_at"] = moment.isoformat()
    result["would_schedule"] = 0
    result["scanned"] = 0
    result["verdicts"] = {}
    return result


def _judged(entry: dict[str, Any], target: _Target | None) -> dict[str, Any]:
    """What the verdict below was made about, each field named by its source.

    The plan's own words (`plan_*`) and the row that was actually measured
    (`post_public_id`, `channel_*`) are kept apart on purpose: `_find_target`
    joins on `cluster_public_id`, so the channel whose cap, local day and
    timezone decided the verdict is the gated post's channel — which is not
    necessarily the `channel_id` the planner wrote down. `target_read` is false
    for an action that never reached a lookup (`hold`, `produce`, anything
    unknown), so its empty `post_public_id` reads as "not looked at" instead of
    "looked and found nothing".
    """
    channel = target.channel if target is not None else None
    post = target.post if target is not None else None
    return {
        "plan_channel_id": entry.get("channel_id"),
        "plan_platform": entry.get("platform"),
        "plan_format": entry.get("format"),
        "plan_slot_at": entry.get("slot_at"),
        "target_read": target is not None,
        "post_public_id": str(post.public_id) if post is not None else None,
        "channel_id": str(channel.id) if channel is not None else None,
        "channel_platform": str(channel.platform) if channel is not None else None,
        "channel_timezone": channel.timezone if channel is not None else None,
    }


def _entry_row(
    entry: dict[str, Any],
    cluster_id: str,
    action: str,
    outcome: str,
    reason: str,
    target: _Target | None,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """One entry as the report states it: the real shape, or its prediction."""
    if not dry_run:
        return {
            "cluster_public_id": cluster_id,
            "action": action,
            "outcome": outcome,
            "reason": reason,
        }
    return {
        "cluster_public_id": cluster_id,
        "action": action,
        "verdict": _VERDICTS.get(outcome, outcome),
        "reason": reason,
        "judged": _judged(entry, target),
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


def local_hours_allow(
    instant: datetime,
    timezone_name: str | None,
    *,
    config: Any = None,
) -> bool:
    """True when `instant`, seen in the channel's own time, is a publishable hour.

    One implementation of the window, deliberately reachable from outside this
    module: `schedule_ready_posts` has to judge a *booked* slot (`Post.plan_slot_at`)
    against exactly the hours `_decide_schedule` judges the very minute it stamps as
    `scheduled_at` against, and a second copy of the cadence reading is how two
    schedulers start disagreeing about one plan. `None` — 24/7 — allows every
    instant.
    """
    allowed = _legal_local_hours(config if config is not None else get_config())
    if allowed is None:
        return True
    return instant.astimezone(_zone(timezone_name)).hour in allowed


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


# ---------------------------------------------------------- the generation brief
@dataclass(frozen=True)
class GenerationBrief:
    """One `produce` booking, in the form the writer can obey.

    The fields are the plan's own words read straight off
    `editorial_plan_entries`, not a re-derivation: `channel_id` is the destination
    the entry said the post belongs to, `fmt` the shape it booked (a free-text
    column, so it may name a format S10 has no length budget for), `slot_at` the
    instant it reserved, and `entry_id` the row a produced post has to name as its
    origin. `plan_public_id` and `confidence` travel along so a log line — and
    later the panel — can say *which* snapshot decided, not only what it decided.
    """

    entry_id: int
    plan_public_id: str
    cluster_public_id: str
    channel_id: str
    platform: str
    fmt: str | None
    slot_at: datetime | None
    confidence: float | None


def generation_brief(cluster_public_id: str) -> GenerationBrief | None:
    """The ACTIVE plan's `produce` booking for one topic, or `None`.

    Off the flag this returns without opening a session, which is the whole point
    of putting the test here: the base path has no plan consumer at all, and even
    an unread row would make the two paths differ in what they touch. S4 calls
    this on every cluster and, on `None`, decides exactly as it did before the
    plan existed.

    The newest snapshot owns the topic — `get_active_plan`'s own rule, spelled as
    one bounded read here because the caller is a per-cluster stage and not the
    portfolio loop — and `entry_id` breaks a tie inside that snapshot so a topic
    cannot alternate between two of its rows from run to run. A `hold` or a
    `schedule` entry is not an order to write something, so neither is a brief.
    """
    if not getattr(get_settings(), "plan_execution_enabled", False):
        return None
    topic = str(cluster_public_id or "").strip()
    if not topic:
        return None
    with session_scope() as session:
        row = session.execute(
            select(
                EditorialPlanEntry.id,
                EditorialPlan.public_id,
                EditorialPlanEntry.channel_id,
                EditorialPlanEntry.platform,
                EditorialPlanEntry.format,
                EditorialPlanEntry.slot_at,
                EditorialPlanEntry.confidence,
            )
            .join(EditorialPlan, EditorialPlanEntry.plan_id == EditorialPlan.id)
            .where(
                EditorialPlan.status == "active",
                EditorialPlanEntry.cluster_public_id == topic,
                EditorialPlanEntry.action == "produce",
            )
            .order_by(EditorialPlan.created_at.desc(), EditorialPlanEntry.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    booked_format = str(row[4] or "").strip().lower() or None
    return GenerationBrief(
        entry_id=int(row[0]),
        plan_public_id=str(row[1]),
        cluster_public_id=topic,
        channel_id=str(row[2] or ""),
        platform=str(row[3] or ""),
        fmt=booked_format,
        slot_at=_slot_at(row[5]),
        confidence=float(row[6]) if row[6] is not None else None,
    )


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
) -> tuple[str, str, datetime | None]:
    """One `schedule` entry → (outcome, reason code, release instant). Never writes.

    The third value is the minute this verdict would stamp, and it is returned
    rather than re-derived by the caller so that the instant gated below and the
    instant handed to `s7_publish.unpublish` cannot be two different numbers. It
    is a `datetime` exactly when the outcome is `scheduled`, and `None` otherwise.

    The five limits the acceptance list names — channel limit, channel local
    time, platform capability, the provenance gate, the daily AI budget — each
    hold here with their own code, and each reuses its owner's constant when the
    owner already names the same fact.
    """
    if target.reason is not None:
        if target.reason == REASON_ALREADY_SCHEDULED:
            return _OUTCOME_SKIPPED, REASON_ALREADY_SCHEDULED, None
        return _OUTCOME_SKIPPED, target.reason, None

    post, channel = target.post, target.channel
    if channel is None:
        return _OUTCOME_SKIPPED, REASON_CHANNEL_MISSING, None

    platform = str(channel.platform or "telegram").lower()
    if not channel.enabled:
        return _OUTCOME_HELD, REASON_CHANNEL_DISABLED, None
    if capability_of(platform) != "publish":
        # Named before the operator's switch on purpose: a destination the farm
        # cannot deliver without a human (`draft_only`, `manual_upload`) is the
        # harder fact, and switching it off would not make the queueing safe.
        return _OUTCOME_HELD, REASON_CAPABILITY, None
    if not state.platform_enabled(platform):
        return _OUTCOME_HELD, s7_publish.GATE_PLATFORM_DISABLED, None
    if platform not in s7_publish.DELIVERABLE_PLATFORMS:
        # `due_posts` refuses it on every tick, so queueing it would be a rot
        # the plan caused; an adapter that crosspost owns is not the primary
        # queue, however capable the destination is.
        return _OUTCOME_HELD, s7_publish.GATE_PLATFORM_NOT_DELIVERABLE, None
    if state.settings.approval_required:
        # Human-in-the-loop mode: nothing may leave the gated state automatically,
        # whatever the plan decided.
        return _OUTCOME_HELD, s7_publish.GATE_APPROVAL_REQUIRED, None
    if (
        platform == "dzen"
        and getattr(state.settings, "dzen_requires_llm", False)
        and str(post.generation_provider or "") == "offline"
    ):
        # Same gate, same reason string as `schedule_ready_posts` and `review`.
        return _OUTCOME_HELD, s7_publish.GATE_DZEN_NEEDS_ONLINE_MODEL, None

    from tgfarm.distribution import clamp_channel_daily_cap
    from tgfarm.llm import policy

    budget = max(0, int(getattr(state.settings, "ai_daily_budget", 0) or 0))
    if policy.charged_today(state.moment) >= budget:
        return _OUTCOME_HELD, REASON_BUDGET, None

    cap = clamp_channel_daily_cap(max(1, int(channel.posts_per_day or 1)))
    used = state.channel_day_count(session, channel) + state.pending_channel.get(channel.id, 0)
    if used >= cap:
        return _OUTCOME_HELD, REASON_DAILY_CAP, None
    if platform in s7_publish.PRIMARY_PLATFORM_DAILY_CAPS:
        platform_cap = clamp_channel_daily_cap(s7_publish.PRIMARY_PLATFORM_DAILY_CAPS[platform])
        platform_used = (
            state.platform_day_count(session, channel, platform)
            + state.pending_platform.get(platform, 0)
        )
        if platform_used >= platform_cap:
            return _OUTCOME_HELD, REASON_PLATFORM_CAP, None

    slot = _slot_at(entry.get("slot_at"))
    if slot is None:
        # The planner returned a `schedule` decision with an empty horizon: the
        # slot it meant does not exist, so there is nothing to queue against.
        return _OUTCOME_HELD, REASON_NO_SLOT, None
    if slot > state.moment:
        return _OUTCOME_HELD, REASON_SLOT_NOT_DUE, None
    # Since W21-B the released row carries *this* instant: `_hand_over` passes
    # `slot` to `unpublish`, which stamps it as `scheduled_at`. So the minute an
    # entry claims is the minute the plan reserved, and the legal-local-hours
    # limit — whose whole job is to refuse a publication in an hour the channel
    # may not post in — has to be answered about the instant being stamped, not
    # about the tick that got there first. That is also exactly what the other
    # consumer of a booked minute does: `schedule_ready_posts` gates
    # `plan_slot_at` itself through this same `local_hours_allow` and refuses it
    # under `GATE_SLOT_OUTSIDE_LOCAL_HOURS` rather than moving it. One instant,
    # one predicate, two schedulers that cannot disagree.
    #
    # The verdict this replaces was a workaround for a transition that could only
    # say `now`: an entry whose reserved minute fell in an illegal hour was held
    # until a legal tick came and then queued at that tick's hour, which is a
    # shift the plan never ordered and W20-C had to report as `shifted`. Such an
    # entry is now refused by name instead, and stays refused until the plan
    # reserves it somewhere the channel may post — the config drift that produces
    # one (a cadence window narrowed after the snapshot was built) is exactly the
    # case the registered gate exists to make visible rather than deliver.
    if not local_hours_allow(slot, channel.timezone, config=state.config):
        return _OUTCOME_HELD, REASON_LOCAL_HOURS, None

    state.pending_channel[channel.id] = state.pending_channel.get(channel.id, 0) + 1
    if platform in s7_publish.PRIMARY_PLATFORM_DAILY_CAPS:
        state.pending_platform[platform] = state.pending_platform.get(platform, 0) + 1
    return _OUTCOME_SCHEDULED, "", slot


def apply_plan(
    *,
    now: datetime | None = None,
    limit: int = MAX_ENTRIES_PER_RUN,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Act on the ACTIVE plan: qualified `schedule` entries become queued posts.

    Read the plan, decide per entry behind the limits, then hand the survivors to
    the existing scheduling transition in one call and report what the database
    actually says afterwards — never what the intent was.

    `dry_run=True` keeps every step of that — the plan read, `_find_target`,
    `_decide_schedule`, the in-run cap counters — and skips the one write: no
    `unpublish`, no status change, no journal row, no plan rebuild. It is a
    parameter rather than a second implementation because a judgement that
    disagrees with the real run is worse than no judgement, and two copies of
    eleven guards drift apart by the twelfth. The flag is deliberately not
    consulted in that mode (it gates the writes, not the reading); the report
    still carries `flag_enabled`, because the operator is deciding right now
    whether to turn it on.
    """
    moment = ensure_utc(now) or datetime.now(UTC)
    settings = get_settings()
    flag_enabled = bool(getattr(settings, "plan_execution_enabled", False))
    result = _empty_result(flag_enabled=flag_enabled)
    if dry_run:
        _dry_run_extras(result, moment)
    if not flag_enabled and not dry_run:
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
    if dry_run:
        result["scanned"] = len(entries)
    state = _RunState(
        moment=moment,
        settings=settings,
        config=get_config(),
        pending_channel={},
        pending_platform={},
        day_cache={},
        platform_cache={},
    )
    handovers: list[tuple[dict[str, Any], str, datetime]] = []

    with session_scope() as session:
        for entry in entries:
            cluster_id = str(entry.get("cluster_public_id") or "")
            action = str(entry.get("action") or "")
            target: _Target | None = None
            if action == "hold":
                # The plan already decided; this module never touches the row.
                outcome, reason, release = _OUTCOME_SKIPPED, REASON_PLAN_HOLD, None
            elif action == "produce":
                # Writing the post is S4's job, not this loop's. The booking is
                # read per topic by `generation_brief`, so the honest statement is
                # "not generated here", not "nothing consumes this entry".
                outcome, reason, release = _OUTCOME_SKIPPED, REASON_NO_GENERATION, None
            elif action == "schedule":
                target = _find_target(session, cluster_id)
                outcome, reason, release = _decide_schedule(session, state, entry, target)
                if outcome == _OUTCOME_SCHEDULED and not dry_run:
                    # `release` is the entry's own reserved minute, already gated
                    # by `_decide_schedule` for due-ness and for the channel's
                    # legal local hours: it travels to the transition instead of
                    # the transition inventing a clock of its own.
                    handovers.append((entry, str(target.post.public_id), release))
            else:
                outcome, reason, release = _OUTCOME_SKIPPED, REASON_UNKNOWN_ACTION, None

            if outcome != _OUTCOME_SCHEDULED:
                _count(result["reasons"], reason)
                if outcome == _OUTCOME_HELD:
                    result["held"] += 1
                if dry_run:
                    _count(result["verdicts"], str(_VERDICTS.get(outcome, outcome)))
                result["entries"].append(
                    _entry_row(entry, cluster_id, action, outcome, reason, target, dry_run=dry_run)
                )
            elif dry_run:
                # The only difference from a real run, in both directions: this
                # entry would have been handed over, and here it is only counted.
                # `_decide_schedule` already spent the in-run cap for it, so the
                # dry run reserves slots exactly like the run it predicts.
                result["would_schedule"] += 1
                _count(result["verdicts"], VERDICT_WOULD_SCHEDULE)
                result["entries"].append(
                    _entry_row(entry, cluster_id, action, outcome, reason, target, dry_run=True)
                )

    if handovers:
        _hand_over(result, handovers)

    if dry_run:
        log.info(
            "plan: dry run judged %d would schedule %d held %d (plan=%s, flag=%s, reasons=%s)",
            result["scanned"],
            result["would_schedule"],
            result["held"],
            result["plan_id"],
            flag_enabled,
            dict(result["reasons"]),
        )
    else:
        log.info(
            "plan: applied %d held %d (plan=%s, reasons=%s)",
            result["applied"],
            result["held"],
            result["plan_id"],
            dict(result["reasons"]),
        )
    return result


def evaluate_plan(
    *, now: datetime | None = None, limit: int = MAX_ENTRIES_PER_RUN
) -> dict[str, Any]:
    """The verdicts `apply_plan` would reach, with the write withheld.

    A named entry point so a caller cannot ask for a dry run by remembering the
    keyword, and a thin one: it forwards to `apply_plan` instead of arranging its
    own copy of the loop, which is what makes the two agree by construction.
    """
    return apply_plan(now=now, limit=limit, dry_run=True)


def _hand_over(
    result: dict[str, Any], handovers: list[tuple[dict[str, Any], str, datetime]]
) -> None:
    """The one write this module makes, and it is not a column write.

    `s7_publish.unpublish` is the scheduling path: one conditional UPDATE per
    batch that moves `gated` → `scheduled` with the same claim discipline the
    publish worker relies on. The reserved minute goes to it as `at`, so what the
    row reads as `scheduled_at` is the instant the plan booked rather than the
    instant this loop happened to run — the difference between the two used to be
    one tick interval on every entry, which is what made `plan_execution` the one
    consumer that could never produce an honoured booking.

    Entries are batched by instant because each one carries its own: one UPDATE
    per distinct reserved minute keeps the conditional-UPDATE discipline intact
    instead of trading it for a hand-written column, which is the guarantee
    `tests/test_plan_execution_flag.py::test_the_module_never_writes_a_post_state_by_hand`
    pins.

    Each entry is then reported from the row as it now reads, so a handover that
    raced with another worker is counted as the other worker's, not as ours.
    """
    batches: dict[datetime, list[str]] = {}
    for _entry, public_id, release in handovers:
        batches.setdefault(release, []).append(public_id)
    for release, public_ids in batches.items():
        s7_publish.unpublish(public_ids, at=release)
    with session_scope() as session:
        for entry, public_id, _release in handovers:
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
    "GenerationBrief",
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
    "VERDICT_WOULD_HOLD",
    "VERDICT_WOULD_SCHEDULE",
    "VERDICT_WOULD_SKIP",
    "apply_plan",
    "apply_plan_tick",
    "build_plan_job",
    "build_plan_tick",
    "evaluate_plan",
    "generation_brief",
    "local_hours_allow",
]

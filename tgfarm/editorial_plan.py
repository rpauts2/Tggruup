"""Global editorial plan: a dynamic, revisable publication calendar.

The plan turns the ranked topic queue (``tgfarm.editorial``) plus gated
drafts into a portfolio-wide set of decisions with priority, confidence,
justification and a concrete slot per channel. Every refresh supersedes
the previous active snapshot but keeps it as decision history, so the
system never silently rewrites its past choices.

What is true at this commit: the plan is a *ledger, not a driver*. Nothing in
the pipeline consumes it — no generation, routing, scheduling or maintenance
stage imports this module, and ``build_editorial_plan`` is reached only from
the three HTTP endpoints in ``tgfarm.api`` (``GET /api/editorial/plan``,
``POST /api/editorial/plan/refresh``, ``GET /api/editorial/plan/history``).
An entry becomes a post only because some other flow happened to act on the
topic on its own; the plan never publishes anything and never will by itself.
Each entry therefore carries an ``outcome`` measured afterwards from real
posts (see ``_entry_dict``), and each plan carries ``coverage`` saying how
many of its decisions could be answered at all.

CONSUMER SEAM — the one marked place where a consumer may be recorded later.
``CONSUMER_MODULE`` below stays ``None`` until some module actually reads the
entries; when that happens it is set here *and* the paragraph above is
rewritten in the same commit, so this docstring can never again claim a flow
that does not exist.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from tgfarm.core.run import ensure_utc
from tgfarm.db.models import Channel, Cluster, CrosspostLog, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial import queue as editorial_queue

PLAN_HORIZON_DAYS = 7
SLOT_STEP_MINUTES = 90
SLOT_START_OFFSET_MINUTES = 30
STALE_HOURS = 96
MAX_ENTRIES = 40

# The consumer seam named in the module docstring: module path of whatever
# actually turns entries into work. None == "no consumer exists".
CONSUMER_MODULE: str | None = None

# A gated draft that has not moved for this many hours is a decision nobody
# picked up: the entry that planned to schedule it is reported as abandoned
# instead of waiting forever in "not_yet". Stated here because the number is
# a policy, not a measurement.
ABANDON_GATED_HOURS = 72

# Posts whose status means "this cluster was reached by a real publication
# step" for the purposes of the outcome measurement.
_MATERIALIZED_STATUSES = ("scheduled", "published")
_FAILED_STATUSES = ("rejected", "failed")
# Chunked IN lists: SQLite binds one variable per id and dies past its limit,
# and an enormous plan must not turn the read into a per-entry query.
_IN_CHUNK = 400


def _now() -> datetime:
    return datetime.now(UTC)


def _pick_channel(niche: str, channels: list[Channel], load: dict[int, int]) -> Channel | None:
    """Best enabled channel for a niche; fallback to the least loaded one.

    ``load`` counts entries already assigned in this build, so topics without
    a niche match spread across the portfolio instead of piling onto one
    channel.
    """

    if not channels:
        return None
    for channel in channels:
        if channel.niche and channel.niche == niche:
            return channel
    return min(channels, key=lambda c: load.get(c.id, 0))


def _next_free_slot(
    channel: Channel, taken: dict[int, set[datetime]], now: datetime, horizon_days: int
) -> datetime | None:
    """First free slot in the channel's schedule inside the horizon.

    Slots are spaced SLOT_STEP_MINUTES apart starting shortly after ``now``;
    slots already occupied by scheduled/published posts of this channel are
    skipped. Returns None when the horizon is full.
    """

    occupied = taken.get(channel.id, set())
    start = now + timedelta(minutes=SLOT_START_OFFSET_MINUTES)
    limit = now + timedelta(days=horizon_days)
    candidates = int(horizon_days * 24 * 60 / SLOT_STEP_MINUTES) + 2
    candidate = start
    for _ in range(candidates):
        key = candidate.replace(second=0, microsecond=0)
        if key not in occupied:
            return candidate.replace(second=0, microsecond=0)
        candidate = candidate + timedelta(minutes=SLOT_STEP_MINUTES)
        if candidate > limit:
            break
    return None


def _priority(rank: int, total: int, row: dict[str, Any]) -> int:
    """Queue-rank tiers: the hottest topic is always P1, breaking news is
    promoted one tier. Absolute tiers keep small portfolios meaningful."""
    level = 1 if rank == 1 else (2 if (rank - 1) * 10 <= (total - 1) * 6 else 3)
    # Fresh news is promoted one level (never demoted).
    if row.get("is_news") and row.get("age_hours", STALE_HOURS) < 24 and level > 1:
        level -= 1
    return level


def _confidence(row: dict[str, Any]) -> float:
    sources = min(int(row.get("sources") or 0), 4)
    freshness = max(0.0, 1.0 - float(row.get("age_hours") or 0.0) / STALE_HOURS)
    ai_quality = float(row.get("ai_quality") or 0.0)
    value = 0.30 + 0.08 * sources + 0.25 * ai_quality + 0.15 * freshness
    return round(max(0.05, min(0.95, value)), 2)


def _reasons(row: dict[str, Any], action: str, channel: Channel | None, slot: datetime | None) -> list[str]:
    parts = [
        f"температура {row.get('heat', 0):.1f}",
        f"источники: {row.get('sources', 0)}",
        f"возраст {int(row.get('age_hours') or 0)}ч",
    ]
    if row.get("ai_quality"):
        parts.append(f"качество AI {row['ai_quality']:.2f}")
    if action == "schedule" and channel is not None:
        parts.append(f"черновик готов для {channel.platform}:{channel.title}")
        if slot is not None:
            parts.append(f"слот свободен: {slot.strftime('%d.%m %H:%M')} UTC")
    elif action == "produce" and channel is not None:
        parts.append(f"направление {channel.platform}:{channel.title}")
    elif action == "hold":
        if row.get("cooling"):
            parts.append("тема недавно уже публиковалась — cooldown")
        else:
            parts.append(f"тема устарела ({int(row.get('age_hours') or 0)}ч)")
    return parts


def _iso(value: datetime | None) -> str | None:
    moment = ensure_utc(value)
    return moment.isoformat() if moment is not None else None


def _unknown_outcome(checked_at: datetime) -> dict[str, Any]:
    """The honest answer when the data cannot tell us anything.

    ``unknown`` is a state of its own and is never folded into the other three
    or into a zero: an entry nobody could check is not an entry that failed.
    """

    return {
        "state": "unknown",
        "post_status": None,
        "scheduled_at": None,
        "checked_at": _iso(checked_at),
    }


def _post_reached_at(post: Any) -> datetime | None:
    """When this post last entered a state a publication step writes."""

    return (
        ensure_utc(post.published_at)
        or ensure_utc(post.scheduled_at)
        or ensure_utc(post.updated_at)
        or ensure_utc(post.created_at)
    )


def _last_touched(post: Any) -> datetime | None:
    return ensure_utc(post.updated_at) or ensure_utc(post.created_at)


def _load_outcome_rows(
    session: Any, cluster_ids: list[str]
) -> tuple[dict[str, list], dict[str, Any]]:
    """One bounded read for the whole plan: its posts and its cluster rows.

    Exactly two statements per chunk of ids, never one per entry — this runs on
    every ``GET /api/editorial/plan``, so a per-entry query would be an N+1 on
    the endpoint the panel polls.
    """

    posts_by_cluster: dict[str, list] = defaultdict(list)
    clusters: dict[str, Any] = {}
    ids = sorted(set(cluster_ids))
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        for row in session.execute(
            select(
                Post.cluster_public_id,
                Post.channel_id,
                Post.status,
                Post.scheduled_at,
                Post.published_at,
                Post.created_at,
                Post.updated_at,
            ).where(Post.cluster_public_id.in_(chunk))
        ):
            if row.cluster_public_id:
                posts_by_cluster[str(row.cluster_public_id)].append(row)
        for row in session.execute(select(Cluster.public_id).where(Cluster.public_id.in_(chunk))):
            clusters[str(row[0])] = True
    return posts_by_cluster, clusters


def _classify_entry(
    entry: EditorialPlanEntry,
    posts: list,
    cluster_known: bool,
    plan_created_at: datetime,
    horizon_days: int,
    checked_at: datetime,
) -> dict[str, Any]:
    """What became of this decision, from post rows and cluster rows only.

    The question is the same for every action: did this cluster reach a real
    publication step after the plan was written. For ``produce`` and
    ``schedule`` "yes" means the decision was carried out (a ``schedule`` entry
    additionally has to land on the channel the plan picked, otherwise another
    flow published the topic and this plan did not); for ``hold`` the same
    answer means the hold did not stick. ``not_yet`` is only ever used inside
    the plan's own horizon — once the horizon closes with nothing happening the
    decision lapsed, which is a fact and not a waiting state.
    """

    def _outcome(state: str, post: Any = None) -> dict[str, Any]:
        return {
            "state": state,
            "post_status": str(post.status) if post is not None else None,
            "scheduled_at": _iso(post.scheduled_at) if post is not None else None,
            "checked_at": _iso(checked_at),
        }

    any_channel = entry.action != "schedule" or not entry.channel_id

    def _matches_channel(post: Any) -> bool:
        return any_channel or post.channel_id == entry.channel_id

    def _stamp(post: Any) -> datetime:
        return _post_reached_at(post) or plan_created_at

    reached = [
        post
        for post in posts
        if post.status in _MATERIALIZED_STATUSES
        and _matches_channel(post)
        and _stamp(post) >= plan_created_at
    ]
    if reached:
        return _outcome("materialized", max(reached, key=_stamp))

    failures = [
        post
        for post in posts
        if post.status in _FAILED_STATUSES and _matches_channel(post) and _stamp(post) >= plan_created_at
    ]
    if failures:
        return _outcome("abandoned", max(failures, key=_stamp))

    stalled = [
        post
        for post in posts
        if post.status == "gated"
        and _matches_channel(post)
        and checked_at - (_last_touched(post) or checked_at) >= timedelta(hours=ABANDON_GATED_HOURS)
    ]
    if stalled:
        oldest = min(stalled, key=lambda post: _last_touched(post) or checked_at)
        return _outcome("abandoned", oldest)

    if not cluster_known:
        # The topic disappeared from the queue while the plan still lists it:
        # nothing can materialise this entry any more.
        return _outcome("abandoned")

    horizon_end = plan_created_at + timedelta(days=max(1, int(horizon_days)))
    if checked_at <= horizon_end:
        return _outcome("not_yet")
    return _outcome("abandoned")


def _coverage(states: list[str], note: str) -> dict[str, Any]:
    materialized = states.count("materialized")
    not_yet = states.count("not_yet")
    abandoned = states.count("abandoned")
    unknown = states.count("unknown")
    return {
        "coverage": {
            # `measured` is the denominator the other three counts divide by;
            # `unknown` stays outside it on purpose, so a plan nobody checked
            # can never be read as "0 % done".
            "measured": materialized + not_yet + abandoned,
            "materialized": materialized,
            "not_yet": not_yet,
            "abandoned": abandoned,
            "unknown": unknown,
        },
        "coverage_note": note,
    }


def _measure_plan(
    session: Any,
    plan: EditorialPlan,
    entries: list[EditorialPlanEntry],
    *,
    now: datetime,
    persisted: bool = True,
) -> dict[str, Any]:
    """Attach a per-entry outcome plus plan coverage.

    Derived at read time and never stored: an ``EditorialPlanEntry`` is a
    snapshot of a decision, while the outcome describes what the rest of the
    system did afterwards, so persisting it would freeze an answer that keeps
    changing (and would need a migration for a column that is not a fact about
    the plan).
    """

    total = len(entries)
    # Callers hand us a wall clock that may be naive (a test, a passed-in
    # moment); SQLite always hands back naive timestamps, so normalise once.
    checked_at = ensure_utc(now) or _now()
    if not persisted:
        # A preview was never written down, so there is no decision history to
        # answer for: measured stays 0 and the note says "nothing ran".
        return {
            "outcomes": [_unknown_outcome(checked_at) for _ in entries],
            **_coverage(
                ["unknown"] * total,
                f"План не сохранён (это предпросмотр): из {total} решений не измерено ни одного — "
                "сверять нечего, это не провал плана.",
            ),
        }
    created_at = ensure_utc(plan.created_at)
    if created_at is None:
        return {
            "outcomes": [_unknown_outcome(checked_at) for _ in entries],
            **_coverage(
                ["unknown"] * total,
                f"У плана нет created_at: из {total} решений не измерено ни одного — "
                "время решения неизвестно, сравнивать не с чем.",
            ),
        }
    try:
        posts_by_cluster, clusters = _load_outcome_rows(
            session, [entry.cluster_public_id for entry in entries if entry.cluster_public_id]
        )
    except SQLAlchemyError:
        # The read failed. Say so in the note instead of reporting zeros, which
        # a reader would take for "the plan produced nothing".
        return {
            "outcomes": [_unknown_outcome(checked_at) for _ in entries],
            **_coverage(
                ["unknown"] * total,
                f"Чтение базы не удалось: из {total} решений не измерено ни одного. "
                "Это сбой проверки, а не нулевой результат плана.",
            ),
        }

    outcomes = [
        _classify_entry(
            entry,
            posts_by_cluster.get(entry.cluster_public_id, []),
            entry.cluster_public_id in clusters,
            created_at,
            int(plan.horizon_days or PLAN_HORIZON_DAYS),
            checked_at,
        )
        for entry in entries
    ]
    states = [str(outcome["state"]) for outcome in outcomes]
    measured = sum(1 for state in states if state != "unknown")
    if not total:
        note = "В плане нет ни одного решения — измерять нечего (0 решений в знаменателе)."
    elif measured == 0:
        note = (
            f"Из {total} решений плана не измерено ни одного: все остались без ответа "
            "(\u00abнеизвестно\u00bb), а не \u00abне выполнено\u00bb."
        )
    else:
        note = (
            f"Знаменатель — {measured} из {total} решений плана: выполнено "
            f"{states.count('materialized')}, не дошло {states.count('abandoned')}, "
            f"пока в горизонте {states.count('not_yet')}, без ответа {states.count('unknown')}."
        )
    return {"outcomes": outcomes, **_coverage(states, note)}


def _entry_dict(
    entry: EditorialPlanEntry, outcome: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "cluster_public_id": entry.cluster_public_id,
        "title": entry.title,
        "niche": entry.niche,
        "action": entry.action,
        "platform": entry.platform,
        "channel_id": entry.channel_id,
        "channel": entry.payload.get("channel_title") if entry.payload else None,
        "format": entry.format,
        "priority": entry.priority,
        "confidence": entry.confidence,
        "reasons": entry.reasons or [],
        "slot_at": entry.slot_at.isoformat() if entry.slot_at else None,
        "payload": entry.payload or {},
        # Additive: without a measurement the state is `unknown`, never a
        # guessed `not_yet`.
        "outcome": dict(outcome) if outcome else _unknown_outcome(_now()),
    }


def _plan_dict(
    plan: EditorialPlan,
    entries: list[EditorialPlanEntry],
    measurement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outcomes = list((measurement or {}).get("outcomes") or [])
    summary = dict(plan.summary or {})
    # Additive: coverage is derived from live posts, so it is computed per read
    # and deliberately never written back into the persisted summary.
    fallback = _coverage(["unknown"] * len(entries), "")
    measured_coverage = (measurement or {}).get("coverage")
    summary["coverage"] = dict(measured_coverage) if measured_coverage else fallback["coverage"]
    summary["coverage_note"] = str(
        (measurement or {}).get("coverage_note")
        or "Исход решений плана не проверялся: ни одно решение не измерено."
    )
    return {
        "public_id": plan.public_id,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "horizon_days": plan.horizon_days,
        "status": plan.status,
        "summary": summary,
        "entries": [
            _entry_dict(entry, outcomes[index] if index < len(outcomes) else None)
            for index, entry in enumerate(entries)
        ],
    }


def build_editorial_plan(
    *,
    now: datetime | None = None,
    horizon_days: int = PLAN_HORIZON_DAYS,
    persist: bool = True,
    queue_limit: int = 25,
    max_entries: int = MAX_ENTRIES,
) -> dict[str, Any]:
    """Build (and by default persist) the global editorial plan snapshot."""

    moment = now or _now()
    rows = editorial_queue(limit=queue_limit, now=moment)

    with session_scope() as session:
        channels = [
            c for c in session.execute(select(Channel).where(Channel.enabled == True)).scalars()  # noqa: E712
        ]
        gated: dict[str, tuple[Post, Channel]] = {}
        for post, channel in session.execute(
            select(Post, Channel)
            .join(Channel, Post.channel_id == Channel.id)
            .where(Post.status == "gated")
            .order_by(Post.created_at.desc())
            .limit(200)
        ):
            gated.setdefault(post.cluster_public_id, (post, channel))

        taken: dict[int, set[datetime]] = defaultdict(set)
        for slot_at, channel_id in session.execute(
            select(Post.scheduled_at, Post.channel_id)
            .where(Post.status.in_(["scheduled", "published"]))
            .where(Post.scheduled_at.is_not(None))
            .where(Post.scheduled_at.between(moment, moment + timedelta(days=horizon_days)))
        ):
            if slot_at is not None and channel_id is not None:
                taken[channel_id].add(slot_at.replace(second=0, microsecond=0))

        published_by_platform = dict(
            session.execute(
                select(CrosspostLog.platform, func.count())
                .where(CrosspostLog.status == "posted")
                .where(CrosspostLog.created_at >= moment - timedelta(days=1))
                .group_by(CrosspostLog.platform)
            ).all()
        )

        previous = session.execute(
            select(EditorialPlan)
            .where(EditorialPlan.status == "active")
            .order_by(EditorialPlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        previous_keys: set[tuple[str, str]] = set()
        if previous is not None:
            previous_keys = {
                (e.cluster_public_id, e.action)
                for e in session.execute(
                    select(EditorialPlanEntry).where(EditorialPlanEntry.plan_id == previous.id)
                ).scalars()
            }

        entries: list[EditorialPlanEntry] = []
        load: dict[int, int] = defaultdict(int)
        for rank, row in enumerate(rows[:max_entries], start=1):
            cluster_id = row["cluster_public_id"]
            pair = gated.get(cluster_id)
            stale = float(row.get("age_hours") or 0.0) >= STALE_HOURS
            if stale:
                action, channel, slot = "hold", None, None
            elif pair is not None:
                # A gated draft beats the cooldown signal: the draft itself
                # counts as a "recent post" in the queue, but it was not
                # published yet, so scheduling it is not a repeat.
                post, channel = pair
                action = "schedule"
                slot = _next_free_slot(channel, taken, moment, horizon_days)
                if slot is not None:
                    taken[channel.id].add(slot.replace(second=0, microsecond=0))
                load[channel.id] += 1
            elif row.get("cooling"):
                action, channel, slot = "hold", None, None
            else:
                channel = _pick_channel(row.get("niche") or "", channels, load)
                action, slot = "produce", None
                if channel is not None:
                    load[channel.id] += 1

            entry = EditorialPlanEntry(
                cluster_public_id=cluster_id,
                title=str(row.get("label") or ""),
                niche=str(row.get("niche") or ""),
                action=action,
                platform=channel.platform if channel else None,
                channel_id=channel.id if channel else None,
                format=row.get("post_format") or None,
                priority=_priority(rank, len(rows), row),
                confidence=_confidence(row),
                reasons=_reasons(row, action, channel, slot),
                slot_at=slot,
                payload={
                    "score": row.get("score"),
                    "heat": row.get("heat"),
                    "sources": row.get("sources"),
                    "age_hours": row.get("age_hours"),
                    "is_news": bool(row.get("is_news")),
                    "needs_media": action != "hold",
                    "channel_title": channel.title if channel else None,
                },
            )
            entries.append(entry)

        new_keys = {(e.cluster_public_id, e.action) for e in entries}
        added = sorted({cid for cid, _ in new_keys - previous_keys})
        removed = sorted({cid for cid, _ in previous_keys - new_keys})
        by_platform: dict[str, int] = defaultdict(int)
        for e in entries:
            if e.platform:
                by_platform[e.platform] += 1
        summary = {
            "total": len(entries),
            "produce": sum(1 for e in entries if e.action == "produce"),
            "schedule": sum(1 for e in entries if e.action == "schedule"),
            "hold": sum(1 for e in entries if e.action == "hold"),
            "by_platform": dict(by_platform),
            "published_last_24h": published_by_platform,
            "diff": {
                "added": added,
                "removed": removed,
                "previous_plan_id": previous.public_id if previous else None,
            },
        }

        if not persist:
            transient = EditorialPlan(
                public_id="transient",
                created_at=moment,
                horizon_days=horizon_days,
                status="transient",
                summary=summary,
            )
            for e in entries:
                e.plan_id = 0
            measurement = _measure_plan(session, transient, entries, now=moment, persisted=False)
            return _plan_dict(transient, entries, measurement)

        plan = EditorialPlan(
            public_id=uuid.uuid4().hex,
            created_at=moment,
            horizon_days=horizon_days,
            status="active",
            summary=summary,
        )
        session.add(plan)
        session.flush()
        for e in entries:
            e.plan_id = plan.id
            session.add(e)
        if previous is not None:
            previous.status = "superseded"
            previous.superseded_by_id = plan.id
        session.flush()
        measurement = _measure_plan(session, plan, entries, now=moment)
        return _plan_dict(plan, entries, measurement)


def get_active_plan() -> dict[str, Any] | None:
    """Currently active persisted plan with each decision's outcome, or None.

    None means the plan was never built — the caller must show "nothing ran",
    never a plan with zero materialized entries.
    """

    with session_scope() as session:
        plan = session.execute(
            select(EditorialPlan)
            .where(EditorialPlan.status == "active")
            .order_by(EditorialPlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if plan is None:
            return None
        entries = list(
            session.execute(
                select(EditorialPlanEntry)
                .where(EditorialPlanEntry.plan_id == plan.id)
                .order_by(EditorialPlanEntry.priority, EditorialPlanEntry.id)
            ).scalars()
        )
        measurement = _measure_plan(session, plan, entries, now=_now())
        return _plan_dict(plan, entries, measurement)


def plan_history(limit: int = 10) -> list[dict[str, Any]]:
    """Recent plan snapshots (decision history, newest first)."""

    with session_scope() as session:
        plans = (
            session.execute(
                select(EditorialPlan).order_by(EditorialPlan.created_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return [
            {
                "public_id": p.public_id,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "status": p.status,
                "horizon_days": p.horizon_days,
                "summary": p.summary or {},
            }
            for p in plans
        ]

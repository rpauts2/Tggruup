"""Global editorial plan: a dynamic, revisable publication calendar.

The plan turns the ranked topic queue (``tgfarm.editorial``) plus gated
drafts into a portfolio-wide set of decisions with priority, confidence,
justification and a concrete slot per channel. Every refresh supersedes
the previous active snapshot but keeps it as decision history, so the
system never silently rewrites its past choices.

The plan never publishes anything by itself: entries feed the existing
generation and scheduling flows.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tgfarm.db.models import Channel, CrosspostLog, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope
from tgfarm.editorial import queue as editorial_queue

PLAN_HORIZON_DAYS = 7
SLOT_STEP_MINUTES = 90
SLOT_START_OFFSET_MINUTES = 30
STALE_HOURS = 96
MAX_ENTRIES = 40


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


def _entry_dict(entry: EditorialPlanEntry) -> dict[str, Any]:
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
    }


def _plan_dict(plan: EditorialPlan, entries: list[EditorialPlanEntry]) -> dict[str, Any]:
    return {
        "public_id": plan.public_id,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "horizon_days": plan.horizon_days,
        "status": plan.status,
        "summary": plan.summary or {},
        "entries": [_entry_dict(e) for e in entries],
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
            return _plan_dict(transient, entries)

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
        return _plan_dict(plan, entries)


def get_active_plan() -> dict[str, Any] | None:
    """Currently active persisted plan, or None when nothing was planned yet."""

    with session_scope() as session:
        plan = session.execute(
            select(EditorialPlan)
            .where(EditorialPlan.status == "active")
            .order_by(EditorialPlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if plan is None:
            return None
        entries = (
            session.execute(
                select(EditorialPlanEntry)
                .where(EditorialPlanEntry.plan_id == plan.id)
                .order_by(EditorialPlanEntry.priority, EditorialPlanEntry.id)
            )
            .scalars()
            .all()
        )
        return _plan_dict(plan, entries)


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

"""Editorial queue: rank ready clusters by heat, corroboration and freshness.

The pipeline used to take the hottest clusters in raw heat order, which
over-rewards one loud source and starves fresh, well-covered stories. This queue
is deterministic and DB-backed so the same ranking is visible in the API, the
CLI and the cycle that spends generation budget.

The queue answers one question — which topics the farm still owes an editorial
decision to — and it has always had only half of it. A cluster goes `consumed`
the moment S4 writes a draft (`s4_generation.run` calls
`s2_trends.mark_consumed`), so from that instant the plan could not see the
topic any more: `tgfarm.editorial_plan.build_editorial_plan` iterates exactly
the rows this function returns, and it answers `schedule` only for a topic that
is in that list while a gated draft exists. Measured on `main` at `d848eee`, the
offline chain's second plan look therefore built a plan with zero entries and
no self-running walk could ever book a slot. The other half, added here, is the
open editorial order: a topic whose draft the plan itself ordered and that
neither the plan nor a scheduler has answered yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from tgfarm.core.run import ensure_utc
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Cluster, RawItem
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

CLUSTER_TTL_HOURS = 36
COOLDOWN_HOURS = 72
NEWS_PRIORITY_BOOST = 0.2

#: The post states that still count as an open editorial order. `gated` is the
#: quality gate's own word for "ready to ship"; a draft is not one yet, because
#: nothing has decided the topic may go out at all.
OPEN_ORDER_POST_STATUSES = ("gated",)
#: Open orders read per queue call. One plan snapshot holds at most 40 entries
#: (`editorial_plan.MAX_ENTRIES`), so this is the whole ledger and not a slice.
MAX_OPEN_ORDERS = 40


def _open_order_conditions() -> tuple:
    """The one definition of "the plan ordered this row and nobody answered it".

    `plan_entry_id IS NOT NULL` is what makes the wider view self-limiting: the
    topic is only re-shown when the plan itself wrote the draft (`persist_draft`
    takes the id from `plan_execution.generation_brief`, which only speaks behind
    the executor's switch). `plan_slot_at IS NULL` and `scheduled_at IS NULL` say
    the booking has not been written and no scheduler has taken the row, so an
    order stops haunting the queue the moment it is answered — from either side.
    """
    return (
        PostRow.cluster_public_id.is_not(None),
        PostRow.status.in_(OPEN_ORDER_POST_STATUSES),
        PostRow.scheduled_at.is_(None),
        PostRow.plan_slot_at.is_(None),
        PostRow.plan_entry_id.is_not(None),
    )


def _plan_consumer_enabled() -> bool:
    """Whether anything exists that could honour an open order.

    Off `PLAN_EXECUTION_ENABLED` no plan is built, no entry carries a slot and no
    consumer books one, so re-showing a consumed topic would only hand generation
    budget back to a topic that already spent it. The measured baseline is that
    the flag is off by default and the row set below is therefore byte-identical
    to main's until an operator (or the demo's own isolated store) turns it on.
    """
    return bool(getattr(get_settings(), "plan_execution_enabled", False))


def open_order_cluster_ids(limit: int = MAX_OPEN_ORDERS) -> list[str]:
    """Topics whose plan-ordered draft is still waiting for a booked slot.

    Newest draft first: an open order is a decision the farm is late with, and
    the freshest one is the one a plan tick can still put inside the horizon.
    """
    with session_scope() as session:
        rows = (
            session.execute(
                select(PostRow.cluster_public_id)
                .where(*_open_order_conditions())
                .group_by(PostRow.cluster_public_id)
                .order_by(func.max(PostRow.created_at).desc())
                .limit(max(1, limit))
            )
            .scalars()
            .all()
        )
    return [str(public_id) for public_id in rows if public_id]


def awaiting_booking(cluster_public_id: str) -> bool:
    """True when this topic is an open order, not material generation may spend.

    Shared by the queue and by `s4_generation.run`, so "the plan still owes this
    topic a slot" and "S4 must not draft this topic again" cannot drift apart.
    """
    if not cluster_public_id:
        return False
    with session_scope() as session:
        found = session.execute(
            select(PostRow.public_id)
            .where(PostRow.cluster_public_id == cluster_public_id, *_open_order_conditions())
            .limit(1)
        ).first()
    return found is not None


def _score(
    heat: float, 
    diversity: int, 
    age_hours: float, 
    cooling: bool,
    ai_relevance: float = 0.0,
    ai_quality: float = 0.0,
    is_news: bool = False,
) -> float:
    freshness = max(0.0, 1.0 - age_hours / CLUSTER_TTL_HOURS)
    source_score = min(1.0, diversity / 3.0)  # max 1.0 for 3+ sources
    
    # Base score combines heat, source diversity, and freshness
    value = heat * 0.40 + source_score * 0.20 + freshness * 0.15

    # Add AI-driven quality and relevance, if available
    value += ai_relevance * 0.15
    value += ai_quality * 0.10

    if is_news:
        value += NEWS_PRIORITY_BOOST

    if cooling:
        value -= 0.5
    
    return round(max(0.0, value), 4)


def queue(limit: int = 25, now: datetime | None = None) -> list[dict]:
    """Topics the farm still owes an editorial decision to, best first.

    Ready-and-unconsumed clusters, plus — only while a plan consumer exists to
    answer them — the open orders: topics whose plan-ordered draft is gated and
    still has no booked slot. Without the second half the plan is blind to every
    draft the farm has already written, so `build_editorial_plan` can only ever
    answer `produce` for them and a self-running walk cannot book a slot.
    """
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(hours=COOLDOWN_HOURS)
    open_orders = open_order_cluster_ids(limit) if _plan_consumer_enabled() else []
    open_order_ids = set(open_orders)
    with session_scope() as session:
        clusters = list(
            session.execute(
                select(Cluster)
                .where(Cluster.status == "ready", Cluster.consumed_at.is_(None))
                .order_by(Cluster.heat.desc())
                .limit(max(1, limit) * 2)
            ).scalars().all()
        )
        if open_orders:
            seen_ids = {cluster.id for cluster in clusters}
            # `ready` or `consumed` only: a `forming` cluster has not been ranked
            # yet, and re-offering one would let the queue outrank `promote_ready`.
            for cluster in session.execute(
                select(Cluster)
                .where(
                    Cluster.public_id.in_(open_orders),
                    Cluster.status.in_(("ready", "consumed")),
                )
                .limit(max(1, limit))
            ).scalars():
                if cluster.id not in seen_ids:
                    seen_ids.add(cluster.id)
                    clusters.append(cluster)
        if not clusters:
            return []

        ids = [c.id for c in clusters]
        sources: dict[int, set[str]] = {}
        for cluster_id, source_id in session.execute(
            select(RawItem.cluster_id, RawItem.source_id)
            .where(RawItem.cluster_id.in_(ids))
            .distinct()
        ):
            sources.setdefault(cluster_id, set()).add(source_id)

        recent_posts = session.execute(
            select(PostRow.cluster_public_id, PostRow.angle, PostRow.quality)
            .where(
                PostRow.cluster_public_id.in_([c.public_id for c in clusters]),
                PostRow.created_at >= cutoff,
            )
        ).all()
        recent_cluster_ids = {p.cluster_public_id for p in recent_posts}
        post_data = {p.cluster_public_id: p for p in recent_posts}

    ranked: list[dict] = []
    for cluster in clusters:
        seen = ensure_utc(cluster.last_seen_at) or ensure_utc(cluster.created_at)
        age_hours = 0.0
        if seen is not None:
            age_hours = max(0.0, (current - seen).total_seconds() / 3600)
        diversity = len(sources.get(cluster.id, set()))

        # Get AI scores and angle if a post exists for this cluster
        post = post_data.get(cluster.public_id)
        ai_relevance = post.quality.get("scores", {}).get("relevance", 0.0) if post and post.quality else 0.0
        ai_quality = post.quality.get("scores", {}).get("quality", 0.0) if post and post.quality else 0.0
        is_news = post.angle == "news" if post else False
        
        score = _score(
            cluster.heat, 
            diversity, 
            age_hours, 
            cluster.public_id in recent_cluster_ids,
            ai_relevance=ai_relevance,
            ai_quality=ai_quality,
            is_news=is_news,
        )
        ranked.append(
            {
                "cluster_public_id": cluster.public_id,
                "label": cluster.label or "",
                "niche": cluster.niche or "",
                "heat": round(cluster.heat, 4),
                "sources": diversity,
                "age_hours": round(age_hours, 2),
                "cooling": cluster.public_id in recent_cluster_ids,
                "score": score,
                "ai_relevance": round(ai_relevance, 3),
                "ai_quality": round(ai_quality, 3),
                "is_news": is_news,
                # Additive: the queue says which half of its answer this row is,
                # so a caller that spends generation budget can tell a topic that
                # still has none from a topic whose draft awaits a slot.
                "awaiting_booking": cluster.public_id in open_order_ids,
            }
        )
    ranked.sort(key=lambda row: (row["score"], row["heat"], -row["age_hours"]), reverse=True)
    return ranked[: max(1, limit)]


def rank_cluster_ids(limit: int = 25, now: datetime | None = None) -> list[str]:
    return [row["cluster_public_id"] for row in queue(limit, now)]


def queue_age_hours(now: datetime | None = None) -> float | None:
    current = now or datetime.now(UTC)
    with session_scope() as session:
        oldest = session.execute(
            select(Cluster.created_at)
            .where(Cluster.status == "ready", Cluster.consumed_at.is_(None))
            .order_by(Cluster.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
    if oldest is None:
        return None
    oldest = ensure_utc(oldest)
    return round(max(0.0, (current - oldest).total_seconds() / 3600), 2) if oldest else None

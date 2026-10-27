"""Editorial queue: rank ready clusters by heat, corroboration and freshness.

The pipeline used to take the hottest clusters in raw heat order, which
over-rewards one loud source and starves fresh, well-covered stories. This queue
is deterministic and DB-backed so the same ranking is visible in the API, the
CLI and the cycle that spends generation budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tgfarm.core.run import ensure_utc
from tgfarm.db.models import Cluster, RawItem
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

CLUSTER_TTL_HOURS = 36
COOLDOWN_HOURS = 72
NEWS_PRIORITY_BOOST = 0.2


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
    """Ready clusters in the order generation should consider them."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(hours=COOLDOWN_HOURS)
    with session_scope() as session:
        clusters = session.execute(
            select(Cluster)
            .where(Cluster.status == "ready", Cluster.consumed_at.is_(None))
            .order_by(Cluster.heat.desc())
            .limit(max(1, limit) * 2)
        ).scalars().all()
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

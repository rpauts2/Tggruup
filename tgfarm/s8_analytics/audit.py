"""Operational content audit: what the farm produced, skipped and spent."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from tgfarm.core.settings import get_settings
from tgfarm.db.models import AIUsage, Cluster, RawItem, Source
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.editorial import queue_age_hours
from tgfarm.llm import policy


def _reason(value: str | None) -> str:
    return (value or "unknown").split(":", 1)[0].strip()[:80] or "unknown"


def content_audit(
    days: int = 7,
    *,
    channel_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(UTC)
    days = max(1, int(days))
    cutoff = current - timedelta(days=days)

    with session_scope() as session:
        stmt = select(PostRow).where(PostRow.created_at >= cutoff)
        if channel_id:
            stmt = stmt.where(PostRow.channel_id == channel_id)
        posts = session.execute(stmt).scalars().all()

        by_status: dict[str, int] = defaultdict(int)
        by_angle: dict[str, int] = defaultdict(int)
        by_format: dict[str, int] = defaultdict(int)
        by_provider: dict[str, int] = defaultdict(int)
        rejected: dict[str, int] = defaultdict(int)
        published_days: dict[str, int] = defaultdict(int)
        for post in posts:
            by_status[post.status] += 1
            by_angle[post.angle or "unknown"] += 1
            by_format[post.post_format or "unknown"] += 1
            by_provider[post.generation_provider or "offline"] += 1
            if post.status == "rejected":
                rejected[_reason(post.reject_reason)] += 1
            if post.status == "published" and post.published_at is not None:
                published_at = post.published_at
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                published_days[published_at.strftime("%Y-%m-%d")] += 1

        raw_stmt = select(RawItem).where(RawItem.fetched_at >= cutoff)
        raw_items = session.execute(raw_stmt).scalars().all()
        yield_by_source: dict[str, int] = defaultdict(int)
        duplicates = 0
        for item in raw_items:
            yield_by_source[item.source_id] += 1
            if item.duplicate_of is not None:
                duplicates += 1
        names = {
            row.id: row.name
            for row in session.execute(
                select(Source).where(Source.id.in_(list(yield_by_source)))
            ).scalars().all()
        }

        ready = session.execute(
            select(func.count())
            .select_from(Cluster)
            .where(Cluster.status == "ready", Cluster.consumed_at.is_(None))
        ).scalar_one()

        ai_rows = session.execute(
            select(AIUsage).where(AIUsage.created_at >= cutoff)
        ).scalars().all()

    by_stage: dict[str, dict[str, int]] = {}
    for bucket in {
        row.stage for row in ai_rows
    }:
        rows = [row for row in ai_rows if row.stage == bucket]
        by_stage[bucket] = {
            "calls": len(rows),
            "used": sum(1 for row in rows if row.used),
            "charged": sum(1 for row in rows if row.charged),
            "fallbacks": sum(1 for row in rows if row.reason == "fallback:offline"),
            "skipped": sum(
                1
                for row in rows
                if not row.used and row.reason not in ("fallback:offline", "")
            ),
        }

    total_raw = len(raw_items)
    published = sum(published_days.values())
    return {
        "window_days": days,
        "period": {
            "from": cutoff.isoformat(timespec="seconds"),
            "to": current.isoformat(timespec="seconds"),
        },
        "posts": {
            "total": len(posts),
            "by_status": dict(sorted(by_status.items())),
            "by_angle": dict(sorted(by_angle.items())),
            "by_format": dict(sorted(by_format.items())),
            "by_provider": dict(sorted(by_provider.items())),
            "rejection_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(rejected.items(), key=lambda item: -item[1])[:10]
            ],
        },
        "sources": {
            "items": total_raw,
            "duplicates": duplicates,
            "duplicate_rate": round(duplicates / total_raw, 4) if total_raw else 0.0,
            "yield": [
                {
                    "source_id": source_id,
                    "name": names.get(source_id, source_id),
                    "items": count,
                }
                for source_id, count in sorted(
                    yield_by_source.items(), key=lambda item: -item[1]
                )[:10]
            ],
        },
        "cadence": {
            "published": published,
            "per_day": dict(sorted(published_days.items())),
            "avg_per_day": round(published / days, 2),
        },
        "queue": {
            "ready": int(ready or 0),
            "oldest_age_hours": queue_age_hours(current),
        },
        "ai": {
            "policy": get_settings().ai_policy,
            "daily_budget": get_settings().ai_daily_budget,
            "summary": policy.usage_summary(days, now=current),
            "by_stage": by_stage,
        },
    }

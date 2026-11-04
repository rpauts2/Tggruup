"""Format performance: which shapes of post actually earned attention.

The planner can only prefer a format when the farm knows how formats performed,
and until now nothing measured that: `hook_style_performance` covers hooks,
`angle_performance` covers angles, but `post_format` was never aggregated. This
module answers one question with real rows: for each format, how many posts were
published and what engagement did the measured ones earn.

Honest limits, stated up front:

* engagement comes from ``post_stats`` (reactions + comments + forwards). A post
  without a snapshot is counted as published but not as measured, so a format
  cannot look good on the strength of unmeasured posts;
* rows are per channel owner, not per crosspost destination;
* a window with fewer than ``MIN_SAMPLE`` measured posts is reported with
  ``reliable: False`` instead of a confident number;
* ``platform`` scopes the whole report to one destination family, because a
  format that works on Reddit has not earned the right to decide a Дзен post.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope

log = get_logger(__name__)

#: Measured posts a format needs before its average may drive a decision.
MIN_SAMPLE = 3


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def format_performance(
    days: int = 30,
    *,
    platform: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Published posts and measured engagement per post format inside the window.

    ``platform`` restricts the report to channels of that platform; without it the
    report mixes every destination and must not drive a per-platform choice.
    """
    current = now or datetime.now(UTC)
    current = _aware(current) or current
    days = max(1, min(int(days), 365))
    since = current - timedelta(days=days)
    platform_key = str(platform or "").strip().lower() or None

    rows: dict[str, dict[str, Any]] = {}
    with session_scope() as session:
        statement = select(Post.id, Post.post_format, Post.published_at).where(
            Post.status == "published",
            Post.published_at.isnot(None),
            Post.published_at >= since,
        )
        if platform_key is not None:
            statement = statement.join(Channel, Channel.id == Post.channel_id).where(
                func.lower(Channel.platform) == platform_key
            )
        posts = session.execute(statement).all()
        if not posts:
            return {
                "window_days": days,
                "since": since.isoformat(),
                "platform": platform_key,
                "formats": {},
                "measured_total": 0,
                "published_total": 0,
                "min_sample": MIN_SAMPLE,
            }
        post_ids = [row.id for row in posts]
        stats: dict[int, tuple[int, int, int]] = {}
        # One grouped read instead of a query per post: the table can hold tens of
        # thousands of snapshots and this runs inside the analytics stage.
        for post_id, reactions, comments, forwards in session.execute(
            select(
                PostStat.post_id,
                func.max(PostStat.reactions),
                func.max(PostStat.comments),
                func.max(PostStat.forwards),
            )
            .where(PostStat.post_id.in_(post_ids))
            .group_by(PostStat.post_id)
        ).all():
            stats[int(post_id)] = (int(reactions or 0), int(comments or 0), int(forwards or 0))

    for row in posts:
        fmt = str(row.post_format or "news")
        item = rows.setdefault(
            fmt,
            {"format": fmt, "published": 0, "measured": 0, "engagement_sum": 0.0},
        )
        item["published"] += 1
        stats_row = stats.get(int(row.id))
        if stats_row is None:
            continue
        engagement = sum(stats_row)
        item["measured"] += 1
        item["engagement_sum"] += float(engagement)

    formats: dict[str, dict[str, Any]] = {}
    for fmt, item in rows.items():
        measured = int(item["measured"])
        average = round(item["engagement_sum"] / measured, 4) if measured else 0.0
        formats[fmt] = {
            "format": fmt,
            "published": int(item["published"]),
            "measured": measured,
            "avg_engagement": average,
            "reliable": measured >= MIN_SAMPLE,
            "n": measured,
        }
    ordered = dict(sorted(formats.items(), key=lambda pair: (-pair[1]["avg_engagement"], pair[0])))
    return {
        "window_days": days,
        "since": since.isoformat(),
        "platform": platform_key,
        "formats": ordered,
        "measured_total": sum(item["measured"] for item in formats.values()),
        "published_total": sum(item["published"] for item in formats.values()),
        "min_sample": MIN_SAMPLE,
    }


def planner_history(
    platform: str,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Rows shaped for `s10_format.plan_with_history`, scoped to one platform.

    `measured` is what the planner gates its sample on: a published post nobody
    ever measured is activity, not evidence. `published` travels alongside it so
    the reason string can say how much of the window stayed unmeasured.
    """
    report = format_performance(days, platform=platform, now=now)
    history: dict[str, dict[str, Any]] = {}
    for fmt, item in report["formats"].items():
        history[fmt] = {
            "measured": int(item["measured"]),
            "published": int(item["published"]),
            "avg_engagement": float(item["avg_engagement"]),
            "reliable": bool(item["reliable"]),
        }
    return history


def best_format(days: int = 30, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The format with the best measured engagement, or ``None`` without evidence."""
    report = format_performance(days, now=now)
    for item in report["formats"].values():
        if item["reliable"]:
            return item
    return None


__all__ = ["MIN_SAMPLE", "best_format", "format_performance", "planner_history"]

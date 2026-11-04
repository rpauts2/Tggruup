"""S8 — analytics and the feedback loop.

Collects per-post performance (via MTProto read-only when a read session is
available, otherwise from Bot API updates the bot already receives) and closes
the loop: post performance feeds back into the heat weights in S2 and into slot
selection in S7, so the farm gets measurably better instead of just louder.

Metrics are also reported in the shapes that matter commercially — ERR, ER and
CPA — because those are the numbers a channel is actually bought for.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, ensure_utc, to_thread
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.text import content_tokens
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import ChannelStat, CrosspostLog, PostStat
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

log = get_logger(__name__)


@dataclass
class PostMetrics:
    post_public_id: str
    channel_id: str
    angle: str
    published_at: datetime | None
    hook_style: str | None = None
    variant: str = "a"
    impressions: int = 0
    opens: int = 0
    views: int = 0
    reactions: int = 0
    comments: int = 0
    forwards: int = 0

    @property
    def engagement(self) -> int:
        return self.reactions + self.comments + self.forwards

    @property
    def er(self) -> float:
        return round(self.engagement / self.views, 4) if self.views else 0.0

    @property
    def ctr(self) -> float:
        return round(min(1.0, self.opens / self.impressions), 4) if self.impressions else 0.0

    @property
    def read_rate(self) -> float:
        return round(min(1.0, self.views / self.opens), 4) if self.opens else 0.0

    @property
    def quality(self) -> float:
        """One comparable score for channels that expose a feed.

        Dzen often has impressions and openings before it has any reactions, so
        CTR x read-through is a better early signal than ER alone. Telegram-style
        channels without impressions keep using ER.
        """
        if self.impressions >= 100:
            return round(self.ctr * self.read_rate, 4)
        return self.er

    def as_dict(self) -> dict:
        data = asdict(self)
        data |= {
            "er": self.er,
            "ctr": self.ctr,
            "read_rate": self.read_rate,
            "quality": self.quality,
        }
        return data


@dataclass
class ChannelReport:
    channel_id: str
    title: str
    posts_7d: int = 0
    avg_views: float = 0.0
    avg_er: float = 0.0
    avg_ctr: float = 0.0
    avg_read_rate: float = 0.0
    impressions_total: int = 0
    by_angle: dict[str, float] = field(default_factory=dict)
    by_hour: dict[int, float] = field(default_factory=dict)
    best_hour: int | None = None
    best_angle: str | None = None
    subs: int | None = None
    err: float | None = None


def recent_metrics(days: int = 7) -> list[PostMetrics]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with session_scope() as session:
        rows = session.execute(
            select(PostRow).where(PostRow.status == "published", PostRow.published_at >= cutoff)
        ).scalars().all()
        stats: dict[int, PostStat] = {}
        if rows:
            for stat in session.execute(
                select(PostStat).where(PostStat.post_id.in_([r.id for r in rows]))
            ).scalars().all():
                if stat.post_id not in stats or stat.collected_at > stats[stat.post_id].collected_at:
                    stats[stat.post_id] = stat

        out: list[PostMetrics] = []
        for row in rows:
            stat = stats.get(row.id)
            out.append(
                PostMetrics(
                    post_public_id=row.public_id,
                    channel_id=row.channel_id,
                    angle=row.angle,
                    published_at=ensure_utc(row.published_at),
                    hook_style=row.hook_style,
                    variant=row.variant or "a",
                    impressions=stat.impressions if stat else 0,
                    opens=stat.opens if stat else 0,
                    views=stat.views if stat else 0,
                    reactions=stat.reactions if stat else 0,
                    comments=stat.comments if stat else 0,
                    forwards=stat.forwards if stat else 0,
                )
            )
    return out


def channel_report(channel_id: str, days: int = 7) -> ChannelReport:
    config = get_config()
    persona = config.persona(channel_id)
    report = ChannelReport(
        channel_id=channel_id, title=persona.title if persona else channel_id
    )
    metrics = [m for m in recent_metrics(days) if m.channel_id == channel_id]
    if not metrics:
        return report

    report.posts_7d = len(metrics)
    report.avg_views = sum(m.views for m in metrics) / len(metrics)
    report.avg_er = sum(m.er for m in metrics) / len(metrics)
    reported = [m for m in metrics if m.impressions]
    report.impressions_total = sum(m.impressions for m in metrics)
    if reported:
        report.avg_ctr = sum(m.ctr for m in reported) / len(reported)
        report.avg_read_rate = sum(m.read_rate for m in reported) / len(reported)

    angles: dict[str, list[float]] = defaultdict(list)
    hours: dict[int, list[float]] = defaultdict(list)
    for m in metrics:
        if m.views or m.impressions:
            angles[m.angle].append(m.quality)
        if ensure_utc(m.published_at):
            hours[ensure_utc(m.published_at).hour].append(m.quality)

    report.by_angle = {k: round(sum(v) / len(v), 4) for k, v in angles.items() if v}
    report.by_hour = {k: round(sum(v) / len(v), 4) for k, v in hours.items() if v}
    report.best_angle = max(report.by_angle, key=report.by_angle.get) if report.by_angle else None
    report.best_hour = max(report.by_hour, key=report.by_hour.get) if report.by_hour else None
    return report


def fleet_summary(days: int = 7) -> dict:
    metrics = recent_metrics(days)
    by_channel: dict[str, list[PostMetrics]] = defaultdict(list)
    for m in metrics:
        by_channel[m.channel_id].append(m)

    channels = []
    for channel_id, items in by_channel.items():
        channels.append(
            {
                "channel_id": channel_id,
                "posts": len(items),
                "views_total": sum(i.views for i in items),
                "avg_views": round(sum(i.views for i in items) / len(items), 1),
                "avg_er": round(sum(i.er for i in items) / len(items), 4),
            }
        )
    channels.sort(key=lambda c: c["views_total"], reverse=True)

    return {
        "window_days": days,
        "posts": len(metrics),
        "views_total": sum(m.views for m in metrics),
        "engagement_total": sum(m.engagement for m in metrics),
        "avg_er": round(sum(m.er for m in metrics) / len(metrics), 4) if metrics else 0.0,
        "channels": channels,
    }


# ------------------------------------------------------------- feedback loop
def angle_performance(
    days: int = 14, channel_id: str | None = None
) -> dict[str, dict[str, float | int]]:
    """Aggregate a comparable quality score by angle.

    Dzen exposes impressions long before it has enough views for a stable ER,
    so measured angles combine CTR and read-through. Channels without a feed
    still fall back to ER. Very small samples are ignored.
    """
    buckets: dict[str, list[PostMetrics]] = defaultdict(list)
    for m in recent_metrics(days):
        if channel_id and m.channel_id != channel_id:
            continue
        if m.impressions >= 100 or m.views >= 30:
            buckets[m.angle].append(m)
    out: dict[str, dict[str, float | int]] = {}
    for angle, values in buckets.items():
        impressions = sum(v.impressions for v in values)
        opens = sum(v.opens for v in values)
        out[angle] = {
            "n": len(values),
            "er": round(sum(v.er for v in values) / len(values), 4),
            "ctr": round(opens / impressions, 4) if impressions else 0.0,
            "quality": round(sum(v.quality for v in values) / len(values), 4),
        }
    return out


def hook_style_performance(
    days: int = 14, channel_id: str | None = None
) -> dict[str, dict[str, float | int]]:
    """Same aggregation as angles, keyed by the learned hook style.

    Hook patterns compete for the same reader attention, so the reward is the
    same blend of CTR, read-through and ER that angles use.
    """
    buckets: dict[str, list[PostMetrics]] = defaultdict(list)
    for m in recent_metrics(days):
        if channel_id and m.channel_id != channel_id:
            continue
        if not m.hook_style:
            continue
        if m.impressions >= 100 or m.views >= 30:
            buckets[m.hook_style].append(m)
    out: dict[str, dict[str, float | int]] = {}
    for style, values in buckets.items():
        impressions = sum(v.impressions for v in values)
        opens = sum(v.opens for v in values)
        out[style] = {
            "n": len(values),
            "er": round(sum(v.er for v in values) / len(values), 4),
            "ctr": round(opens / impressions, 4) if impressions else 0.0,
            "quality": round(sum(v.quality for v in values) / len(values), 4),
        }
    return out


def holdout_report(days: int = 30) -> dict[str, dict[str, float | int]]:
    """Treatment vs holdout: is the few-shot voice actually better?

    Holdout posts are generated without the examples; comparing their quality
    and views against the treatment bucket is the only honest read on whether
    the machinery helps rather than merely changes the text.
    """
    buckets: dict[str, list[PostMetrics]] = defaultdict(list)
    for m in recent_metrics(days):
        buckets[m.variant or "a"].append(m)
    out: dict[str, dict[str, float | int]] = {}
    for variant, values in buckets.items():
        out[variant] = {
            "n": len(values),
            "quality": round(sum(v.quality for v in values) / len(values), 4),
            "views": round(sum(v.views for v in values) / len(values), 1),
        }
    return out


NGRAM_SIZE = 6


def style_diversity(days: int = 7) -> dict[str, float | int]:
    """Share of repeated 6-grams across recent hooks.

    This is the guardrail metric: an optimizer that keeps picking the winning
    pattern eventually rewrites the same hook every time, and Dzen punishes
    exactly that template feel.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    with session_scope() as session:
        hooks = list(
            session.execute(
                select(PostRow.hook)
                .where(PostRow.status == "published", PostRow.published_at >= cutoff)
                .order_by(PostRow.published_at.asc())
            ).scalars().all()
        )
    seen: set[tuple[str, ...]] = set()
    repeated = total = 0
    for hook in hooks:
        tokens = content_tokens(hook or "")
        grams = {
            tuple(tokens[i : i + NGRAM_SIZE])
            for i in range(max(0, len(tokens) - NGRAM_SIZE + 1))
        }
        if not grams:
            continue
        repeated += len(grams & seen)
        total += len(grams)
        seen |= grams
    return {
        "posts": len(hooks),
        "grams": total,
        "duplicate_gram_share": round(repeated / total, 4) if total else 0.0,
    }


def platform_health(days: int = 7) -> dict[str, dict[str, int | str | None]]:
    """Where publishing hurts right now: platform failures and queue errors.

    VK answers flood control with an error code, Dzen fails silently inside the
    browser; grouping the last week of failures by platform is the fastest way
    to see which surface is the bottleneck.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    out: dict[str, dict[str, int | str | None]] = {}
    with session_scope() as session:
        logs = session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.status == "failed", CrosspostLog.created_at >= cutoff)
            .order_by(CrosspostLog.created_at.desc())
        ).scalars().all()
        for entry in logs:
            bucket = out.setdefault(entry.platform, {"failed": 0, "last_error": None})
            bucket["failed"] = int(bucket["failed"]) + 1
            if not bucket["last_error"]:
                bucket["last_error"] = (entry.error or "")[:200]
        queue_errors = session.execute(
            select(PostRow)
            .where(PostRow.publish_error.isnot(None), PostRow.created_at >= cutoff)
            .order_by(PostRow.id.desc())
        ).scalars().all()
        if queue_errors:
            out["queue"] = {
                "failed": len(queue_errors),
                "last_error": (queue_errors[0].publish_error or "")[:200],
            }
    return out


def slot_performance(days: int = 14) -> dict[int, float]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for m in recent_metrics(days):
        published = ensure_utc(m.published_at)
        if published and (m.impressions >= 100 or m.views >= 30):
            buckets[published.hour].append(m.quality)
    return {hour: round(sum(v) / len(v), 4) for hour, v in buckets.items() if v}


def write_daily_stats() -> int:
    """Materialise per-channel per-day aggregates for dashboards and alerting."""
    config = get_config()
    metrics = recent_metrics(1)
    grouped: dict[tuple[str, str], list[PostMetrics]] = defaultdict(list)
    for m in metrics:
        if ensure_utc(m.published_at):
            published = ensure_utc(m.published_at)
            grouped[(m.channel_id, published.strftime("%Y-%m-%d"))].append(m)

    written = 0
    with session_scope() as session:
        for (channel_id, day), items in grouped.items():
            row = session.execute(
                select(ChannelStat).where(ChannelStat.channel_id == channel_id, ChannelStat.day == day)
            ).scalar_one_or_none()
            if row is None:
                row = ChannelStat(channel_id=channel_id, day=day)
                session.add(row)
            row.posts = len(items)
            row.views_sum = sum(i.views for i in items)
            row.reactions_sum = sum(i.reactions for i in items)
            row.err = round(
                sum(i.engagement for i in items) / max(1, sum(i.views for i in items)), 4
            )
            persona = config.persona(channel_id)
            if persona:
                row.er = round(row.views_sum / max(1, persona.posts_per_day), 1)
            written += 1
    return written


def record_post_stat(
    post_public_id: str,
    *,
    impressions: int = 0,
    opens: int = 0,
    views: int = 0,
    reactions: int = 0,
    comments: int = 0,
    forwards: int = 0,
) -> bool:
    with session_scope() as session:
        post = session.execute(
            select(PostRow).where(PostRow.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            return False
        session.add(
            PostStat(
                post_id=post.id,
                impressions=impressions,
                opens=opens,
                views=views,
                reactions=reactions,
                comments=comments,
                forwards=forwards,
            )
        )
        return True


async def run() -> dict[str, Any]:
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    dzen_result: dict = {}
    vk_result: dict = {}
    with StageTimer("s8_analytics") as timer:
        written = await to_thread(write_daily_stats)
        angles = await to_thread(angle_performance)
        # Dzen metrics live behind the authenticated Studio page. They are only
        # collected during live operation; `tgfarm dzen-stats` can force a run
        # in any mode. Failure here must not fail the rest of analytics.
        if settings.live and settings.dzen_stats_enabled:
            from tgfarm.s8_analytics.dzen_web import collect_and_record

            try:
                dzen_result = await collect_and_record()
            except Exception as exc:  # noqa: BLE001 - analytics is best-effort
                message = redact_sensitive_text(exc, limit=200)
                log.warning("s8: Dzen metrics unavailable: %s", message)
                dzen_result = {
                    "status": "blocked",
                    "error": message,
                    "reason": f"сбор Дзена не выполнился: {message}",
                }
        # VK is the volume channel and has no browser fallback: wall.getById is
        # the only honest source of its views/likes/comments/reposts.
        if settings.live:
            from tgfarm.s8_analytics.vk_stats import collect_and_record as collect_vk

            try:
                vk_result = await collect_vk()
            except Exception as exc:  # noqa: BLE001 - analytics is best-effort
                message = redact_sensitive_text(exc, limit=200)
                log.warning("s8: VK metrics unavailable: %s", message)
                vk_result = {
                    "status": "blocked",
                    "error": message,
                    "reason": f"сбор ВК не выполнился: {message}",
                }
        timer.items_out = (
            written
            + int(dzen_result.get("saved", 0) or 0)
            + int(vk_result.get("saved", 0) or 0)
        )
    log.info(
        "s8: %d channel-days written, angles measured: %s, dzen: %s, vk: %s",
        written,
        list(angles),
        dzen_result or "skipped",
        vk_result or "skipped",
    )
    return {
        "channel_days": written,
        "angles": len(angles),
        "dzen_saved": int(dzen_result.get("saved", 0) or 0),
        "vk_saved": int(vk_result.get("saved", 0) or 0),
        # A stage that collected nothing must say why; `saved: 0` alone cannot
        # be told apart from a platform with nothing to measure.
        "dzen_status": dzen_result.get("status") or ("skipped" if not dzen_result else "collected"),
        "dzen_reason": dzen_result.get("reason"),
        "vk_status": vk_result.get("status") or ("skipped" if not vk_result else "collected"),
        "vk_reason": vk_result.get("reason"),
    }


def channel_inventory() -> list[dict]:
    config = get_config()
    with session_scope() as session:
        rows = session.execute(select(ChannelRow)).scalars().all()
        today = datetime.now(UTC).date()
        inventory: list[dict] = []
        for row in rows:
            todays = session.execute(
                select(func.count(PostRow.id)).where(
                    PostRow.channel_id == row.id,
                    PostRow.status.in_(("scheduled", "publishing", "published")),
                    func.date(PostRow.created_at) == today.isoformat(),
                )
            ).scalar_one()
            persona = config.persona(row.id)
            inventory.append(
                {
                    "id": row.id,
                    "title": row.title or (persona.title if persona else row.id),
                    "niche": row.niche,
                    "platform": row.platform or "telegram",
                    "catch_all": bool(persona.catch_all) if persona else False,
                    "enabled": row.enabled,
                    "tg_chat_id": row.tg_chat_id,
                    "posts_today": int(todays or 0),
                    "posts_per_day": persona.posts_per_day if persona else 0,
                    "timezone": row.timezone,
                }
            )

        # External crosspost destinations (VK, MAX, X) are not Telegram channels,
        # but operators want to see them alongside the channel fleet and how much
        # was published there today.
        try:
            from tgfarm.core.settings import get_settings
            from tgfarm.s9_growth.adapters import ADAPTERS

            settings = get_settings()
            platform_titles = {
                "vk": "VK группа",
                "max": "MAX канал",
                "x": "X (Twitter)",
            }
            for platform, adapter in ADAPTERS.items():
                if not adapter.configured():
                    continue
                todays = session.execute(
                    select(func.count(CrosspostLog.id)).where(
                        CrosspostLog.platform == platform,
                        CrosspostLog.status == "posted",
                        func.date(CrosspostLog.created_at) == today.isoformat(),
                    )
                ).scalar_one()
                inventory.append(
                    {
                        "id": f"cp:{platform}",
                        "title": platform_titles.get(platform, platform.upper()),
                        "niche": "crosspost",
                        "platform": platform,
                        "catch_all": False,
                        "enabled": True,
                        "tg_chat_id": None,
                        "posts_today": int(todays or 0),
                        "posts_per_day": 0,
                        "timezone": settings.timezone or "Europe/Moscow",
                    }
                )
        except Exception:  # noqa: BLE001 - crosspost inventory is optional
            pass
    return inventory


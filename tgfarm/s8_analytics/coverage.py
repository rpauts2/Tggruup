"""Which publications are actually measured, platform by platform, and why not.

``post_stats`` is the only table the learning layers read: ``formats.py`` counts a
format as evidence only when its posts have a snapshot, and S12 can only learn
from engagement that exists. The farm therefore has three different problems that
look identical from the outside - a platform nobody reads, a platform nobody can
read without something the operator has not provided, and a platform with a
working reader that only runs when a human asks. This module separates them with
facts from the database alone.

Read-only by construction: every query here is a ``select``, nothing is inserted,
updated or deleted, and no network call is made. The result is a diagnosis, not a
side effect.

Three numbers per platform must never be conflated:

* ``published`` - posts the farm considers live;
* ``measured_posts`` - distinct posts with at least one snapshot;
* ``measured_with_signal`` - of those, posts whose newest snapshot has a counter
  above zero, because an all-zero snapshot is a failed read, not a measurement.

Attribution is reported separately, because a collector that cannot point a
scraped row at a post is blocked by our own bookkeeping rather than by a platform:
the Dzen publisher used to confirm a publication without storing its URL, which
left the Studio collector with rows it could never attribute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import CrosspostLog, PostStat
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

log = get_logger(__name__)

#: Where the metrics of each platform are read from, by module the farm owns.
#: ``None`` means nobody reads them, which is a coverage gap, not a platform
#: limitation - the reason string has to say which.
COLLECTORS: dict[str, str | None] = {
    "dzen": "tgfarm.s8_analytics.dzen_web",
    "vk": "tgfarm.s8_analytics.vk_stats",
    "telegram": "tgfarm.tg_sync",
    "max": None,
    "x": None,
    "reddit": None,
    "telegraph": None,
}

#: Collectors that exist but are reached only when somebody asks for them.
#: Their coverage grows while an operator clicks and stops the moment nobody
#: does, so a gap here is a missing schedule, not a missing reader - which is a
#: different sentence from "nobody can read this platform at all".
ON_DEMAND_COLLECTORS: dict[str, str] = {
    "telegram": "tg_sync.collect_post_stats запускают POST /api/telegram/stats, "
    "`tgfarm telegram stats` и меню десктопа, но ни одна задача планировщика",
}

#: Collectors that cannot run without something the operator has not provided.
#: Kept as wording, never as a value: no secret is read or echoed here.
BLOCKED_REASONS: dict[str, str] = {
    "vk": "нужен VK_USER_TOKEN: wall.getById читается только пользовательским "
    "токеном, групповой отвечает ошибкой 27",
}

#: Every counter column that can make a snapshot mean something.
_NON_ZERO = tuple(
    column > 0
    for column in (
        PostStat.impressions,
        PostStat.opens,
        PostStat.views,
        PostStat.reactions,
        PostStat.comments,
        PostStat.forwards,
    )
)

STATUS_MEASURED = "measured"
STATUS_PARTIAL = "partial"
STATUS_UNMEASURED = "unmeasured"
STATUS_BLOCKED = "blocked"


def _platform_keys() -> set[str]:
    """Platforms that have a channel or a posted crosspost row.

    Both sources matter: a destination can be published to without owning a
    channel, and a channel can hold posts the crosspost log never recorded.
    """
    keys: set[str] = set()
    with session_scope() as session:
        for platform in session.execute(select(func.distinct(ChannelRow.platform))).scalars():
            if str(platform or "").strip():
                keys.add(str(platform).strip().lower())
        for platform in session.execute(
            select(func.distinct(CrosspostLog.platform)).where(CrosspostLog.status == "posted")
        ).scalars():
            if str(platform or "").strip():
                keys.add(str(platform).strip().lower())
    return keys


def _platform_facts(platform: str) -> dict[str, int]:
    with session_scope() as session:
        published = int(
            session.execute(
                select(func.count(PostRow.id))
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(
                    func.lower(ChannelRow.platform) == platform,
                    PostRow.status == "published",
                )
            ).scalar_one()
            or 0
        )
        measured_posts = int(
            session.execute(
                select(func.count(func.distinct(PostStat.post_id)))
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(func.lower(ChannelRow.platform) == platform)
            ).scalar_one()
            or 0
        )
        stat_rows = int(
            session.execute(
                select(func.count(PostStat.id))
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(func.lower(ChannelRow.platform) == platform)
            ).scalar_one()
            or 0
        )
        measured_with_signal = int(
            session.execute(
                select(func.count(func.distinct(PostStat.post_id)))
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(func.lower(ChannelRow.platform) == platform, or_(*_NON_ZERO))
            ).scalar_one()
            or 0
        )
        latest = session.execute(
            select(func.max(PostStat.collected_at))
            .join(PostRow, PostRow.id == PostStat.post_id)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(func.lower(ChannelRow.platform) == platform)
        ).scalar_one_or_none()
        posted_rows = session.execute(
            select(
                func.count(CrosspostLog.id),
                func.sum(
                    case(
                        (
                            or_(
                                CrosspostLog.remote_id.isnot(None)
                                & (CrosspostLog.remote_id != ""),
                                CrosspostLog.url.isnot(None) & (CrosspostLog.url != ""),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).where(CrosspostLog.platform == platform, CrosspostLog.status == "posted")
        ).one()
    return {
        "published": published,
        "measured_posts": measured_posts,
        "stat_rows": stat_rows,
        "measured_with_signal": measured_with_signal,
        "latest_snapshot_at": latest,
        "crosspost_posted": int(posted_rows[0] or 0),
        "crosspost_with_remote": int(posted_rows[1] or 0),
    }


def _classify(row: dict[str, Any], platform: str) -> tuple[str, str | None]:
    """``(status, reason)`` - "collected zero" and "could not collect" apart."""
    collector = COLLECTORS.get(platform)
    has_anything = row["published"] > 0 or row["crosspost_posted"] > 0
    if not has_anything:
        return STATUS_UNMEASURED, "публикаций этого платформа нет"
    if row["measured_with_signal"] > 0:
        if row["measured_posts"] < (row["published"] or row["measured_posts"]):
            gap = row["published"] - row["measured_posts"]
            note = ""
            if collector is None:
                note = (
                    "; сборщика метрик в s8 нет, поэтому недостающие снимки появятся "
                    "только после того, как он появится"
                )
            elif platform in ON_DEMAND_COLLECTORS:
                note = f"; остальное не соберётся само: {ON_DEMAND_COLLECTORS[platform]}"
            elif platform in BLOCKED_REASONS and row["crosspost_with_remote"] > row[
                "measured_posts"
            ]:
                note = f"; остальное недоступно без нового ключа: {BLOCKED_REASONS[platform]}"
            return (
                STATUS_PARTIAL,
                f"измерено {row['measured_posts']} из {row['published']}: "
                f"{gap} публикаций без ни одного снимка{note}",
            )
        return STATUS_MEASURED, None
    if platform in BLOCKED_REASONS:
        return STATUS_BLOCKED, BLOCKED_REASONS[platform]
    if collector is None:
        return STATUS_BLOCKED, "сборщика метрик нет"
    if row["crosspost_posted"] and not row["crosspost_with_remote"]:
        return (
            STATUS_PARTIAL,
            f"из {row['crosspost_posted']} записей публикации только "
            f"{row['crosspost_with_remote']} имеют remote id/url: остальным нужна "
            "привязка по заголовку",
        )
    return (
        STATUS_UNMEASURED,
        "сборщик есть, публикаций достаточно, но в post_stats нет ни одного "
        "значимого снимка: сбор либо не запускался, либо ничего не сопоставил",
    )


def measurement_coverage(*, now: datetime | None = None) -> dict[str, Any]:
    """Per-platform published/measured counts, collector, attribution and reason.

    ``error`` is set instead of raising when the database has no analytics tables
    yet, so a caller can show the failure rather than an invented zero.
    """
    current = now or datetime.now(UTC)
    platforms: dict[str, dict[str, Any]] = {}
    error: str | None = None
    try:
        keys = sorted(_platform_keys())
        for platform in keys:
            facts = _platform_facts(platform)
            latest = facts.pop("latest_snapshot_at")
            latest_iso = None
            if latest is not None:
                latest = latest.replace(tzinfo=UTC) if latest.tzinfo is None else latest
                latest_iso = latest.isoformat()
                facts["age_hours"] = round(max(0.0, (current - latest).total_seconds() / 3600), 2)
            else:
                facts["age_hours"] = None
            row: dict[str, Any] = {
                "platform": platform,
                "collector": COLLECTORS.get(platform),
                "last_snapshot_at": latest_iso,
                **facts,
            }
            row["status"], row["reason"] = _classify(row, platform)
            platforms[platform] = row
    except SQLAlchemyError as exc:
        error = redact_sensitive_text(exc, limit=200)
        log.warning("s8: coverage read failed: %s", error)
        platforms = {}

    totals = {
        "published": sum(row["published"] for row in platforms.values()),
        "measured_posts": sum(row["measured_posts"] for row in platforms.values()),
        "measured_with_signal": sum(
            row["measured_with_signal"] for row in platforms.values()
        ),
        "stat_rows": sum(row["stat_rows"] for row in platforms.values()),
    }
    return {
        "generated_at": current.isoformat(),
        "platforms": platforms,
        "totals": totals,
        "coverage": (
            round(totals["measured_posts"] / totals["published"], 4)
            if totals["published"]
            else 0.0
        ),
        "gaps": [
            {
                "platform": row["platform"],
                "status": row["status"],
                "unmeasured": max(0, row["published"] - row["measured_posts"]),
                "reason": row["reason"],
            }
            for row in sorted(
                platforms.values(),
                key=lambda item: -(item["published"] - item["measured_posts"]),
            )
            if row["status"] != STATUS_MEASURED and row["published"]
        ],
        "error": error,
    }


def coverage_table(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """The audit as rows a dashboard or a report can print directly."""
    report = measurement_coverage(now=now)
    return [
        {
            "platform": row["platform"],
            "published": row["published"],
            "measured_posts": row["measured_posts"],
            "measured_with_signal": row["measured_with_signal"],
            "stat_rows": row["stat_rows"],
            "attributable": row["crosspost_with_remote"],
            "collector": row["collector"] or "нет",
            "status": row["status"],
            "reason": row["reason"],
        }
        for row in report["platforms"].values()
    ]


__all__ = [
    "BLOCKED_REASONS",
    "COLLECTORS",
    "ON_DEMAND_COLLECTORS",
    "STATUS_BLOCKED",
    "STATUS_MEASURED",
    "STATUS_PARTIAL",
    "STATUS_UNMEASURED",
    "coverage_table",
    "measurement_coverage",
]

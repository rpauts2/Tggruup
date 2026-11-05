"""Which publications are actually measured, platform by platform, and why not.

``post_stats`` is the only table the learning layers read: ``formats.py`` counts a
format as evidence only when its posts have a snapshot, and S12 can only learn
from engagement that exists. The farm therefore has four different problems that
look identical from the outside - a platform nobody reads, a platform nobody can
read without something the operator has not provided, a platform with a working
reader that only runs when a human asks, and a platform whose reader is scheduled
but still cannot read until the operator provides a session. This module separates
them with facts from the database alone.

Read-only by construction: every query here is a ``select``, nothing is inserted,
updated or deleted, and no network call is made. The result is a diagnosis, not a
side effect.

The last two kinds must not be merged, because they have different fixes: a
missing schedule is ours to write, a missing session or token is the operator's to
grant. That is why ``SCHEDULED_COLLECTORS`` and ``SESSION_GATED_COLLECTORS`` are
separate constants - a collector can be both scheduled and gated, and saying only
one of the two would send an operator to look for a bug that is not there.

Three numbers per platform must never be conflated:

* ``published`` - posts the farm considers live;
* ``measured_posts`` - distinct posts with at least one snapshot;
* ``measured_with_signal`` - of those, posts whose newest snapshot has a counter
  above zero, because an all-zero snapshot is a failed read, not a measurement.

Attribution is reported separately, because a collector that cannot point a
scraped row at a post is blocked by our own bookkeeping rather than by a platform:
the Dzen publisher used to confirm a publication without storing its URL, which
left the Studio collector with rows it could never attribute. For the same reason
Telegram also reports ``askable_posts`` - published posts carrying the message and
chat ids a collector needs even to ask a question.
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
#:
#: Telegram left this dict when `telegram-post-metrics` was registered in
#: `pipeline._build_scheduler`; its manual entry points still exist, but they are
#: no longer the only way in, so the wording moved to ``SCHEDULED_COLLECTORS``.
#: An empty dict is a real state, not a placeholder: it means every collector the
#: farm owns is on a schedule.
ON_DEMAND_COLLECTORS: dict[str, str] = {}

#: Collectors a scheduler job runs by itself, with the job id named so an operator
#: can check the schedule rather than take the claim on trust. A gap here is ours
#: to fix in code, never the platform's or the operator's.
SCHEDULED_COLLECTORS: dict[str, str] = {
    "telegram": "задача планировщика `telegram-post-metrics` (и разовая "
    "`startup-telegram-post-metrics`) зовёт tg_sync.collect_post_stats сама, "
    "а вручную - `tgfarm telegram stats` и POST /api/telegram/stats",
}

#: Collectors that are scheduled but read through a credential only the operator
#: can supply. This is the fourth kind of silence: the schedule exists and runs,
#: it simply has nothing to read with, and it says so instead of inventing a
#: number. Kept as wording, never as a value - the session file itself is never
#: opened or echoed here, so this module still makes no network call and writes
#: nothing.
SESSION_GATED_COLLECTORS: dict[str, str] = {
    "telegram": "нужна авторизованная пользовательская сессия Telegram: без неё "
    "задача честно отвечает `no_session` и ничего не записывает "
    "(выполните `tgfarm telegram login`)",
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


def _platform_facts(platform: str) -> dict[str, Any]:
    askable_posts: int | None = None
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
        if platform == "telegram":
            # How many published posts the collector could even ask about: a
            # message id plus the chat it lives in, on the channel or on the post.
            # Zero here means the schedule is not the problem - the ids are - and
            # that is a different sentence from "no session".
            askable_posts = int(
                session.execute(
                    select(func.count(PostRow.id))
                    .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                    .where(
                        func.lower(ChannelRow.platform) == platform,
                        PostRow.status == "published",
                        PostRow.tg_message_id.isnot(None),
                        or_(
                            PostRow.tg_chat_id.isnot(None),
                            ChannelRow.tg_chat_id.isnot(None),
                        ),
                    )
                ).scalar_one()
                or 0
            )
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
        # None, not 0, for platforms whose identity this column does not describe.
        "askable_posts": askable_posts,
    }


def _askable_note(row: dict[str, Any]) -> str:
    """A measured sentence about ids, or "" when the audit does not know any."""
    askable = row.get("askable_posts")
    published = int(row["published"] or 0)
    if askable is None or not published:
        return ""
    if askable >= published:
        return f" и id для запроса есть у всех {published} публикаций"
    return f" и id для запроса есть лишь у {askable} из {published} публикаций"


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
            elif platform in SESSION_GATED_COLLECTORS:
                # The fourth silence: scheduled, not broken, still empty until the
                # operator hands the collector a session.
                note = (
                    f"; расписание есть: {SCHEDULED_COLLECTORS[platform]}, "
                    f"но {SESSION_GATED_COLLECTORS[platform]}"
                )
                note += _askable_note(row)
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
    if platform in SESSION_GATED_COLLECTORS:
        # Scheduled and gated: same colour as a missing token, because from here
        # it is the same problem - the operator has not provided what only the
        # operator can provide. The text still says the schedule exists, so the
        # two are never confused for a collector that was never written.
        return (
            STATUS_BLOCKED,
            f"{SCHEDULED_COLLECTORS[platform]}: значимых снимков нет, и "
            f"{SESSION_GATED_COLLECTORS[platform]}" + _askable_note(row),
        )
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
    "SCHEDULED_COLLECTORS",
    "SESSION_GATED_COLLECTORS",
    "STATUS_BLOCKED",
    "STATUS_MEASURED",
    "STATUS_PARTIAL",
    "STATUS_UNMEASURED",
    "coverage_table",
    "measurement_coverage",
]

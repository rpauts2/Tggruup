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
grant. And "scheduled" is not yet "measuring": a registered job still no-ops when
a configuration flag stops it (off mode, a missing VK user token, a Telegram
session nobody logged in), so the report says which flag - never "scheduled,
therefore covered".

Since P52 none of this is hand-copied prose: ``SCHEDULED_COLLECTORS``,
``ON_DEMAND_COLLECTORS``, ``SESSION_GATED_COLLECTORS``, ``BLOCKED_REASONS`` and
``COLLECTORS`` are derived at import from ``pipeline.collector_job_specs()`` -
the same tuple ``pipeline._build_scheduler`` registers - plus each spec's gates.
A collector cannot be called scheduled in a report before it is scheduled in
code, because the report has no other way to learn about it. That hand-written
duplication is what drifted twice: ``SCHEDULED_COLLECTORS`` named only telegram
while the hourly ``metrics-and-learning`` job already ran the Дзен and VK
collectors (``s8_analytics/__init__.py:438`` and ``:453``), and ``COLLECTORS``
kept ``telegraph: None`` while ``telegraph-metrics`` was registered.

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

from tgfarm import pipeline
from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import CrosspostLog, PostStat
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

log = get_logger(__name__)

#: Where the metrics of each platform are read from, by module the farm owns -
#: derived from the collector registry, so a module listed here has a registered
#: job behind it. A platform missing from this mapping has ``None`` from
#: ``dict.get``: nobody reads its metrics at all, which is a coverage gap of
#: ours, not a platform limitation.
COLLECTORS: dict[str, str | None] = {
    target.platform: target.module
    for spec in pipeline.collector_job_specs()
    for target in spec.targets
}


def _scheduled_wording(spec, platform: str) -> str:
    """The schedule sentence for one platform, built from the spec's own fields.

    Byte-shape inherited from the hand-written telegram entry it replaces: an
    operator can check the named job ids rather than take the claim on trust, and
    ``s8_analytics.freshness`` quotes this string verbatim - two panels wording
    one platform two ways is the defect ``test_metrics_freshness`` pins against.
    """
    target = spec.target(platform)
    text = (
        f"задача планировщика `{spec.job_id}` (и разовая `{spec.startup_job_id}`) "
        f"зовёт {target.entry} сама"
    )
    manual = spec.manual.get(platform)
    if manual:
        text += f", а вручную - {manual}"
    return text


#: Collectors a scheduler job runs by itself, with the job ids named so an
#: operator can check the schedule rather than take the claim on trust. A gap
#: here is ours to fix in code, never the platform's or the operator's. Derived
#: membership: the ids come from the registration tuple itself.
SCHEDULED_COLLECTORS: dict[str, str] = {
    platform: _scheduled_wording(spec, platform)
    for spec in pipeline.collector_job_specs()
    for platform in spec.platforms
}

#: Collectors that exist but are reached only when somebody asks for them.
#: Derived: a collector with no registered job. The set is empty today - every
#: collector the farm owns is on a schedule - and an empty dict is a real state,
#: not a placeholder.
ON_DEMAND_COLLECTORS: dict[str, str] = {
    platform: f"сборщик {module} есть, но задачи планировщика нет: покрытие растёт, "
    "только пока оператор жмёт кнопку"
    for platform, module in COLLECTORS.items()
    if platform not in SCHEDULED_COLLECTORS
}


def _gates_by_kind(platform: str, kind: str):
    """Spec gates of one kind for one platform, read from the registry."""
    return [
        gate
        for spec in pipeline.metrics_collector_jobs().get(platform, ())
        for gate in spec.gates_for(platform)
        if gate.kind == kind
    ]


#: Collectors that are scheduled but read through a credential only the operator
#: can supply. This is the fourth kind of silence: the schedule exists and runs,
#: it simply has nothing to read with, and it says so instead of inventing a
#: number. Kept as wording, never as a value - the gate check stats the session
#: file's existence at most (``pipeline._telegram_session_present``); its
#: contents are never opened or echoed here, so this module still makes no
#: network call and writes nothing.
SESSION_GATED_COLLECTORS: dict[str, str] = {
    platform: gates[0].description
    for platform in SCHEDULED_COLLECTORS
    if (gates := _gates_by_kind(platform, "session"))
}

#: Collectors that cannot run without an operator-provided token the farm can
#: check for emptiness in configuration alone. Kept as wording, never as a
#: value: no secret is read or echoed here.
BLOCKED_REASONS: dict[str, str] = {
    platform: gates[0].description
    for platform in SCHEDULED_COLLECTORS
    if (gates := _gates_by_kind(platform, "credential"))
}

#: Which kind of silence a platform's measurement is, as one machine-readable
#: word. The panel must keep the kinds apart and cannot do it from ``collector``
#: alone, because a scheduled collector that provably writes no rows (a stopped
#: flag, a missing session) looks identical to a working one in that field:
#:
#: * ``absent`` - no collector module and no registered job; our gap to code;
#: * ``scheduled_other_table`` - a job runs, but its snapshots do not land in
#:   ``post_stats``, the only table the learning layers read (telegraph);
#: * ``scheduled_gated`` - a job is registered yet at least one configuration
#:   gate currently stops it; the reason names every stopping flag;
#: * ``scheduled_running`` - registered and able to run under the current
#:   configuration.
STATE_ABSENT = "absent"
STATE_OTHER_TABLE = "scheduled_other_table"
STATE_GATED = "scheduled_gated"
STATE_RUNNING = "scheduled_running"


def blocking_gates(platform: str) -> tuple:
    """Gates that stand between a registered job and a written row, right now.

    Reads configuration only (cached settings, one existence check); nothing here
    opens a credential or touches the network.
    """
    return tuple(
        gate
        for spec in pipeline.metrics_collector_jobs().get(platform, ())
        for gate in spec.gates_for(platform)
        if not gate.ready()
    )


def collector_state(platform: str) -> str:
    """One word for why this platform's silence is what it is - see ``STATE_*``."""
    jobs = pipeline.metrics_collector_jobs()
    if platform not in jobs:
        return STATE_ABSENT
    if not any(spec.feeds_post_stats for spec in jobs[platform]):
        return STATE_OTHER_TABLE
    if blocking_gates(platform):
        return STATE_GATED
    return STATE_RUNNING


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


def _gate_note(platform: str, *, skip_kinds: tuple[str, ...] = ()) -> str:
    """Name the flags that stop a registered job right now; "" when none do.

    `skip_kinds` drops gates the caller already narrated in full - the session
    sentence must not appear twice in one reason.
    """
    gates = [gate for gate in blocking_gates(platform) if gate.kind not in skip_kinds]
    if not gates:
        return ""
    return "; тик зарегистрирован, но при текущей конфигурации ничего не запишет: " + "; ".join(
        gate.description for gate in gates
    )


def _classify(row: dict[str, Any], platform: str) -> tuple[str, str | None]:
    """``(status, reason)`` - "collected zero" and "could not collect" apart."""
    state = row.get("collector_state") or collector_state(platform)
    has_anything = row["published"] > 0 or row["crosspost_posted"] > 0
    if not has_anything:
        return STATUS_UNMEASURED, "публикаций этого платформа нет"
    if row["measured_with_signal"] > 0:
        if row["measured_posts"] < (row["published"] or row["measured_posts"]):
            gap = row["published"] - row["measured_posts"]
            note = ""
            if state == STATE_ABSENT:
                note = (
                    "; сборщика метрик в s8 нет, поэтому недостающие снимки появятся "
                    "только после того, как он появится"
                )
            elif state == STATE_OTHER_TABLE:
                jobs = pipeline.metrics_collector_jobs().get(platform, ())
                note = (
                    f"; расписание есть: {SCHEDULED_COLLECTORS[platform]}, но его снимки "
                    f"пишутся в `{jobs[0].snapshot_table}`, а обучение читает только "
                    "`post_stats`: недостающих строк эта задача не принесёт"
                )
            elif platform in SESSION_GATED_COLLECTORS:
                # The fourth silence: scheduled, not broken, still empty until the
                # operator hands the collector a session.
                note = (
                    f"; расписание есть: {SCHEDULED_COLLECTORS[platform]}, "
                    f"но {SESSION_GATED_COLLECTORS[platform]}"
                )
                note += _askable_note(row)
                note += _gate_note(platform, skip_kinds=("session",))
            elif platform in ON_DEMAND_COLLECTORS:
                note = f"; остальное не соберётся само: {ON_DEMAND_COLLECTORS[platform]}"
            elif platform in BLOCKED_REASONS and row["crosspost_with_remote"] > row[
                "measured_posts"
            ]:
                note = f"; остальное недоступно без нового ключа: {BLOCKED_REASONS[platform]}"
            elif blocking := _gate_note(platform):
                # Scheduled but provably idle under the current configuration:
                # the note names every stopping flag, so "scheduled" never reads
                # as "covered".
                note = f"; расписание есть: {SCHEDULED_COLLECTORS[platform]}{blocking}"
            return (
                STATUS_PARTIAL,
                f"измерено {row['measured_posts']} из {row['published']}: "
                f"{gap} публикаций без ни одного снимка{note}",
            )
        return STATUS_MEASURED, None
    if platform in BLOCKED_REASONS:
        return STATUS_BLOCKED, BLOCKED_REASONS[platform]
    if state == STATE_ABSENT:
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
        "значимого снимка: сбор либо не запускался, либо ничего не сопоставил"
        + _gate_note(platform),
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
                "collector_state": collector_state(platform),
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
    "STATE_ABSENT",
    "STATE_GATED",
    "STATE_OTHER_TABLE",
    "STATE_RUNNING",
    "STATUS_BLOCKED",
    "STATUS_MEASURED",
    "STATUS_PARTIAL",
    "STATUS_UNMEASURED",
    "blocking_gates",
    "collector_state",
    "coverage_table",
    "measurement_coverage",
]

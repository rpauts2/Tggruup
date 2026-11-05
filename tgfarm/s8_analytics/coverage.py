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

A fourth must not be conflated with the first either: ``published`` is the farm's
own claim, and the publisher used to stamp it on sends that never happened, so
each platform now also reports how many of those rows anything on the platform
side can point at (``published_evidenced`` / ``published_unevidenced`` /
``published_unrulled``, under the proof rules ``s8_analytics.formats`` applies —
the same ones ``maintenance.publication_evidence_repair`` moves such rows for).
The totals stand so a reader can see both: a platform can be fully measured and
still not be proven to have published anything.

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

# One definition of "this publication can be pointed at", shared with the report
# that computes format rates and with the sweep that repairs the rows: a coverage
# audit and an analytics report that disagree about what a phantom publication is
# would be the second copy of that rule, which is how this repo drifted before.
from tgfarm.s8_analytics.formats import EVIDENCE_RULES, RULE_MESSAGE_ID

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


def _ratio(numerator: int, denominator: int) -> float | None:
    """A share, or ``None`` when there is nothing to divide by.

    ``0.0`` used to answer «no publication exists» with «nothing was measured»,
    which is exactly the fabricated zero the operator rules out: a farm that has
    published nothing and a farm nobody reads are two different facts, and a
    reader who sees only the number cannot tell them apart.
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _proof_condition(platform: str):
    """SQL for «anything on the platform side can point at this publication».

    The rules are imported from `s8_analytics.formats`, which imports them from
    the sweep in `maintenance` - one definition of a phantom publication, so the
    coverage audit and the repair can never disagree about what it is counting.

    ``None`` when the platform has no rule yet. That is not proof: it is the
    absence of a way to disprove, and the rows are counted under
    ``published_unrulled`` so the difference stays visible instead of quietly
    reading as evidenced.
    """
    rule = EVIDENCE_RULES.get(platform)
    if rule is None:
        return None
    if rule == RULE_MESSAGE_ID:
        return PostRow.tg_message_id.isnot(None)
    return (
        select(CrosspostLog.id)
        .where(
            CrosspostLog.post_public_id == PostRow.public_id,
            CrosspostLog.platform == platform,
            CrosspostLog.status == "posted",
        )
        .exists()
    )


#: A journal row that carries something to point at — an id or a URL.
def _attributed_condition(platform: str):
    """SQL for «a published post the collector could match to a remote object».

    Counted as *posts*, not as journal rows: `crosspost_with_remote` next to
    `measured_posts: 0` read as coverage of 399 publications that had never been
    measured once, which is the conflation this field exists to avoid.
    """
    return (
        select(CrosspostLog.id)
        .where(
            CrosspostLog.post_public_id == PostRow.public_id,
            CrosspostLog.platform == platform,
            CrosspostLog.status == "posted",
            or_(
                CrosspostLog.remote_id.isnot(None) & (CrosspostLog.remote_id != ""),
                CrosspostLog.url.isnot(None) & (CrosspostLog.url != ""),
            ),
        )
        .exists()
    )


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
    proof = _proof_condition(platform)
    published_filters = (PostRow.status == "published", func.lower(ChannelRow.platform) == platform)
    with session_scope() as session:
        published = int(
            session.execute(
                select(func.count(PostRow.id))
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(*published_filters)
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
        # What the platform itself can point at, kept apart from the count of rows
        # that merely claim it: `published` is what the farm wrote down, and a dry
        # run used to write that down too.
        published_evidenced = int(
            session.execute(
                select(func.count(PostRow.id))
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(*(published_filters + ((proof,) if proof is not None else ())))
            ).scalar_one()
            or 0
        )
        # Snapshots only cover the output when they hang off a publication: a
        # snapshot of a draft is a measurement of something nobody published.
        measured_published = int(
            session.execute(
                select(func.count(func.distinct(PostStat.post_id)))
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(
                    func.lower(ChannelRow.platform) == platform,
                    PostRow.status == "published",
                )
            ).scalar_one()
            or 0
        )
        measured_evidenced = int(
            session.execute(
                select(func.count(func.distinct(PostStat.post_id)))
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(
                    *(
                        (
                            func.lower(ChannelRow.platform) == platform,
                            PostRow.status == "published",
                        )
                        + ((proof,) if proof is not None else ())
                    )
                )
            ).scalar_one()
            or 0
        )
        # Attribution as posts rather than as journal rows, so a platform whose
        # 399 publications were never measured once cannot read as 399 attributed.
        attributed_posts = int(
            session.execute(
                select(func.count(PostRow.id))
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(
                    *published_filters,
                    _attributed_condition(platform),
                )
            ).scalar_one()
            or 0
        )
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
        # The publication-evidence split of `published`: `+ unevidenced + unrulled`
        # is exactly `published`, and `unrulled` is the share nobody can disprove
        # rather than a share somebody proved.
        "published_evidenced": published_evidenced,
        "published_unevidenced": max(0, published - published_evidenced),
        "published_unrulled": published if proof is None else 0,
        "measured_published": measured_published,
        "measured_evidenced": measured_evidenced,
        "attributed_posts": attributed_posts,
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
        # A row that declares no `measured_published` is vouched for by its caller,
        # exactly as `s10_format._row_evidence_status` reads a row that declares no
        # state: hand-built rows keep the behaviour they always had, and only rows
        # this module produced itself get the population check.
        declared_measured = row.get("measured_published")
        measured_published = int(declared_measured or 0)
        if (
            declared_measured is not None
            and measured_published < row["published"]
            and row["measured_posts"] > measured_published
        ):
            # Two populations, one ratio. A snapshot can hang off a post that was
            # never published, and counting those as coverage would let a platform
            # be called fully measured on the strength of drafts: the numerator and
            # the denominator have to be about the same posts before either number
            # means anything.
            return (
                STATUS_PARTIAL,
                f"измерено {measured_published} из {row['published']} публикаций: "
                f"{row['measured_posts'] - measured_published} снимков относятся не к "
                "опубликованным постам, а к черновикам или отклонённым",
            )
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
            disproved = int(row.get("published_unevidenced") or 0)
            rule = EVIDENCE_RULES.get(platform)
            unevidenced = ""
            if disproved and rule:
                # The denominator is itself in question, and the sentence has to
                # say so: these rows are publications the farm wrote down for a
                # send it cannot point at, so "measured 1 of 4" is a ratio over a
                # claim. Named, not removed - `published` above still counts them.
                unevidenced = (
                    f"; из {row['published']} публикаций {disproved} без доказательства "
                    f"отправки (доказательство: {rule})"
                )
            return (
                STATUS_PARTIAL,
                f"измерено {row['measured_posts']} из {row['published']}: "
                f"{gap} публикаций без ни одного снимка{note}{unevidenced}",
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

    ``coverage`` and ``coverage_evidenced`` are ``None`` when there is nothing of
    the kind they divide by: no publication means no share of publications, not a
    share of zero.
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
        "published_evidenced": sum(row["published_evidenced"] for row in platforms.values()),
        "published_unevidenced": sum(row["published_unevidenced"] for row in platforms.values()),
        "published_unrulled": sum(row["published_unrulled"] for row in platforms.values()),
        "measured_published": sum(row["measured_published"] for row in platforms.values()),
        "measured_evidenced": sum(row["measured_evidenced"] for row in platforms.values()),
        "attributed_posts": sum(row["attributed_posts"] for row in platforms.values()),
    }
    # `published + unevidenced + unrulled == published`, and it is the pair a
    # reader needs: the first ratio answers «how much of what we claim is read»,
    # the second answers «how much of what can be proved is read». Reported
    # together, because dropping the unproven rows from the denominator and
    # saying nothing would make the number look better, which is not the same
    # thing as making it true.
    coverage = _ratio(totals["measured_posts"], totals["published"])
    coverage_evidenced = _ratio(
        totals["measured_evidenced"],
        totals["published_evidenced"] + totals["published_unrulled"],
    )
    return {
        "generated_at": current.isoformat(),
        "platforms": platforms,
        "totals": totals,
        "coverage": coverage,
        "coverage_evidenced": coverage_evidenced,
        "gaps": [
            {
                "platform": row["platform"],
                "status": row["status"],
                "unmeasured": max(0, row["published"] - row["measured_posts"]),
                "unmeasured_evidenced": max(
                    0,
                    row["published_evidenced"] + row["published_unrulled"]
                    - row["measured_evidenced"],
                ),
                "unevidenced_published": row["published_unevidenced"],
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
    """The audit as rows a dashboard or a report can print directly.

    `attributable` stays the journal-row count it always was, because a panel
    reads it; `attributed_posts` is the same claim about *posts*, which is the
    claim that has to sit next to `measured_posts` — 399 journal rows with remote
    ids describe 399 publications nobody ever measured, not coverage.
    """
    report = measurement_coverage(now=now)
    return [
        {
            "platform": row["platform"],
            "published": row["published"],
            "published_evidenced": row["published_evidenced"],
            "published_unevidenced": row["published_unevidenced"],
            "measured_posts": row["measured_posts"],
            "measured_published": row["measured_published"],
            "measured_evidenced": row["measured_evidenced"],
            "measured_with_signal": row["measured_with_signal"],
            "stat_rows": row["stat_rows"],
            "attributable": row["crosspost_with_remote"],
            "attributed_posts": row["attributed_posts"],
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

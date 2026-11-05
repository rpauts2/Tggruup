"""How fresh are the metrics we actually have, platform by platform.

``post_stats`` is an append-only set of snapshots: a platform either has rows
that were really collected or it has nothing. This module reports that
difference instead of printing zeroes. Absent data becomes ``never`` (nothing
collected yet) or ``blocked`` (collection is impossible for a named reason),
never a fabricated ``0``.

Attribution goes through ``PostStat.post_id -> Post.channel_id ->
Channel.platform``: a snapshot row says nothing about its platform on its own.
That is the only attribution the schema stores, so a snapshot taken for a
crossposted VK/Dzen publication is counted under the platform of the channel
that owns the post, not under the destination it was copied to.

Timestamps are returned as ISO 8601 strings in UTC so the result is directly
JSON-serialisable for the API layer.

``stale`` is a purely factual age test ("the newest snapshot is older than
24 hours"). ``status`` is the classification: a platform can be ``blocked``
while its last zero-only snapshot is already old, and then ``stale`` is still
true because the row really is old.

A per-platform row can only ever speak about ``post_stats``, and that is not
enough. ``telegram`` and ``telegraph`` are both scheduled, both are in the
catalogue, and only the first is supposed to land rows in ``post_stats`` at all;
meanwhile a scheduled collector whose job fires on time and writes nothing looks
identical, in a platform row, to a collector that never ran. So this module
reports per *collector* as well: for every reader ``pipeline`` registers, it
names the destination table the registry declares and reads the newest timestamp
in that table. The freshness verdict comes out of the table and nowhere else -
the schedule is a separate field beside it, never an input to it. That is the
whole point: "the collector is scheduled" has to stay sayable without letting
anybody read it as "the numbers are fresh".

Where a collector is scheduled and its table holds nothing, the honest word is
``нет отчёта`` with the age left ``None`` - never a ``0`` and never ``ok``. The
run journal (``pipeline_runs``, under the stage names the registry itself
declares) is then consulted to tell the two silences apart: "the tick never got
as far as recording an attempt" and "the tick ran and produced no dated row".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, inspect, or_, select, union
from sqlalchemy.exc import SQLAlchemyError

from tgfarm import pipeline
from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Base, CrosspostLog, PipelineRun, PostStat
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.platforms import CATALOG
from tgfarm.s8_analytics.coverage import SCHEDULED_COLLECTORS, SESSION_GATED_COLLECTORS

log = get_logger(__name__)

STALE_AFTER_HOURS = 24.0

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_NEVER = "never"
STATUS_BLOCKED = "blocked"
STATUSES: tuple[str, ...] = (STATUS_OK, STATUS_STALE, STATUS_NEVER, STATUS_BLOCKED)

# Operator-facing texts. The VK wording names the exact API answer, so an
# operator does not spend an evening retrying a group token.
VK_TOKEN_REASON = "нужен VK_USER_TOKEN (групповой токен отклоняется с error 27)"
# Telegram's collector exists and is on the scheduler's clock (`telegram-post-metrics`,
# registered in `pipeline._build_scheduler`), so "there is no Telegram collector" has
# been false since it was written. The wording is borrowed from `coverage`, not
# paraphrased: two panels describing one platform in two ways is the defect.
# NOTE the class of claim this is: it says the job exists and names the gate that
# stops it, and it is emitted only when `post_stats` really holds no Telegram row.
# Being scheduled is never evidence of being fresh, so the data side of the same
# question - the newest row in the table this job feeds, its age, and whether any
# run recorded an attempt at all - lives in `collector_freshness` below.
TELEGRAM_BLOCKED_REASON = (
    f"{SCHEDULED_COLLECTORS['telegram']}: снимков метрик нет, и {SESSION_GATED_COLLECTORS['telegram']}"
)
DZEN_NO_PUBLICATIONS_REASON = "опубликованных статей нет"
NO_SNAPSHOTS_REASON = "снимков метрик нет"

# A snapshot of all zeroes is not a measurement: VK returns zeros for posts it
# could not read, and publishing one as "fresh metrics" would be a lie.
_NON_ZERO_COUNTERS = tuple(
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


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo, so naive values are read as UTC, never as local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_hours(latest: datetime | None, now: datetime) -> float | None:
    if latest is None:
        return None
    # A clock skew must not produce a negative age that reads like a bug.
    return max(0.0, (now - latest).total_seconds() / 3600.0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _vk_token_present() -> bool:
    """Treat an unreadable settings object as "no token", never as "maybe"."""
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - freshness must not break the dashboard
        log.warning("s8: settings unavailable for freshness: %s", redact_sensitive_text(exc, limit=120))
        return False
    return bool(str(getattr(settings, "vk_user_token", "") or "").strip())


def _load_facts() -> tuple[dict[str, dict[str, Any]], int, int, int, str | None]:
    """One session, read-only: per-platform index, totals, and blocker evidence.

    Returns ``(index, snapshots_total, vk_meaningful, dzen_publications, error)``.
    On a database that was never bootstrapped the read fails; a dashboard should
    show that failure instead of a 500 or invented ``never`` statuses.
    """
    try:
        with session_scope() as session:
            grouped = session.execute(
                select(
                    ChannelRow.platform,
                    func.count(PostStat.id),
                    func.max(PostStat.collected_at),
                )
                .select_from(PostStat)
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .group_by(ChannelRow.platform)
            ).all()
            snapshots_total = int(
                session.execute(select(func.count(PostStat.id))).scalar_one() or 0
            )
            vk_meaningful = int(
                session.execute(
                    select(func.count(PostStat.id))
                    .join(PostRow, PostRow.id == PostStat.post_id)
                    .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                    .where(ChannelRow.platform == "vk", or_(*_NON_ZERO_COUNTERS))
                ).scalar_one()
                or 0
            )
            dzen_publications = int(
                session.execute(_dzen_publications_query()).scalar_one() or 0
            )
    except SQLAlchemyError as exc:
        detail = redact_sensitive_text(exc, limit=200)
        log.warning("s8: freshness read failed: %s", detail)
        return {}, 0, 0, 0, f"post_stats недоступна: {detail}"

    index: dict[str, dict[str, Any]] = {}
    for platform, count, latest in grouped:
        key = str(platform or "").strip().lower() or "unknown"
        bucket = index.setdefault(key, {"snapshots": 0, "last": None})
        bucket["snapshots"] += int(count or 0)
        newest = _utc(latest)
        if newest is not None and (bucket["last"] is None or newest > bucket["last"]):
            bucket["last"] = newest
    return index, snapshots_total, vk_meaningful, dzen_publications, None


def _dzen_publications_query():
    """Count distinct Dzen publications from both places that record them.

    A Dzen article can be a published post on a Dzen channel, a ``posted``
    crosspost log row, or both; a union deduplicates the pair.
    """
    published = (
        select(PostRow.public_id.label("public_id"))
        .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
        .where(
            ChannelRow.platform == "dzen",
            PostRow.status == "published",
            PostRow.public_id != "",
        )
    )
    posted = select(CrosspostLog.post_public_id.label("public_id")).where(
        CrosspostLog.platform == "dzen",
        CrosspostLog.status == "posted",
        CrosspostLog.post_public_id != "",
    )
    combined = union(published, posted).subquery()
    return select(func.count()).select_from(combined)


def _classify(
    platform_id: str,
    *,
    snapshots: int,
    age: float | None,
    vk_token: bool,
    vk_meaningful: int,
    dzen_publications: int,
) -> tuple[str, str | None]:
    """Return ``(status, reason)`` for one platform.

    Order matters: a hard blocker wins over "no data yet", because the operator
    needs the requirement, not a vague "never".
    """
    if platform_id == "vk" and vk_meaningful == 0 and not vk_token:
        return STATUS_BLOCKED, VK_TOKEN_REASON
    if platform_id == "telegram" and snapshots == 0:
        return STATUS_BLOCKED, TELEGRAM_BLOCKED_REASON
    if platform_id == "dzen" and snapshots == 0 and dzen_publications == 0:
        return STATUS_BLOCKED, DZEN_NO_PUBLICATIONS_REASON

    if snapshots == 0 or age is None:
        reason = (
            "публикации есть, но снимков метрик нет"
            if platform_id == "dzen" and dzen_publications
            else NO_SNAPSHOTS_REASON
        )
        return STATUS_NEVER, reason

    if platform_id == "vk" and vk_meaningful == 0:
        # The token is present here (otherwise the first rule fired), but every
        # snapshot is all zeroes. Reporting "ok" would dress up an empty read.
        return STATUS_NEVER, "снимки ВК есть, но все счётчики нулевые: измерять нечего"

    if age > STALE_AFTER_HOURS:
        reason = f"последний снимок {age:.1f} ч назад (порог {STALE_AFTER_HOURS:.0f} ч)"
        if platform_id == "vk" and not vk_token:
            reason = f"{reason}; обновление недоступно: {VK_TOKEN_REASON}"
        return STATUS_STALE, reason

    return STATUS_OK, None


# ------------------------------------------------------- collector-level claims
#: What one collector's own destination table can answer. Kept apart from the
#: platform statuses on purpose: a platform row says "do I hold numbers for this
#: platform", a collector row says "does the job the scheduler runs leave dated
#: rows in the table it is supposed to fill", and only the second one catches a
#: collector that is registered, healthy, on time and producing nothing.
COLLECTOR_OK = "ok"
COLLECTOR_STALE = "stale"
COLLECTOR_NO_REPORT = "no_report"
COLLECTOR_MISSING_TABLE = "missing_table"
COLLECTOR_UNREADABLE = "unreadable"
COLLECTOR_STATUSES: tuple[str, ...] = (
    COLLECTOR_OK,
    COLLECTOR_STALE,
    COLLECTOR_NO_REPORT,
    COLLECTOR_MISSING_TABLE,
    COLLECTOR_UNREADABLE,
)

#: The wording for "this collector cannot tell you how fresh anything is". An
#: empty destination table holds no report: it is never a ``0``, and it is never
#: ``ok``, however many jobs point at it.
NO_REPORT_CLAIM = "нет отчёта"


def _registry_collectors() -> tuple[tuple[Any, Any], ...]:
    """``(spec, target)`` pairs, taken straight from the scheduler's own list.

    Not a second register of the collectors: this is the tuple
    ``pipeline._build_scheduler`` adds jobs from, so a collector the report calls
    scheduled is scheduled in code, and one that stops being registered stops
    being reported.
    """
    return tuple(
        (spec, target)
        for spec in pipeline.collector_job_specs()
        for target in spec.targets
    )


def _dated_tables() -> tuple[str, ...]:
    """Every ORM table that dates its rows with ``collected_at``, by name.

    Derived from the schema rather than listed here, so a snapshot table no
    collector writes to is still counted instead of staying invisible next to the
    ones that are.
    """
    return tuple(
        sorted(
            name
            for name, table in Base.metadata.tables.items()
            if "collected_at" in table.c
        )
    )


def _collector_table(name: str):
    """The ORM table a registry entry names, or ``None`` if the schema has no such."""
    return Base.metadata.tables.get(str(name or "").strip())


def _table_is_here(session, name: str) -> bool:
    """Whether this database physically has the table, without querying it.

    Probing instead of selecting keeps one absent table from poisoning the
    session the remaining collectors are still read from.
    """
    try:
        return bool(inspect(session.get_bind()).has_table(name))
    except SQLAlchemyError as exc:
        detail = redact_sensitive_text(exc, limit=120)
        log.warning("s8: freshness table probe failed for %s: %s", name, detail)
        return False


def _table_facts(session, table, platform: str | None) -> tuple[int, datetime | None]:
    """Rows and the newest ``collected_at``, counted inside one destination table.

    A shared table says nothing about its platform on its own, so it is
    attributed the only way the schema stores (``post_id -> Post.channel_id ->
    Channel.platform``); a table with no ``post_id`` belongs to exactly one
    collector and is therefore counted whole.
    """
    dated = table.c["collected_at"] if "collected_at" in table.c else None
    if "post_id" in table.c and platform is not None:
        stmt = (
            select(func.count(), func.max(dated))
            .select_from(table)
            .join(PostRow, PostRow.id == table.c.post_id)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(ChannelRow.platform == platform)
        )
    else:
        stmt = select(func.count(), func.max(dated)).select_from(table)
    total, newest = session.execute(stmt).one()
    return int(total or 0), _utc(newest)


def _journal_facts(
    session, stages: tuple[str, ...], *, journal_is_here: bool
) -> tuple[int | None, datetime | None, str | None]:
    """How often this job reached the point of recording, and when it last did.

    Three different no-answers stay three different values: ``None`` runs means
    the registry names no stage for this job (or the journal itself is absent), so
    the silence is unexplainable, while ``0`` runs means the journal is readable
    and genuinely holds nothing - which is the difference between "the tick never
    got that far" and "nobody knows".
    """
    if not stages or not journal_is_here:
        return None, None, None
    where = PipelineRun.stage.in_(stages)
    runs = int(
        session.execute(select(func.count()).select_from(PipelineRun).where(where)).scalar_one() or 0
    )
    newest = session.execute(
        select(PipelineRun.created_at, PipelineRun.status)
        .where(where)
        .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
        .limit(1)
    ).first()
    if newest is None:
        return runs, None, None
    stamp = _utc(newest[0])
    status = str(newest[1]) if newest[1] is not None else None
    return runs, stamp, status


def _collector_status(rows: int | None, age: float | None) -> str:
    """The data verdict for one collector, from its destination table alone.

    This signature is the defect class: no schedule, no job id, no gate and no
    cadence can be read here, so a registered collector with an empty table can
    only ever come out ``no_report``.
    """
    if rows is None or rows == 0 or age is None:
        return COLLECTOR_NO_REPORT
    if age > STALE_AFTER_HOURS:
        return COLLECTOR_STALE
    return COLLECTOR_OK


def _attempt_clause(row: dict[str, Any], *, last_run: datetime | None, latest: datetime | None) -> str:
    """The schedule-and-journal sentence, kept beside the data sentence.

    Says what the registry and the run log prove - which job, how often, whether
    any run ever recorded an attempt - and never what the data is.
    """
    stages = tuple(row["journal_stages"])
    job = row["job_id"]
    entry = row["entry"]
    table = row["snapshot_table"]
    interval = row["interval_hours"]
    if row["scheduled"] and stages:
        plain = (
            f"задача `{job}` (и разовая `{row['startup_job_id']}`) стоит в расписании "
            f"каждые {interval} ч и зовёт {entry} сама"
        )
    elif row["scheduled"]:
        plain = f"задача `{job}` стоит в расписании каждые {interval} ч"
    else:
        plain = f"задачи планировщика за `{table}` нет"
    if not stages:
        return (
            f"{plain}; этап в `pipeline_runs` за этой задачей не регистрирован, "
            "почему именно молчит - неизвестно"
        )
    named = ", ".join(f"`{stage}`" for stage in stages)
    if row["runs_recorded"] is None:
        return (
            f"{plain}, а журнал `pipeline_runs` недоступен: дошёл ли тик до записи - "
            "неизвестно, и это не ноль"
        )
    if row["runs_recorded"] == 0:
        return (
            f"{plain}, но в `pipeline_runs` нет ни одной записи этапа {named}: "
            "ни один запуск планировщика ещё не дошёл до записи"
        )
    if last_run is None:
        return f"{plain}, а записей этапа {named} в `pipeline_runs` нет"
    stamp = last_run.isoformat()
    if latest is None:
        return (
            f"{plain}; последний запуск этапа {named} - {stamp} "
            f"(статус `{row['last_run_status']}`), а датированных снимков нет"
        )
    if last_run >= latest:
        return (
            f"{plain}; последний запуск этапа {named} - {stamp} "
            f"(статус `{row['last_run_status']}`) нового снимка не оставил"
        )
    return plain


def _attributes_by_post(table_name: str) -> bool:
    """Whether rows of this table can be pinned to one platform at all."""
    table = _collector_table(table_name)
    return table is not None and "post_id" in table.c


def _collector_reason(
    row: dict[str, Any],
    *,
    status: str,
    latest: datetime | None,
    age: float | None,
    last_run: datetime | None,
) -> str | None:
    """Operator-facing sentence for one collector, always led by the data."""
    if status == COLLECTOR_OK:
        return None
    table = row["snapshot_table"]
    if status == COLLECTOR_MISSING_TABLE:
        return f"{NO_REPORT_CLAIM}: таблицы `{table}` в этой базе нет"
    if row["rows"] is None:
        return f"{NO_REPORT_CLAIM}: таблицу `{table}` прочитать не удалось"
    attempt = _attempt_clause(row, last_run=last_run, latest=latest)
    if row["rows"] == 0:
        scope = f"для платформы {row['platform']}" if _attributes_by_post(table) else "во всей таблице"
        return f"{NO_REPORT_CLAIM}: в таблице `{table}` ни одного снимка {scope}; {attempt}"
    if latest is None:
        return (
            f"{NO_REPORT_CLAIM}: снимки в таблице `{table}` есть ({row['rows']}), "
            f"но ни один не датирован; {attempt}"
        )
    return (
        f"снимков {row['rows']}, последний в `{table}` - {latest.isoformat()}, "
        f"{age:.1f} ч назад (порог {STALE_AFTER_HOURS:.0f} ч): "
        f"новый снимок не записан с {latest.isoformat()}; {attempt}"
    )


def _collector_row(spec: Any, target: Any) -> dict[str, Any]:
    """One collector described by the registry, with the data fields left empty."""
    platform = str(target.platform)
    return {
        "id": f"{spec.job_id}:{platform}",
        "platform": platform,
        "module": str(target.module),
        "entry": str(target.entry),
        "job_id": str(spec.job_id),
        "startup_job_id": str(spec.startup_job_id),
        "interval_hours": int(spec.interval_hours),
        # The two schedule claims, kept in fields of their own so nothing below
        # can mistake them for a statement about the data.
        "scheduled": True,
        "schedule": SCHEDULED_COLLECTORS.get(platform),
        "snapshot_table": str(spec.snapshot_table),
        "feeds_post_stats": bool(spec.feeds_post_stats),
        "journal_stages": list(spec.run_stages),
        # Everything from here is read out of the table, or stays None.
        "rows": None,
        "last_collected_at": None,
        "age_hours": None,
        "stale": False,
        "fresh": False,
        "data_status": COLLECTOR_UNREADABLE,
        "reason": None,
        "runs_recorded": None,
        "last_run_at": None,
        "last_run_status": None,
        "scheduled_but_silent": False,
    }


def _outside_table_row(name: str) -> dict[str, Any]:
    """A dated table the registry never names, reported rather than left unseen."""
    return {
        "id": name,
        "platform": None,
        "table": name,
        "snapshot_table": name,
        "scheduled": False,
        "job_id": None,
        "startup_job_id": None,
        "entry": None,
        "interval_hours": None,
        "journal_stages": [],
        "rows": None,
        "last_collected_at": None,
        "age_hours": None,
        "stale": False,
        "fresh": False,
        "data_status": COLLECTOR_UNREADABLE,
        "reason": None,
        "runs_recorded": None,
        "last_run_at": None,
        "last_run_status": None,
        "scheduled_but_silent": False,
    }


def _fill_collector(
    session, row: dict[str, Any], *, current: datetime, journal_is_here: bool
) -> None:
    """Attach one collector's data claims, read from the table it is meant to feed."""
    table = _collector_table(row["snapshot_table"])
    if table is None:
        row["data_status"] = COLLECTOR_MISSING_TABLE
        row["reason"] = (
            f"{NO_REPORT_CLAIM}: в схеме нет таблицы `{row['snapshot_table']}`, "
            f"которую должна наполнять задача `{row['job_id']}`"
        )
        row["scheduled_but_silent"] = bool(row["scheduled"])
        return
    if not _table_is_here(session, row["snapshot_table"]):
        row["data_status"] = COLLECTOR_MISSING_TABLE
        row["reason"] = _collector_reason(
            row, status=COLLECTOR_MISSING_TABLE, latest=None, age=None, last_run=None
        )
        row["scheduled_but_silent"] = bool(row["scheduled"])
        return

    rows, latest = _table_facts(session, table, row["platform"])
    runs, last_run, run_status = _journal_facts(
        session, tuple(row["journal_stages"]), journal_is_here=journal_is_here
    )
    age = _age_hours(latest, current)
    status = _collector_status(rows, age)
    row.update(
        {
            "rows": rows,
            "last_collected_at": _iso(latest),
            "age_hours": round(age, 2) if age is not None else None,
            "stale": bool(age is not None and age > STALE_AFTER_HOURS),
            "fresh": status == COLLECTOR_OK,
            "data_status": status,
            "runs_recorded": runs,
            "last_run_at": _iso(last_run),
            "last_run_status": run_status,
        }
    )
    row["scheduled_but_silent"] = bool(row["scheduled"] and status != COLLECTOR_OK)
    row["reason"] = _collector_reason(
        row, status=status, latest=latest, age=age, last_run=last_run
    )


def _fill_outside_table(session, row: dict[str, Any], *, current: datetime) -> None:
    """Attach the data claims for a dated table no registered collector names."""
    name = row["snapshot_table"]
    unregistered = f"расписания для `{name}` нет: ни одна зарегистрированная задача её не наполняет"
    table = _collector_table(name)
    if table is None or not _table_is_here(session, name):
        row["data_status"] = COLLECTOR_MISSING_TABLE
        row["reason"] = f"{NO_REPORT_CLAIM}: таблицы `{name}` в этой базе нет; {unregistered}"
        return
    rows, latest = _table_facts(session, table, None)
    age = _age_hours(latest, current)
    status = _collector_status(rows, age)
    if status == COLLECTOR_NO_REPORT and rows == 0:
        reason = f"{NO_REPORT_CLAIM}: в таблице `{name}` ни одного снимка; {unregistered}"
    elif status == COLLECTOR_NO_REPORT:
        reason = (
            f"{NO_REPORT_CLAIM}: снимки в таблице `{name}` есть ({rows}), "
            f"но ни один не датирован; {unregistered}"
        )
    elif status == COLLECTOR_STALE:
        reason = (
            f"снимков {rows}, последний - {latest.isoformat()}, {age:.1f} ч назад "
            f"(порог {STALE_AFTER_HOURS:.0f} ч): новый снимок не записан с "
            f"{latest.isoformat()}; {unregistered}"
        )
    else:
        reason = unregistered
    row.update(
        {
            "rows": rows,
            "last_collected_at": _iso(latest),
            "age_hours": round(age, 2) if age is not None else None,
            "stale": bool(age is not None and age > STALE_AFTER_HOURS),
            "fresh": status == COLLECTOR_OK,
            "data_status": status,
            "reason": reason,
        }
    )


def _load_collectors(
    current: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Per-collector and per-unregistered-table claims, in one read-only session.

    Returns ``(collectors, tables_without_collector, error)``. The registry says
    which reader exists, which call it makes and which table it should fill; every
    freshness claim is then read out of that table, so no collector can be
    ``ok`` because a job is registered.
    """
    collectors = [_collector_row(spec, target) for spec, target in _registry_collectors()]
    registered = {row["snapshot_table"] for row in collectors}
    outside = [_outside_table_row(name) for name in _dated_tables() if name not in registered]

    try:
        with session_scope() as session:
            journal_is_here = _table_is_here(session, PipelineRun.__tablename__)
            for row in collectors:
                _fill_collector(session, row, current=current, journal_is_here=journal_is_here)
            for row in outside:
                _fill_outside_table(session, row, current=current)
    except SQLAlchemyError as exc:
        detail = redact_sensitive_text(exc, limit=200)
        log.warning("s8: collector freshness read failed: %s", detail)
        for row in [*collectors, *outside]:
            row["data_status"] = COLLECTOR_UNREADABLE
            row["rows"] = None
            row["age_hours"] = None
            row["stale"] = False
            row["fresh"] = False
            row["reason"] = f"{NO_REPORT_CLAIM}: таблицы снимков недоступны: {detail}"
        return collectors, outside, f"таблицы снимков недоступны: {detail}"

    return collectors, outside, None


def collector_freshness(now: datetime | None = None) -> dict[str, Any]:
    """Every registered collector, judged by the rows it left behind.

    The mapping is ``{"generated_at", "stale_after_hours", "collectors",
    "tables_without_collector", "error"}``, where each collector row separates the
    schedule claims (``scheduled``, ``job_id``, ``interval_hours``, ``schedule``)
    from the data claims (``rows``, ``last_collected_at``, ``age_hours``,
    ``stale``, ``fresh``, ``data_status``, ``reason``) and from the attempt claims
    (``runs_recorded``, ``last_run_at``, ``last_run_status``). ``age_hours`` is
    ``None`` whenever there is nothing to date, and ``data_status`` is then
    ``no_report`` - never ``ok``, never ``fresh``.
    """
    current = _utc(now) or datetime.now(UTC)
    collectors, outside, error = _load_collectors(current)
    return {
        "generated_at": current.isoformat(),
        "stale_after_hours": STALE_AFTER_HOURS,
        "collectors": collectors,
        "tables_without_collector": outside,
        "error": error,
    }


def metrics_freshness(now: datetime | None = None) -> dict[str, Any]:
    """Per-platform snapshot count, newest timestamp, age, staleness and status.

    Every platform from ``tgfarm.platforms.CATALOG`` is present in the result,
    including platforms that were never measured. The returned mapping is::

        {
          "generated_at": "2026-11-04T12:00:00+00:00",
          "stale_after_hours": 24.0,
          "snapshots_total": 123,
          "platforms": {
            "vk": {
              "id": "vk", "name": "ВКонтакте", "symbol": "VK",
              "category": "Российские", "mode": "built_in",
              "snapshots": 40,
              "last_snapshot_at": "2026-11-04T10:00:00+00:00" | None,
              "age_hours": 2.0 | None,
              "stale": False,
              "status": "ok" | "stale" | "never" | "blocked",
              "reason": None | "нужен VK_USER_TOKEN (...)",
            },
            ...
          },
          "collectors": [
            {
              "id": "telegram-post-metrics:telegram", "platform": "telegram",
              "job_id": ..., "interval_hours": 6, "scheduled": True, "schedule": ...,
              "snapshot_table": "post_stats",
              "rows": 0 | None, "last_collected_at": ... | None,
              "age_hours": 2.0 | None, "stale": bool, "fresh": bool,
              "data_status": "ok" | "stale" | "no_report" | "missing_table" | "unreadable",
              "reason": None | "нет отчёта: ...",
              "runs_recorded": 0 | None, "last_run_at": ... | None,
              "scheduled_but_silent": bool,
            },
            ...
          ],
          "tables_without_collector": [ {...}, ... ],
          "collectors_error": None,
          "error": None,
        }

    ``snapshots_total`` counts every ``post_stats`` row, including rows whose
    channel platform is not in the catalog; per-platform counts only count what
    the catalog knows about.

    ``collectors`` and ``tables_without_collector`` come from
    ``_load_collectors`` and are the per-job side of the same question: a platform
    row can only ever describe ``post_stats``, while a collector row is judged by
    the table its own registered reader is supposed to fill. The platform status
    is deliberately not derived from any of it.
    """
    current = _utc(now) or datetime.now(UTC)
    index, snapshots_total, vk_meaningful, dzen_publications, error = _load_facts()
    collectors, outside, collectors_error = _load_collectors(current)
    vk_token = _vk_token_present()

    platforms: dict[str, dict[str, Any]] = {}
    for item in CATALOG:
        platform_id = str(item.get("id") or "").strip().lower()
        if not platform_id:
            continue
        bucket = index.get(platform_id) or {}
        snapshots = int(bucket.get("snapshots", 0) or 0)
        latest = bucket.get("last")
        age = _age_hours(latest, current)
        if error is not None:
            # The read itself failed: no data is knowable, and the reason must
            # point at the read, not at a platform requirement.
            status, reason = STATUS_BLOCKED, error
        else:
            status, reason = _classify(
                platform_id,
                snapshots=snapshots,
                age=age,
                vk_token=vk_token,
                vk_meaningful=vk_meaningful,
                dzen_publications=dzen_publications,
            )
        platforms[platform_id] = {
            "id": platform_id,
            "name": str(item.get("name") or platform_id),
            "symbol": str(item.get("symbol") or ""),
            "category": str(item.get("category") or ""),
            "mode": str(item.get("mode") or ""),
            "snapshots": snapshots,
            "last_snapshot_at": _iso(latest),
            "age_hours": round(age, 2) if age is not None else None,
            "stale": bool(age is not None and age > STALE_AFTER_HOURS),
            "status": status,
            "reason": reason,
        }

    return {
        "generated_at": current.isoformat(),
        "stale_after_hours": STALE_AFTER_HOURS,
        "snapshots_total": snapshots_total,
        "platforms": platforms,
        "collectors": collectors,
        "tables_without_collector": outside,
        "collectors_error": collectors_error,
        "error": error,
    }


def freshness_summary(now: datetime | None = None) -> dict[str, Any]:
    """Count platforms per status, total snapshots, and the newest snapshot.

    ``freshest`` is the newest ``post_stats`` row across all catalog platforms,
    regardless of whether its counters are non-zero; ``None`` means the table is
    empty. Counts always add up to ``platforms_total``.

    The collector half is counted separately, because the two questions have
    different answers on a real database: every platform can be ``never`` while a
    collector is quietly writing, and every platform can look cared-for while a
    registered collector has written nothing for a week. ``collectors_ok`` is the
    only number here that can ever mean "fresh", and ``silent_collectors`` is the
    list of jobs the scheduler runs that the data does not confirm: for each one
    ``claim`` spells out which table holds what since when, so the card can say
    "scheduled, but nothing collected since <that timestamp>" in the same breath
    as "scheduled" - and never because of it.
    """
    report = metrics_freshness(now)
    counts = {status: 0 for status in STATUSES}
    collector_counts = {status: 0 for status in COLLECTOR_STATUSES}
    freshest: dict[str, Any] | None = None

    for row in report["platforms"].values():
        counts[row["status"]] += 1
        if row["last_snapshot_at"] is None or row["age_hours"] is None:
            continue
        if freshest is None or row["age_hours"] < freshest["age_hours"]:
            freshest = {
                "platform": row["id"],
                "name": row["name"],
                "status": row["status"],
                "collected_at": row["last_snapshot_at"],
                "age_hours": row["age_hours"],
            }

    silent: list[dict[str, Any]] = []
    for collector in report["collectors"]:
        collector_counts[collector["data_status"]] += 1
        if not collector["scheduled_but_silent"]:
            continue
        silent.append(
            {
                "id": collector["id"],
                "platform": collector["platform"],
                "job_id": collector["job_id"],
                "snapshot_table": collector["snapshot_table"],
                "data_status": collector["data_status"],
                "rows": collector["rows"],
                "last_collected_at": collector["last_collected_at"],
                "age_hours": collector["age_hours"],
                "runs_recorded": collector["runs_recorded"],
                "claim": collector["reason"],
            }
        )

    return {
        "generated_at": report["generated_at"],
        "stale_after_hours": report["stale_after_hours"],
        "platforms_total": len(report["platforms"]),
        "ok": counts[STATUS_OK],
        "stale": counts[STATUS_STALE],
        "never": counts[STATUS_NEVER],
        "blocked": counts[STATUS_BLOCKED],
        "snapshots_total": report["snapshots_total"],
        "freshest": freshest,
        "collectors": report["collectors"],
        "collectors_total": len(report["collectors"]),
        "collectors_ok": collector_counts[COLLECTOR_OK],
        "collectors_stale": collector_counts[COLLECTOR_STALE],
        "collectors_no_report": collector_counts[COLLECTOR_NO_REPORT],
        "collectors_missing_table": collector_counts[COLLECTOR_MISSING_TABLE],
        "collectors_unreadable": collector_counts[COLLECTOR_UNREADABLE],
        "silent_collectors": silent,
        "tables_without_collector": report["tables_without_collector"],
        "collectors_error": report["collectors_error"],
        "error": report["error"],
    }


__all__ = [
    "COLLECTOR_MISSING_TABLE",
    "COLLECTOR_NO_REPORT",
    "COLLECTOR_OK",
    "COLLECTOR_STATUSES",
    "COLLECTOR_STALE",
    "COLLECTOR_UNREADABLE",
    "NO_REPORT_CLAIM",
    "STALE_AFTER_HOURS",
    "STATUSES",
    "collector_freshness",
    "freshness_summary",
    "metrics_freshness",
]

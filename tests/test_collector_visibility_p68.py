"""A table the scheduler fills is a collector destination, not an orphan.

Measured on the live database (read-only, 2026-11-05, injected ``now`` of
2026-11-05 12:00 UTC): ``channel_stats`` held **206 rows** whose newest
``collected_at`` was ``2026-11-04T17:43:38.047566+00:00``, and the only thing the
freshness report had to say about it was «расписания для `channel_stats` нет: ни
одна зарегистрированная задача её не наполняет». That sentence was false, and the
four milliseconds between it and the newest ``pipeline_runs`` row for stage
``s8_analytics`` (``2026-11-04T17:43:38.051047``) are the proof: one and the same
hourly ``metrics-and-learning`` tick wrote the aggregate and journalled itself. The
registry could not say so because ``CollectorJobSpec`` carried exactly one
``snapshot_table``, so a second destination of an existing job had nowhere to live
and fell through into the orphan list.

These tests pin the decision, not the wording of the fix:

* ``channel_stats`` is a *registered destination* of ``metrics-and-learning``,
  inheriting that job's cadence and its journal stage, and it is no longer
  reported as a table nobody fills;
* the registry's claim is backed by the code it names - the writer exists, the
  table exists, and the stage is the one the tick actually records;
* a table that really has no registered writer keeps the orphan sentence, so
  removing ``channel_stats`` from that list has not quietly emptied it;
* P34: rows means rows, an absent table is ``None``, and a table with rows in it
  is never ``no_report``.

The second defect lives one level up, in the bound. ``STALE_AFTER_HOURS`` was one
constant shared by every collector, so the hourly ``metrics-and-learning`` job read
as ``ok`` at 18.27 hours of silence - and on the live database ``channel_stats``
said exactly that, ``ok``, with its newest row 18 hours behind a schedule that
ticks every hour. 23 missed ticks were still printable as fresh. The bound is
therefore derived per collector from the cadence the registry already carries
(``interval_hours``), with a floor, and every row prints its own number together
with the sentence of how it was obtained; the platform vocabulary's 24 hours stays
exactly where it was, because ``ok``/``stale``/``never``/``blocked`` mean that to
the rest of the modules.

Every test runs against a private SQLite file, because "no rows" and "206 rows"
are both unassertable in a session database other tests write into.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tgfarm import pipeline
from tgfarm.db.models import (
    Base,
    Channel,
    ChannelStat,
    CrosspostLog,
    PipelineRun,
    Post,
    PostStat,
    RedditMetricSnapshot,
)
from tgfarm.s8_analytics import freshness

#: The measured "today" of this work, so no age below depends on the wall clock.
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

#: The live shape the fix is about: 206 rows, newest one and a half hours ago.
LIVE_CHANNEL_STATS_ROWS = 206
LIVE_NEWEST_CHANNEL_STATS = datetime(2026, 11, 4, 17, 43, 38, 47566, tzinfo=UTC)

#: The false claim, kept as text so a rewrite of the wording has to fail here.
NO_COLLECTOR_CLAIM = "ни одна зарегистрированная задача её не наполняет"
NO_REPORT = "нет отчёта"

#: The destination this work adds to the registry.
DESTINATION = "channel_stats"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway database and a blank VK token: no settings, no network."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'p68.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(freshness, "session_scope", scope)
    monkeypatch.setattr(freshness, "get_settings", lambda: SimpleNamespace(vk_user_token=""))
    try:
        yield factory
    finally:
        engine.dispose()


def _channel_id(session, platform: str = "dzen") -> str:
    channel_id = f"{platform}-{uuid4().hex[:8]}"
    session.add(Channel(id=channel_id, title="Тест", platform=platform))
    session.flush()
    return channel_id


def _seed_channel_stat(
    factory, *, collected_at: datetime, day: str | None = None, platform: str = "dzen"
) -> None:
    """One aggregate row, written the way `write_daily_stats` writes it."""
    with factory() as session:
        session.add(
            ChannelStat(
                channel_id=_channel_id(session, platform),
                day=day or collected_at.strftime("%Y-%m-%d"),
                posts=3,
                views_sum=42,
                collected_at=collected_at,
            )
        )
        session.commit()


def _seed_run(factory, stage: str, *, created_at: datetime, status: str = "ok") -> None:
    with factory() as session:
        session.add(
            PipelineRun(
                run_id=f"p68{uuid4().hex[:8]}", stage=stage, status=status, created_at=created_at
            )
        )
        session.commit()


def _drop(factory, table: str) -> None:
    with factory() as session:
        session.execute(text(f"DROP TABLE {table}"))
        session.commit()


def _seed_post_stat(factory, platform: str, *, collected_at: datetime) -> None:
    """One `post_stats` snapshot on a published post of one platform."""
    with factory() as session:
        post = Post(
            public_id=f"p68{uuid4().hex[:8]}",
            channel_id=_channel_id(session, platform),
            status="published",
            published_at=collected_at,
            hook="Хук",
            body="Текст",
        )
        session.add(post)
        session.flush()
        session.add(PostStat(post_id=post.id, views=40, collected_at=collected_at))
        session.commit()


def _seed_reddit_stat(factory, *, collected_at: datetime) -> None:
    """A row of the one dated table no registered tick writes."""
    with factory() as session:
        log_row = CrosspostLog(
            post_public_id=f"p68{uuid4().hex[:8]}", platform="reddit", status="posted"
        )
        session.add(log_row)
        session.flush()
        session.add(
            RedditMetricSnapshot(
                crosspost_log_id=log_row.id,
                subreddit="test",
                score=7,
                num_comments=2,
                collected_at=collected_at,
            )
        )
        session.commit()


def _by_table(report: dict) -> dict[str, dict]:
    """Every registry row by the table it is judged by, readers and destinations."""
    return {
        row["snapshot_table"]: row
        for row in [*report["collectors"], *report["secondary_destinations"]]
    }


# ------------------------------------------------- the registry claim itself


def test_channel_stats_is_named_by_a_registered_job(db):
    """The registry says which job fills it, with that job's own cadence and stage.

    This is the claim the report was denying: the table is not on its own
    schedule, it is on `metrics-and-learning`'s.
    """
    specs = {spec.job_id: spec for spec in pipeline.collector_job_specs()}
    owner = specs["metrics-and-learning"]

    assert DESTINATION in owner.destination_tables
    assert owner.snapshot_table == PostStat.__tablename__
    # A destination is not a second interval and not a second journal stage.
    assert [item.snapshot_table for item in owner.additional_destinations] == [DESTINATION]
    writer = owner.additional_destinations[0]
    assert (writer.module, writer.entry) == (
        "tgfarm.s8_analytics",
        "s8_analytics.write_daily_stats",
    )
    assert owner.interval_hours == 1
    assert owner.run_stages == ("s8_analytics",)


def test_the_writer_the_registry_names_really_writes_that_table(db):
    """A destination the code does not write would be a new fiction, so prove it.

    Three links, each read out of the source rather than asserted from prose: the
    named callable exists and writes ``ChannelStat``; ``s8_analytics.run`` calls it
    under the stage name the spec journals; and the registered tick calls
    ``s8_analytics.run``.
    """
    from tgfarm import s8_analytics

    owner = next(
        spec for spec in pipeline.collector_job_specs() if spec.additional_destinations
    )
    writer = owner.additional_destinations[0]
    module = __import__(writer.module, fromlist=["*"])
    call = getattr(module, writer.entry.split(".")[-1], None)
    assert callable(call), f"{writer.module} has no callable {writer.entry}"

    assert "ChannelStat(" in inspect.getsource(call), "наполняет не эта функция"
    assert ChannelStat.__tablename__ == DESTINATION
    assert "write_daily_stats" in inspect.getsource(s8_analytics.run)
    stage = owner.run_stages[0]
    assert f'StageTimer("{stage}")' in inspect.getsource(s8_analytics.run)
    assert "s8_analytics" in inspect.getsource(pipeline._metrics_learning_tick)


def test_registered_destination_is_not_reported_as_an_orphan_table(db):
    """The defect in one assertion: 206 live rows, and "nobody fills this".

    The orphan sentence is now only sayable of a table no registered tick writes.
    """
    report = freshness.collector_freshness(now=NOW)

    orphans = {row["snapshot_table"]: row for row in report["tables_without_collector"]}
    assert DESTINATION not in orphans, orphans[DESTINATION]["reason"]
    assert all(NO_COLLECTOR_CLAIM not in (row["reason"] or "") for row in report["collectors"])
    for row in report["secondary_destinations"]:
        assert NO_COLLECTOR_CLAIM not in (row["reason"] or ""), row["id"]
        assert row["scheduled"] is True
        assert row["job_id"] is not None

    destination = _by_table(report)[DESTINATION]
    assert destination["kind"] == freshness.KIND_DESTINATION
    assert destination["id"] == f"metrics-and-learning:{DESTINATION}"
    assert destination["platform"] is None, "агрегат пишется по всем каналам сразу"
    assert destination["snapshot_table"] == DESTINATION
    assert destination["feeds_post_stats"] is False
    assert destination["journal_stages"] == ["s8_analytics"]
    assert destination["rows"] == 0, "пустая таблица - это ноль, а не None"
    assert destination["age_hours"] is None
    assert destination["data_status"] == freshness.COLLECTOR_NO_REPORT
    assert destination["fresh"] is False


def test_destination_rows_are_read_from_the_table_they_are_named_for(db):
    """Rows, newest stamp, age and journal come out of `channel_stats` itself.

    The live shape replays exactly: 206 rows, newest one and a half hours old, and
    one recorded run of the owner's stage beside it. Which verdict that age earns
    is the bound's business - see the cadence tests below.
    """
    for index in range(LIVE_CHANNEL_STATS_ROWS):
        day = (LIVE_NEWEST_CHANNEL_STATS - timedelta(days=index)).strftime("%Y-%m-%d")
        collected = LIVE_NEWEST_CHANNEL_STATS - timedelta(days=index)
        _seed_channel_stat(db, collected_at=collected, day=day)
    _seed_run(db, "s8_analytics", created_at=NOW - timedelta(hours=18))

    report = freshness.collector_freshness(now=NOW)
    row = _by_table(report)[DESTINATION]

    assert row["rows"] == LIVE_CHANNEL_STATS_ROWS
    assert row["last_collected_at"] == LIVE_NEWEST_CHANNEL_STATS.isoformat()
    assert row["age_hours"] == pytest.approx(18.27, abs=0.01)
    assert row["runs_recorded"] == 1
    assert row["last_run_at"] == (NOW - timedelta(hours=18)).isoformat()
    # A table with rows in it is a table that has a report. P34's opposite.
    assert row["data_status"] != freshness.COLLECTOR_NO_REPORT
    assert row["data_status"] != freshness.COLLECTOR_UNREADABLE
    assert NO_REPORT not in (row["reason"] or "")
    # Nothing about the platform rows changed: the destination is a table claim.
    assert report["error"] is None


def test_a_destination_with_rows_is_never_no_report(db):
    """The P34 half of the pair, stated for both kinds of registry row."""
    _seed_channel_stat(db, collected_at=NOW - timedelta(hours=1))

    row = _by_table(freshness.collector_freshness(now=NOW))[DESTINATION]
    assert row["rows"] == 1
    assert row["data_status"] == freshness.COLLECTOR_OK
    assert row["fresh"] is True
    assert row["reason"] is None
    assert row["scheduled_but_silent"] is False


def test_an_absent_destination_table_is_none_rows_not_zero_rows(db):
    """Nothing to look at is not an empty table: `None`, and still not `ok`."""
    _drop(db, DESTINATION)

    row = _by_table(freshness.collector_freshness(now=NOW))[DESTINATION]

    assert row["rows"] is None
    assert row["age_hours"] is None
    assert row["last_collected_at"] is None
    assert row["data_status"] == freshness.COLLECTOR_MISSING_TABLE
    assert row["fresh"] is False
    assert row["scheduled"] is True
    assert NO_REPORT in row["reason"]
    assert f"таблицы `{DESTINATION}` в этой базе нет" in row["reason"]


# ------------------------------------------------------- the list stays useful


def test_only_a_genuinely_unwritten_table_may_be_called_unfilled(db):
    """`reddit_metric_snapshots` is written on demand from the API, never by a job.

    Without this, "channel_stats is no longer an orphan" could be read as "the
    orphan list was deleted", which would hide the real gap rather than fix it.
    """
    report = freshness.collector_freshness(now=NOW)
    orphans = {row["snapshot_table"]: row for row in report["tables_without_collector"]}

    assert "reddit_metric_snapshots" in orphans
    row = orphans["reddit_metric_snapshots"]
    assert row["rows"] == 0
    assert row["scheduled"] is False
    assert row["kind"] == freshness.KIND_UNREGISTERED
    assert NO_COLLECTOR_CLAIM in row["reason"]


def test_every_dated_table_is_reported_exactly_once(db):
    """Readers, destinations and orphans partition the schema's dated tables.

    A table cannot be dropped by the reclassification, and cannot be counted twice
    so that one silence shows up as two.
    """
    report = freshness.collector_freshness(now=NOW)
    named = (
        {row["snapshot_table"] for row in report["collectors"]}
        | {row["snapshot_table"] for row in report["secondary_destinations"]}
        | {row["snapshot_table"] for row in report["tables_without_collector"]}
    )
    dated = set(freshness._dated_tables())

    assert named <= dated, f"отчёт назвал таблицы, которых в схеме нет: {named - dated}"
    assert dated <= named, f"датированная таблица не попала ни в один список: {dated - named}"
    registered = (
        {row["snapshot_table"] for row in report["collectors"]}
        | {row["snapshot_table"] for row in report["secondary_destinations"]}
    )
    orphans = {row["snapshot_table"] for row in report["tables_without_collector"]}
    assert registered.isdisjoint(orphans)
    assert DESTINATION in registered


def test_a_destination_is_not_counted_as_a_platform_reader(db):
    """The summary keeps the two registry claims apart instead of summing them.

    `collectors_total` is still one row per registered reader, so a second table
    from the same tick cannot inflate the number of collectors the farm has.
    """
    _seed_channel_stat(db, collected_at=NOW - timedelta(hours=1))

    summary = freshness.freshness_summary(now=NOW)
    readers = summary["collectors"]
    destinations = summary["secondary_destinations"]

    assert summary["collectors_total"] == len(readers)
    assert all(row["kind"] == freshness.KIND_PRIMARY for row in readers)
    assert [row["snapshot_table"] for row in destinations] == [DESTINATION]
    assert summary["secondary_destinations_total"] == len(destinations)
    counts_sum = (
        summary["collectors_ok"]
        + summary["collectors_stale"]
        + summary["collectors_no_report"]
        + summary["collectors_missing_table"]
        + summary["collectors_unreadable"]
    )
    assert counts_sum == summary["collectors_total"]
    assert summary["secondary_destinations_ok"] == 1
    assert summary["secondary_destinations_stale"] == 0
    # `collectors_ok` stays the reader-only number it always was.
    assert summary["collectors_ok"] == 0
    assert summary["collectors_error"] is None


def test_a_silent_destination_joins_the_silent_list_with_its_own_kind(db):
    """Scheduled, empty, and labelled as the destination it is - not a reader.

    The journal is what says the tick did reach recording, which is the difference
    between "no vk snapshots" and "no collector at all".
    """
    _seed_run(db, "s8_analytics", created_at=NOW - timedelta(hours=2))

    summary = freshness.freshness_summary(now=NOW)
    silent = {item["id"]: item for item in summary["silent_collectors"]}
    entry = silent[f"metrics-and-learning:{DESTINATION}"]

    assert entry["kind"] == freshness.KIND_DESTINATION
    assert entry["platform"] is None
    assert entry["rows"] == 0
    assert entry["age_hours"] is None
    assert entry["runs_recorded"] == 1
    assert NO_REPORT in entry["claim"]
    assert "ни одного снимка во всей таблице" in entry["claim"]
    assert NO_COLLECTOR_CLAIM not in entry["claim"]


# ------------------------------------------------- the bound each row owns


def test_the_bound_is_three_of_the_job_s_own_cadences_with_a_floor():
    """The rule, named: multiple of the registered interval, floored, never shared.

    The cadence is the only input, so no new setting and no environment variable
    can move a bound: it moves when the schedule moves.
    """
    hourly = freshness.stale_bound_hours(1)
    six_hourly = freshness.stale_bound_hours(6)

    assert hourly == freshness.STALE_BOUND_CADENCES * 1.0
    assert six_hourly == freshness.STALE_BOUND_CADENCES * 6.0
    # The acceptance pair: 1 h and 6 h cannot be judged by the same constant.
    assert hourly != six_hourly
    assert hourly < six_hourly
    # The floor, and what is not a cadence at all.
    assert freshness.stale_bound_hours(0.25) == freshness.STALE_BOUND_FLOOR_HOURS
    assert freshness.stale_bound_hours(None) is None
    assert freshness.stale_bound_hours(0) is None
    assert freshness.stale_bound_hours(-3) is None


def test_every_row_bound_is_the_registry_s_own_number(db):
    """Each row's bound equals its job's cadence, and only an orphan uses the default."""
    report = freshness.collector_freshness(now=NOW)
    rows = [*report["collectors"], *report["secondary_destinations"]]

    assert rows, "реестр сборщиков обязан быть непустым"
    for row in rows:
        assert row["stale_bound_source"] == freshness.BOUND_FROM_CADENCE, row["id"]
        assert row["stale_after_hours"] == freshness.stale_bound_hours(row["interval_hours"])
        assert row["interval_hours"] >= 1
    for row in report["tables_without_collector"]:
        assert row["interval_hours"] is None
        assert row["stale_bound_source"] == freshness.BOUND_FROM_UNREGISTERED_DEFAULT
        assert row["stale_after_hours"] == freshness.STALE_AFTER_HOURS


def test_the_same_age_is_stale_for_the_hourly_job_and_ok_for_the_six_hour_one(db):
    """One age, two verdicts, because two schedules. The whole of defect (b).

    Under the old single 24-hour constant both rows below were ``ok``: an hourly
    reader can be silent for 23 hours and still be printed as fresh.
    """
    aged = NOW - timedelta(hours=10)
    _seed_post_stat(db, "dzen", collected_at=aged)
    _seed_post_stat(db, "telegram", collected_at=aged)

    readers = {row["platform"]: row for row in freshness.collector_freshness(now=NOW)["collectors"]}
    dzen, telegram = readers["dzen"], readers["telegram"]

    assert (dzen["interval_hours"], telegram["interval_hours"]) == (1, 6)
    assert dzen["age_hours"] == pytest.approx(10.0, abs=0.01)
    assert telegram["age_hours"] == pytest.approx(10.0, abs=0.01)
    assert dzen["stale_after_hours"] == pytest.approx(3.0)
    assert telegram["stale_after_hours"] == pytest.approx(18.0)

    assert dzen["data_status"] == freshness.COLLECTOR_STALE
    assert dzen["stale"] is True
    assert dzen["fresh"] is False
    assert telegram["data_status"] == freshness.COLLECTOR_OK
    assert telegram["stale"] is False
    assert telegram["fresh"] is True


def test_the_live_hourly_silence_is_stale_rather_than_ok(db):
    """The measured live row, replayed: 206 rows, newest 18.27 h old, hourly job.

    That table was printed as ``ok``. Against its own cadence it is 18 hours of
    silence in a job that ticks every hour, and the honest word is ``stale``.
    """
    for index in range(LIVE_CHANNEL_STATS_ROWS):
        collected = LIVE_NEWEST_CHANNEL_STATS - timedelta(days=index)
        _seed_channel_stat(
            db, collected_at=collected, day=collected.strftime("%Y-%m-%d"), platform="dzen"
        )

    row = _by_table(freshness.collector_freshness(now=NOW))[DESTINATION]

    assert row["rows"] == LIVE_CHANNEL_STATS_ROWS
    assert row["age_hours"] == pytest.approx(18.27, abs=0.01)
    assert row["stale_after_hours"] == pytest.approx(3.0)
    assert row["data_status"] == freshness.COLLECTOR_STALE
    assert row["stale"] is True
    assert row["fresh"] is False
    assert row["scheduled_but_silent"] is True


def test_the_reason_prints_the_bound_that_measured_the_row(db):
    """Never a bare number: each threshold arrives with the rule behind it."""
    _seed_post_stat(db, "dzen", collected_at=NOW - timedelta(hours=10))

    row = {r["platform"]: r for r in freshness.collector_freshness(now=NOW)["collectors"]}["dzen"]

    assert row["stale_bound_derivation"] == "3 интервала(ов) по 1 ч"
    assert "порог 3 ч: 3 интервала(ов) по 1 ч" in row["reason"]
    assert "порог 24 ч" not in row["reason"]


def test_an_orphan_table_calls_its_bound_a_default(db):
    """No registered cadence means no derived bound - and the text must say so."""
    _seed_reddit_stat(db, collected_at=NOW - timedelta(hours=30))

    orphan = freshness.collector_freshness(now=NOW)["tables_without_collector"]
    row = {item["snapshot_table"]: item for item in orphan}["reddit_metric_snapshots"]

    assert row["interval_hours"] is None
    assert row["stale_after_hours"] == freshness.STALE_AFTER_HOURS
    assert row["stale_bound_source"] == freshness.BOUND_FROM_UNREGISTERED_DEFAULT
    assert row["data_status"] == freshness.COLLECTOR_STALE
    assert row["age_hours"] == pytest.approx(30.0, abs=0.01)
    assert "по умолчанию" in row["reason"]
    assert "порог 24 ч: по умолчанию: зарегистрированного интервала нет" in row["reason"]
    assert "интервала(ов) по" not in row["reason"]
    assert "нет отчёта" not in row["reason"]


def test_the_platform_vocabulary_keeps_its_own_bound(db):
    """The 24-hour platform rule did not move, so other modules read the same words.

    The same 10-hour-old snapshot is a ``stale`` collector row and an ``ok``
    platform row at once, and that disagreement is the point: the two vocabularies
    are judged by their own bounds.
    """
    aged = NOW - timedelta(hours=10)
    _seed_post_stat(db, "dzen", collected_at=aged)

    detail = freshness.metrics_freshness(now=NOW)
    platform = detail["platforms"]["dzen"]
    reader = {row["platform"]: row for row in detail["collectors"]}["dzen"]

    assert detail["platform_stale_after_hours"] == freshness.STALE_AFTER_HOURS
    assert platform["status"] == freshness.STATUS_OK
    assert platform["stale"] is False
    assert platform["age_hours"] == pytest.approx(10.0, abs=0.01)
    assert reader["data_status"] == freshness.COLLECTOR_STALE


def test_no_blanket_threshold_is_printed_above_the_rows(db):
    """One number for all rows is what lied, so the payload no longer carries it."""
    report = freshness.collector_freshness(now=NOW)
    rows = [
        *report["collectors"],
        *report["secondary_destinations"],
        *report["tables_without_collector"],
    ]
    block = report["stale_bound"]

    assert "stale_after_hours" not in report
    assert block["cadences"] == freshness.STALE_BOUND_CADENCES
    assert block["floor_hours"] == freshness.STALE_BOUND_FLOOR_HOURS
    assert block["unregistered_default_hours"] == freshness.STALE_AFTER_HOURS
    assert "интервала" in block["rule"]
    assert set(block["per_row"]) == {row["id"] for row in rows}
    assert len(block["per_row"]) == len(rows), "у строки должен быть свой порог"
    assert len(set(block["per_row"].values())) > 1, "порог не может быть одним для всех"
    assert all(row["stale_after_hours"] is not None for row in rows)

    summary = freshness.freshness_summary(now=NOW)
    assert "stale_after_hours" not in summary
    assert summary["platform_stale_after_hours"] == freshness.STALE_AFTER_HOURS
    assert summary["stale_bound"]["per_row"] == block["per_row"]
    for entry in summary["silent_collectors"]:
        assert entry["stale_after_hours"] == block["per_row"][entry["id"]]


def test_a_tighter_bound_never_turns_an_empty_table_into_a_report(db):
    """Being scheduled, and having a 3-hour bound, still cannot make an empty row ``ok``.

    A bound is an age test for rows that exist. The two silences this guards
    against are separate: no dated row at all is ``no_report`` whatever the bound,
    and a row older than the bound is ``stale`` whoever registered the job.
    """
    report = freshness.collector_freshness(now=NOW)
    row = {r["platform"]: r for r in report["collectors"]}["vk"]

    assert row["scheduled"] is True
    assert row["stale_after_hours"] == pytest.approx(3.0)
    assert row["rows"] == 0
    assert row["age_hours"] is None
    assert row["data_status"] == freshness.COLLECTOR_NO_REPORT
    assert row["fresh"] is False
    # The verdict function takes no schedule at all: same inputs, same answer.
    assert freshness._collector_status(0, None, 3.0) == freshness.COLLECTOR_NO_REPORT
    assert freshness._collector_status(0, 0.5, 3.0) == freshness.COLLECTOR_NO_REPORT
    assert freshness._collector_status(5, 2.9, 3.0) == freshness.COLLECTOR_OK
    assert freshness._collector_status(5, 3.1, 3.0) == freshness.COLLECTOR_STALE


def test_the_bound_is_stated_even_when_the_table_cannot_be_read(db):
    """An unreadable table is still a scheduled job with a computable bound.

    The bound comes from the registry, so it survives; the measurement does not,
    so `rows` stays ``None`` and the age stays ``None`` rather than being inferred.
    """
    _drop(db, DESTINATION)

    row = _by_table(freshness.collector_freshness(now=NOW))[DESTINATION]

    assert row["rows"] is None
    assert row["age_hours"] is None
    assert row["stale"] is False
    assert row["fresh"] is False
    assert row["data_status"] == freshness.COLLECTOR_MISSING_TABLE
    assert row["stale_after_hours"] == pytest.approx(3.0)
    assert row["stale_bound_source"] == freshness.BOUND_FROM_CADENCE


def test_a_silent_destination_carries_its_own_bound_text(db):
    """The card can print the bound without recomputing it from a constant."""
    report = freshness.freshness_summary(now=NOW)
    entry = {item["id"]: item for item in report["silent_collectors"]}[
        f"metrics-and-learning:{DESTINATION}"
    ]

    assert entry["stale_after_hours"] == pytest.approx(3.0)
    assert entry["stale_bound_source"] == freshness.BOUND_FROM_CADENCE
    assert entry["stale_bound_derivation"] == "3 интервала(ов) по 1 ч"

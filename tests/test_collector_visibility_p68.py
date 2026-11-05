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
from tgfarm.db.models import Base, Channel, ChannelStat, PipelineRun, PostStat
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

"""A collector is judged by the rows it left behind, never by its own schedule.

The defect these tests hunt is on the analytics side of the same mistake
``5744712`` fixed on the publishing side ("recorded a send that never happened"):
*reported fresh because something is scheduled*. ``commit e81ea0c`` made the
collector schedules registry-derived, so a report can now say - truthfully - that
``telegram-post-metrics`` runs ``tg_sync.collect_post_stats`` every six hours,
while the table that job feeds holds nothing newer than 2026-10-29. Both
sentences are true; only the second one is about the data. So every freshness
claim in here must be read out of a table, and the schedule must stay a separate,
clearly-labelled field that no status can be derived from.

The live database (read-only, 2026-11-05) gives the numbers the replay test uses:
22 ``post_stats`` rows, newest 2026-10-29 19:16:16.515327; 7 of them Telegram,
newest 2026-10-10 12:17:22.343469; ``reddit_metric_snapshots`` and
``telegraph_metric_snapshots`` both empty; no ``pipeline_runs`` row for stage
``telegram_post_metrics`` at all.

Like ``tests/test_metrics_freshness.py``, these run against a private SQLite file:
freshness is about what is *absent*, and a row left behind by another test in the
shared session database would make "no rows" unassertable.
"""

from __future__ import annotations

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
    CrosspostLog,
    PipelineRun,
    Post,
    PostStat,
    TelegraphMetricSnapshot,
)
from tgfarm.s8_analytics import freshness

#: The measured "today" of this work. Every age below is against it.
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

#: Newest `post_stats` rows as measured in the live database on 2026-11-05.
LIVE_NEWEST_DZEN = datetime(2026, 10, 29, 19, 16, 16, 515327, tzinfo=UTC)
LIVE_NEWEST_TELEGRAM = datetime(2026, 10, 10, 12, 17, 22, 343469, tzinfo=UTC)

#: (job_id, platform) -> the registry entry the report must be reading, kept here
#: as the shape the report is not allowed to hand-copy.
REGISTRY = {
    (spec.job_id, target.platform): (target.entry, spec.snapshot_table, spec.interval_hours)
    for spec, target in freshness._registry_collectors()
}


@pytest.fixture()
def claims_db(tmp_path, monkeypatch):
    """A throwaway database, a blank VK token, and no settings or network reads."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'claims.db').as_posix()}", future=True)
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


def _post_id(session, platform: str) -> int:
    """A published post on a fresh channel of one platform, without a snapshot."""
    suffix = uuid4().hex[:8]
    channel_id = f"{platform}-{suffix}"
    session.add(Channel(id=channel_id, title="Тест", platform=platform))
    session.flush()
    post = Post(
        public_id=f"fc{suffix}",
        channel_id=channel_id,
        status="published",
        published_at=NOW,
        hook="Хук",
        body="Текст",
    )
    session.add(post)
    session.flush()
    return int(post.id)


def _seed_snapshot(factory, platform: str, *, collected_at: datetime, views: int = 40) -> None:
    with factory() as session:
        session.add(
            PostStat(
                post_id=_post_id(session, platform),
                views=views,
                collected_at=collected_at,
            )
        )
        # `Session` as a context manager closes without committing, so each seeder
        # commits its own rows.
        session.commit()


def _seed_telegraph(factory, *, collected_at: datetime, views: int = 12) -> None:
    """A Telegraph page-view snapshot: lands in its own table, never `post_stats`."""
    with factory() as session:
        log_row = CrosspostLog(
            post_public_id=f"fc{uuid4().hex[:8]}",
            platform="telegraph",
            status="posted",
        )
        session.add(log_row)
        session.flush()
        session.add(
            TelegraphMetricSnapshot(
                crosspost_log_id=log_row.id, views=views, collected_at=collected_at
            )
        )
        session.commit()


def _seed_run(factory, stage: str, *, created_at: datetime, status: str = "ok") -> None:
    """One journal row: proof a tick got far enough to record an attempt."""
    with factory() as session:
        session.add(
            PipelineRun(
                run_id=f"fc{uuid4().hex[:8]}", stage=stage, status=status, created_at=created_at
            )
        )
        session.commit()


def _by_platform(report: dict) -> dict[str, dict]:
    return {row["platform"]: row for row in report["collectors"]}


def _drop(factory, table: str) -> None:
    with factory() as session:
        session.execute(text(f"DROP TABLE {table}"))
        session.commit()


# ---------------------------------------------------------------- the headline


def test_no_registered_collector_is_ever_called_ok_on_an_empty_database(claims_db):
    """The whole task in one assertion: scheduled must not mean fresh.

    Every collector the registry knows is scheduled by construction, so on a
    database with no rows at all each one has to come back as "no report" with an
    unknown age - and never as `ok`, never as a `0`-hour age.
    """
    report = freshness.collector_freshness(now=NOW)

    assert report["error"] is None
    assert report["collectors"], "реестр сборщиков обязан быть непустым"
    for row in report["collectors"]:
        assert row["scheduled"] is True, row["id"]
        assert row["rows"] == 0, row["id"]
        assert row["age_hours"] is None, row["id"]
        assert row["data_status"] == freshness.COLLECTOR_NO_REPORT, row["id"]
        assert row["fresh"] is False, row["id"]
        assert row["stale"] is False, row["id"]
        assert row["last_collected_at"] is None, row["id"]
        assert row["scheduled_but_silent"] is True, row["id"]
        assert freshness.NO_REPORT_CLAIM in row["reason"], row["id"]
        assert "0 ч" not in row["reason"], row["id"]


def test_a_dated_row_is_the_only_way_a_collector_becomes_ok(claims_db):
    """The positive control: `ok` requires data, and data alone is enough."""
    collected_at = NOW - timedelta(hours=2)
    _seed_snapshot(claims_db, "telegram", collected_at=collected_at)

    row = _by_platform(freshness.collector_freshness(now=NOW))["telegram"]

    assert row["rows"] == 1
    assert row["data_status"] == freshness.COLLECTOR_OK
    assert row["fresh"] is True
    assert row["stale"] is False
    assert row["scheduled_but_silent"] is False
    assert row["reason"] is None
    assert row["last_collected_at"] == collected_at.isoformat()
    assert row["age_hours"] == pytest.approx(2.0)


def test_age_is_measured_against_the_injected_now(claims_db):
    """`now` is an argument, not a guess: the same row ages differently by clock."""
    _seed_snapshot(claims_db, "dzen", collected_at=LIVE_NEWEST_DZEN)

    at_noon = _by_platform(freshness.collector_freshness(now=NOW))["dzen"]
    later = _by_platform(freshness.collector_freshness(now=NOW + timedelta(hours=12)))["dzen"]

    assert at_noon["age_hours"] == pytest.approx(160.73, abs=0.01)
    assert later["age_hours"] == pytest.approx(172.73, abs=0.01)
    assert at_noon["data_status"] == later["data_status"] == freshness.COLLECTOR_STALE


def test_cold_table_with_a_live_schedule_says_so_in_one_breath(claims_db):
    """«расписание есть, нового снимка нет с <дата>» - both claims, one reason.

    This is the reader's protection: the schedule sentence may appear next to the
    data sentence, but the status itself is the data's.
    """
    _seed_snapshot(claims_db, "dzen", collected_at=LIVE_NEWEST_DZEN)

    row = _by_platform(freshness.collector_freshness(now=NOW))["dzen"]
    stamp = LIVE_NEWEST_DZEN.isoformat()

    assert row["data_status"] == freshness.COLLECTOR_STALE
    assert row["fresh"] is False
    assert row["stale"] is True
    assert row["scheduled_but_silent"] is True
    assert row["last_collected_at"] == stamp
    assert f"новый снимок не записан с {stamp}" in row["reason"]
    assert "задача `metrics-and-learning`" in row["reason"]
    # The status never came from the schedule it just quoted.
    assert row["data_status"] != freshness.COLLECTOR_OK


# ------------------------------------------------------- the telegram verdict


def test_telegram_post_stats_collector_is_scheduled_and_has_never_recorded_a_run(claims_db):
    """The exact live verdict, pinned as a sentence.

    `telegram-post-metrics` is in the registry-derived schedule
    (`pipeline.collector_job_specs()`, feeds `post_stats`, gated by off-mode and a
    Telegram user session), yet the journal holds no row for stage
    `telegram_post_metrics` and the table holds no snapshot. The report has to be
    able to say "scheduled, but no run has produced a row" and mean it.
    """
    report = freshness.collector_freshness(now=NOW)
    row = _by_platform(report)["telegram"]

    assert row["job_id"] == "telegram-post-metrics"
    assert row["startup_job_id"] == "startup-telegram-post-metrics"
    assert row["entry"] == "tg_sync.collect_post_stats"
    assert row["snapshot_table"] == "post_stats"
    assert row["feeds_post_stats"] is True
    assert row["interval_hours"] == pipeline.TELEGRAM_METRICS_INTERVAL_H
    assert row["journal_stages"] == ["telegram_post_metrics"]

    assert row["scheduled"] is True
    assert row["rows"] == 0
    assert row["runs_recorded"] == 0
    assert row["age_hours"] is None
    assert row["data_status"] == freshness.COLLECTOR_NO_REPORT
    assert row["fresh"] is False

    claim = row["reason"]
    assert freshness.NO_REPORT_CLAIM in claim
    assert "ни одного снимка для платформы telegram" in claim
    assert "стоит в расписании каждые 6 ч" in claim
    assert "tg_sync.collect_post_stats" in claim
    assert "нет ни одной записи этапа `telegram_post_metrics`" in claim
    assert "ни один запуск планировщика ещё не дошёл до записи" in claim


def test_a_recorded_run_that_left_no_new_snapshot_is_named_as_such(claims_db):
    """The second silence: the tick did reach the journal, and still wrote nothing.

    Without the journal the two states are indistinguishable, so the reason must
    change when a run row exists - which also proves the sentence is read, not
    hard-coded.
    """
    _seed_snapshot(claims_db, "telegram", collected_at=LIVE_NEWEST_TELEGRAM)
    _seed_run(
        claims_db,
        "telegram_post_metrics",
        created_at=NOW - timedelta(hours=1),
        status="error",
    )

    row = _by_platform(freshness.collector_freshness(now=NOW))["telegram"]

    assert row["runs_recorded"] == 1
    assert row["last_run_status"] == "error"
    assert row["data_status"] == freshness.COLLECTOR_STALE
    assert row["last_collected_at"] == LIVE_NEWEST_TELEGRAM.isoformat()
    assert "последний запуск этапа `telegram_post_metrics`" in row["reason"]
    assert "статус `error`" in row["reason"]
    assert "нового снимка не оставил" in row["reason"]
    assert "нет ни одной записи этапа" not in row["reason"]


def test_a_recorded_run_over_an_empty_table_is_named_instead_of_blamed_on_data(claims_db):
    """The tick reached the journal and still wrote no snapshot: say both, plainly.

    `post_stats.collected_at` is NOT NULL in the schema, so a row can never be
    undated here; the reachable silence is a run that recorded itself and
    measured nothing, and the age must stay `None` while the reason credits the
    attempt without dressing it up as a measurement.
    """
    _seed_run(
        claims_db,
        "telegram_post_metrics",
        created_at=NOW - timedelta(hours=1),
        status="partial",
    )

    row = _by_platform(freshness.collector_freshness(now=NOW))["telegram"]

    assert row["rows"] == 0
    assert row["age_hours"] is None
    assert row["last_collected_at"] is None
    assert row["data_status"] == freshness.COLLECTOR_NO_REPORT
    assert row["fresh"] is False
    assert row["runs_recorded"] == 1
    assert row["last_run_status"] == "partial"
    assert row["last_run_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert freshness.NO_REPORT_CLAIM in row["reason"]
    assert "последний запуск этапа `telegram_post_metrics`" in row["reason"]
    assert "датированных снимков нет" in row["reason"]
    assert "нет ни одной записи этапа" not in row["reason"]


# -------------------------------------------------- each table, not one table


def test_each_collector_is_read_from_the_table_the_registry_names(claims_db):
    """Telegraph's own table can be fresh while `post_stats` knows nothing of it.

    Before this, the only table freshness could speak about was `post_stats`, so a
    collector writing elsewhere was reported through the wrong table - and the
    platform row for `telegraph` stayed `never` no matter what its snapshots said.
    """
    collected_at = NOW - timedelta(hours=3)
    _seed_telegraph(claims_db, collected_at=collected_at)

    detail = freshness.metrics_freshness(now=NOW)
    collector = _by_platform(detail)["telegraph"]

    assert collector["snapshot_table"] == "telegraph_metric_snapshots"
    assert collector["feeds_post_stats"] is False
    assert collector["rows"] == 1
    assert collector["age_hours"] == pytest.approx(3.0)
    assert collector["data_status"] == freshness.COLLECTOR_OK
    assert collector["fresh"] is True
    # The platform half still describes `post_stats`, and says what it sees there.
    assert detail["platforms"]["telegraph"]["snapshots"] == 0
    assert detail["platforms"]["telegraph"]["status"] == "never"
    assert detail["platforms"]["telegraph"]["age_hours"] is None


def test_a_missing_destination_table_is_none_rows_not_zero_rows(claims_db):
    """An unbootstrapped table has no count at all: `None`, never a measured 0."""
    _drop(claims_db, "telegraph_metric_snapshots")

    row = _by_platform(freshness.collector_freshness(now=NOW))["telegraph"]

    assert row["data_status"] == freshness.COLLECTOR_MISSING_TABLE
    assert row["rows"] is None
    assert row["age_hours"] is None
    assert row["last_collected_at"] is None
    assert row["fresh"] is False
    assert row["scheduled"] is True
    assert row["scheduled_but_silent"] is True
    assert freshness.NO_REPORT_CLAIM in row["reason"]
    assert "таблицы `telegraph_metric_snapshots` в этой базе нет" in row["reason"]


def test_snapshot_table_without_any_registered_collector_is_still_reported(claims_db):
    """A dated table no job feeds is a gap the card must show, not hide.

    `reddit_metric_snapshots` is written on demand from the API
    (`s9_growth/reddit_metrics.py`), never by a registered collector. Deriving the
    orphan list from the schema keeps it visible without inventing a schedule for
    it in prose.
    """
    report = freshness.collector_freshness(now=NOW)
    outside = {row["snapshot_table"]: row for row in report["tables_without_collector"]}

    assert "reddit_metric_snapshots" in outside
    row = outside["reddit_metric_snapshots"]
    assert row["rows"] == 0
    assert row["age_hours"] is None
    assert row["data_status"] == freshness.COLLECTOR_NO_REPORT
    assert row["fresh"] is False
    assert row["scheduled"] is False
    assert freshness.NO_REPORT_CLAIM in row["reason"]
    assert "ни одна зарегистрированная задача её не наполняет" in row["reason"]

    # A table is reported once: the registry's own destinations never reappear as
    # orphans, so the card cannot count the same silence twice.
    registered = {item["snapshot_table"] for item in report["collectors"]}
    assert registered.isdisjoint(outside)
    assert "post_stats" in registered and "post_stats" not in outside


# ---------------------------------------------------------------- derivation


def test_collector_rows_are_the_registry_itself_not_a_copy_of_it(claims_db):
    """One row per registered reader, with the registry's own ids and cadences."""
    rows = freshness.collector_freshness(now=NOW)["collectors"]

    assert {(row["job_id"], row["platform"]) for row in rows} == set(REGISTRY)
    for row in rows:
        entry, table, interval = REGISTRY[(row["job_id"], row["platform"])]
        assert row["entry"] == entry
        assert row["snapshot_table"] == table
        assert row["interval_hours"] == interval
    # `dzen` and `vk` share one job but are two collectors, and the report keeps
    # them apart instead of collapsing the pair into a platform row.
    assert {row["platform"] for row in rows if row["job_id"] == "metrics-and-learning"} == {
        "dzen",
        "vk",
    }
    assert len({row["id"] for row in rows}) == len(rows)


def test_summary_counts_collectors_separately_from_platforms(claims_db):
    """The card gets its own collector tally and the list of silent schedules."""
    _seed_snapshot(claims_db, "dzen", collected_at=NOW - timedelta(hours=1))

    summary = freshness.freshness_summary(now=NOW)
    counts = (
        summary["collectors_ok"]
        + summary["collectors_stale"]
        + summary["collectors_no_report"]
        + summary["collectors_missing_table"]
        + summary["collectors_unreadable"]
    )

    assert summary["collectors_total"] == len(summary["collectors"])
    assert counts == summary["collectors_total"]
    assert summary["collectors_ok"] == 1
    assert summary["collectors_no_report"] == summary["collectors_total"] - 1
    assert summary["collectors_error"] is None

    silent = {item["platform"]: item for item in summary["silent_collectors"]}
    assert "dzen" not in silent
    assert "telegram" in silent
    assert silent["telegram"]["snapshot_table"] == "post_stats"
    assert silent["telegram"]["rows"] == 0
    assert silent["telegram"]["age_hours"] is None
    assert freshness.NO_REPORT_CLAIM in silent["telegram"]["claim"]
    assert "tables_without_collector" in summary

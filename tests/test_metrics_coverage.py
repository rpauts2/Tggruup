"""The coverage audit must say what is measured, and why the rest is not.

Live numbers on 2026-11-05: 448 published posts, 15 of them with a snapshot, and
VK - 376 posts, every one of them attributable - has zero because the operator
has no ``VK_USER_TOKEN``. Those are three different problems, and a report that
prints "0 measured" for all three is useless. This module is read-only, so the
tests also pin that reading the audit changes nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from tgfarm.db.models import Channel, ChannelStat, CrosspostLog, Media, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import coverage
from tgfarm.s8_analytics.coverage import (
    STATUS_BLOCKED,
    STATUS_MEASURED,
    STATUS_PARTIAL,
    STATUS_UNMEASURED,
    coverage_table,
    measurement_coverage,
)

#: Children first for the wipe; parents first for the restore. The session
#: schema turns ``PRAGMA foreign_keys=ON`` on, and other suites leave
#: media/channel_stats rows hanging off the same channels.
_ANALYTICS_TABLES = (
    PostStat.__table__,
    Media.__table__,
    ChannelStat.__table__,
    CrosspostLog.__table__,
    Post.__table__,
    Channel.__table__,
)


def _snapshot_and_wipe(tables):
    with session_scope() as session:
        snapshot = {
            table.name: [dict(row) for row in session.execute(select(table)).mappings().all()]
            for table in tables
        }
        for table in tables:
            session.execute(delete(table))
    return snapshot


def _restore(tables, snapshot):
    with session_scope() as session:
        for table in tables:
            session.execute(delete(table))
        for table in reversed(tables):
            rows = snapshot.get(table.name) or []
            if rows:
                session.execute(table.insert(), rows)


@pytest.fixture(autouse=True)
def _isolated_analytics_tables():
    """The audit counts the whole database, so each case needs its own numbers.

    The wipe is scoped to this file and undone on teardown: the session-scoped
    fleet seeded by conftest must survive for the suites that run afterwards.
    """
    snapshot = _snapshot_and_wipe(_ANALYTICS_TABLES)
    yield
    _restore(_ANALYTICS_TABLES, snapshot)


def _seed_platform(
    platform: str,
    *,
    published: int = 0,
    measured: int = 0,
    zero_only: int = 0,
    with_remote: int = 0,
    headline_only: int = 0,
) -> list[str]:
    """Create ``published`` posts, of which ``measured`` get a real snapshot."""
    suffix = uuid4().hex[:8]
    channel_id = f"cov_{platform}_{suffix}"
    public_ids: list[str] = []
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title=platform, niche="news", platform=platform)
        )
        for index in range(published):
            public_id = f"cov{platform}{suffix}{index}"
            public_ids.append(public_id)
            post = Post(
                public_id=public_id,
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook=f"Заголовок публикации {index} для {platform} с достаточной длиной",
                body="Текст",
            )
            session.add(post)
            session.flush()
            if index < measured:
                session.add(PostStat(post_id=post.id, views=40, reactions=3))
            elif index < measured + zero_only:
                session.add(PostStat(post_id=post.id))
            if index < with_remote + headline_only:
                remote = f"remote-{index}" if index < with_remote else None
                session.add(
                    CrosspostLog(
                        post_public_id=public_id,
                        platform=platform,
                        status="posted",
                        remote_id=remote,
                        url=f"https://example.com/{remote}" if remote else None,
                        created_at=datetime.now(UTC),
                    )
                )
    return public_ids


def _row(report: dict, platform: str) -> dict:
    return report["platforms"][platform]


def test_measured_partial_and_blocked_are_three_different_answers():
    _seed_platform("dzen", published=4, measured=2, with_remote=2)
    _seed_platform("vk", published=3, with_remote=3)
    _seed_platform("telegram", published=2, measured=1, with_remote=0)

    report = measurement_coverage()
    assert _row(report, "dzen")["status"] == STATUS_PARTIAL
    assert _row(report, "vk")["status"] == STATUS_BLOCKED
    assert _row(report, "telegram")["status"] == STATUS_PARTIAL
    assert "VK_USER_TOKEN" in _row(report, "vk")["reason"]


def test_a_partially_measured_platform_without_a_collector_names_the_collector():
    """Live Telegraph: some posts measured, and the rest stays out of `post_stats`.

    A bare "8 публикаций без снимка" reads like a schedule that has not run yet.
    The reason has to say the remaining rows are unreachable, not merely overdue.
    Since P52 `telegraph-metrics` is a registered job (pipeline.py), so the audit
    names its collector and says its snapshots land in another table - the old
    "no collector at all" sentence had become a lie.
    """
    _seed_platform("telegraph", published=4, measured=1, with_remote=0)
    row = _row(measurement_coverage(), "telegraph")
    assert row["collector"] == "tgfarm.s9_growth.telegraph_metrics"
    assert row["collector_state"] == "scheduled_other_table"
    assert row["status"] == STATUS_PARTIAL
    assert "измерено 1 из 4" in row["reason"]
    assert "пишутся в `telegraph_metric_snapshots`" in row["reason"]


def test_telegram_is_measured_by_a_schedule_that_needs_a_session():
    """Telegram has been mis-reported twice, and both fixes are pinned here.

    Until `860bbc6` the collector crashed writing a `PostStat.day` column the
    schema never had. Then it worked but only ran when a human asked, which the
    audit said out loud. `telegram-post-metrics` ended that sentence: the reader
    is scheduled now, and what can still keep `post_stats` empty is an authorised
    user session the operator has to provide. Repeating the old text would send an
    operator to fix a schedule that already exists.
    """
    _seed_platform("telegram", published=4, measured=1, with_remote=0)
    row = _row(measurement_coverage(), "telegram")
    assert row["collector"] == "tgfarm.tg_sync"
    assert row["status"] == STATUS_PARTIAL
    assert "не соберётся само" not in row["reason"]
    assert "telegram-post-metrics" in row["reason"]
    assert "tgfarm telegram login" in row["reason"]
    assert "PostStat.day" not in row["reason"]
    assert "сборщика метрик в s8 нет" not in row["reason"]


def test_a_platform_without_a_collector_says_so_instead_of_blaming_the_platform():
    _seed_platform("max", published=3, with_remote=3)
    row = _row(measurement_coverage(), "max")
    assert row["collector"] is None
    assert row["status"] == STATUS_BLOCKED
    assert row["reason"] == "сборщика метрик нет"
    assert row["published"] == 3


def test_all_zero_snapshots_are_not_reported_as_measured():
    """An empty read is not evidence; VK answers zeros for posts it could not read."""
    _seed_platform("reddit", published=3, zero_only=3, with_remote=3)
    row = _row(measurement_coverage(), "reddit")
    assert row["measured_posts"] == 3
    assert row["measured_with_signal"] == 0
    assert row["status"] == STATUS_BLOCKED
    assert "сборщика" in row["reason"]


def test_totals_and_coverage_share_the_same_denominator():
    _seed_platform("dzen", published=10, measured=4, with_remote=4)
    _seed_platform("vk", published=6, with_remote=6)
    report = measurement_coverage()
    assert report["totals"]["published"] == 16
    assert report["totals"]["measured_posts"] == 4
    assert report["coverage"] == 0.25
    gaps = {gap["platform"]: gap for gap in report["gaps"]}
    # The biggest hole first: the planner needs to know where measuring pays most.
    assert list(gaps) == sorted(gaps, key=lambda key: -gaps[key]["unmeasured"])
    assert gaps["vk"]["unmeasured"] == 6
    assert gaps["dzen"]["unmeasured"] == 6


def test_headline_only_publications_are_named_as_an_attribution_limit():
    _seed_platform("x", published=3, with_remote=0, headline_only=3)
    # No collector for x at all, so the collector rule fires first.
    assert _row(measurement_coverage(), "x")["collector"] is None

    _seed_platform("dzen", published=5, with_remote=1, headline_only=4)
    row = _row(measurement_coverage(), "dzen")
    assert row["crosspost_posted"] == 5
    assert row["crosspost_with_remote"] == 1


def test_coverage_table_is_printable_and_carries_the_reason():
    _seed_platform("vk", published=2, with_remote=2)
    rows = coverage_table()
    assert {"platform", "published", "measured_posts", "collector", "status", "reason"} <= set(
        rows[0]
    )
    vk = next(row for row in rows if row["platform"] == "vk")
    assert vk["collector"] == "tgfarm.s8_analytics.vk_stats"
    assert vk["status"] == STATUS_BLOCKED


def test_everything_measured_is_the_one_case_without_a_reason():
    _seed_platform("dzen", published=3, measured=3, with_remote=3)
    row = _row(measurement_coverage(), "dzen")
    assert row["status"] == STATUS_MEASURED
    assert row["reason"] is None
    assert measurement_coverage()["gaps"] == []


def test_a_platform_with_nothing_published_is_never_called_blocked():
    _seed_platform("pinterest", published=0)
    row = _row(measurement_coverage(), "pinterest")
    assert row["status"] == STATUS_UNMEASURED
    assert row["published"] == 0


def test_reading_the_audit_writes_nothing():
    """The audit runs against the live database; it must stay a reader."""
    _seed_platform("dzen", published=3, measured=1, with_remote=1)
    before = _snapshot_counts()
    measurement_coverage()
    coverage_table()
    assert _snapshot_counts() == before


def _snapshot_counts() -> tuple[int, int, int]:
    with session_scope() as session:
        return (
            int(session.execute(select(func.count(Post.id))).scalar_one()),
            int(session.execute(select(func.count(PostStat.id))).scalar_one()),
            int(session.execute(select(func.count(CrosspostLog.id))).scalar_one()),
        )


def test_missing_analytics_tables_is_reported_not_raised(monkeypatch):
    from sqlalchemy.exc import OperationalError

    def boom(*args, **kwargs):
        raise OperationalError("select", {}, Exception("no such table: post_stats"))

    monkeypatch.setattr(coverage, "_platform_keys", boom)
    report = measurement_coverage()
    assert report["platforms"] == {}
    assert report["error"]
    assert "post_stats" in report["error"]


def test_reasons_never_carry_a_credential_value(monkeypatch):
    """The blocked text names the requirement, never a secret."""
    secret = "vk1.a.SECRETVALUE-not-printable"
    monkeypatch.setenv("VK_USER_TOKEN", secret)
    _seed_platform("vk", published=2, with_remote=2)
    report = measurement_coverage()
    blob = repr(report)
    assert secret not in blob
    assert _row(report, "vk")["status"] == STATUS_BLOCKED


def test_sqlite_read_only_connection_sees_the_same_facts_as_the_audit():
    """The audit's numbers must be reproducible with nothing but a SELECT."""
    public_ids = _seed_platform("dzen", published=4, measured=2, with_remote=2)
    row = _row(measurement_coverage(), "dzen")
    url = session_url()
    if url is None:  # pragma: no cover - the suite always runs on sqlite
        return
    connection = sqlite3.connect(f"file:{url}?mode=ro", uri=True)
    try:
        measured = connection.execute(
            "select count(distinct ps.post_id) from post_stats ps "
            "join posts p on p.id = ps.post_id join channels c on c.id = p.channel_id "
            "where lower(c.platform) = 'dzen'"
        ).fetchone()[0]
        published = connection.execute(
            "select count(*) from posts p join channels c on c.id = p.channel_id "
            "where lower(c.platform) = 'dzen' and p.status = 'published'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert (published, measured) == (row["published"], row["measured_posts"])
    assert len(public_ids) == 4


def session_url() -> str | None:
    from tgfarm.core.settings import get_settings

    url = str(get_settings().database_url or "")
    if not url.startswith("sqlite:///"):
        return None
    path = url.replace("sqlite:///", "")
    return None if path in ("", ":memory:") else path


# ---------------------------------------------------------------------------
# The wave-7 merge additions: the stage result and the API surface.
# ---------------------------------------------------------------------------


def test_coverage_endpoint_serves_the_audit():
    """The audit must be reachable by the dashboard, not only in-process."""
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    _seed_platform("dzen", published=4, measured=2, with_remote=2)
    _seed_platform("vk", published=3, with_remote=3)
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.get("/api/metrics/coverage")
        assert response.status_code == 200
        body = response.json()

    assert body["totals"]["published"] == 7
    assert body["totals"]["measured_posts"] == 2
    assert body["platforms"]["vk"]["status"] == STATUS_BLOCKED
    assert body["platforms"]["dzen"]["status"] == STATUS_PARTIAL
    assert [gap["platform"] for gap in body["gaps"]][0] in {"vk", "dzen"}


def test_run_reports_blocked_collectors_without_leaking_secrets(monkeypatch):
    """`saved: 0` from a crash must arrive as a named block, redacted."""
    from types import SimpleNamespace

    from tgfarm.s8_analytics import dzen_web, vk_stats

    live = SimpleNamespace(live=True, dzen_stats_enabled=True)
    monkeypatch.setattr("tgfarm.core.settings.get_settings", lambda: live)

    async def dzen_boom(*args, **kwargs):
        raise RuntimeError("studio failed: token=SUPERSECRETNOTFORLOGS")

    async def vk_boom(*args, **kwargs):
        raise RuntimeError("wall.getById rejected token=SUPERSECRETNOTFORLOGS")

    monkeypatch.setattr(dzen_web, "collect_and_record", dzen_boom)
    monkeypatch.setattr(vk_stats, "collect_and_record", vk_boom)

    import asyncio

    from tgfarm.s8_analytics import run

    result = asyncio.run(run())
    assert result["dzen_status"] == "blocked"
    assert result["vk_status"] == "blocked"
    assert "сбор Дзена не выполнился" in result["dzen_reason"]
    assert "сбор ВК не выполнился" in result["vk_reason"]
    assert result["dzen_saved"] == 0 and result["vk_saved"] == 0
    blob = repr(result)
    assert "SUPERSECRETNOTFORLOGS" not in blob


def test_run_says_skipped_when_live_mode_is_off(monkeypatch):
    """In demo mode the collectors never ran; that is `skipped`, not `blocked`."""
    import asyncio
    from types import SimpleNamespace

    from tgfarm.s8_analytics import run

    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(live=False, dzen_stats_enabled=False),
    )
    result = asyncio.run(run())
    assert result["dzen_status"] == "skipped"
    assert result["vk_status"] == "skipped"
    assert result["dzen_reason"] is None and result["vk_reason"] is None

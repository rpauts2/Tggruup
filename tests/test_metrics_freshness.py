"""Metric freshness must never turn "we have nothing" into zeroes.

These tests run against a private SQLite file, not the shared test database:
freshness is about what is *absent*, so a row left behind by another test would
make the assertions meaningless.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tgfarm.db.models import Base, Channel, Post, PostStat
from tgfarm.platforms import CATALOG
from tgfarm.s8_analytics import freshness

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A throwaway database plus a settings stub with no VK user token."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'freshness.db').as_posix()}", future=True
    )
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


def _seed(
    factory,
    platform: str,
    *,
    collected_at: datetime,
    views: int | None = 100,
    post_status: str = "published",
) -> str:
    """One channel + one published post, optionally with one snapshot."""
    suffix = uuid4().hex[:8]
    public_id = f"fresh-{suffix}"
    with factory() as session:
        channel_id = f"{platform}-{suffix}"
        session.add(Channel(id=channel_id, title="Тест", platform=platform))
        session.flush()
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status=post_status,
            published_at=collected_at,
            hook="Хук",
            body="Текст",
        )
        session.add(post)
        session.flush()
        if views is not None:
            session.add(PostStat(post_id=post.id, views=views, collected_at=collected_at))
        session.commit()
    return public_id


def _statuses(report: dict) -> set[str]:
    return {row["status"] for row in report["platforms"].values()}


def test_empty_database_reports_never_or_blocked_without_raising(fresh_db):
    report = freshness.metrics_freshness(now=NOW)

    assert set(report["platforms"]) == {item["id"] for item in CATALOG}
    assert _statuses(report) <= {"never", "blocked"}
    assert report["snapshots_total"] == 0
    assert report["error"] is None

    for platform_id in ("telegram", "vk", "dzen"):
        assert report["platforms"][platform_id]["status"] == "blocked"

    telegram = report["platforms"]["telegram"]
    # `f235908` put `tg_sync.collect_post_stats` on the scheduler, so the silence
    # is a missing session, not a missing collector. Pinned as a negative because
    # the old sentence was the defect: it told the operator to wait for code that
    # already exists.
    assert "сборщика" not in telegram["reason"]
    assert telegram["reason"] == freshness.TELEGRAM_BLOCKED_REASON

    vk = report["platforms"]["vk"]
    assert "VK_USER_TOKEN" in vk["reason"]
    assert "error 27" in vk["reason"]

    dzen = report["platforms"]["dzen"]
    assert dzen["reason"] == "опубликованных статей нет"

    for row in report["platforms"].values():
        assert row["snapshots"] == 0
        assert row["last_snapshot_at"] is None
        assert row["age_hours"] is None
        assert row["stale"] is False

    summary = freshness.freshness_summary(now=NOW)
    assert summary["ok"] == 0
    assert summary["stale"] == 0
    assert summary["blocked"] == 3
    assert summary["never"] == len(CATALOG) - 3
    assert summary["platforms_total"] == len(CATALOG)
    assert summary["snapshots_total"] == 0
    assert summary["freshest"] is None


def test_snapshot_makes_platform_ok_and_age_is_computed(fresh_db, monkeypatch):
    monkeypatch.setattr(freshness, "get_settings", lambda: SimpleNamespace(vk_user_token="vk-user"))
    collected_at = NOW - timedelta(hours=2)
    _seed(fresh_db, "vk", collected_at=collected_at, views=1200)

    report = freshness.metrics_freshness(now=NOW)
    vk = report["platforms"]["vk"]

    assert vk["status"] == "ok"
    assert vk["snapshots"] == 1
    assert vk["last_snapshot_at"] == collected_at.isoformat()
    assert vk["age_hours"] == pytest.approx(2.0)
    assert vk["stale"] is False
    assert vk["reason"] is None
    assert report["snapshots_total"] == 1

    # A platform without data is still not inventing zeros.
    assert report["platforms"]["telegram"]["status"] == "blocked"


def test_old_snapshot_is_stale(fresh_db):
    _seed(fresh_db, "dzen", collected_at=NOW - timedelta(hours=30), views=80)

    report = freshness.metrics_freshness(now=NOW)
    dzen = report["platforms"]["dzen"]

    assert dzen["status"] == "stale"
    assert dzen["stale"] is True
    assert dzen["age_hours"] == pytest.approx(30.0)
    assert dzen["last_snapshot_at"] == (NOW - timedelta(hours=30)).isoformat()
    assert "30.0" in dzen["reason"]


def test_vk_without_user_token_is_blocked_with_error_27_reason(fresh_db):
    report = freshness.metrics_freshness(now=NOW)
    vk = report["platforms"]["vk"]

    assert vk["status"] == "blocked"
    assert vk["snapshots"] == 0
    assert vk["reason"] == "нужен VK_USER_TOKEN (групповой токен отклоняется с error 27)"


def test_vk_zero_only_snapshot_is_not_meaningful_data(fresh_db, monkeypatch):
    _seed(fresh_db, "vk", collected_at=NOW - timedelta(hours=1), views=0)

    blocked = freshness.metrics_freshness(now=NOW)["platforms"]["vk"]
    assert blocked["snapshots"] == 1
    assert blocked["status"] == "blocked"
    assert "error 27" in blocked["reason"]

    monkeypatch.setattr(freshness, "get_settings", lambda: SimpleNamespace(vk_user_token="vk-user"))
    with_token = freshness.metrics_freshness(now=NOW)["platforms"]["vk"]
    assert with_token["status"] == "never"
    assert with_token["snapshots"] == 1


def test_dzen_without_publications_is_blocked_but_published_article_is_not(fresh_db):
    blocked = freshness.metrics_freshness(now=NOW)["platforms"]["dzen"]
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "опубликованных статей нет"

    _seed(fresh_db, "dzen", collected_at=NOW - timedelta(hours=1), views=None)
    published = freshness.metrics_freshness(now=NOW)["platforms"]["dzen"]
    assert published["status"] == "never"
    assert published["snapshots"] == 0
    assert published["reason"] == "публикации есть, но снимков метрик нет"
    assert published["reason"] != "опубликованных статей нет"


def test_summary_counts_statuses_snapshots_and_freshest(fresh_db, monkeypatch):
    monkeypatch.setattr(freshness, "get_settings", lambda: SimpleNamespace(vk_user_token="vk-user"))
    _seed(fresh_db, "vk", collected_at=NOW - timedelta(hours=1), views=900)
    _seed(fresh_db, "dzen", collected_at=NOW - timedelta(hours=40), views=70)

    summary = freshness.freshness_summary(now=NOW)

    assert summary["ok"] == 1
    assert summary["stale"] == 1
    assert summary["blocked"] == 1  # Telegram: not one snapshot, and the dial is the session.
    assert summary["never"] == len(CATALOG) - 3
    assert summary["platforms_total"] == len(CATALOG)
    assert (
        summary["ok"] + summary["stale"] + summary["never"] + summary["blocked"]
        == summary["platforms_total"]
    )
    assert summary["snapshots_total"] == 2
    freshest = summary["freshest"]
    assert freshest["platform"] == "vk"
    assert freshest["name"] == "ВКонтакте"
    assert freshest["status"] == "ok"
    assert freshest["collected_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert freshest["age_hours"] == pytest.approx(1.0)


def test_the_telegram_reason_names_the_job_and_the_session_not_a_missing_collector(fresh_db):
    """The sentence was written when nobody scheduled Telegram metrics.

    `f235908` registered `telegram-post-metrics` (plus the one-shot
    `startup-telegram-post-metrics`) in `pipeline._build_scheduler`, so the
    operator-actionable cause is now the read session, not a collector that has
    still to be written. The status itself stays `blocked` — the platform really
    is unread — only the reason was lying.
    """
    telegram = freshness.metrics_freshness(now=NOW)["platforms"]["telegram"]

    assert telegram["status"] == "blocked"
    assert "сборщика" not in telegram["reason"], "сборщик есть, и это надо говорить"
    assert "telegram-post-metrics" in telegram["reason"]
    assert "tg_sync.collect_post_stats" in telegram["reason"]
    assert "нужна авторизованная пользовательская сессия" in telegram["reason"]
    assert "tgfarm telegram login" in telegram["reason"], "причина обязана заканчиваться командой"


def test_the_two_panels_say_one_and_the_same_thing_about_telegram(fresh_db):
    """Freshness quotes `coverage`'s constants verbatim instead of rewording them.

    A panel that describes one platform two ways is the defect this pins against:
    if either wording changes, this test has to change with it, so the two
    reports cannot drift apart silently.
    """
    from tgfarm.s8_analytics.coverage import SCHEDULED_COLLECTORS, SESSION_GATED_COLLECTORS

    telegram = freshness.metrics_freshness(now=NOW)["platforms"]["telegram"]

    assert SCHEDULED_COLLECTORS["telegram"] in telegram["reason"]
    assert SESSION_GATED_COLLECTORS["telegram"] in telegram["reason"]

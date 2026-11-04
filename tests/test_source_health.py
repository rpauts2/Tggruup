from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select


def test_source_health_alert_ignores_healthy_and_not_yet_failing_sources():
    from tgfarm.pipeline import _source_health_alert

    assert _source_health_alert([]) is None
    assert _source_health_alert([
        SimpleNamespace(failures=0, last_status="ok"),
        SimpleNamespace(failures=0, last_status="new"),
    ]) is None
    # A single transient error is not yet a repeated-failure/cooldown alert.
    assert _source_health_alert([
        SimpleNamespace(failures=1, last_status="error"),
    ]) is None


def test_source_health_alert_warns_when_only_part_of_the_source_fleet_is_degraded():
    from tgfarm.pipeline import _source_health_alert

    alert = _source_health_alert([
        SimpleNamespace(failures=0, last_status="ok"),
        SimpleNamespace(failures=3, last_status="error"),
        SimpleNamespace(failures=0, last_status="ok"),
    ])

    assert alert is not None
    assert alert["level"] == "warning"
    assert alert["code"] == "source_fleet_degraded"
    assert "1 из 3" in alert["message"]


def test_source_health_alert_escalates_when_every_enabled_source_is_unhealthy():
    from tgfarm.pipeline import _source_health_alert

    alert = _source_health_alert([
        SimpleNamespace(failures=3, last_status="error"),
        SimpleNamespace(failures=4, last_status="cooldown"),
    ])

    assert alert is not None
    assert alert["level"] == "error"
    assert alert["code"] == "source_fleet_down"
    assert "Все 2 включённых источника" in alert["message"]


def test_source_health_alert_does_not_expose_source_errors_or_urls():
    from tgfarm.pipeline import _source_health_alert

    alert = _source_health_alert([
        SimpleNamespace(
            failures=3,
            last_status="cooldown",
            last_error="Authorization=Bearer top-secret-token",
            url="https://private.example/feed?token=top-secret-token",
        ),
    ])

    assert alert is not None
    assert "top-secret" not in repr(alert)
    assert "private.example" not in repr(alert)


def test_source_freshness_alert_ignores_recent_sources_and_empty_catalog():
    from datetime import UTC, datetime, timedelta

    from tgfarm.pipeline import _source_freshness_alert

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    assert _source_freshness_alert([], now) is None
    assert _source_freshness_alert([
        SimpleNamespace(
            last_fetched_at=now - timedelta(minutes=15),
            created_at=now - timedelta(days=2),
            min_interval_s=600, idle_streak=0, failures=0,
        ),
    ], now) is None


def test_source_freshness_alert_warns_for_partial_staleness():
    from datetime import UTC, datetime, timedelta

    from tgfarm.pipeline import _source_freshness_alert

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    alert = _source_freshness_alert([
        SimpleNamespace(last_fetched_at=now - timedelta(minutes=20), created_at=now, min_interval_s=600, idle_streak=0, failures=0),
        SimpleNamespace(last_fetched_at=now - timedelta(hours=4), created_at=now, min_interval_s=600, idle_streak=0, failures=0),
        SimpleNamespace(last_fetched_at=now - timedelta(minutes=30), created_at=now, min_interval_s=600, idle_streak=0, failures=0),
    ], now)

    assert alert is not None
    assert alert["level"] == "warning"
    assert alert["code"] == "source_collection_degraded"
    assert "1 из 3" in alert["message"]


def test_source_freshness_alert_escalates_when_all_sources_are_stale():
    from datetime import UTC, datetime, timedelta

    from tgfarm.pipeline import _source_freshness_alert

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    alert = _source_freshness_alert([
        SimpleNamespace(last_fetched_at=now - timedelta(hours=5), created_at=now, min_interval_s=600, idle_streak=0, failures=0),
        SimpleNamespace(last_fetched_at=None, created_at=now - timedelta(hours=5), min_interval_s=600, idle_streak=0, failures=0),
    ], now)

    assert alert is not None
    assert alert["level"] == "error"
    assert alert["code"] == "source_collection_stale"
    assert "Все включённые источники (2)" in alert["message"]


def test_source_freshness_alert_respects_long_interval_when_adaptive_cadence_is_disabled():
    from datetime import UTC, datetime, timedelta

    from tgfarm.pipeline import _source_freshness_alert

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    long_interval_recent = (now - timedelta(hours=24)).replace(tzinfo=None)
    from types import SimpleNamespace as Namespace

    assert _source_freshness_alert([
        SimpleNamespace(
            last_fetched_at=long_interval_recent,
            created_at=now - timedelta(days=3),
            min_interval_s=24 * 60 * 60, idle_streak=0, failures=0,
        ),
    ], now, Namespace(source_adaptive_cadence=False)) is None


# --------------------------------------------------------------------------- #
# S1 fleet health view (tgfarm/s1_sources/health.py)
# --------------------------------------------------------------------------- #
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture()
def fleet():
    """Give each health test an empty ``sources`` table, then restore it exactly.

    The suite shares one throwaway database for the whole session, so the rows
    present before the test are snapshotted and written back afterwards: an empty
    fleet is needed for deterministic totals, but no other test should inherit an
    empty registry.
    """
    from tgfarm.db.models import Source
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        saved = [
            {column.name: getattr(row, column.name) for column in Source.__table__.columns}
            for row in session.execute(select(Source)).scalars().all()
        ]
        session.execute(delete(Source))
    try:
        yield
    finally:
        with session_scope() as session:
            session.execute(delete(Source))
            for payload in saved:
                session.add(Source(**payload))


def _source(**fields) -> str:
    """Insert one source row with only the fields the test cares about."""
    from tgfarm.db.models import Source
    from tgfarm.db.session import session_scope

    source_id = fields.pop("id", None) or f"test-health-{uuid4().hex[:10]}"
    payload = {
        "name": f"Тестовый источник {source_id}",
        "kind": "rss",
        "url": "https://example.invalid/feed",
        "enabled": True,
        "min_interval_s": 600,
        "last_status": "new",
        "failures": 0,
        "last_yield": 0,
        "idle_streak": 0,
        "created_at": NOW - timedelta(days=30),
    }
    payload.update(fields)
    with session_scope() as session:
        session.add(Source(id=source_id, **payload))
    return source_id


def _one(health: dict, source_id: str) -> dict:
    matches = [row for row in health["sources"] if row["id"] == source_id]
    assert len(matches) == 1, f"{source_id} missing from the health view"
    return matches[0]


def test_empty_fleet_reports_zeroes_without_raising(fleet):
    from tgfarm.s1_sources.health import STATES, source_health, source_health_summary

    health = source_health(NOW)
    assert health["total"] == 0
    assert health["sources"] == []

    summary = source_health_summary(NOW)
    assert summary["total"] == 0
    assert summary["enabled"] == 0
    assert summary["attention"] == 0
    assert summary["stale"] == 0
    assert summary["stale_ids"] == []
    assert summary["top_problems"] == []
    assert summary["counts"] == dict.fromkeys(STATES, 0)


def test_active_cooldown_is_reported_with_reason_and_remaining_time(fleet):
    from tgfarm.s1_sources.health import source_health

    source_id = _source(
        last_fetched_at=NOW - timedelta(minutes=20),
        last_status="cooldown",
        failures=5,
        cooldown_until=NOW + timedelta(hours=6),
        last_error="ProviderUnavailable: GET https://example.invalid/feed -> 429",
    )

    entry = _one(source_health(NOW), source_id)

    assert entry["state"] == "cooldown"
    assert entry["cooldown_active"] is True
    assert entry["cooldown_remaining_h"] == pytest.approx(6.0, abs=0.01)
    assert "429" in entry["reason"]
    assert "6.0 ч" in entry["reason"]


def test_disabled_source_is_reported_as_disabled_and_keeps_its_attempt_history(fleet):
    from tgfarm.s1_sources.health import source_health

    source_id = _source(
        enabled=False,
        last_fetched_at=NOW - timedelta(days=3),
        last_status="ok",
    )

    entry = _one(source_health(NOW), source_id)

    assert entry["enabled"] is False
    assert entry["state"] == "disabled"
    assert "отключён" in entry["reason"]
    # The last attempt was a success, so that timestamp is trustworthy even now.
    assert entry["last_success_at"] is not None
    assert entry["last_success_known"] is True


def test_repeated_failures_are_reported_as_failing(fleet):
    from tgfarm.s1_sources.health import source_health

    source_id = _source(
        last_fetched_at=NOW - timedelta(minutes=40),
        last_status="error",
        failures=4,
        cooldown_until=None,
        last_error="TimeoutError: read timeout",
    )

    entry = _one(source_health(NOW, failure_threshold=3), source_id)

    assert entry["state"] == "failing"
    assert entry["cooldown_active"] is False
    assert "4" in entry["reason"]
    assert "порог автопаузы 3" in entry["reason"]
    assert "TimeoutError" in entry["reason"]


def test_a_single_failure_is_visible_but_not_called_repeated(fleet):
    from tgfarm.s1_sources.health import source_health

    source_id = _source(
        last_fetched_at=NOW - timedelta(minutes=40),
        last_status="error",
        failures=1,
        last_error="ConnectionResetError: peer reset",
    )

    entry = _one(source_health(NOW, failure_threshold=5), source_id)

    assert entry["state"] == "failing"
    assert "до автопаузы 5" in entry["reason"]


def test_never_attempted_source_is_due_and_not_stale_yet(fleet):
    from tgfarm.s1_sources.health import source_health, source_health_summary

    source_id = _source(
        last_fetched_at=None,
        last_status="new",
        created_at=NOW - timedelta(minutes=5),
    )

    entry = _one(source_health(NOW), source_id)

    assert entry["state"] == "due"
    assert entry["last_attempt_at"] is None
    assert entry["last_success_at"] is None
    assert entry["last_success_known"] is False

    summary = source_health_summary(NOW, stale_hours=1)
    assert summary["never_checked"] == 1
    assert summary["never_checked_ids"] == [source_id]
    assert summary["stale"] == 0


def test_reset_clears_cooldown_and_writes_the_operator_log(fleet):
    from tgfarm.db.models import OperatorLog, Source
    from tgfarm.db.session import session_scope
    from tgfarm.s1_sources.health import RESET_ACTION, reset_source_cooldown, source_health

    started = datetime.now(UTC)
    source_id = _source(
        last_fetched_at=started - timedelta(minutes=10),
        last_status="cooldown",
        failures=5,
        cooldown_until=started + timedelta(hours=5),
        last_error="HTTP 429",
    )

    assert reset_source_cooldown(source_id, reason="оператор снял паузу после проверки фида") is True

    with session_scope() as session:
        row = session.get(Source, source_id)
        assert row is not None
        assert row.cooldown_until is None
        # Unparking is not a claim of health: the streak and the error stay.
        assert row.failures == 5
        assert row.last_status == "error"
        audits = session.execute(
            select(OperatorLog).where(OperatorLog.target == source_id)
        ).scalars().all()

    assert [entry.action for entry in audits] == [RESET_ACTION]
    assert "оператор снял паузу" in (audits[0].detail or "")

    # The source is immediately visible as failing instead of silently parked.
    entry = _one(source_health(), source_id)
    assert entry["state"] == "failing"
    assert entry["cooldown_until"] is None
    assert entry["failures"] == 5

    assert reset_source_cooldown("test-health-missing", reason="нет такого источника") is False


def test_summary_counts_every_state_and_ranks_the_worst_offenders(fleet):
    from tgfarm.s1_sources.health import source_health_summary

    healthy = _source(last_fetched_at=NOW - timedelta(minutes=5), last_status="ok")
    due = _source(last_fetched_at=NOW - timedelta(minutes=20), last_status="ok")
    parked = _source(
        last_fetched_at=NOW - timedelta(hours=30),
        last_status="cooldown",
        failures=6,
        cooldown_until=NOW + timedelta(hours=2),
    )
    failing = _source(
        last_fetched_at=NOW - timedelta(hours=5),
        last_status="error",
        failures=2,
    )
    switched_off = _source(
        enabled=False,
        last_fetched_at=NOW - timedelta(days=2),
        last_status="ok",
    )

    summary = source_health_summary(NOW, stale_hours=1, failure_threshold=3)

    assert summary["total"] == 5
    assert summary["enabled"] == 4
    assert summary["counts"] == {
        "ok": 1,
        "due": 1,
        "cooldown": 1,
        "failing": 1,
        "disabled": 1,
    }
    assert sum(summary["counts"].values()) == summary["total"]
    assert summary["attention"] == 2
    assert summary["stale"] == 2
    assert set(summary["stale_ids"]) == {parked, failing}
    assert switched_off not in summary["stale_ids"]
    assert summary["never_checked"] == 0
    assert [item["id"] for item in summary["top_problems"]] == [parked, failing]
    assert summary["top_problems"][0]["state"] == "cooldown"

    capped = source_health_summary(NOW, stale_hours=1, failure_threshold=3, top=1)
    assert len(capped["top_problems"]) == 1

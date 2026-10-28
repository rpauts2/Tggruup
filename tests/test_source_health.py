from __future__ import annotations

from types import SimpleNamespace


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
            last_error="Authorization: Bearer top-secret",
            url="https://private.example/feed?token=top-secret",
        ),
    ])

    assert alert is not None
    assert "top-secret" not in repr(alert)
    assert "private.example" not in repr(alert)

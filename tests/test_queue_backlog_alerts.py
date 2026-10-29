from __future__ import annotations


def test_queue_backlog_alerts_are_quiet_when_live_mode_is_disabled():
    from tgfarm.pipeline import _queue_backlog_alerts

    assert _queue_backlog_alerts(
        500,
        500,
        live=False,
        scheduled_threshold=100,
        gated_threshold=50,
    ) == []


def test_queue_backlog_alerts_warn_for_scheduled_and_editorial_queues():
    from tgfarm.pipeline import _queue_backlog_alerts

    alerts = _queue_backlog_alerts(
        120,
        60,
        live=True,
        scheduled_threshold=100,
        gated_threshold=50,
    )

    assert [item["code"] for item in alerts] == [
        "scheduled_queue_backlog",
        "editorial_review_backlog",
    ]
    assert all(item["level"] == "warning" for item in alerts)
    assert "120 постов" in alerts[0]["message"]
    assert "60 постов" in alerts[1]["message"]


def test_queue_backlog_alert_escalates_a_severely_overloaded_publish_queue():
    from tgfarm.pipeline import _queue_backlog_alerts

    alerts = _queue_backlog_alerts(
        250,
        0,
        live=True,
        scheduled_threshold=100,
        gated_threshold=50,
    )

    assert len(alerts) == 1
    assert alerts[0]["level"] == "error"
    assert alerts[0]["code"] == "scheduled_queue_backlog"


def test_queue_backlog_alert_thresholds_are_clamped_and_messages_expose_no_post_text():
    from tgfarm.pipeline import _queue_backlog_alerts

    alerts = _queue_backlog_alerts(
        1,
        1,
        live=True,
        scheduled_threshold=0,
        gated_threshold=-5,
    )

    assert [item["code"] for item in alerts] == [
        "scheduled_queue_backlog",
        "editorial_review_backlog",
    ]
    assert all("secret title" not in repr(item) for item in alerts)

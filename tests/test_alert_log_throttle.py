"""A static alert must not write the same ERROR line every five minutes.

Live noise (2026-11-04): `publish_outcome_unknown: 51 пост(ов) требуют сверки`
was logged on every watchdog tick, thousands of identical lines a day. The alert
itself must stay visible in /api/status and in the delivery path; only the log
repeat is throttled.
"""

from __future__ import annotations

from tgfarm import pipeline


def test_same_alert_is_logged_once(monkeypatch):
    pipeline._reset_alert_log_state()
    lines: list[str] = []

    class _Log:
        def error(self, fmt, *args):
            lines.append(fmt % args)

        def warning(self, fmt, *args):
            lines.append(fmt % args)

    monkeypatch.setattr(pipeline, "log", _Log())
    alert = {"level": "error", "code": "publish_outcome_unknown", "message": "51 пост(ов)"}

    assert pipeline._log_alert_once(alert, now=0.0) is True
    assert pipeline._log_alert_once(alert, now=60.0) is False
    assert pipeline._log_alert_once(alert, now=3000.0) is False
    # After the window it is repeated once, so a long-standing problem is not lost.
    assert pipeline._log_alert_once(alert, now=4000.0) is True
    assert len(lines) == 2


def test_changed_message_is_logged_immediately(monkeypatch):
    pipeline._reset_alert_log_state()
    seen: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "log",
        type("L", (), {
            "error": lambda self, fmt, *a: seen.append(fmt % a),
            "warning": lambda self, fmt, *a: seen.append(fmt % a),
        })(),
    )
    pipeline._log_alert_once({"level": "error", "code": "x", "message": "51"}, now=0.0)
    assert pipeline._log_alert_once({"level": "error", "code": "x", "message": "52"}, now=1.0) is True
    assert len(seen) == 2


def test_different_codes_are_independent(monkeypatch):
    pipeline._reset_alert_log_state()
    seen: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "log",
        type("L", (), {
            "error": lambda self, fmt, *a: seen.append(fmt % a),
            "warning": lambda self, fmt, *a: seen.append(fmt % a),
        })(),
    )
    pipeline._log_alert_once({"level": "error", "code": "a", "message": "m"}, now=0.0)
    assert pipeline._log_alert_once({"level": "warning", "code": "b", "message": "m"}, now=1.0) is True
    assert len(seen) == 2

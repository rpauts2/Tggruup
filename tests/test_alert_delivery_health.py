from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_alert_delivery_failure_projection_is_generic_and_secret_safe():
    from tgfarm.pipeline import _alert_delivery_failure_alert

    assert _alert_delivery_failure_alert(None) is None
    assert _alert_delivery_failure_alert(SimpleNamespace(status="ok", error=None)) is None

    alert = _alert_delivery_failure_alert(
        SimpleNamespace(status="error", error="Bearer never-show-this")
    )
    assert alert is not None
    assert alert["code"] == "alert_delivery_failed"
    assert alert["level"] == "warning"
    assert "never-show-this" not in repr(alert)
    assert "ALERT_CHAT_ID" in alert["message"]


def test_deliver_alerts_records_http_and_network_failures_without_raw_response(monkeypatch):
    import httpx

    from tgfarm import pipeline

    calls = []

    class FakeResponse:
        status_code = 503
        text = "Authorization: Bearer never-show-this"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            if len(calls) == 1:
                return FakeResponse()
            raise httpx.ConnectTimeout("Bearer never-show-this")

    recorded = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_bot_token="bot-secret",
            alert_chat_id="42",
            alert_cooldown_min=30,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "collect_alerts",
        lambda: [
            {"level": "error", "code": "first_problem", "message": "first"},
            {"level": "warning", "code": "second_problem", "message": "second"},
        ],
    )
    monkeypatch.setattr(pipeline, "_alert_last_sent", {})
    monkeypatch.setattr(
        pipeline,
        "_record",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    sent = asyncio.run(pipeline.deliver_alerts())

    assert sent == 0
    assert len(calls) == 2
    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args[1:3] == ("alert_delivery", "error")
    assert kwargs["items_in"] == 2
    assert kwargs["items_out"] == 0
    assert kwargs["meta"] == {"attempted": 2, "sent": 0, "failed": 2}
    assert "HTTP503" in kwargs["error"]
    assert "ConnectTimeout" in kwargs["error"]
    assert "never-show-this" not in repr(recorded)
    assert "bot-secret" not in repr(recorded)


def test_deliver_alerts_records_success_after_successful_send(monkeypatch):
    import httpx

    from tgfarm import pipeline

    recorded = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_bot_token="bot-secret",
            alert_chat_id="42",
            alert_cooldown_min=30,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "collect_alerts",
        lambda: [{"level": "warning", "code": "some_problem", "message": "check"}],
    )
    monkeypatch.setattr(pipeline, "_alert_last_sent", {})
    monkeypatch.setattr(
        pipeline,
        "_record",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    assert asyncio.run(pipeline.deliver_alerts()) == 1
    args, kwargs = recorded[0]
    assert args[1:3] == ("alert_delivery", "ok")
    assert kwargs["items_in"] == 1
    assert kwargs["items_out"] == 1
    assert kwargs["error"] is None

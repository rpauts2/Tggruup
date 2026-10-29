from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _post(status: str, *, attempts: int = 0, error: str | None = None) -> tuple[int, str]:
    suffix = uuid4().hex[:12]
    channel_id = f"retry_{suffix}"
    public_id = f"retrypost{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Retry test", niche="test", platform="telegram"))
        row = Post(
            public_id=public_id,
            channel_id=channel_id,
            status=status,
            attempts=attempts,
            scheduled_at=(datetime.now(UTC) - timedelta(minutes=1)) if status == "scheduled" else None,
            publish_error=error,
            hook="Retry test",
            body="Test body",
        )
        session.add(row)
        session.flush()
        return row.id, public_id


def test_permanent_http_rejections_are_terminal_but_rate_limits_are_retryable():
    from tgfarm.s7_publish import _is_permanent_publish_error

    def error(status_code: int):
        return SimpleNamespace(response=SimpleNamespace(status_code=status_code))

    assert _is_permanent_publish_error(error(400)) is True
    assert _is_permanent_publish_error(error(401)) is True
    assert _is_permanent_publish_error(error(403)) is True
    assert _is_permanent_publish_error(error(429)) is False
    assert _is_permanent_publish_error(error(503)) is False
    assert _is_permanent_publish_error(RuntimeError("connection refused")) is False


@pytest.mark.asyncio
async def test_primary_publish_stops_after_configured_definite_failure_limit(monkeypatch):
    from tgfarm import platforms, s7_publish

    post_id, _ = _post("scheduled")
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(
            live=True,
            telegram_bot_token="test-token",
            primary_publish_max_attempts=1,
        ),
    )

    async def fail_before_send(_target: int, _text: str, _media: str | None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=fail_before_send))

    result = await s7_publish._publish_one(post_id, 12345, "test message", None, "telegram")

    assert result["ok"] is False
    assert result["terminal_failure"] is True
    assert result["outcome_unknown"] is False
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.scheduled_at is None
        assert row.publish_heartbeat_at is None


def test_manual_retry_requeues_only_terminal_failures_and_resets_attempt_budget(monkeypatch):
    from tgfarm import platforms
    from tgfarm.api import _retry_failed_publish

    _, public_id = _post("failed", attempts=5, error="temporary network issue")
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)

    result = _retry_failed_publish(public_id)

    assert result["ok"] is True
    assert result["platform"] == "telegram"
    with session_scope() as session:
        row = session.query(Post).filter_by(public_id=public_id).one()
        assert row.status == "scheduled"
        assert row.attempts == 0
        assert row.publish_error is None
        assert row.scheduled_at is not None
        assert row.publish_heartbeat_at is None


def test_manual_retry_refuses_unknown_outcome(monkeypatch):
    from tgfarm import platforms
    from tgfarm.api import _retry_failed_publish

    _, public_id = _post("publish_unknown", attempts=2, error="request timeout")
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)

    result = _retry_failed_publish(public_id)

    assert result["ok"] is False
    assert result["error"] == "not_failed"
    with session_scope() as session:
        row = session.query(Post).filter_by(public_id=public_id).one()
        assert row.status == "publish_unknown"


def test_watchdog_alerts_on_terminal_primary_publish_failures():
    from tgfarm.pipeline import collect_alerts

    _post("failed", attempts=5, error="permanent rejection")
    alerts = collect_alerts()

    alert = next(item for item in alerts if item["code"] == "publish_terminal_failure")
    assert alert["level"] == "error"
    assert "исправь причину" in alert["message"]

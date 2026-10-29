from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _response_error(status_code: int, headers: dict[str, str]):
    exc = RuntimeError("provider rate limit")
    exc.response = SimpleNamespace(status_code=status_code, headers=headers)
    return exc


def test_retry_after_reads_integer_seconds_and_caps_untrusted_values():
    from tgfarm.s7_publish import _publish_retry_after_seconds

    assert _publish_retry_after_seconds(_response_error(429, {"Retry-After": "120"})) == 120
    assert _publish_retry_after_seconds(_response_error(429, {"Retry-After": "999999"})) == 86_400
    assert _publish_retry_after_seconds(_response_error(429, {"Retry-After": "-5"})) == 0
    assert _publish_retry_after_seconds(_response_error(503, {"Retry-After": "120"})) is None


def test_retry_after_reads_http_date_and_telegram_style_timedelta():
    from tgfarm.s7_publish import _publish_retry_after_seconds

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    retry_at = now + timedelta(minutes=3)
    http_date = format_datetime(retry_at, usegmt=True)
    assert _publish_retry_after_seconds(
        _response_error(429, {"Retry-After": http_date}),
        now=now,
    ) == 180

    exc = RuntimeError("retry later")
    exc.retry_after = timedelta(seconds=45)
    assert _publish_retry_after_seconds(exc, now=now) == 45


def test_retry_after_rejects_malformed_headers():
    from tgfarm.s7_publish import _publish_retry_after_seconds

    assert _publish_retry_after_seconds(_response_error(429, {"Retry-After": "not-a-date"})) is None
    assert _publish_retry_after_seconds(_response_error(429, {})) is None


@pytest.mark.asyncio
async def test_primary_publisher_schedules_retry_after_exact_provider_delay(monkeypatch):
    from tgfarm import platforms, s7_publish

    suffix = uuid4().hex[:12]
    channel_id = f"retryafter_{suffix}"
    post_id = None
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Retry-After test", niche="test", platform="telegram"))
        row = Post(
            public_id=f"retryafterpost{suffix}",
            channel_id=channel_id,
            status="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            hook="Retry-After test",
            body="Test body",
        )
        session.add(row)
        session.flush()
        post_id = row.id

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(
            live=True,
            telegram_bot_token="test-token",
            primary_publish_max_attempts=5,
        ),
    )

    async def rate_limited(_target: int, _text: str, _media: str | None):
        raise _response_error(429, {"Retry-After": "600"})

    monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=rate_limited))
    before = datetime.now(UTC)
    result = await s7_publish._publish_one(post_id, 12345, "test message", None, "telegram")
    after = datetime.now(UTC)

    assert result["ok"] is False
    assert result["terminal_failure"] is False
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        assert row.status == "scheduled"
        assert row.scheduled_at is not None
        deadline = row.scheduled_at.replace(tzinfo=UTC) if row.scheduled_at.tzinfo is None else row.scheduled_at
        assert before + timedelta(seconds=599) <= deadline <= after + timedelta(seconds=601)

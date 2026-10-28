from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
import uuid

from sqlalchemy import select


def _exception_with_response(status_code: int, headers: dict[str, str]) -> RuntimeError:
    exc = RuntimeError("provider response")
    exc.response = SimpleNamespace(status_code=status_code, headers=headers)
    return exc


def test_retry_after_parses_seconds_dates_and_bounds():
    from tgfarm.s9_growth.adapters import crosspost_retry_after_seconds

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert crosspost_retry_after_seconds(
        _exception_with_response(429, {"Retry-After": "300"}), now=now
    ) == 300
    assert crosspost_retry_after_seconds(
        _exception_with_response(
            503, {"Retry-After": format_datetime(now + timedelta(seconds=211), usegmt=True)}
        ),
        now=now,
    ) == 211
    assert crosspost_retry_after_seconds(
        _exception_with_response(429, {"Retry-After": "tomorrow-ish"}), now=now
    ) is None
    assert crosspost_retry_after_seconds(
        _exception_with_response(400, {"Retry-After": "300"}), now=now
    ) is None
    assert crosspost_retry_after_seconds(
        _exception_with_response(429, {"Retry-After": "999999"}), now=now
    ) == 86_400


def test_provider_retry_after_extends_automatic_crosspost_cooldown():
    from tgfarm.core.schemas import CrosspostResult
    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.session import session_scope
    from tgfarm.s9_growth.adapters import already_done, begin_crosspost, log_result

    public_id = "crosspost_retry_after_" + uuid.uuid4().hex[:8]
    log_result(
        public_id,
        CrosspostResult(
            platform="vk",
            status="failed",
            error="rate limit",
            retry_after_seconds=3600,
        ),
        attempt=1,
    )

    with session_scope() as session:
        row = session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.post_public_id == public_id, CrosspostLog.platform == "vk")
            .order_by(CrosspostLog.id.desc())
            .limit(1)
        ).scalar_one()
        assert row.retry_after_at is not None
        deadline = row.retry_after_at.replace(tzinfo=UTC) if row.retry_after_at.tzinfo is None else row.retry_after_at
        assert deadline >= datetime.now(UTC) + timedelta(minutes=59)

    assert already_done(public_id, "vk") is True
    assert begin_crosspost(public_id, "vk") is None

    with session_scope() as session:
        row = session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.post_public_id == public_id, CrosspostLog.platform == "vk")
            .order_by(CrosspostLog.id.desc())
            .limit(1)
        ).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(minutes=3)
        row.retry_after_at = datetime.now(UTC) - timedelta(minutes=1)

    assert already_done(public_id, "vk") is False

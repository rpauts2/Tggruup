"""Safe reanimation of posts that never left the farm (mission priority 2).

Live case (2026-11-04): 365 Dzen posts failed on ``page.goto`` with
``net::ERR_NAME_NOT_RESOLVED`` and 51 died with "Connection closed while reading
from the driver". Retrying all of them blindly would have published week-old
news and could have duplicated the 51 uncertain ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from tgfarm.db.models import Channel, OperatorLog, Post
from tgfarm.db.session import session_scope


def _seed(
    *,
    status: str = "scheduled",
    error: str = "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://dzen.ru/profile/editor/create",
    age_hours: float = 1.0,
    platform: str = "dzen",
) -> str:
    suffix = uuid4().hex[:10]
    channel_id = f"requeue_{suffix}"
    public_id = f"requeue{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Requeue test", niche="test", platform=platform)
        )
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status=status,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=5),
            created_at=datetime.now(UTC) - timedelta(hours=age_hours),
            attempts=14,
            publish_error=error,
            hook="test",
            body="body",
        )
        session.add(post)
        session.flush()
    return public_id


def _row(public_id: str) -> Post:
    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one()
        return post


def test_dry_run_changes_nothing():
    from tgfarm.s7_publish import requeue_unsubmitted

    public_id = _seed(age_hours=1)
    result = requeue_unsubmitted(platform="dzen", apply=False)
    assert public_id in result["requeue"]
    assert result["changed"] == 0
    row = _row(public_id)
    assert row.status == "scheduled"
    assert row.attempts == 14


def test_fresh_pre_submit_failure_is_requeued_with_new_budget():
    from tgfarm.s7_publish import requeue_unsubmitted

    public_id = _seed(age_hours=2)
    result = requeue_unsubmitted(platform="dzen", apply=True, max_age_hours=48)
    assert public_id in result["requeue"]
    row = _row(public_id)
    assert row.status == "scheduled"
    assert row.attempts == 0
    assert "pre-submit transport failure" in (row.publish_error or "")
    with session_scope() as session:
        log = session.execute(
            select(OperatorLog).where(OperatorLog.target == public_id)
        ).scalars().all()
    assert any(entry.action == "requeue-unsubmitted" for entry in log)


def test_stale_pre_submit_failure_expires_instead_of_publishing_old_news():
    from tgfarm.s7_publish import requeue_unsubmitted

    public_id = _seed(age_hours=150)
    result = requeue_unsubmitted(platform="dzen", apply=True, max_age_hours=48)
    assert public_id in result["expired"]
    row = _row(public_id)
    assert row.status == "failed"
    assert row.scheduled_at is None
    assert "expired" in (row.publish_error or "")
    assert "stale" in (row.publish_error or "")
    with session_scope() as session:
        log = session.execute(
            select(OperatorLog).where(OperatorLog.target == public_id)
        ).scalars().all()
    assert any(entry.action == "expire-stale-unsubmitted" for entry in log)


def test_unknown_driver_outcome_is_parked_not_retried():
    from tgfarm.s7_publish import requeue_unsubmitted

    public_id = _seed(
        error="Connection closed while reading from the driver",
        age_hours=3,
    )
    requeue_unsubmitted(platform="dzen", apply=True)
    row = _row(public_id)
    assert row.status == "publish_unknown"
    assert row.scheduled_at is None
    assert "verify the platform" in (row.publish_error or "")


def test_published_and_unrelated_posts_are_untouched():
    from tgfarm.s7_publish import requeue_unsubmitted

    published = _seed(status="published", age_hours=200)
    other_platform = _seed(platform="vk", age_hours=1)
    requeue_unsubmitted(platform="dzen", apply=True)
    assert _row(published).status == "published"
    assert _row(other_platform).status == "scheduled"
    assert _row(other_platform).attempts == 14


def test_error_marker_classification():
    from tgfarm.s7_publish import _pre_submit_error, _uncertain_driver_error

    assert _pre_submit_error("net::ERR_NAME_NOT_RESOLVED at https://dzen.ru")
    assert _pre_submit_error("getaddrinfo failed for api.vk.com")
    assert not _pre_submit_error("Connection closed while reading from the driver")
    assert _uncertain_driver_error("Connection closed while reading from the driver")
    assert _uncertain_driver_error("Target page, context or browser has been closed")
    assert not _uncertain_driver_error("net::ERR_NAME_NOT_RESOLVED")

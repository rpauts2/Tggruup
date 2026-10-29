from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _publishing_post(*, started_at: datetime, heartbeat_at: datetime) -> tuple[int, str]:
    suffix = uuid4().hex[:12]
    channel_id = f"heartbeat_{suffix}"
    public_id = f"heartbeatpost{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Heartbeat test", niche="test", platform="telegram"))
        row = Post(
            public_id=public_id,
            channel_id=channel_id,
            status="publishing",
            scheduled_at=started_at,
            publish_heartbeat_at=heartbeat_at,
            hook="test",
            body="test",
        )
        session.add(row)
        session.flush()
        return row.id, public_id


def test_publish_heartbeat_refreshes_only_active_publication():
    from tgfarm.s7_publish import _touch_publish_heartbeat

    old = datetime.now(UTC) - timedelta(minutes=5)
    post_id, _ = _publishing_post(started_at=old, heartbeat_at=old)

    assert _touch_publish_heartbeat(post_id) is True

    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        refreshed = row.publish_heartbeat_at
        assert refreshed is not None
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        assert refreshed > old

        row.status = "publish_unknown"

    assert _touch_publish_heartbeat(post_id) is False


def test_stuck_detector_uses_heartbeat_instead_of_old_start_time():
    from tgfarm.s7_publish import _requeue_stuck

    now = datetime.now(UTC)
    post_id, _ = _publishing_post(
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(seconds=5),
    )

    _requeue_stuck(minutes=15)
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        assert row.status == "publishing", "a live heartbeat must protect a slow publish"

        row.publish_heartbeat_at = now - timedelta(hours=1)

    _requeue_stuck(minutes=15)
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        assert row.status == "publish_unknown"
        assert row.publish_heartbeat_at is None


def test_posts_schema_has_nullable_publish_heartbeat_column():
    from sqlalchemy import inspect

    from tgfarm.db.session import get_engine

    columns = {column["name"]: column for column in inspect(get_engine()).get_columns("posts")}

    assert "publish_heartbeat_at" in columns
    assert columns["publish_heartbeat_at"]["nullable"] is True

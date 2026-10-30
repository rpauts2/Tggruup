from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid


def test_crosspost_health_aggregates_latest_delivery_states_and_stale_claims():
    from tgfarm.api import _crosspost_health
    from tgfarm.db.models import CrosspostClaim, CrosspostLog
    from tgfarm.db.session import session_scope

    before = _crosspost_health()
    prior = before["totals"]
    suffix = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=45)
    rows = [
        CrosspostLog(
            post_public_id=f"health-posted-{suffix}", platform="health_vk",
            status="posted", remote_id="remote-ok", url="https://example.test/p/1",
            attempt=1, created_at=now,
        ),
        CrosspostLog(
            post_public_id=f"health-unknown-{suffix}", platform="health_bluesky",
            status="unknown", error="read timeout", attempt=1, created_at=now,
        ),
        CrosspostLog(
            post_public_id=f"health-permanent-{suffix}", platform="health_reddit",
            status="permanent", error="invalid credentials", attempt=1, created_at=now,
        ),
        CrosspostLog(
            post_public_id=f"health-failed-{suffix}", platform="health_tiktok",
            status="failed", error="temporary rate limit", attempt=1, created_at=now,
        ),
        CrosspostLog(
            post_public_id=f"health-stuck-{suffix}", platform="health_max",
            status="publishing", attempt=1, created_at=stale,
        ),
        CrosspostLog(
            post_public_id=f"health-exhausted-{suffix}", platform="health_youtube",
            status="failed", error="retry budget exhausted", attempt=5, created_at=now,
        ),
    ]
    with session_scope() as session:
        session.add_all(rows)
        session.add(CrosspostClaim(
            post_public_id=f"health-stuck-{suffix}",
            platform="health_max",
            claimed_at=stale,
        ))

    after = _crosspost_health()
    totals = after["totals"]
    assert totals["targets"] - prior["targets"] == 6
    assert totals["posted"] - prior["posted"] == 1
    assert totals["unknown"] - prior["unknown"] == 1
    assert totals["permanent"] - prior["permanent"] == 1
    assert totals["failed"] - prior["failed"] == 2
    assert totals["publishing"] - prior["publishing"] == 1
    assert totals["stuck"] - prior["stuck"] == 1
    assert totals["stale_claims"] - prior["stale_claims"] == 1
    assert totals["needs_attention"] - prior["needs_attention"] == 4
    assert after["state"] == "attention"

    by_platform = {item["platform"]: item for item in after["platforms"]}
    assert by_platform["health_vk"]["state"] == "healthy"
    assert by_platform["health_bluesky"]["state"] == "attention"
    assert by_platform["health_tiktok"]["state"] == "working"
    assert by_platform["health_max"]["stuck"] == 1
    assert by_platform["health_youtube"]["state"] == "attention"
    assert "invalid credentials" not in repr(after)
    assert after["generated_at"]

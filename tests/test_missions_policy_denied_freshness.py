"""Recent evidence cannot turn a denied/unknown policy into permission."""
from datetime import UTC, datetime, timedelta

import pytest

from tgfarm.community_policy import rules_freshness
from tgfarm.db.models import CommunityPolicySnapshot
from tgfarm.db.session import session_scope


@pytest.mark.parametrize("status,available,future", [
    ("unknown", True, False), ("blocked", True, False), ("ok", False, False),
    ("ok", True, True),
])
def test_recent_rule_text_does_not_override_refusal(status, available, future):
    now = datetime(2026, 11, 6, tzinfo=UTC)
    target = f"mission_denied_{status}_{available}"
    with session_scope() as session:
        row = CommunityPolicySnapshot(
            platform="reddit", target_type="subreddit", target_id=target,
            target_name=target, rules_source="https://www.reddit.com/r/test/about/rules",
            rules_hash="a" * 64, rules_available=available,
            rules_excerpt="Only science posts", verdict_status=status,
            confidence=1.0, requirements_json=[],
            checked_at=now + timedelta(days=30) if future else now, recorded_at=now,
        )
        session.add(row)
        session.flush()
        row_id = row.id
    try:
        result = rules_freshness("reddit", target, now=now)
        assert result["publishable"] is False
        assert result["reason"]
    finally:
        with session_scope() as session:
            session.delete(session.get(CommunityPolicySnapshot, row_id))

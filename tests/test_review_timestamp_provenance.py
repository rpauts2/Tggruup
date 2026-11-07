"""Offline regressions from the ten-role review; no real storage or transport."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tgfarm import community_policy as cp
from tgfarm.editorial_plan import plan_provenance, resolve_plan_labels


@pytest.mark.parametrize("stamp", ["not-a-timestamp", "future"])
def test_bad_fetch_time_cannot_grant_permission(monkeypatch, stamp):
    now = datetime(2026, 11, 6, tzinfo=UTC)
    if stamp == "future":
        stamp = (now + timedelta(days=30)).isoformat()
    monkeypatch.setattr(cp, "record_policy_snapshot", lambda *a, **k: {"recorded": True})
    result = cp.record_rules_snapshot(
        {"platform": "reddit", "target_type": "subreddit", "target_id": "offline_time",
         "auto_publish_supported": True},
        cp.FetchedRules(text="Only science posts", ok=True, fetched_at=stamp), now=now,
    )
    assert result.get("allow_publish", False) is False
    assert result.get("state") != "fresh"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_plan_reference_is_unknown_without_lookup(value):
    row = SimpleNamespace(plan_entry_id=value, plan_slot_at=None)
    assert plan_provenance(row, {}) == {
        "plan_entry_id": None, "plan_slot_at": None, "plan_public_id": None,
    }
    assert resolve_plan_labels(None, [value]) == {}

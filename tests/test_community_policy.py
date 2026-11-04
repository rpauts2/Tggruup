"""Tests for the unified CommunityPolicy contract (P3)."""

from __future__ import annotations

import asyncio

from datetime import datetime, timedelta, timezone

from tgfarm.community_policy import (
    assess_policy,
    gate_plan,
    policy_from_candidate,
    policy_from_reddit,
    policy_history,
    record_policy_snapshot,
)


def _pikabu_candidate() -> dict:
    return {
        "target_type": "community",
        "name": "Наука и техника",
        "slug": "science",
        "url": "https://pikabu.ru/community/science",
        "description": "Сообщество о науке и технологиях",
        "rules": "Запрещены广告 и ссылки на сторонние магазины.",
        "rules_available": True,
        "topic_rules_available": True,
        "global_rules_available": True,
        "score": 72,
        "matches": ["наука", "техника"],
        "risks": ["спорный заголовок"],
        "blockers": [],
        "recommended": True,
        "editorial_brief": ["Строго по фактам", "Без саморекламы"],
        "suggested_tags": ["наука"],
    }


def test_policy_from_pikabu_candidate() -> None:
    policy = policy_from_candidate("pikabu", _pikabu_candidate())
    assert policy.platform == "pikabu"
    assert policy.target_type == "community"
    assert policy.target_id == "science"
    assert policy.name == "Наука и техника"
    assert policy.audience.startswith("Сообщество")
    assert "наука" in policy.allowed_topics
    assert policy.tag_requirements == ("наука",)
    # Contract decision: the destination's own rules come first, verbatim and
    # inert, then the selector's editorial brief.  Dropping the raw rule text
    # would leave the gate without evidence (see test_to_dict_round_trip_and_inert_rules).
    assert policy.requirements == (
        "Запрещены广告 и ссылки на сторонние магазины.",
        "Строго по фактам",
        "Без саморекламы",
    )
    assert 0.0 <= policy.confidence <= 1.0
    assert policy.rules_available is True
    assert policy.rules_source
    assert policy.notes  # risks survive as notes
    # checked_at is a fresh ISO UTC timestamp
    checked = datetime.fromisoformat(policy.checked_at)
    assert checked.tzinfo is not None


def test_policy_from_vc_topic_candidate_rules_flag() -> None:
    candidate = {
        "target_type": "topic",
        "name": "Технологии",
        "path": "/flows/tech",
        "url": "https://vc.ru/tech",
        "score": 40,
        "matches": ["it"],
        "topic_rules_available": True,
        "global_rules_available": False,
    }
    policy = policy_from_candidate("vc", candidate)
    assert policy.target_type == "topic"
    assert policy.target_id == "/flows/tech"
    assert policy.rules_available is False  # both flags must be true
    assert policy.confidence == 0.4


def test_policy_from_reddit_community() -> None:
    community = {
        "title": "r/python",
        "url": "https://www.reddit.com/r/python/",
        "description": "News about the Python language",
        "subscribers": 1200000,
    }
    policy = policy_from_reddit(
        community,
        rules=["No self-promotion", "Follow reddiquette"],
        rules_source="https://www.reddit.com/r/python/about/rules",
    )
    assert policy.platform == "reddit"
    assert policy.target_type == "subreddit"
    assert policy.name == "r/python"
    assert policy.audience == "News about the Python language"
    assert policy.requirements == ("No self-promotion", "Follow reddiquette")
    assert policy.rules_available is True


def test_assess_policy_unknown_when_rules_unavailable() -> None:
    policy = policy_from_candidate("pikabu", {"name": "x", "rules_available": False})
    verdict = assess_policy(policy)
    assert verdict["status"] == "unknown"
    assert verdict["allow_publish"] is False


def test_assess_policy_stale_after_max_age() -> None:
    policy = policy_from_candidate("pikabu", _pikabu_candidate())
    stale_time = datetime.fromisoformat(policy.checked_at) + timedelta(hours=25)
    verdict = assess_policy(policy, max_age_hours=24, now=stale_time)
    assert verdict["status"] == "stale"
    assert verdict["allow_publish"] is False


def test_assess_policy_ok_when_fresh() -> None:
    policy = policy_from_candidate("pikabu", _pikabu_candidate())
    verdict = assess_policy(policy)
    assert verdict["status"] == "ok"
    assert verdict["allow_publish"] is True


def test_to_dict_round_trip_and_inert_rules() -> None:
    policy = policy_from_candidate("pikabu", _pikabu_candidate())
    data = policy.to_dict()
    assert isinstance(data, dict)
    assert data["platform"] == "pikabu"
    assert all(isinstance(value, (str, bool, float, int, list, type(None))) for value in data.values())
    # Rules text is stored verbatim as inert strings; nothing executable survives.
    assert "Запрещены" in " ".join(policy.requirements) or policy.requirements == ()
    assert policy.forbidden_topics == ()


def test_policy_history_api_endpoint() -> None:
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    candidate = {**_pikabu_candidate(), "slug": "api_history", "name": "API"}
    record_policy_snapshot(policy_from_candidate("pikabu", candidate), {"status": "ok"})

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.get("/api/community-policy/history", params={"platform": "pikabu"})
        assert response.status_code == 200
        body = response.json()
        assert body["count"] >= 1
        item = next(row for row in body["items"] if row["target_id"] == "api_history")
        assert item["verdict_status"] == "ok"
        assert item["rules_excerpt"]
        assert item["rules_hash"]


def test_gate_plan_without_target_refuses() -> None:
    verdict = gate_plan("pikabu", {"selected": None, "candidates": []})
    assert verdict["status"] == "no_target"
    assert verdict["allow_publish"] is False
    assert verdict["policy"] is None


def test_gate_plan_matches_selector_plan() -> None:
    plan = {
        "platform": "pikabu",
        "selected": _pikabu_candidate(),
        "rules_source": "https://pikabu.ru/rules",
        "auto_publish_supported": True,
    }
    verdict = gate_plan("pikabu", plan)
    assert verdict["status"] == "ok"
    assert verdict["allow_publish"] is True
    assert verdict["auto_publish"] is True
    assert verdict["policy"]["rules_source"] == "https://pikabu.ru/rules"
    assert verdict["policy"]["rules_hash"]


def test_gate_plan_reddit_inspect_payload() -> None:
    inspect = {
        "name": "python",
        "about": {"display_name": "python", "public_description": "News about Python", "subscribers": 10},
        "rules": [{"short_name": "No self-promotion", "description": "Do not advertise"}],
        "requirements": {},
    }
    verdict = gate_plan("reddit", {"selected": inspect, "rules_source": ""})
    assert verdict["status"] == "ok"
    assert verdict["allow_publish"] is True
    policy = verdict["policy"]
    assert policy["target_id"] == "python"
    assert policy["name"] == "r/python"
    assert policy["requirements"][0] == "No self-promotion — Do not advertise"
    assert policy["rules_source"].endswith("/r/python/about/rules")


def test_gate_plan_reddit_without_rules_refuses() -> None:
    inspect = {"name": "python", "about": {"display_name": "python"}, "rules": []}
    verdict = gate_plan("reddit", {"selected": inspect})
    assert verdict["status"] == "unknown"
    assert verdict["allow_publish"] is False
    assert verdict["blockers"] == ["rules_unavailable"]


def test_record_policy_snapshot_detects_rules_change() -> None:
    candidate = {**_pikabu_candidate(), "slug": "history_change", "name": "История"}
    policy = policy_from_candidate("pikabu", candidate)
    first = record_policy_snapshot(policy, {"status": "ok"})
    assert first["recorded"] is True
    assert first["changed"] is True

    # Identical check inside the collapse window is not written twice.
    duplicate = record_policy_snapshot(policy, {"status": "ok"})
    assert duplicate["recorded"] is False
    assert duplicate["changed"] is False

    changed_policy = policy_from_candidate(
        "pikabu",
        {**candidate, "rules": "Запрещены ссылки на маркетплейсы и реклама."},
    )
    second = record_policy_snapshot(changed_policy, {"status": "ok"})
    assert second["recorded"] is True
    assert second["changed"] is True
    assert second["previous_hash"] == policy.rules_hash != changed_policy.rules_hash


def test_policy_history_newest_first() -> None:
    candidate = {**_pikabu_candidate(), "slug": "history_order", "name": "Порядок"}
    policy = policy_from_candidate("pikabu", candidate)
    record_policy_snapshot(policy, {"status": "ok"})
    record_policy_snapshot(
        policy_from_candidate("pikabu", {**candidate, "rules": "Правила обновлены."}),
        {"status": "stale"},
    )
    rows = policy_history(platform="pikabu", target_id="history_order")
    assert len(rows) == 2
    assert rows[0]["verdict_status"] == "stale"
    assert rows[1]["verdict_status"] == "ok"
    assert rows[0]["id"] > rows[1]["id"]


def test_publish_gate_refuses_unknown_rules(monkeypatch) -> None:
    from tgfarm import community_drafts, community_targets

    async def fake_selection(post_public_id, *, post_override=None):
        return {
            "ok": True,
            "platform": "pikabu",
            "selected": {
                "target_type": "community",
                "name": "Наука",
                "slug": "gate_science",
                "url": "https://pikabu.ru/community/gate_science",
                "rules": "",
                "rules_available": False,
                "score": 80,
                "suggested_tags": ["наука"],
            },
            "tags": ["наука"],
            "rules_source": "https://pikabu.ru/rules",
            "auto_publish_supported": True,
        }

    monkeypatch.setattr(community_targets, "recommend_automatic_pikabu_target", fake_selection)
    result = asyncio.run(
        community_drafts.publish_pikabu_scheduled(
            "gate_unknown_rules",
            title="Заголовок поста",
            body="Текст материала для сообщества.",
        )
    )
    assert result["ok"] is False
    assert result["status"] == "skipped"
    assert "правила" in result["detail"].lower()
    assert result["policy_verdict"]["blockers"] == ["rules_unavailable"]

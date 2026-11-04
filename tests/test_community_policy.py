"""Tests for the unified CommunityPolicy contract (P3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tgfarm.community_policy import (
    assess_policy,
    policy_from_candidate,
    policy_from_reddit,
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
    assert policy.requirements == ("Строго по фактам", "Без саморекламы")
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

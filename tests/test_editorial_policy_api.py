"""The editorial preview must carry the community policy it was judged by.

Wave 1 wiring: the selector already returns ``policy_verdict`` with the full
CommunityPolicy (P3). The preview endpoint used to drop it, so the native editor
never saw the community's requirements and could not honestly claim compliance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from tgfarm.community_policy import policy_from_candidate
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _published_post() -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"policy_api_{suffix}"
    public_id = f"policyapi{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Пикабу", niche="tech", platform="pikabu")
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook="Почему роутеры ломаются чаще, чем кажется",
                body="Разбираем причины отказов и что можно проверить самому. " * 4,
                sources=[{"title": "Docs", "url": "https://example.org/a"}],
            )
        )
    return public_id


def _plan_with_policy() -> dict:
    candidate = {
        "target_type": "community",
        "name": "Наука",
        "slug": "science",
        "url": "https://pikabu.ru/community/science",
        "description": "Сообщество о науке",
        "rules": "Запрещена самореклама и ссылки на магазины.",
        "rules_available": True,
        "score": 80,
        "matches": ["наука"],
        "suggested_tags": ["наука"],
        "editorial_brief": ["Без саморекламы"],
    }
    policy = policy_from_candidate("pikabu", candidate, rules_source="https://pikabu.ru/rules")
    return {
        "platform": "pikabu",
        "selected": candidate,
        "candidates": [candidate],
        "rules_source": "https://pikabu.ru/rules",
        "auto_publish_supported": True,
        "policy_verdict": {
            "status": "ok",
            "allow_publish": True,
            "policy": policy.to_dict(),
        },
    }


def test_preview_passes_community_policy_to_brief_and_adaptation(monkeypatch):
    from tgfarm import community_targets
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    public_id = _published_post()

    async def fake_recommend(platform_id, post_public_id):
        return _plan_with_policy()

    monkeypatch.setattr(community_targets, "recommend_community_target", fake_recommend)

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.post(f"/api/platform-drafts/pikabu/{public_id}/editorial")
        assert response.status_code == 200, response.text
        body = response.json()

    brief_policy = body["brief"]["community_policy"]
    assert brief_policy["applied"] is True
    assert any("самореклама" in line.lower() for line in brief_policy["hard_constraints"])

    audit = body["community_policy"]
    assert audit["applied"] is True
    assert audit["rules_source"] == "https://pikabu.ru/rules"
    # The AI step records its own audit; in offline mode it is deterministic and
    # must not claim the constraints were enforced.
    adaptation_audit = body["adaptation_community_policy"]
    assert adaptation_audit is not None
    assert adaptation_audit["verified"] is False


def test_preview_without_policy_says_so(monkeypatch):
    from tgfarm import community_targets
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    public_id = _published_post()
    plan = _plan_with_policy()
    plan["policy_verdict"] = {}

    async def fake_recommend(platform_id, post_public_id):
        return plan

    monkeypatch.setattr(community_targets, "recommend_community_target", fake_recommend)

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        body = client.post(f"/api/platform-drafts/pikabu/{public_id}/editorial").json()

    assert body["brief"]["community_policy"]["applied"] is False
    assert "не учтены" in body["brief"]["community_policy"]["note"]

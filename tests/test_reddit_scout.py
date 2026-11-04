from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tgfarm.s9_growth import adapters
from tgfarm.s9_growth import reddit_scout as scout


ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        reddit_client_id="test-client-id",
        reddit_client_secret="test-client-secret",
        reddit_refresh_token="test-refresh-token",
        reddit_api_approved=True,
        reddit_subreddit="learnpython",
        reddit_user_agent="python:tgfarm-tests:1.0 (by /u/test_account)",
    )


def test_reddit_scout_refuses_data_api_without_explicit_approval(monkeypatch):
    settings = _settings()
    settings.reddit_api_approved = False
    adapter = adapters.ADAPTERS["reddit"]
    monkeypatch.setattr(adapter, "data_api_approved", lambda: False)

    with pytest.raises(scout.RedditScoutError, match="REDDIT_API_APPROVED"):
        scout._adapter()


def _mock_httpx(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        return ORIGINAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


def _about(name: str, title: str, description: str, subscribers: int, active: int) -> dict:
    return {
        "kind": "t5",
        "data": {
            "display_name": name,
            "title": title,
            "public_description": description,
            "subscribers": subscribers,
            "active_user_count": active,
            "over18": False,
            "subreddit_type": "public",
            "submission_type": "any",
            "restrict_posting": False,
        },
    }


def test_candidate_scoring_rewards_topic_match_and_explains_its_estimate():
    learn, learn_parts, learn_reasons = scout._candidate_score(
        {
            "display_name": "learnpython",
            "title": "Learn Python",
            "public_description": "Python questions tutorials automation",
            "subscribers": 120_000,
            "active_user_count": 1200,
        },
        query_terms=["python", "automation"],
        content_terms=["python", "automation", "testing"],
        result_index=0,
        result_count=2,
    )
    weak, _, _ = scout._candidate_score(
        {
            "display_name": "gardening",
            "title": "Gardening",
            "public_description": "Plants and flowers",
            "subscribers": 900_000,
            "active_user_count": 4000,
        },
        query_terms=["python", "automation"],
        content_terms=["python", "automation", "testing"],
        result_index=1,
        result_count=2,
    )

    assert learn > weak
    assert learn_parts["query"] > 0
    assert any("python" in text.lower() for text in learn_reasons)


def test_validate_submission_enforces_machine_readable_requirements():
    community = {
        "name": "example",
        "about": {
            "over18": False,
            "subreddit_type": "public",
            "submission_type": "any",
        },
        "requirements": {
            "is_flair_required": True,
            "title_text_min_length": 20,
            "title_text_max_length": 80,
            "title_blacklisted_strings": ["free money"],
            "body_blacklisted_strings": ["affiliate"],
            "domain_whitelist": ["example.org"],
        },
    }

    problems = scout.validate_submission(
        "Free money today",
        "Read more at https://spam.example.net and use my affiliate.",
        community,
    )

    assert any("flair" in item.lower() for item in problems)
    assert any("минимума" in item.lower() for item in problems)
    assert any("запрещённый" in item.lower() for item in problems)
    assert any("whitelist" in item.lower() for item in problems)


def test_validate_submission_blocks_link_only_closed_and_over18_communities():
    link_only = {
        "name": "news",
        "about": {"over18": False, "subreddit_type": "public", "submission_type": "link"},
        "requirements": {},
    }
    private = {
        "name": "private",
        "about": {"over18": False, "subreddit_type": "private", "submission_type": "any"},
        "requirements": {},
    }
    adult = {
        "name": "adult",
        "about": {"over18": True, "subreddit_type": "public", "submission_type": "any"},
        "requirements": {},
    }

    assert any("link-посты" in item for item in scout.validate_submission("Title", "Body", link_only))
    assert any("закрытое" in item for item in scout.validate_submission("Title", "Body", private))
    assert any("18+" in item for item in scout.validate_submission("Title", "Body", adult))


def test_search_communities_reads_about_rules_and_requirements(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _settings)
    adapter = adapters.ADAPTERS["reddit"]

    async def token():
        return "scout-access-token"

    monkeypatch.setattr(adapter, "_access_token", token)

    subreddits = [
        {
            "kind": "t5",
            "data": {
                "display_name": "learnpython",
                "title": "Learn Python",
                "public_description": "Python questions tutorials automation",
                "subscribers": 120_000,
                "active_user_count": 1200,
                "over18": False,
            },
        },
        {
            "kind": "t5",
            "data": {
                "display_name": "gardening",
                "title": "Gardening",
                "public_description": "Plants and flowers",
                "subscribers": 900_000,
                "active_user_count": 4000,
                "over18": False,
            },
        },
        {
            "kind": "t5",
            "data": {
                "display_name": "adult_python",
                "title": "Adult Python",
                "public_description": "Python",
                "subscribers": 900_000,
                "active_user_count": 4000,
                "over18": True,
            },
        },
    ]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer scout-access-token"
        assert request.headers["user-agent"] == "python:tgfarm-tests:1.0 (by /u/test_account)"
        path = request.url.path
        if path == "/subreddits/search":
            assert request.url.params["q"] == "python automation"
            return httpx.Response(200, json={"data": {"children": subreddits}}, request=request)
        if path == "/r/learnpython/about":
            return httpx.Response(
                200,
                json=_about("learnpython", "Learn Python", "Python questions tutorials automation", 120_000, 1200),
                request=request,
            )
        if path == "/r/learnpython/about/rules":
            return httpx.Response(
                200,
                json={"rules": [{"short_name": "Show your work", "description": "Explain what you tried.", "kind": "link"}]},
                request=request,
            )
        if path == "/api/v1/learnpython/post_requirements":
            return httpx.Response(
                200,
                json={"is_flair_required": True, "body_restriction_policy": "required"},
                request=request,
            )
        if path == "/r/gardening/about":
            return httpx.Response(
                200,
                json=_about("gardening", "Gardening", "Plants and flowers", 900_000, 4000),
                request=request,
            )
        if path == "/r/gardening/about/rules":
            return httpx.Response(200, json={"rules": []}, request=request)
        if path == "/api/v1/gardening/post_requirements":
            return httpx.Response(200, json={}, request=request)
        raise AssertionError(f"Unexpected Reddit API path: {path}")

    _mock_httpx(monkeypatch, handler)
    result = asyncio.run(
        scout.search_communities(
            "python automation",
            title="Automate Python tests",
            body="Python test automation example",
            limit=6,
        )
    )

    assert result["read_only"] is True
    assert result["returned"] == 2
    assert result["candidates"][0]["name"] == "learnpython"
    assert result["candidates"][0]["risk_flags"]
    assert result["candidates"][0]["requirements"]["is_flair_required"] is True
    # The answer is measured, not written into the code: the scout used to put a
    # constant false here and say nothing about why.
    capability = scout.automatic_publish_capability()
    assert capability["can_auto_publish"] is False
    assert capability["blockers"]
    assert result["candidates"][0]["can_auto_publish"] == capability["can_auto_publish"]
    assert result["candidates"][0]["capability_blockers"] == capability["blockers"]
    assert all(item["name"] != "adult_python" for item in result["candidates"])
    assert len(seen) == 7


def test_tailor_uses_safe_fallback_when_ai_policy_blocks_online_call(monkeypatch):
    monkeypatch.setattr(
        scout,
        "_post_record",
        lambda post_id: {
            "public_id": post_id,
            "status": "published",
            "title": "A clear technical headline",
            "body": "This post explains the example in detail.\n\n#python #automation",
            "cta": "Subscribe now",
            "hashtags": ["python", "automation"],
            "sources": [{"url": "https://example.org/article"}],
            "channel_id": "test-channel",
        },
    )
    async def fake_inspect(subreddit):
        return _community_result(subreddit)

    monkeypatch.setattr(scout, "inspect_community", fake_inspect)
    monkeypatch.setattr(
        scout.policy,
        "should_use_ai",
        lambda *args, **kwargs: (False, "policy off"),
    )
    monkeypatch.setattr(scout.policy, "record_skip", lambda *args, **kwargs: None)

    result = asyncio.run(scout.tailor_post("published_123", "r/learnpython"))

    assert result["subreddit"] == "learnpython"
    assert result["adaptation"]["mode"] == "extractive_fallback"
    assert "#python" not in result["draft"]["body"]
    assert "Источник: https://example.org/article" in result["draft"]["body"]
    assert result["review_required"] is True
    assert result["can_auto_publish"] is False


def _community_result(name: str) -> dict:
    name = name.removeprefix("r/")
    return {
        "name": name,
        "about": {
            "title": "Learn Python",
            "public_description": "Python learning and programming",
            "subscribers": 120_000,
            "active_user_count": 1200,
            "over18": False,
            "subreddit_type": "public",
            "submission_type": "any",
            "restrict_posting": False,
        },
        "rules": [{"title": "Be constructive", "description": "Give context.", "kind": "link", "violation_reason": ""}],
        "requirements": {"body_restriction_policy": "required"},
        "risk_flags": [],
    }


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_reddit_scout_api_requires_auth_and_returns_candidates(client, monkeypatch):
    from tgfarm.core.apiauth import HEADER, ensure_token

    async def fake_search(query, **kwargs):
        return {"query": query, "candidates": [{"name": "learnpython"}], "returned": 1, "read_only": True}

    monkeypatch.setattr(scout, "search_communities", fake_search)

    assert client.post("/api/reddit/scout", json={"query": "python"}).status_code == 401
    response = client.post(
        "/api/reddit/scout",
        json={"query": "python", "limit": 4},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["name"] == "learnpython"


def test_reddit_publish_needs_explicit_confirmation(client):
    from tgfarm.core.apiauth import HEADER, ensure_token

    response = client.post(
        "/api/reddit/publish",
        json={
            "post_public_id": "published_123",
            "subreddit": "learnpython",
            "title": "A title",
            "body": "A useful body",
            "confirm": False,
        },
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 400
    assert "Подтверди" in response.json()["detail"]


def test_reddit_publish_is_blocked_in_dry_mode(client, monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(api_module, "get_settings", lambda: SimpleNamespace(live=False))
    response = client.post(
        "/api/reddit/publish",
        json={
            "post_public_id": "published_123",
            "subreddit": "learnpython",
            "title": "A title",
            "body": "A useful body",
            "confirm": True,
        },
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 409
    assert "TGFARM_MODE" in response.json()["detail"]



def test_dashboard_exposes_reddit_scout_and_human_review_controls(client):
    from tgfarm.core.apiauth import HEADER, ensure_token

    headers = {HEADER: ensure_token()}
    page = client.get("/", headers=headers)
    script = client.get("/ui/app.js", headers=headers)
    assert page.status_code == 200
    assert 'id="reddit-scout-form"' in page.text
    assert 'id="reddit-tailor-panel"' in page.text
    assert 'id="reddit-publish-button"' in page.text
    assert script.status_code == 200
    assert "submitRedditScout" in script.text
    assert "tailorRedditCandidate" in script.text
    assert "publishRedditDraft" in script.text


def test_reddit_publish_rechecks_global_switch_after_rule_fetch(client, monkeypatch):
    import tgfarm.api as api_module
    from tgfarm import platforms
    from tgfarm.core.apiauth import HEADER, ensure_token

    settings = SimpleNamespace(live=True)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    switch_values = iter([True, False])
    monkeypatch.setattr(
        platforms,
        "is_platform_enabled",
        lambda platform_id: next(switch_values),
    )
    monkeypatch.setattr(
        scout,
        "_post_record",
        lambda post_id: {
            "public_id": post_id,
            "status": "published",
            "title": "Original title",
            "body": "Original body",
            "cta": "",
            "hashtags": [],
            "sources": [],
            "channel_id": "channel-1",
        },
    )

    async def fake_inspect(subreddit):
        return {
            "name": subreddit,
            "about": {
                "over18": False,
                "subreddit_type": "public",
                "submission_type": "any",
                "restrict_posting": False,
            },
            "rules": [],
            "requirements": {},
            "risk_flags": [],
        }

    monkeypatch.setattr(scout, "inspect_community", fake_inspect)
    monkeypatch.setattr(scout, "validate_submission", lambda title, body, community: [])
    monkeypatch.setattr("tgfarm.s9_growth.already_done", lambda post_id, platform: False)
    monkeypatch.setattr("tgfarm.s9_growth.begin_crosspost", lambda post_id, platform: 1)
    logs = []
    monkeypatch.setattr(adapters, "log_result", lambda *args, **kwargs: logs.append(args))
    monkeypatch.setattr(api_module, "_operator", lambda *args, **kwargs: None)

    adapter = adapters.ADAPTERS["reddit"]
    monkeypatch.setattr(adapter, "configured", lambda: True)

    async def fail_if_submitted(payload):
        pytest.fail("Reddit must be rechecked before the submit side effect")

    monkeypatch.setattr(adapter, "publish", fail_if_submitted)

    response = client.post(
        "/api/reddit/publish",
        json={
            "post_public_id": "published_123",
            "subreddit": "learnpython",
            "title": "A useful title",
            "body": "A useful post body",
            "confirm": True,
        },
        headers={HEADER: ensure_token()},
    )

    assert response.status_code == 409
    assert "выключен до отправки" in response.json()["detail"]
    assert len(logs) == 1
    assert logs[0][1].status == "skipped"


def test_automatic_submission_selects_one_best_community_and_revalidates_rules(monkeypatch):
    public_post = {
        "public_id": "published_123",
        "status": "published",
        "title": "Новый инструмент для анализа Python-кода",
        "body": "Инструмент помогает разработчикам находить ошибки и улучшать качество кода.",
        "cta": "",
        "hashtags": ["python", "coding"],
        "sources": [{"title": "Docs", "url": "https://example.org/docs"}],
        "channel_id": "tech",
    }
    monkeypatch.setattr(scout, "_post_record", lambda _post_id: public_post)
    monkeypatch.setattr(
        scout,
        "search_communities",
        lambda *args, **kwargs: _async_result({
            "candidates": [
                {
                    "name": "python",
                    "score": 92,
                    "over18": False,
                    "subreddit_type": "public",
                    "restrict_posting": False,
                    "submission_type": "any",
                    "risk_flags": ["Правила упоминают AI-контент"],
                    "rules": [{"title": "Be kind"}],
                    "requirements": {},
                },
                {
                    "name": "learnpython",
                    "title": "Learn Python",
                    "url": "https://www.reddit.com/r/learnpython/",
                    "score": 84,
                    "subscribers": 120000,
                    "active_user_count": 1200,
                    "over18": False,
                    "subreddit_type": "public",
                    "restrict_posting": False,
                    "submission_type": "self",
                    "risk_flags": [],
                    "rules": [{"title": "Be constructive", "description": "Give context."}],
                    "requirements": {"body_restriction_policy": "required"},
                },
            ]
        }),
    )
    monkeypatch.setattr(
        scout,
        "tailor_post",
        lambda _post_id, subreddit: _async_result({
            "draft": {
                "title": "A practical Python code review tool",
                "body": "The tool finds errors and helps developers improve code quality.",
            },
            "adaptation": {"mode": "ai", "provider": "test", "model": "test-model"},
        }),
    )
    monkeypatch.setattr(scout, "inspect_community", lambda subreddit: _async_result({
        "name": subreddit,
        "about": {
            "title": "Learn Python",
            "over18": False,
            "subreddit_type": "public",
            "submission_type": "self",
            "restrict_posting": False,
        },
        "rules": [{"title": "Be constructive"}],
        "requirements": {"body_restriction_policy": "required"},
        "risk_flags": [],
    }))
    monkeypatch.setattr(scout, "validate_submission", lambda title, body, community: [])

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is True
    assert result["subreddit"] == "learnpython"
    assert result["score"] == 84
    assert result["rules_checked"] == 1
    assert "AI-контент" not in result["reason"]


def _async_result(value):
    async def _return():
        return value

    return _return()


def test_automatic_submission_fails_closed_when_only_candidates_have_policy_risks(monkeypatch):
    monkeypatch.setattr(
        scout,
        "_post_record",
        lambda _post_id: {
            "public_id": "published_123",
            "status": "published",
            "title": "A topic worth discussing",
            "body": "A useful explanation with source-backed facts and context.",
            "cta": "",
            "hashtags": [],
            "sources": [],
            "channel_id": "general",
        },
    )
    monkeypatch.setattr(
        scout,
        "search_communities",
        lambda *args, **kwargs: _async_result({
            "candidates": [{
                "name": "selfpromotion",
                "score": 95,
                "over18": False,
                "subreddit_type": "public",
                "restrict_posting": False,
                "submission_type": "any",
                "risk_flags": ["Self-promotion ограничен."],
                "rules": [{"title": "No promotion"}],
                "requirements": {},
            }]
        }),
    )
    monkeypatch.setattr(
        scout,
        "tailor_post",
        lambda *_args, **_kwargs: pytest.fail("must not tailor a risky community"),
    )

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is False
    assert "правила содержат риск-флаги" in result["reason"]

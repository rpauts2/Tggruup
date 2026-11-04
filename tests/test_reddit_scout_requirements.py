"""Gap #1: the scout must know what the account can actually do in a subreddit.

These tests drive the real ``/api/v1/me`` identity fetch, the documented
minimum parser and ``inspect_destination``/``build_automatic_submission`` with
mocked HTTP only - no live publication ever happens.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx

from tgfarm.s9_growth import reddit_scout as scout

ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        reddit_client_id="test-client-id",
        reddit_client_secret="test-client-secret",
        reddit_refresh_token="test-refresh-token",
        reddit_api_approved=True,
        reddit_subreddit="",
        reddit_user_agent="python:tgfarm-tests:1.0 (by /u/test_account)",
    )


def _mock_httpx(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        return ORIGINAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


def _about(name: str, **extra) -> dict:
    data = {
        "display_name": name,
        "title": name.replace("_", " ").title(),
        "public_description": "python programming help and discussion",
        "subscribers": 120_000,
        "active_user_count": 1200,
        "over18": False,
        "subreddit_type": "public",
        "submission_type": "self",
        "restrict_posting": False,
    }
    data.update(extra)
    return {"kind": "t5", "data": data}


def _rules(*descriptions: str) -> dict:
    return {
        "rules": [
            {"short_name": f"Rule {index}", "description": text, "kind": "all"}
            for index, text in enumerate(descriptions, start=1)
        ]
    }


def _me(**overrides) -> dict:
    payload = {
        "name": "farm_account",
        "link_karma": 10,
        "comment_karma": 40,
        "created_utc": time.time() - 400 * 86_400,
    }
    payload.update(overrides)
    return payload


def _async_result(value):
    async def _return(*_args, **_kwargs):
        return value

    return _return()


# --------------------------------------------------------------------------
# Identity fetch
# --------------------------------------------------------------------------
def test_fetch_account_identity_reads_karma_and_age(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/me"
        return httpx.Response(200, json=_me(), request=request)

    _mock_httpx(monkeypatch, handler)

    async def run():
        async with httpx.AsyncClient() as client:
            return await scout._fetch_account_identity(client)

    identity = asyncio.run(run())
    assert identity["status"] == "known"
    assert identity["username"] == "farm_account"
    assert identity["total_karma"] == 50
    assert identity["account_age_days"] is not None
    assert identity["account_age_days"] > 300


def test_fetch_account_identity_marks_unknown_on_401_without_raising(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"}, request=request)

    _mock_httpx(monkeypatch, handler)

    async def run():
        async with httpx.AsyncClient() as client:
            return await scout._fetch_account_identity(client)

    identity = asyncio.run(run())
    assert identity["status"] == "unknown"
    assert identity.get("username") is None


# --------------------------------------------------------------------------
# Documented minimum + assessment (pure)
# --------------------------------------------------------------------------
def test_documented_minimum_parsed_from_rule_text_as_data():
    minimum = scout._documented_account_minimum(
        {},
        [{"title": "No spam", "description": "Accounts need a minimum karma of 100 and be at least 30 days old."}],
        {},
    )
    assert minimum["min_total_karma"] == 100
    assert minimum["min_account_age_days"] == 30
    assert minimum["requires_account"] is True


def test_assess_account_requirement_blocks_below_documented_karma():
    identity = {"status": "known", "username": "farm", "total_karma": 50, "account_age_days": 400}
    minimum = {"min_total_karma": 100, "min_account_age_days": None, "requires_account": True}
    result = scout._assess_account_requirements(identity, minimum)
    assert result["status"] == "insufficient"
    assert result["eligible"] is False
    assert "карма" in result["reason"]


def test_unknown_identity_never_assumed_to_pass_documented_minimum():
    identity = {"status": "unknown", "reason": "Reddit /api/v1/me недоступен."}
    minimum = {"min_total_karma": 100, "min_account_age_days": None, "requires_account": True}
    result = scout._assess_account_requirements(identity, minimum)
    assert result["status"] == "unknown"
    assert result["eligible"] is False


def test_community_without_documented_minimum_is_not_required_even_without_identity():
    identity = {"status": "unknown", "reason": "unavailable"}
    minimum = scout._documented_account_minimum({}, [{"title": "Be kind", "description": "Stay respectful."}], {})
    result = scout._assess_account_requirements(identity, minimum)
    assert minimum["requires_account"] is False
    assert result["status"] == "not_required"
    assert result["eligible"] is True


# --------------------------------------------------------------------------
# inspect_destination wires identity -> account assessment
# --------------------------------------------------------------------------
def _destination_handler(monkeypatch, *, me_status, karma, age_days, rule_text):
    created = time.time() - age_days * 86_400 if age_days is not None else None

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/me":
            if me_status != 200:
                return httpx.Response(me_status, json={"error": "no"}, request=request)
            body = {"name": "farm_account", "link_karma": karma, "comment_karma": 0}
            if created is not None:
                body["created_utc"] = created
            return httpx.Response(200, json=body, request=request)
        if path.endswith("/about"):
            return httpx.Response(200, json=_about("somepy"), request=request)
        if path.endswith("/about/rules"):
            return httpx.Response(200, json=_rules(rule_text), request=request)
        if path.endswith("/post_requirements"):
            return httpx.Response(200, json={}, request=request)
        if path.endswith("/new"):
            now = time.time()
            return httpx.Response(
                200,
                json={
                    "data": {
                        "children": [
                            {"data": {"created_utc": now - hours * 3600}} for hours in range(1, 25)
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected path {path}")

    _mock_httpx(monkeypatch, handler)


def test_inspect_destination_rejects_account_below_documented_karma(monkeypatch):
    _destination_handler(
        monkeypatch,
        me_status=200,
        karma=5,
        age_days=400,
        rule_text="Minimum karma of 500 required to post.",
    )

    async def run():
        async with httpx.AsyncClient() as client:
            identity = await scout._fetch_account_identity(client)
            return await scout.inspect_destination("somepy", client, identity=identity, title="Python tips", body="python")

    result = asyncio.run(run())
    assert result["account"]["status"] == "insufficient"
    assert result["account"]["eligible"] is False
    assert "карма" in result["account"]["reason"]


def test_inspect_destination_marks_unknown_when_identity_unavailable(monkeypatch):
    _destination_handler(
        monkeypatch,
        me_status=401,
        karma=0,
        age_days=None,
        rule_text="Minimum karma of 500 required to post.",
    )

    async def run():
        async with httpx.AsyncClient() as client:
            identity = await scout._fetch_account_identity(client)
            return await scout.inspect_destination("somepy", client, identity=identity, title="Python tips", body="python")

    result = asyncio.run(run())
    assert result["account"]["status"] == "unknown"
    assert result["account"]["eligible"] is False


# --------------------------------------------------------------------------
# Selection path rejects an ineligible subreddit and records the reason
# --------------------------------------------------------------------------
def _patch_build(monkeypatch, candidates, identity):
    monkeypatch.setattr(
        scout,
        "_post_record",
        lambda _pid: {
            "public_id": "published_123",
            "status": "published",
            "title": "Python automation guide",
            "body": "A python automation walkthrough with examples.",
            "cta": "",
            "hashtags": ["python"],
            "sources": [],
            "channel_id": "tech",
        },
    )
    monkeypatch.setattr(scout, "search_communities", lambda *a, **k: _async_result({"candidates": candidates}))
    monkeypatch.setattr(
        scout,
        "tailor_post",
        lambda *_a, **_k: _async_result(
            {"draft": {"title": "Python automation guide", "body": "Native rewrite"}, "adaptation": {"mode": "ai"}}
        ),
    )

    async def fake_inspect(subreddit):
        return {
            "name": subreddit,
            "about": {"submission_type": "self", "subreddit_type": "public", "over18": False},
            "rules": [{"title": "Be constructive"}],
            "requirements": {},
            "risk_flags": [],
            "policy_assessment": {
                "status": "ok",
                "allow_publish": True,
                "can_auto_publish": True,
                "reason": "fresh",
                "blockers": [],
            },
        }

    monkeypatch.setattr(scout, "inspect_community", fake_inspect)
    monkeypatch.setattr(scout, "validate_submission", lambda *a, **k: [])

    async def fake_open_client():
        return None, identity

    monkeypatch.setattr(scout, "_open_identity_client", fake_open_client)


def test_build_excludes_ineligible_subreddit_with_recorded_reason(monkeypatch):
    candidates = [
        {
            "name": "gatekeepsub",
            "score": 88,
            "over18": False,
            "subreddit_type": "public",
            "restrict_posting": False,
            "submission_type": "self",
            "risk_flags": [],
            "rules": [{"title": "Karma", "description": "Minimum karma of 1000 required.", "kind": "all"}],
            "requirements": {},
        },
        {
            "name": "openpython",
            "score": 80,
            "over18": False,
            "subreddit_type": "public",
            "restrict_posting": False,
            "submission_type": "self",
            "risk_flags": [],
            "rules": [{"title": "Be kind", "description": "Stay respectful.", "kind": "all"}],
            "requirements": {},
        },
    ]
    identity = {"status": "known", "username": "farm", "total_karma": 20, "account_age_days": 400}
    _patch_build(monkeypatch, candidates, identity)

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    # The high-scoring but inaccessible community is rejected; the accessible one wins.
    assert result["ok"] is True
    assert result["subreddit"] == "openpython"
    rejected = " ".join(result["rejected_candidates"])
    assert "gatekeepsub" in rejected
    assert "карма" in rejected


def test_build_rejects_documented_minimum_when_identity_unknown(monkeypatch):
    candidates = [
        {
            "name": "gatekeepsub",
            "score": 90,
            "over18": False,
            "subreddit_type": "public",
            "restrict_posting": False,
            "submission_type": "self",
            "risk_flags": [],
            "rules": [{"title": "Karma", "description": "Minimum karma of 1000 required.", "kind": "all"}],
            "requirements": {},
        }
    ]
    identity = {"status": "unknown", "reason": "Reddit /api/v1/me unavailable"}
    _patch_build(monkeypatch, candidates, identity)

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is False
    # The account cannot be verified against a stated requirement, so it is not assumed to pass.
    assert "gatekeepsub" in result["reason"]


# --------------------------------------------------------------------------
# Regression: the CommunityPolicy gate and rules-fetch still block publication
# --------------------------------------------------------------------------
def test_build_still_blocks_when_rules_cannot_be_fetched(monkeypatch):
    candidates = [
        {
            "name": "coruleless",
            "score": 90,
            "over18": False,
            "subreddit_type": "public",
            "restrict_posting": False,
            "submission_type": "self",
            "risk_flags": [],
            "rules": [],  # /about/rules returned nothing
            "requirements": {},
        }
    ]
    identity = {"status": "known", "username": "farm", "total_karma": 5000, "account_age_days": 900}
    _patch_build(monkeypatch, candidates, identity)

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is False
    assert "правила сообщества" in result["reason"]


def test_build_still_blocks_when_policy_recheck_denies_publish(monkeypatch):
    candidates = [
        {
            "name": "revokescustom",
            "score": 92,
            "over18": False,
            "subreddit_type": "public",
            "restrict_posting": False,
            "submission_type": "self",
            "risk_flags": [],
            "rules": [{"title": "Be kind", "description": "Stay respectful."}],
            "requirements": {},
        }
    ]
    identity = {"status": "known", "username": "farm", "total_karma": 5000, "account_age_days": 900}
    _patch_build(monkeypatch, candidates, identity)

    # The rules were visible during discovery but disappear on the fresh
    # re-check, so the CommunityPolicy gate built from this payload reports
    # them as unknown and the submission is refused.
    async def denied_inspect(subreddit):
        return {
            "name": subreddit,
            "about": {"submission_type": "self", "subreddit_type": "public", "over18": False},
            "rules": [],
            "requirements": {},
            "risk_flags": [],
        }

    monkeypatch.setattr(scout, "inspect_community", denied_inspect)

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is False
    assert "CommunityPolicy не разрешает публикацию" in result["reason"]


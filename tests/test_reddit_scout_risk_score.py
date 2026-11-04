"""Gap #3 and #4: a numeric removal-risk score and a measured activity rate.

The risk score is assembled from the qualitative flags plus the newly known
account/flair/activity facts and is used as an explicit exclusion and
tie-breaker inside the selection path. Activity is a real posts-per-day figure
derived from ``/r/{name}/new`` timestamps, never just ``active_user_count``.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from tgfarm.s9_growth import reddit_scout as scout

ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_httpx(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        return ORIGINAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


def _async_result(value):
    async def _return(*_args, **_kwargs):
        return value

    return _return()


# --------------------------------------------------------------------------
# Gap #3 - risk scoring (pure)
# --------------------------------------------------------------------------
def _base_kwargs(**overrides):
    kwargs = {
        "risk_flags": [],
        "requirements": {},
        "about": {"submission_type": "self", "subreddit_type": "public", "over18": False},
        "account_status": "not_required",
        "activity": {"posts_per_day": 8, "measured": True},
        "flair_required": False,
        "flair_selected": False,
    }
    kwargs.update(overrides)
    return kwargs


def test_removal_risk_is_a_bounded_zero_to_one_score():
    safe = scout._removal_risk_score(**_base_kwargs())
    assert 0.0 <= safe <= 1.0
    extreme = scout._removal_risk_score(
        **_base_kwargs(
            risk_flags=["self promotion", "no links", "no bots", "low effort", "ai"],
            requirements={"body_restriction_policy": "notAllowed", "domain_whitelist": ["only.example"]},
            about={"submission_type": "link", "over18": True, "subreddit_type": "restricted"},
            account_status="insufficient",
            activity={"posts_per_day": 0.1, "measured": True},
            flair_required=True,
            flair_selected=False,
        )
    )
    assert extreme == 1.0


def test_risk_score_orders_two_otherwise_equal_subreddits():
    quiet = scout._removal_risk_score(**_base_kwargs(risk_flags=["есть ограничения на ссылки"]))
    clean = scout._removal_risk_score(**_base_kwargs())
    assert quiet > clean


def test_measured_activity_changes_the_score_but_static_unknown_is_only_neutral():
    measured_slow = scout._removal_risk_score(**_base_kwargs(activity={"posts_per_day": 0.2, "measured": True}))
    measured_fast = scout._removal_risk_score(**_base_kwargs(activity={"posts_per_day": 40, "measured": True}))
    unknown = scout._removal_risk_score(**_base_kwargs(activity={"posts_per_day": None, "measured": False}))
    assert measured_fast < measured_slow
    # Unknown activity is a small penalty, not an automatic rejection.
    assert unknown < measured_slow


# --------------------------------------------------------------------------
# Gap #4 - measured activity from listing timestamps
# --------------------------------------------------------------------------
def test_posts_per_day_from_created_utc_timestamps():
    now = time.time()
    items = [{"created_utc": now - hours * 3600} for hours in (1, 6, 12, 24, 48)]
    activity = scout._posts_per_day(items, now=now)
    assert activity["measured"] is True
    assert activity["sample_size"] == 5
    assert activity["posts_per_day"] > 0


def test_posts_per_day_is_none_without_timestamps():
    assert scout._posts_per_day([])["measured"] is False
    assert scout._posts_per_day([{"created_utc": "bad"}])["posts_per_day"] is None


def test_discovery_activity_is_an_estimate_and_the_fallback_reports_unknown():
    parts, reasons = scout._candidate_score(
        {"display_name": "python", "title": "Python", "public_description": "python discussion",
         "active_user_count": 900_000, "subscribers": 5_000_000},
        query_terms=["python"],
        content_terms=["python"],
        result_index=0,
        result_count=1,
    )[1:]
    # Community search returns counters only, so the reason must say it is an
    # estimate rather than dress the counter up as a measured cadence.
    assert any("статичная оценка" in reason for reason in reasons)
    assert parts["activity"] > 0
    # Without a listing of timestamps no posting rate is invented.
    dest = scout._candidate_destination({"name": "python"}, {"status": "unknown"})
    assert dest["activity"] == {"posts_per_day": None, "measured": False}


def test_inspect_destination_measures_a_real_rate(monkeypatch):
    now = time.time()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/about"):
            return httpx.Response(
                200,
                json={
                    "kind": "t5",
                    "data": {
                        "display_name": "python",
                        "title": "Python",
                        "public_description": "python news",
                        "subscribers": 4_000_000,
                        "active_user_count": 9_000,
                        "over18": False,
                        "subreddit_type": "public",
                        "submission_type": "self",
                        "restrict_posting": False,
                    },
                },
                request=request,
            )
        if path.endswith("/about/rules"):
            return httpx.Response(200, json={"rules": [{"short_name": "Topic", "description": "Stay on topic."}]}, request=request)
        if path.endswith("/post_requirements"):
            return httpx.Response(200, json={}, request=request)
        if path.endswith("/new"):
            return httpx.Response(
                200,
                json={"data": {"children": [{"data": {"created_utc": now - h * 3600}} for h in range(1, 13)]}},
                request=request,
            )
        raise AssertionError(f"Unexpected path {path}")

    _mock_httpx(monkeypatch, handler)

    async def run():
        async with httpx.AsyncClient() as client:
            return await scout.inspect_destination(
                "python", client, identity={"status": "unknown"}, title="python", body="python"
            )

    result = asyncio.run(run())
    assert result["activity"]["measured"] is True
    assert result["activity"]["sample_size"] == 12
    assert result["activity"]["posts_per_day"] > 0


def test_inspect_destination_caches_per_subreddit_within_one_run(monkeypatch):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/about"):
            return httpx.Response(
                200,
                json={"kind": "t5", "data": {
                    "display_name": "python", "title": "Python", "public_description": "python",
                    "subscribers": 10, "active_user_count": 2, "over18": False,
                    "subreddit_type": "public", "submission_type": "self", "restrict_posting": False,
                }},
                request=request,
            )
        if path.endswith("/about/rules"):
            return httpx.Response(200, json={"rules": [{"short_name": "a", "description": "a"}]}, request=request)
        if path.endswith("/post_requirements"):
            return httpx.Response(200, json={}, request=request)
        if path.endswith("/new"):
            return httpx.Response(200, json={"data": {"children": []}}, request=request)
        raise AssertionError(f"Unexpected path {path}")

    _mock_httpx(monkeypatch, handler)

    async def run():
        cache: dict = {}
        async with httpx.AsyncClient() as client:
            first = await scout.inspect_destination("python", client, identity={"status": "unknown"}, cache=cache)
            before = list(calls)
            second = await scout.inspect_destination("python", client, identity={"status": "unknown"}, cache=cache)
        return first, second, before, list(calls)

    first, second, before, after = asyncio.run(run())
    assert before == after  # the cached entry means no extra Reddit requests on the second call
    assert first["removal_risk"] == second["removal_risk"]


# --------------------------------------------------------------------------
# Selection path uses the score as an exclusion and a tie-breaker
# --------------------------------------------------------------------------
def _patch_selection(monkeypatch, candidates):
    monkeypatch.setattr(
        scout,
        "_post_record",
        lambda _pid: {
            "public_id": "published_123",
            "status": "published",
            "title": "python automation guide",
            "body": "python automation with asyncio queues",
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
            {"draft": {"title": "python automation", "body": "native rewrite"}, "adaptation": {"mode": "ai"}}
        ),
    )

    async def fake_inspect(subreddit):
        return {
            "name": subreddit,
            "about": {"submission_type": "self", "subreddit_type": "public", "over18": False},
            "rules": [{"title": "Topic", "description": "Stay on topic."}],
            "requirements": {},
            "risk_flags": [],
            "policy_assessment": {"status": "ok", "allow_publish": True, "can_auto_publish": True},
        }

    monkeypatch.setattr(scout, "inspect_community", fake_inspect)
    monkeypatch.setattr(scout, "validate_submission", lambda *a, **k: [])

    async def fake_open_client():
        class _C:
            async def aclose(self):
                return None

        return _C(), {"status": "unknown", "reason": "unavailable"}

    monkeypatch.setattr(scout, "_open_identity_client", fake_open_client)


def _candidate(name, *, risk, rate=8.0):
    return {
        "name": name,
        "score": 80,
        "subscribers": 50000,
        "active_user_count": 500,
        "over18": False,
        "subreddit_type": "public",
        "restrict_posting": False,
        "submission_type": "self",
        "risk_flags": [],
        "rules": [{"title": "Topic", "description": "Stay on topic."}],
        "requirements": {},
        "_risk": risk,
        "_rate": rate,
    }


def _fake_destination_from_marker(subreddit, client, *, identity=None, title="", body="", cache=None):
    marker = {"low": 0.15, "mid": 0.35, "high": 0.85}

    async def fake(subreddit, client, *, identity=None, title="", body="", cache=None):
        # The candidate carrying the marker is patched in by the test wrapper.
        risk = marker[subreddit]
        return {
            "name": subreddit,
            "rules": [{"title": "Topic"}],
            "requirements": {},
            "risk_flags": [],
            "flair": {"selected": None, "reason": "not required"},
            "flair_required": False,
            "account_minimum": {"requires_account": False},
            "account": {"status": "not_required", "eligible": True, "reason": "no minimum"},
            "activity": {"posts_per_day": 8.0, "measured": True, "sample_size": 10},
            "removal_risk": risk,
            "risk_band": scout._risk_band(risk),
        }

    return fake


def test_build_orders_equal_subreddits_by_lower_risk(monkeypatch):
    candidates = [_candidate("mid", risk="mid"), _candidate("low", risk="low")]
    _patch_selection(monkeypatch, candidates)
    # Both candidates have the same score; the fake destination keys off the name.
    monkeypatch.setattr(scout, "inspect_destination", _fake_destination_from_marker(None, None))

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is True
    assert result["subreddit"] == "low"
    assert result["removal_risk"] < 0.2


def test_build_excludes_candidate_above_risk_threshold(monkeypatch):
    candidates = [_candidate("high", risk="high")]
    _patch_selection(monkeypatch, candidates)
    monkeypatch.setattr(scout, "inspect_destination", _fake_destination_from_marker(None, None))

    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is False
    assert "риск удаления" in result["reason"]
    assert "high" in result["reason"]

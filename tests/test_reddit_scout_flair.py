"""Gap #2: flair is read, matched to the post theme and carried to submit.

No template is ever sent at random: when nothing fits, the flair stays unset
and the reason is recorded. All HTTP is mocked; nothing is published live.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx

from tgfarm.s9_growth import adapters
from tgfarm.s9_growth import reddit_scout as scout

ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_httpx(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        return ORIGINAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


def _reddit_settings(subreddit: str = "learnpython") -> SimpleNamespace:
    return SimpleNamespace(
        reddit_client_id="test-client-id",
        reddit_client_secret="test-client-secret",
        reddit_refresh_token="test-refresh-token",
        reddit_api_approved=True,
        reddit_subreddit=subreddit,
        reddit_user_agent="python:tgfarm-tests:1.0 (by /u/test_account)",
    )


# --------------------------------------------------------------------------
# Template parsing + theme matching (pure)
# --------------------------------------------------------------------------
def test_parse_flair_choices_normalises_and_drops_malformed():
    parsed = scout._parse_flair_choices(
        {
            "choices": [
                {"flair_text": "Tutorial", "flair_template_id": "aa-111"},
                {"text": "Discussion", "id": "bb-222"},
                {"flair_css_class": "help_request"},
                {"nonsense": True},
                "Plain string flair",
            ]
        }
    )
    assert {"flair_text": "Tutorial", "flair_id": "aa-111", "css_class": ""} in parsed
    assert {"flair_text": "Discussion", "flair_id": "bb-222", "css_class": ""} in parsed
    assert {"flair_text": "help request", "flair_id": "", "css_class": "help_request"} in parsed
    assert {"flair_text": "Plain string flair", "flair_id": "", "css_class": ""} in parsed
    assert all(item["flair_text"] or item["flair_id"] for item in parsed)


def test_parse_flair_choices_survives_empty_or_malformed_payloads():
    assert scout._parse_flair_choices({}) == []
    assert scout._parse_flair_choices({"choices": "not-a-list"}) == []
    assert scout._parse_flair_choices(None) == []


def test_select_flair_chooses_template_that_matches_the_theme():
    choices = [
        {"flair_text": "meme", "flair_id": "1"},
        {"flair_text": "python tutorial", "flair_id": "2"},
        {"flair_text": "politics", "flair_id": "3"},
    ]
    result = scout.select_flair(choices, "A python tutorial about asyncio", "Step by step python asyncio guide")
    assert result["selected"] is not None
    assert result["selected"]["flair_id"] == "2"
    assert "python tutorial" in result["selected"]["flair_text"]


def test_select_flair_leaves_flair_unset_when_nothing_fits():
    choices = [
        {"flair_text": "sports", "flair_id": "1"},
        {"flair_text": "recipes", "flair_id": "2"},
    ]
    result = scout.select_flair(choices, "Python asyncio backpressure", "asyncio semaphore and queues")
    assert result["selected"] is None
    assert "не выбран" in result["reason"] or "не вернул" in result["reason"]


def test_select_flair_handles_missing_templates():
    result = scout.select_flair([], "any title", "any body")
    assert result["selected"] is None
    assert result["available"] == 0


# --------------------------------------------------------------------------
# inspect_destination carries the selected flair
# --------------------------------------------------------------------------
def _flaired_handler(monkeypatch, *, choices):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/about"):
            return httpx.Response(
                200,
                json={
                    "kind": "t5",
                    "data": {
                        "display_name": "learnpython",
                        "title": "Learn Python",
                        "public_description": "python learning questions",
                        "subscribers": 120_000,
                        "active_user_count": 1200,
                        "over18": False,
                        "subreddit_type": "public",
                        "submission_type": "self",
                        "restrict_posting": False,
                    },
                },
                request=request,
            )
        if path.endswith("/about/rules"):
            return httpx.Response(200, json={"rules": [{"short_name": "Be kind", "description": "Stay respectful."}]}, request=request)
        if path.endswith("/post_requirements"):
            return httpx.Response(200, json={"is_flair_required": True, "body_restriction_policy": "required"}, request=request)
        if path.endswith("/api/link_flair.json"):
            return httpx.Response(200, json={"choices": choices}, request=request)
        if path.endswith("/new"):
            import time as _time

            now = _time.time()
            return httpx.Response(
                200,
                json={"data": {"children": [{"data": {"created_utc": now - hours * 3600}} for hours in range(1, 20)]}},
                request=request,
            )
        raise AssertionError(f"Unexpected path {path}")

    _mock_httpx(monkeypatch, handler)


def test_inspect_destination_selects_matching_flair_and_records_reason(monkeypatch):
    _flaired_handler(
        monkeypatch,
        choices=[
            {"flair_text": "meme", "flair_template_id": "m1"},
            {"flair_text": "python question", "flair_template_id": "q9"},
        ],
    )

    async def run():
        async with httpx.AsyncClient() as client:
            identity = {"status": "unknown", "reason": "no requirement"}
            return await scout.inspect_destination(
                "learnpython",
                client,
                identity=identity,
                title="A python question about asyncio",
                body="asyncio queues in python",
            )

    result = asyncio.run(run())
    assert result["flair_required"] is True
    assert result["flair"]["selected"]["flair_id"] == "q9"
    assert result["flair"]["selected"]["flair_text"] == "python question"
    assert "python question" in result["flair"]["reason"]


def test_inspect_destination_keeps_flair_unset_when_no_template_fits(monkeypatch):
    _flaired_handler(
        monkeypatch,
        choices=[
            {"flair_text": "sports", "flair_template_id": "s1"},
            {"flair_text": "cooking", "flair_template_id": "c1"},
        ],
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await scout.inspect_destination(
                "learnpython",
                client,
                identity={"status": "unknown"},
                title="python asyncio backpressure",
                body="asyncio semaphore queues python",
            )

    result = asyncio.run(run())
    assert result["flair_required"] is True
    assert result["flair"]["selected"] is None
    # An unmatched required flair keeps its own reason and is recorded.
    assert "flair" in result["flair"]["reason"].lower()


def test_inspect_destination_survives_flair_endpoint_failure(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/about"):
            return httpx.Response(
                200,
                json={
                    "kind": "t5",
                    "data": {
                        "display_name": "learnpython",
                        "title": "Learn Python",
                        "public_description": "python questions",
                        "subscribers": 1000,
                        "active_user_count": 10,
                        "over18": False,
                        "subreddit_type": "public",
                        "submission_type": "self",
                        "restrict_posting": False,
                    },
                },
                request=request,
            )
        if path.endswith("/about/rules"):
            return httpx.Response(200, json={"rules": [{"short_name": "Flair", "description": "Pick one."}]}, request=request)
        if path.endswith("/post_requirements"):
            return httpx.Response(200, json={"is_flair_required": True}, request=request)
        if path.endswith("/api/link_flair.json"):
            return httpx.Response(500, json={"message": "boom"}, request=request)
        if path.endswith("/new"):
            return httpx.Response(200, json={"data": {"children": []}}, request=request)
        raise AssertionError(f"Unexpected path {path}")

    _mock_httpx(monkeypatch, handler)

    async def run():
        async with httpx.AsyncClient() as client:
            return await scout.inspect_destination(
                "learnpython", client, identity={"status": "unknown"}, title="python tips", body="python"
            )

    result = asyncio.run(run())
    assert result["flair"]["selected"] is None
    assert result["flair_required"] is True


# --------------------------------------------------------------------------
# Selection path hands the chosen flair to the adapter meta
# --------------------------------------------------------------------------
def _async_result(value):
    async def _return(*_args, **_kwargs):
        return value

    return _return()


def _patch_selection_with_flair(monkeypatch, flair_selected):
    monkeypatch.setattr(
        scout,
        "_post_record",
        lambda _pid: {
            "public_id": "published_123",
            "status": "published",
            "title": "Python asyncio guide",
            "body": "python asyncio queues and semaphores",
            "cta": "",
            "hashtags": ["python"],
            "sources": [],
            "channel_id": "tech",
        },
    )
    monkeypatch.setattr(
        scout,
        "search_communities",
        lambda *a, **k: _async_result(
            {
                "candidates": [
                    {
                        "name": "learnpython",
                        "score": 90,
                        "over18": False,
                        "subreddit_type": "public",
                        "restrict_posting": False,
                        "submission_type": "self",
                        "risk_flags": [scout.FLAIR_REQUIRED_FLAG],
                        "rules": [{"title": "Use flair", "description": "Select a flair."}],
                        "requirements": {"is_flair_required": True},
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(
        scout,
        "tailor_post",
        lambda *_a, **_k: _async_result(
            {"draft": {"title": "Python asyncio guide", "body": "Native rewrite"}, "adaptation": {"mode": "ai"}}
        ),
    )

    async def fake_inspect(subreddit):
        return {
            "name": subreddit,
            "about": {"submission_type": "self", "subreddit_type": "public", "over18": False},
            "rules": [{"title": "Use flair"}],
            "requirements": {"is_flair_required": True},
            "risk_flags": [scout.FLAIR_REQUIRED_FLAG],
            "policy_assessment": {"status": "ok", "allow_publish": True, "can_auto_publish": True},
        }

    monkeypatch.setattr(scout, "inspect_community", fake_inspect)

    def real_validate(title, body, community):
        # Mirror the machine check the gate performs on the chosen flair.
        selected = community.get("selected_flair") or {}
        if community.get("requirements", {}).get("is_flair_required") and not (selected.get("flair_id") or selected.get("flair_text")):
            return ["flair required"]
        return []

    monkeypatch.setattr(scout, "validate_submission", real_validate)

    async def fake_open_client():
        class _FakeClient:
            async def aclose(self):
                return None

        return _FakeClient(), {"status": "unknown", "reason": "no documented minimum"}

    monkeypatch.setattr(scout, "_open_identity_client", fake_open_client)

    async def fake_destination(subreddit, client, *, identity=None, title="", body="", cache=None):
        return {
            "name": subreddit,
            "rules": [{"title": "Use flair"}],
            "requirements": {"is_flair_required": True},
            "risk_flags": [scout.FLAIR_REQUIRED_FLAG],
            "flair": {"selected": flair_selected, "reason": "chosen by theme" if flair_selected else "no fit"},
            "flair_required": True,
            "account_minimum": {"requires_account": False},
            "account": {"status": "not_required", "eligible": True, "reason": "no minimum"},
            "activity": {"posts_per_day": 5, "measured": True, "sample_size": 10},
            "removal_risk": 0.2,
            "risk_band": "низкий",
        }

    monkeypatch.setattr(scout, "inspect_destination", fake_destination)


def test_build_carries_selected_flair_into_submission(monkeypatch):
    _patch_selection_with_flair(monkeypatch, {"flair_text": "python question", "flair_id": "q9"})
    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is True
    assert result["subreddit"] == "learnpython"
    assert result["flair"]["required"] is True
    assert result["flair"]["selected"]["flair_id"] == "q9"


def test_build_rejects_required_flair_when_no_template_fits(monkeypatch):
    _patch_selection_with_flair(monkeypatch, None)
    result = asyncio.run(scout.build_automatic_submission("published_123"))

    assert result["ok"] is False
    assert "flair" in result["reason"].lower()


# --------------------------------------------------------------------------
# Adapter sends the flair through /api/submit
# --------------------------------------------------------------------------
def test_reddit_adapter_sends_selected_flair_in_submit(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)

    def token_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}, request=request)

    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response(request)
        assert request.url.path == "/api/submit"
        fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen.update(fields)
        return httpx.Response(
            200,
            json={"json": {"errors": [], "data": {"things": [{"kind": "t3", "data": {"id": "xyz", "permalink": "/r/learnpython/comments/xyz/t/"}}]}}},
            request=request,
        )

    _mock_httpx(monkeypatch, handler)
    result = asyncio.run(
        adapters.RedditAdapter().publish(
            SimpleNamespace(
                platform="reddit",
                title="A title",
                text="A body",
                link=None,
                meta={"subreddit": "learnpython", "reddit_kind": "self", "flair": {"flair_id": "q9", "flair_text": "python question"}},
            )
        )
    )

    assert result.status == "posted"
    assert seen["flair_id"] == "q9"
    assert seen["flair_text"] == "python question"


def test_reddit_adapter_omits_flair_when_none_selected(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}, request=request)
        fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen.update(fields)
        return httpx.Response(
            200,
            json={"json": {"errors": [], "data": {"things": [{"kind": "t3", "data": {"id": "xyz"}}]}}},
            request=request,
        )

    _mock_httpx(monkeypatch, handler)
    result = asyncio.run(
        adapters.RedditAdapter().publish(
            SimpleNamespace(
                platform="reddit",
                title="A title",
                text="A body",
                link=None,
                meta={"subreddit": "learnpython", "reddit_kind": "self", "flair": {"required": True, "selected": None}},
            )
        )
    )

    assert result.status == "posted"
    assert "flair_id" not in seen
    assert "flair_text" not in seen

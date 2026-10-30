from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx

from tgfarm.core.schemas import CrosspostPayload
from tgfarm.s9_growth import adapters


ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _reddit_settings(subreddit: str = "learnpython") -> SimpleNamespace:
    return SimpleNamespace(
        reddit_client_id="test-client-id",
        reddit_client_secret="test-client-secret",
        reddit_refresh_token="test-refresh-token",
        reddit_subreddit=subreddit,
        reddit_user_agent="python:tgfarm-tests:1.0 (by /u/test_account)",
    )


def _mock_httpx(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        return ORIGINAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


def _token_response(request: httpx.Request) -> httpx.Response:
    assert request.url == "https://www.reddit.com/api/v1/access_token"
    assert request.headers["user-agent"] == "python:tgfarm-tests:1.0 (by /u/test_account)"
    assert request.headers["authorization"].startswith("Basic ")
    fields = parse_qs(request.content.decode())
    assert fields["grant_type"] == ["refresh_token"]
    assert fields["refresh_token"] == ["test-refresh-token"]
    return httpx.Response(
        200,
        json={"access_token": "temporary-access-token", "expires_in": 3600},
        request=request,
    )


def test_reddit_adapter_publishes_native_self_post_and_captures_remote_url(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url == "https://www.reddit.com/api/v1/access_token":
            return _token_response(request)

        assert request.url == "https://oauth.reddit.com/api/submit"
        assert request.headers["authorization"] == "Bearer temporary-access-token"
        assert request.headers["user-agent"] == "python:tgfarm-tests:1.0 (by /u/test_account)"
        fields = parse_qs(request.content.decode())
        assert fields["api_type"] == ["json"]
        assert fields["sr"] == ["learnpython"]
        assert fields["kind"] == ["self"]
        assert fields["title"] == ["A useful guide"]
        assert fields["text"] == ["Main body\n\nSource: https://example.com/story"]
        assert fields["resubmit"] == ["false"]
        assert fields["sendreplies"] == ["true"]
        return httpx.Response(
            200,
            json={
                "json": {
                    "errors": [],
                    "data": {
                        "things": [
                            {
                                "kind": "t3",
                                "data": {
                                    "id": "abc123",
                                    "name": "t3_abc123",
                                    "permalink": "/r/learnpython/comments/abc123/a_useful_guide/",
                                },
                            }
                        ]
                    },
                }
            },
            request=request,
        )

    _mock_httpx(monkeypatch, handler)
    adapter = adapters.RedditAdapter()
    payload = CrosspostPayload(
        platform="reddit",
        title="A useful guide",
        text="Main body",
        meta={
            "subreddit": "r/learnpython",
            "cta": "Source: https://example.com/story",
        },
    )

    result = asyncio.run(adapter.publish(payload))

    assert result.status == "posted"
    assert result.remote_id == "abc123"
    assert result.url == "https://www.reddit.com/r/learnpython/comments/abc123/a_useful_guide/"
    assert len(seen) == 2


def test_reddit_adapter_caches_oauth_token_across_posts(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)
    token_requests = 0
    post_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, post_requests
        if request.url == "https://www.reddit.com/api/v1/access_token":
            token_requests += 1
            return _token_response(request)
        post_requests += 1
        return httpx.Response(
            200,
            json={
                "json": {
                    "errors": [],
                    "data": {"url": "https://www.reddit.com/r/learnpython/comments/a1/"},
                }
            },
            request=request,
        )

    _mock_httpx(monkeypatch, handler)
    adapter = adapters.RedditAdapter()

    async def run_two_posts():
        for index in range(2):
            result = await adapter.publish(
                CrosspostPayload(
                    platform="reddit",
                    title=f"Post {index}",
                    text="Content",
                )
            )
            assert result.status == "posted"

    asyncio.run(run_two_posts())
    assert token_requests == 1
    assert post_requests == 2


def test_reddit_adapter_treats_subreddit_rejection_as_permanent(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://www.reddit.com/api/v1/access_token":
            return _token_response(request)
        return httpx.Response(
            200,
            json={
                "json": {
                    "errors": [
                        ["SUBREDDIT_NOTALLOWED", "The account cannot post here.", "sr"]
                    ]
                }
            },
            request=request,
        )

    _mock_httpx(monkeypatch, handler)
    result = asyncio.run(
        adapters.RedditAdapter().publish(
            CrosspostPayload(platform="reddit", title="A title", text="A useful body")
        )
    )

    assert result.status == "permanent"
    assert "SUBREDDIT_NOTALLOWED" in (result.error or "")
    assert result.url is None


def test_reddit_rate_limit_is_retryable_but_ambiguous_http_failure_is_not(monkeypatch):
    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)

    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://www.reddit.com/api/v1/access_token":
            return _token_response(request)
        return httpx.Response(
            200,
            json={"json": {"errors": [["RATELIMIT", "Try again later.", ""]]}},
            request=request,
        )

    _mock_httpx(monkeypatch, rate_limit_handler)
    limited = asyncio.run(
        adapters.RedditAdapter().publish(
            CrosspostPayload(platform="reddit", title="Title", text="Body")
        )
    )
    assert limited.status == "failed"

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://www.reddit.com/api/v1/access_token":
            return _token_response(request)
        return httpx.Response(502, json={"message": "temporary gateway failure"}, request=request)

    _mock_httpx(monkeypatch, gateway_handler)
    ambiguous = asyncio.run(
        adapters.RedditAdapter().publish(
            CrosspostPayload(platform="reddit", title="Title", text="Body")
        )
    )
    assert ambiguous.status == "unknown"


def test_reddit_adapter_fails_closed_for_missing_credentials_and_invalid_subreddit(monkeypatch):
    incomplete = SimpleNamespace(
        reddit_client_id="",
        reddit_client_secret="",
        reddit_refresh_token="",
        reddit_subreddit="",
        reddit_user_agent="",
    )
    monkeypatch.setattr(adapters, "get_settings", lambda: incomplete)
    adapter = adapters.RedditAdapter()
    assert adapter.configured() is False
    skipped = asyncio.run(
        adapter.publish(CrosspostPayload(platform="reddit", title="Title", text="Body"))
    )
    assert skipped.status == "skipped"

    monkeypatch.setattr(adapters, "get_settings", _reddit_settings)
    invalid = asyncio.run(
        adapters.RedditAdapter().publish(
            CrosspostPayload(
                platform="reddit",
                title="Title",
                text="Body",
                meta={"subreddit": "../../not-a-subreddit"},
            )
        )
    )
    assert invalid.status == "permanent"
    assert "Invalid Reddit subreddit" in (invalid.error or "")



def test_cli_reddit_live_mode_requires_explicit_confirmation(monkeypatch, capsys):
    from types import SimpleNamespace

    from tgfarm import cli

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(live=True))
    args = SimpleNamespace(post_id="published_post_123", confirm_live=False)

    code = cli.cmd_reddit_test(args)

    assert code == 2
    assert "explicit --confirm-live" in capsys.readouterr().out

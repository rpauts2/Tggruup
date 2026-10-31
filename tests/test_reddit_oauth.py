from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from tgfarm import reddit_oauth


def test_reddit_oauth_uses_permanent_refresh_token_and_required_scopes():
    reddit_oauth._PENDING.clear()
    result = asyncio.run(reddit_oauth.begin_reddit_oauth(
        "test-client-id",
        "test-client-secret",
        "http://127.0.0.1:8000/api/platforms/reddit/auth/callback",
    ))
    parsed = urlparse(result["authorize_url"])
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "www.reddit.com"
    assert parsed.path == "/api/v1/authorize"
    assert query["client_id"] == ["test-client-id"]
    assert query["response_type"] == ["code"]
    assert query["duration"] == ["permanent"]
    assert set(query["scope"][0].split()) == {"identity", "read", "submit"}
    assert query["redirect_uri"] == [
        "http://127.0.0.1:8000/api/platforms/reddit/auth/callback"
    ]
    state = query["state"][0]
    assert state in reddit_oauth._PENDING

    reddit_oauth.cancel_reddit_oauth(state)
    assert state not in reddit_oauth._PENDING


def test_reddit_oauth_rejects_insecure_non_loopback_callback():
    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(reddit_oauth.begin_reddit_oauth(
            "client-id",
            "client-secret",
            "http://example.com/oauth/callback",
        ))


def test_reddit_oauth_requires_app_credentials():
    with pytest.raises(ValueError, match="Client ID"):
        asyncio.run(reddit_oauth.begin_reddit_oauth(
            "",
            "",
            "https://tgfarm.example.com/api/platforms/reddit/auth/callback",
        ))

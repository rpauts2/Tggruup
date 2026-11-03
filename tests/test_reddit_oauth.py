from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

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



def test_reddit_oauth_start_requires_explicit_data_api_approval(monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import HEADER, ensure_token
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reddit_api_approved", False)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/platforms/reddit/auth/start",
            headers={HEADER: ensure_token()},
        )

    assert response.status_code == 403
    assert "REDDIT_API_APPROVED" in response.json()["detail"]


def test_reddit_oauth_callback_stops_if_approval_was_revoked(monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.api import create_app
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reddit_api_approved", False)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    pending = reddit_oauth._PENDING
    pending.clear()
    begun = asyncio.run(
        reddit_oauth.begin_reddit_oauth(
            "test-client-id",
            "test-client-secret",
            "http://127.0.0.1:8000/api/platforms/reddit/auth/callback",
        )
    )
    state = parse_qs(urlparse(begun["authorize_url"]).query)["state"][0]

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        response = client.get(
            "/api/platforms/reddit/auth/callback",
            params={"state": state, "code": "authorization-code"},
        )

    assert response.status_code == 403
    assert state not in pending

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

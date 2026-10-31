from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeTumblrClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse({
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
        })

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({
            "meta": {"status": 200},
            "response": {
                "name": "demo-user",
                "blogs": [
                    {
                        "name": "primary-blog",
                        "title": "Primary",
                        "url": "https://primary-blog.tumblr.com/",
                        "primary": True,
                    },
                    {
                        "name": "other-blog",
                        "title": "Selected Blog",
                        "url": "https://other-blog.tumblr.com/",
                        "primary": False,
                    },
                ],
            },
        })


@pytest.mark.asyncio
async def test_tumblr_oauth_uses_one_time_state_and_selects_requested_blog(monkeypatch):
    from tgfarm import tumblr_oauth

    FakeTumblrClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeTumblrClient)
    started = await tumblr_oauth.begin_tumblr_oauth(
        "client-id",
        "client-secret",
        "https://farm.example/api/platforms/tumblr/auth/callback",
        preferred_blog="other-blog",
    )
    params = parse_qs(urlsplit(started["authorize_url"]).query)
    assert params["scope"] == ["basic write offline_access"]
    assert params["redirect_uri"] == ["https://farm.example/api/platforms/tumblr/auth/callback"]
    state = params["state"][0]

    result = await tumblr_oauth.complete_tumblr_oauth(state, "authorization-code")

    assert result["blog_identifier"] == "other-blog"
    assert result["blog_title"] == "Selected Blog"
    assert result["blog_url"] == "https://other-blog.tumblr.com/"
    assert result["username"] == "demo-user"
    assert result["refresh_token"] == "refresh-secret"
    token_call = FakeTumblrClient.calls[0]
    assert token_call[0] == "POST"
    assert token_call[2]["data"]["grant_type"] == "authorization_code"
    assert token_call[2]["data"]["redirect_uri"] == started["redirect_uri"]
    profile_call = FakeTumblrClient.calls[1]
    assert profile_call[2]["headers"]["Authorization"] == "Bearer access-secret"
    assert profile_call[2]["headers"]["User-Agent"] == tumblr_oauth.USER_AGENT
    with pytest.raises(ValueError, match="истекла или уже использована"):
        await tumblr_oauth.complete_tumblr_oauth(state, "another-code")


@pytest.mark.asyncio
async def test_tumblr_oauth_defaults_to_primary_blog(monkeypatch):
    from tgfarm import tumblr_oauth

    FakeTumblrClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeTumblrClient)
    started = await tumblr_oauth.begin_tumblr_oauth(
        "client-id", "client-secret", "http://127.0.0.1:8000/api/platforms/tumblr/auth/callback"
    )
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    result = await tumblr_oauth.complete_tumblr_oauth(state, "code")
    assert result["blog_identifier"] == "primary-blog"


class FakeRefreshClient(FakeTumblrClient):
    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse({
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 7200,
        })


@pytest.mark.asyncio
async def test_tumblr_access_token_refresh_persists_rotated_refresh_token(monkeypatch):
    from tgfarm import tumblr_oauth
    from tgfarm.core import settings as settings_module
    from tgfarm.platforms import save_platform_credentials

    stored = []
    monkeypatch.setattr(settings_module, "get_settings", lambda: type("Settings", (), {
        "tumblr_access_token": "expired-access",
        "tumblr_refresh_token": "old-refresh",
        "tumblr_client_id": "client-id",
        "tumblr_client_secret": "client-secret",
        "tumblr_token_expires_at": 1,
    })())
    monkeypatch.setattr("tgfarm.platforms.save_platform_credentials", lambda platform, values: stored.append((platform, values)))
    monkeypatch.setattr(httpx, "AsyncClient", FakeRefreshClient)

    result = await tumblr_oauth.get_valid_tumblr_access_token()

    assert result == "rotated-access"
    assert stored == [("tumblr", {
        "TUMBLR_ACCESS_TOKEN": "rotated-access",
        "TUMBLR_TOKEN_EXPIRES_AT": stored[0][1]["TUMBLR_TOKEN_EXPIRES_AT"],
        "TUMBLR_REFRESH_TOKEN": "rotated-refresh",
    })]

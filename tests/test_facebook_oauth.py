from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeFacebookOAuthClient:
    calls = []
    tasks = ["CREATE_CONTENT", "MODERATE"]

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        params = kwargs.get("params") or {}
        if url.endswith("/oauth/access_token") and params.get("code"):
            return FakeResponse({"access_token": "short-user-token", "expires_in": 3600})
        if url.endswith("/oauth/access_token") and params.get("grant_type") == "fb_exchange_token":
            return FakeResponse({"access_token": "long-user-token", "expires_in": 5184000})
        if url.endswith("/me/accounts") or "me/accounts" in url:
            return FakeResponse({
                "data": [{
                    "id": "123456789012345",
                    "name": "TgFarm Page",
                    "access_token": "page-access-token",
                    "tasks": self.tasks,
                }]
            })
        raise AssertionError("unexpected Facebook OAuth GET " + url)


@pytest.mark.asyncio
async def test_facebook_oauth_exchanges_code_and_selects_page_with_create_content(monkeypatch):
    from tgfarm import facebook_oauth

    FakeFacebookOAuthClient.calls = []
    FakeFacebookOAuthClient.tasks = ["CREATE_CONTENT", "MODERATE"]
    monkeypatch.setattr(httpx, "AsyncClient", FakeFacebookOAuthClient)

    started = await facebook_oauth.begin_facebook_oauth(
        "app-id",
        "app-secret",
        "123456789012345",
        "https://farm.example/api/platforms/facebook/auth/callback",
    )
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert query["client_id"] == ["app-id"]
    assert set(query["scope"][0].split(",")) == {
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
    }

    result = await facebook_oauth.complete_facebook_oauth(query["state"][0], "auth-code")
    assert result["page_id"] == "123456789012345"
    assert result["page_name"] == "TgFarm Page"
    assert result["page_access_token"] == "page-access-token"
    assert result["user_access_token"] == "long-user-token"

    page_request = next(c for c in FakeFacebookOAuthClient.calls if c[1].endswith("/me/accounts"))
    assert page_request[2]["params"]["fields"] == "id,name,access_token,tasks"
    assert page_request[2]["params"]["access_token"] == "long-user-token"


@pytest.mark.asyncio
async def test_facebook_oauth_rejects_page_without_create_content(monkeypatch):
    from tgfarm import facebook_oauth

    FakeFacebookOAuthClient.calls = []
    FakeFacebookOAuthClient.tasks = ["ADVERTISE", "ANALYZE"]
    monkeypatch.setattr(httpx, "AsyncClient", FakeFacebookOAuthClient)

    started = await facebook_oauth.begin_facebook_oauth(
        "app-id",
        "app-secret",
        "123456789012345",
        "https://farm.example/api/platforms/facebook/auth/callback",
    )
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]

    with pytest.raises(RuntimeError, match="CREATE_CONTENT"):
        await facebook_oauth.complete_facebook_oauth(state, "auth-code")


def test_facebook_oauth_callback_requires_https_outside_loopback():
    from tgfarm.facebook_oauth import _validate_redirect_uri

    with pytest.raises(ValueError):
        _validate_redirect_uri("http://farm.example/api/platforms/facebook/auth/callback")
    assert _validate_redirect_uri(
        "http://127.0.0.1:8000/api/platforms/facebook/auth/callback"
    ).startswith("http://127.0.0.1")

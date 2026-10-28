from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("fake provider error")


class FakeXOAuthClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        data = kwargs.get("data") or {}
        if data.get("grant_type") == "authorization_code":
            return FakeResponse({
                "access_token": "private-access-token",
                "refresh_token": "private-refresh-token",
                "expires_in": 7200,
                "scope": "tweet.read users.read tweet.write offline.access",
            })
        if data.get("grant_type") == "refresh_token":
            return FakeResponse({
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 3600,
            })
        raise AssertionError("unexpected X token grant")

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({"data": {"id": "98765", "username": "tgfarm_demo", "name": "TgFarm Demo"}})


@pytest.mark.asyncio
async def test_x_oauth_uses_pkce_one_time_state_and_user_scopes(monkeypatch):
    from tgfarm import x_oauth

    FakeXOAuthClient.calls = []
    monkeypatch.setattr(x_oauth.httpx, "AsyncClient", FakeXOAuthClient)
    started = await x_oauth.begin_x_oauth(
        "test-client-id",
        "test-client-secret",
        "http://127.0.0.1:8765/api/platforms/x/auth/callback",
    )
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert urlsplit(started["authorize_url"]).hostname == "twitter.com"
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["tweet.read users.read tweet.write offline.access"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/api/platforms/x/auth/callback"]

    result = await x_oauth.complete_x_oauth(query["state"][0], "one-time-code")
    assert result["account"] == "@tgfarm_demo"
    assert result["user_id"] == "98765"
    assert result["refresh_token"] == "private-refresh-token"
    assert result["access_token"] == "private-access-token"

    token_call = next(call for call in FakeXOAuthClient.calls if call[1].endswith("/2/oauth2/token"))
    assert token_call[2]["auth"] == ("test-client-id", "test-client-secret")
    assert token_call[2]["data"]["code"] == "one-time-code"
    assert token_call[2]["data"]["grant_type"] == "authorization_code"
    verifier = token_call[2]["data"]["code_verifier"]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert query["code_challenge"] == [challenge]

    profile_call = next(call for call in FakeXOAuthClient.calls if call[0] == "GET")
    assert profile_call[1] == "https://api.x.com/2/users/me"
    assert profile_call[2]["headers"]["Authorization"] == "Bearer private-access-token"
    with pytest.raises(ValueError, match="истекла или уже использована"):
        await x_oauth.complete_x_oauth(query["state"][0], "replay-code")


@pytest.mark.asyncio
async def test_x_oauth_refresh_persists_rotated_tokens(monkeypatch):
    from tgfarm import x_oauth, platforms

    FakeXOAuthClient.calls = []
    monkeypatch.setattr(x_oauth.httpx, "AsyncClient", FakeXOAuthClient)
    monkeypatch.setattr(x_oauth.get_settings if hasattr(x_oauth, "get_settings") else platforms, "unused", lambda: None, raising=False)
    monkeypatch.setattr("tgfarm.core.settings.get_settings", lambda: SimpleNamespace(
        x_access_token="expired-token",
        x_refresh_token="refresh-before",
        x_oauth_client_id="client-id",
        x_oauth_client_secret="client-secret",
        x_token_expires_at=1,
    ))
    saved = []
    monkeypatch.setattr(platforms, "save_platform_credentials", lambda platform_id, values: saved.append((platform_id, values)) or {})
    token = await x_oauth.get_valid_x_access_token()
    assert token == "rotated-access-token"
    assert saved[0][0] == "x"
    assert saved[0][1]["X_ACCESS_TOKEN"] == "rotated-access-token"
    assert saved[0][1]["X_REFRESH_TOKEN"] == "rotated-refresh-token"
    assert int(saved[0][1]["X_TOKEN_EXPIRES_AT"]) > 1


def test_x_oauth_callback_does_not_echo_code_or_token(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm import platforms, x_oauth

    saved = []
    checked = []
    seen = {}

    async def complete(state, code):
        seen["state"] = state
        seen["code"] = code
        return {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "expires_at": "9999999999",
            "user_id": "98765",
            "username": "tgfarm_demo",
            "account": "@tgfarm_demo",
        }

    monkeypatch.setattr(x_oauth, "complete_x_oauth", complete)
    monkeypatch.setattr(platforms, "save_platform_credentials", lambda platform_id, values: saved.append((platform_id, values)) or {"status": "authorized"})
    monkeypatch.setattr(platforms, "record_auth_check", lambda *args, **kwargs: checked.append((args, kwargs)) or {"status": "authorized"})
    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/platforms/x/auth/callback?code=do-not-echo-code&state=valid-state")
    assert response.status_code == 200
    assert "@tgfarm_demo" in response.text
    assert "do-not-echo-code" not in response.text
    assert "private-access-token" not in response.text
    assert "client-secret" not in response.text
    assert seen == {"state": "valid-state", "code": "do-not-echo-code"}
    assert saved[0][1]["X_REFRESH_TOKEN"] == "private-refresh-token"
    assert checked[0][0] == ("x", True)


@pytest.mark.asyncio
async def test_x_adapter_posts_with_oauth_user_token(monkeypatch):
    import httpx
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth import adapters
    from tgfarm import x_oauth

    class PostResponse(FakeResponse):
        def __init__(self):
            super().__init__({"data": {"id": "123456789"}})

    class FakePostClient(FakeXOAuthClient):
        calls = []

        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return PostResponse()

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        x_access_token="old-access-token",
        x_refresh_token="refresh-token",
        x_user_id="98765",
    ))
    async def token():
        return "user-context-token"
    monkeypatch.setattr(x_oauth, "get_valid_x_access_token", token)
    monkeypatch.setattr(httpx, "AsyncClient", FakePostClient)
    result = await adapters.XAdapter().publish(CrosspostPayload(
        platform="x", title="Hello X", text="A useful post"
    ))
    assert result.status == "posted"
    assert result.remote_id == "123456789"
    assert result.url == "https://x.com/i/status/123456789"
    assert FakePostClient.calls[0][0] == "https://api.x.com/2/tweets"
    assert FakePostClient.calls[0][1]["headers"]["Authorization"] == "Bearer user-context-token"

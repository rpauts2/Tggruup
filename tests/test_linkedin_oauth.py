from __future__ import annotations

import base64
import hashlib
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeLinkedInOAuthClient:
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
                "access_token": "private-linkedin-access-token",
                "expires_in": 5184000,
                "refresh_token": "private-linkedin-refresh-token",
                "refresh_token_expires_in": 31536000,
            })
        if data.get("grant_type") == "refresh_token":
            return FakeResponse({
                "access_token": "rotated-linkedin-access-token",
                "expires_in": 5184000,
                "refresh_token": "rotated-linkedin-refresh-token",
                "refresh_token_expires_in": 31536000,
            })
        raise AssertionError("unexpected LinkedIn token grant")

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({"sub": "member-123", "name": "TgFarm Demo"})


@pytest.mark.asyncio
async def test_linkedin_oauth_state_scope_code_exchange_and_profile(monkeypatch):
    from tgfarm import linkedin_oauth

    FakeLinkedInOAuthClient.calls = []
    monkeypatch.setattr(linkedin_oauth.httpx, "AsyncClient", FakeLinkedInOAuthClient)
    redirect_uri = "http://127.0.0.1:8765/api/platforms/linkedin/auth/callback"

    started = await linkedin_oauth.begin_linkedin_oauth(
        "linkedin-client-id",
        "linkedin-client-secret",
        redirect_uri,
    )
    parsed = urlsplit(started["authorize_url"])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.hostname == "www.linkedin.com"
    assert parsed.path == "/oauth/native-pkce/authorization"
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid profile w_member_social"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["state"] and len(query["state"][0]) >= 32
    assert query["code_challenge_method"] == ["S256"]
    verifier = linkedin_oauth._PENDING[query["state"][0]]["code_verifier"]
    assert 43 <= len(verifier) <= 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    assert query["code_challenge"] == [challenge]

    credentials = await linkedin_oauth.complete_linkedin_oauth(
        query["state"][0], "one-time-code"
    )
    assert credentials["member_id"] == "member-123"
    assert credentials["member_name"] == "TgFarm Demo"
    assert credentials["account"] == "TgFarm Demo"
    assert credentials["access_token"] == "private-linkedin-access-token"
    assert credentials["refresh_token"] == "private-linkedin-refresh-token"
    assert int(credentials["expires_at"]) > int(time.time())
    assert int(credentials["refresh_token_expires_at"]) > int(time.time())

    exchange = FakeLinkedInOAuthClient.calls[0]
    assert exchange[0] == "POST"
    assert exchange[1] == "https://www.linkedin.com/oauth/v2/accessToken"
    assert exchange[2]["data"]["grant_type"] == "authorization_code"
    assert exchange[2]["data"]["client_id"] == "linkedin-client-id"
    assert "client_secret" not in exchange[2]["data"]
    assert exchange[2]["data"]["code_verifier"] == verifier
    assert exchange[2]["data"]["redirect_uri"] == redirect_uri

    profile = FakeLinkedInOAuthClient.calls[1]
    assert profile[1] == "https://api.linkedin.com/v2/userinfo"
    assert profile[2]["headers"]["Authorization"] == "Bearer private-linkedin-access-token"

    with pytest.raises(ValueError, match="истекла или уже использована"):
        await linkedin_oauth.complete_linkedin_oauth(query["state"][0], "replay-code")


@pytest.mark.asyncio
async def test_linkedin_https_callback_uses_registered_web_oauth_flow():
    from tgfarm import linkedin_oauth

    started = await linkedin_oauth.begin_linkedin_oauth(
        "linkedin-client-id", "linkedin-client-secret",
        "https://farm.example/api/platforms/linkedin/auth/callback",
    )
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert urlsplit(started["authorize_url"]).path == "/oauth/v2/authorization"
    assert "code_challenge" not in query
    assert started["flow"] == "web"
    linkedin_oauth.cancel_linkedin_oauth(query["state"][0])


@pytest.mark.asyncio
async def test_linkedin_refresh_persists_rotated_tokens(monkeypatch):
    from tgfarm import linkedin_oauth, platforms

    FakeLinkedInOAuthClient.calls = []
    monkeypatch.setattr(linkedin_oauth.httpx, "AsyncClient", FakeLinkedInOAuthClient)
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(
            linkedin_access_token="old-access-token",
            linkedin_refresh_token="old-refresh-token",
            linkedin_client_id="client-id",
            linkedin_client_secret="client-secret",
            linkedin_token_expires_at=int(time.time()) + 2 * 86400,
            linkedin_refresh_token_expires_at=int(time.time()) + 100 * 86400,
        ),
    )
    saved = []
    monkeypatch.setattr(
        platforms,
        "save_platform_credentials",
        lambda platform_id, values: saved.append((platform_id, values)) or {},
    )

    token = await linkedin_oauth.get_valid_linkedin_access_token()
    assert token == "rotated-linkedin-access-token"
    assert saved[0][0] == "linkedin"
    assert saved[0][1]["LINKEDIN_ACCESS_TOKEN"] == "rotated-linkedin-access-token"
    assert saved[0][1]["LINKEDIN_REFRESH_TOKEN"] == "rotated-linkedin-refresh-token"
    assert int(saved[0][1]["LINKEDIN_TOKEN_EXPIRES_AT"]) > int(time.time())
    refresh = next(call for call in FakeLinkedInOAuthClient.calls if call[2].get("data", {}).get("grant_type") == "refresh_token")
    assert refresh[1] == "https://www.linkedin.com/oauth/v2/accessToken"
    assert refresh[2]["data"]["refresh_token"] == "old-refresh-token"


def test_linkedin_oauth_callback_never_echoes_code_or_tokens(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm import linkedin_oauth, platforms
    from tgfarm.api import create_app

    saved = []
    checked = []
    seen = {}

    async def complete(state, code):
        seen["state"] = state
        seen["code"] = code
        return {
            "client_id": "private-client-id",
            "client_secret": "private-client-secret",
            "access_token": "private-linkedin-token",
            "refresh_token": "private-linkedin-refresh",
            "expires_at": "9999999999",
            "refresh_token_expires_at": "9999999999",
            "member_id": "member-123",
            "member_name": "TgFarm Demo",
            "account": "TgFarm Demo",
        }

    monkeypatch.setattr(linkedin_oauth, "complete_linkedin_oauth", complete)
    monkeypatch.setattr(
        platforms,
        "save_platform_credentials",
        lambda platform_id, values: saved.append((platform_id, values)) or {"status": "authorized"},
    )
    monkeypatch.setattr(
        platforms,
        "record_auth_check",
        lambda *args, **kwargs: checked.append((args, kwargs)) or {"status": "authorized"},
    )

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1:8765") as client:
        response = client.get(
            "/api/platforms/linkedin/auth/callback?code=do-not-echo-code&state=valid-state"
        )

    assert response.status_code == 200
    assert "TgFarm Demo" in response.text
    for secret in (
        "do-not-echo-code",
        "private-linkedin-token",
        "private-client-secret",
        "private-linkedin-refresh",
    ):
        assert secret not in response.text
    assert seen == {"state": "valid-state", "code": "do-not-echo-code"}
    assert saved[0][0] == "linkedin"
    assert saved[0][1]["LINKEDIN_ACCESS_TOKEN"] == "private-linkedin-token"
    assert saved[0][1]["LINKEDIN_REFRESH_TOKEN"] == "private-linkedin-refresh"
    assert checked[0][0] == ("linkedin", True)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest


def test_normalize_mastodon_instance_url_rejects_insecure_and_local_hosts():
    from tgfarm.mastodon_oauth import normalize_instance_url

    assert normalize_instance_url("mastodon.example") == "https://mastodon.example"
    assert normalize_instance_url("https://mastodon.example/") == "https://mastodon.example"
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_instance_url("http://mastodon.example")
    with pytest.raises(ValueError, match="публичным адресом"):
        normalize_instance_url("https://127.0.0.1")
    with pytest.raises(ValueError):
        normalize_instance_url("https://mastodon.example/unexpected/path")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class OAuthFakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/api/v1/apps"):
            return FakeResponse({"client_id": "client-id", "client_secret": "client-secret"})
        if url.endswith("/oauth/token"):
            return FakeResponse({"access_token": "access-secret", "scope": "read write:statuses"})
        raise AssertionError("unexpected POST URL")

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        assert url.endswith("/api/v1/accounts/verify_credentials")
        return FakeResponse({"acct": "demo", "username": "demo"})


@pytest.mark.asyncio
async def test_mastodon_oauth_uses_pkce_state_and_one_time_authorization_code(monkeypatch):
    from tgfarm import mastodon_oauth

    OAuthFakeClient.calls = []
    monkeypatch.setattr(mastodon_oauth.httpx, "AsyncClient", OAuthFakeClient)
    redirect_uri = "http://127.0.0.1:8000/api/platforms/mastodon/auth/callback"
    started = await mastodon_oauth.begin_mastodon_oauth("mastodon.example", redirect_uri)
    parsed = urlsplit(started["authorize_url"])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.path == "/oauth/authorize"
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["read write:statuses"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32

    app_call = OAuthFakeClient.calls[0]
    assert app_call[1].endswith("/api/v1/apps")
    assert app_call[2]["data"]["scopes"] == "read write:statuses"
    result = await mastodon_oauth.complete_mastodon_oauth(query["state"][0], "one-time-code")
    assert result["account"] == "@demo@mastodon.example"
    assert result["access_token"] == "access-secret"
    token_call = next(row for row in OAuthFakeClient.calls if row[1].endswith("/oauth/token"))
    token_data = token_call[2]["data"]
    assert token_data["code"] == "one-time-code"
    assert token_data["redirect_uri"] == redirect_uri
    assert hashlib.sha256(token_data["code_verifier"].encode("ascii")).digest()
    assert "code_challenge" in query
    import base64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(token_data["code_verifier"].encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert query["code_challenge"] == [challenge]
    with pytest.raises(ValueError, match="истекла или уже использована"):
        await mastodon_oauth.complete_mastodon_oauth(query["state"][0], "replay-code")


@pytest.mark.asyncio
async def test_mastodon_oauth_rejects_unknown_state(monkeypatch):
    from tgfarm import mastodon_oauth

    with pytest.raises(ValueError, match="истекла или уже использована"):
        await mastodon_oauth.complete_mastodon_oauth("not-a-real-state", "code")


def test_mastodon_oauth_callback_saves_secrets_without_echoing_them(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm import mastodon_oauth, platforms

    saved = []
    checked = []
    seen = {}

    async def complete(state, code):
        seen["state"] = state
        seen["code"] = code
        return {
            "instance": "https://mastodon.example",
            "client_id": "client-id-secret",
            "client_secret": "client-secret",
            "access_token": "do-not-echo-token",
            "account": "@demo@mastodon.example",
        }

    monkeypatch.setattr(mastodon_oauth, "complete_mastodon_oauth", complete)
    monkeypatch.setattr(platforms, "save_platform_credentials", lambda platform_id, values: saved.append((platform_id, values)) or {"status": "authorized"})
    monkeypatch.setattr(platforms, "record_auth_check", lambda *args, **kwargs: checked.append((args, kwargs)) or {"status": "authorized"})

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1:8000") as client:
        response = client.get(
            "/api/platforms/mastodon/auth/callback?code=do-not-echo-code&state=valid-state"
        )

    assert response.status_code == 200
    assert "@demo@mastodon.example" in response.text
    assert "do-not-echo-code" not in response.text
    assert "do-not-echo-token" not in response.text
    assert "client-secret" not in response.text
    assert seen == {"state": "valid-state", "code": "do-not-echo-code"}
    assert saved[0][0] == "mastodon"
    assert saved[0][1]["MASTODON_ACCESS_TOKEN"] == "do-not-echo-token"
    assert checked[0][0] == ("mastodon", True)

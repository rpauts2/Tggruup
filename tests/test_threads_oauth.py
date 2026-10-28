from __future__ import annotations

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeThreadsOAuthClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        params = kwargs.get("params") or {}
        assert params.get("grant_type") == "authorization_code"
        return FakeResponse({"access_token": "short-lived-token", "user_id": "threads-user-123"})

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/access_token"):
            return FakeResponse({
                "access_token": "long-lived-private-token",
                "token_type": "bearer",
                "expires_in": 5184000,
            })
        if url.endswith("/v1.0/me"):
            return FakeResponse({"id": "threads-user-123", "username": "tgfarm_demo"})
        if url.endswith("/refresh_access_token"):
            return FakeResponse({
                "access_token": "rotated-long-lived-token",
                "expires_in": 5184000,
            })
        raise AssertionError("unexpected Threads GET URL: " + url)


@pytest.mark.asyncio
async def test_threads_oauth_uses_state_and_official_token_exchange(monkeypatch):
    from tgfarm import threads_oauth

    FakeThreadsOAuthClient.calls = []
    monkeypatch.setattr(threads_oauth.httpx, "AsyncClient", FakeThreadsOAuthClient)
    redirect_uri = "http://127.0.0.1:8765/api/platforms/threads/auth/callback"

    started = await threads_oauth.begin_threads_oauth(
        "threads-app-id", "threads-app-secret", redirect_uri
    )
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert urlsplit(started["authorize_url"]).scheme == "https"
    assert urlsplit(started["authorize_url"]).hostname == "threads.net"
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["threads_basic,threads_content_publish"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["state"] and len(query["state"][0]) >= 32

    credentials = await threads_oauth.complete_threads_oauth(
        query["state"][0], "one-time-code"
    )
    assert credentials["account"] == "@tgfarm_demo"
    assert credentials["user_id"] == "threads-user-123"
    assert credentials["access_token"] == "long-lived-private-token"
    assert int(credentials["expires_at"]) > int(time.time())

    exchange = FakeThreadsOAuthClient.calls[0]
    assert exchange[0] == "POST"
    assert exchange[1] == "https://graph.threads.net/oauth/access_token"
    assert exchange[2]["params"]["client_id"] == "threads-app-id"
    assert exchange[2]["params"]["client_secret"] == "threads-app-secret"
    assert exchange[2]["params"]["redirect_uri"] == redirect_uri

    long_lived = FakeThreadsOAuthClient.calls[1]
    assert long_lived[1] == "https://graph.threads.net/access_token"
    assert long_lived[2]["params"]["grant_type"] == "th_exchange_token"
    assert long_lived[2]["params"]["access_token"] == "short-lived-token"

    profile = FakeThreadsOAuthClient.calls[2]
    assert profile[1] == "https://graph.threads.net/v1.0/me"
    assert profile[2]["params"]["fields"] == "id,username"
    assert profile[2]["params"]["access_token"] == "long-lived-private-token"

    with pytest.raises(ValueError, match="истекла или уже использована"):
        await threads_oauth.complete_threads_oauth(query["state"][0], "replay-code")


@pytest.mark.asyncio
async def test_threads_token_refresh_persists_rotated_token(monkeypatch):
    from tgfarm import platforms, threads_oauth

    FakeThreadsOAuthClient.calls = []
    monkeypatch.setattr(threads_oauth.httpx, "AsyncClient", FakeThreadsOAuthClient)
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(
            threads_access_token="old-token",
            threads_token_expires_at=int(time.time()) + 2 * 86400,
            threads_user_id="threads-user-123",
        ),
    )
    saved = []
    monkeypatch.setattr(
        platforms,
        "save_platform_credentials",
        lambda platform_id, values: saved.append((platform_id, values)) or {},
    )

    token = await threads_oauth.get_valid_threads_access_token()
    assert token == "rotated-long-lived-token"
    assert saved[0][0] == "threads"
    assert saved[0][1]["THREADS_ACCESS_TOKEN"] == "rotated-long-lived-token"
    assert int(saved[0][1]["THREADS_TOKEN_EXPIRES_AT"]) > int(time.time())
    refresh = next(call for call in FakeThreadsOAuthClient.calls if call[1].endswith("/refresh_access_token"))
    assert refresh[2]["params"]["grant_type"] == "th_refresh_token"
    assert refresh[2]["params"]["access_token"] == "old-token"


def test_threads_oauth_callback_does_not_echo_code_or_tokens(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm import platforms, threads_oauth
    from tgfarm.api import create_app

    saved = []
    checked = []
    seen = {}

    async def complete(state, code):
        seen["state"] = state
        seen["code"] = code
        return {
            "client_id": "private-app-id",
            "client_secret": "private-app-secret",
            "access_token": "private-long-lived-token",
            "expires_at": "9999999999",
            "user_id": "threads-user-123",
            "username": "tgfarm_demo",
            "account": "@tgfarm_demo",
        }

    monkeypatch.setattr(threads_oauth, "complete_threads_oauth", complete)
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
            "/api/platforms/threads/auth/callback?code=do-not-echo-code&state=valid-state"
        )

    assert response.status_code == 200
    assert "@tgfarm_demo" in response.text
    for secret in (
        "do-not-echo-code",
        "private-long-lived-token",
        "private-app-secret",
    ):
        assert secret not in response.text
    assert seen == {"state": "valid-state", "code": "do-not-echo-code"}
    assert saved[0][0] == "threads"
    assert saved[0][1]["THREADS_ACCESS_TOKEN"] == "private-long-lived-token"
    assert checked[0][0] == ("threads", True)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"

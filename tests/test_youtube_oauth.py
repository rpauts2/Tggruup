from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._payload


class FakeGoogleOAuthClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        data = kwargs.get("data") or {}
        self.calls.append(("POST", url, kwargs))
        if data.get("grant_type") == "authorization_code":
            return FakeResponse({
                "access_token": "private-youtube-access",
                "refresh_token": "private-youtube-refresh",
                "expires_in": 3600,
            })
        if data.get("grant_type") == "refresh_token":
            return FakeResponse({
                "access_token": "rotated-youtube-access",
                "expires_in": 3600,
            })
        raise AssertionError("unexpected Google token grant")

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({
            "items": [{
                "id": "UC-demo-channel",
                "snippet": {"title": "TgFarm Demo Channel"},
            }]
        })


@pytest.mark.asyncio
async def test_youtube_oauth_uses_state_offline_access_and_verifies_channel(monkeypatch):
    from tgfarm import youtube_oauth

    FakeGoogleOAuthClient.calls = []
    monkeypatch.setattr(youtube_oauth.httpx, "AsyncClient", FakeGoogleOAuthClient)
    redirect_uri = "http://127.0.0.1:8000/api/platforms/youtube/auth/callback"

    started = await youtube_oauth.begin_youtube_oauth(
        "google-client-id",
        "google-client-secret",
        redirect_uri,
    )
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert urlsplit(started["authorize_url"]).hostname == "accounts.google.com"
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    pending = youtube_oauth._PENDING[query["state"][0]]
    verifier = pending["code_verifier"]
    assert 43 <= len(verifier) <= 128
    import base64
    import hashlib
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    assert challenge == query["code_challenge"][0]
    scopes = set(query["scope"][0].split())
    assert scopes == set(youtube_oauth.SCOPES)
    assert "youtube.upload" in query["scope"][0]
    assert started["expires_in_s"] == youtube_oauth.STATE_TTL_SECONDS

    result = await youtube_oauth.complete_youtube_oauth(query["state"][0], "single-use-code")
    assert result["account"] == "TgFarm Demo Channel"
    assert result["channel_id"] == "UC-demo-channel"
    assert result["refresh_token"] == "private-youtube-refresh"
    assert result["access_token"] == "private-youtube-access"

    exchange = next(call for call in FakeGoogleOAuthClient.calls if call[0] == "POST")
    assert exchange[1] == youtube_oauth.TOKEN_URL
    assert exchange[2]["data"]["code"] == "single-use-code"
    assert exchange[2]["data"]["redirect_uri"] == redirect_uri
    assert exchange[2]["data"]["code_verifier"] == verifier
    channel_call = next(call for call in FakeGoogleOAuthClient.calls if call[0] == "GET")
    assert channel_call[1] == youtube_oauth.CHANNELS_URL
    assert channel_call[2]["params"] == {"part": "snippet", "mine": "true"}
    assert channel_call[2]["headers"]["Authorization"] == "Bearer private-youtube-access"

    with pytest.raises(ValueError, match="истекла или уже использована"):
        await youtube_oauth.complete_youtube_oauth(query["state"][0], "replay-code")


@pytest.mark.asyncio
async def test_youtube_oauth_does_not_reuse_a_previous_account_refresh_token(monkeypatch):
    from tgfarm import youtube_oauth
    import tgfarm.core.settings as settings_module

    async def exchange(_data):
        return {"access_token": "new-account-access", "expires_in": 3600}

    async def verify(_access_token):
        raise AssertionError("channel verification must not run without this OAuth grant's refresh token")

    monkeypatch.setattr(youtube_oauth, "_exchange_token", exchange)
    monkeypatch.setattr(youtube_oauth, "_verify_channel", verify)
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(youtube_refresh_token="previous-account-refresh"),
    )
    started = await youtube_oauth.begin_youtube_oauth(
        "google-client-id",
        "google-client-secret",
        "http://127.0.0.1:8000/api/platforms/youtube/auth/callback",
    )
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]

    with pytest.raises(RuntimeError, match="Старый токен не переиспользован"):
        await youtube_oauth.complete_youtube_oauth(state, "new-account-code")

    assert state not in youtube_oauth._PENDING


@pytest.mark.asyncio
async def test_youtube_oauth_rejects_untrusted_redirect_uris():
    from tgfarm import youtube_oauth

    with pytest.raises(ValueError, match="Callback URI"):
        await youtube_oauth.begin_youtube_oauth(
            "client-id",
            "client-secret",
            "http://evil.example/callback",
        )

    with pytest.raises(ValueError, match="Callback URI"):
        await youtube_oauth.begin_youtube_oauth(
            "client-id",
            "client-secret",
            "https://user:password@example.com/callback",
        )


@pytest.mark.asyncio
async def test_youtube_token_refresh_rotates_access_token_and_persists_it(monkeypatch):
    from tgfarm import platforms, youtube_oauth
    import tgfarm.core.settings as settings_module

    FakeGoogleOAuthClient.calls = []
    monkeypatch.setattr(youtube_oauth.httpx, "AsyncClient", FakeGoogleOAuthClient)
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace(
        youtube_access_token="expired-token",
        youtube_refresh_token="refresh-before",
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
        youtube_token_expires_at="1",
    ))
    saved = []
    monkeypatch.setattr(
        platforms,
        "save_platform_credentials",
        lambda platform_id, values: saved.append((platform_id, values)) or {},
    )

    token = await youtube_oauth.get_valid_youtube_access_token()

    assert token == "rotated-youtube-access"
    assert saved[0][0] == "youtube"
    assert saved[0][1]["YOUTUBE_ACCESS_TOKEN"] == "rotated-youtube-access"
    assert saved[0][1]["YOUTUBE_REFRESH_TOKEN"] == "refresh-before"
    assert int(saved[0][1]["YOUTUBE_TOKEN_EXPIRES_AT"]) > 1
    assert "private-youtube-refresh" not in repr(saved)


def test_youtube_oauth_callback_persists_tokens_without_echoing_secrets(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm import platforms, youtube_oauth
    from tgfarm.api import create_app

    saved = []
    checked = []
    seen = {}

    async def complete(state, code):
        seen.update(state=state, code=code)
        return {
            "client_id": "google-client",
            "client_secret": "google-secret",
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "expires_at": "9999999999",
            "channel_id": "UC-demo-channel",
            "channel_title": "TgFarm Demo Channel",
            "account": "TgFarm Demo Channel",
        }

    monkeypatch.setattr(youtube_oauth, "complete_youtube_oauth", complete)
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
    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1:8000") as client:
        # Desktop Google OAuth redirects to the loopback origin, not an
        # application callback path. The middleware captures and strips the
        # credentials before the dashboard route dispatches to the handler.
        response = client.get(
            "/?code=private-one-time-code&state=valid-state"
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert "TgFarm Demo Channel" in response.text
    assert "private-one-time-code" not in response.text
    assert "private-access-token" not in response.text
    assert "private-refresh-token" not in response.text
    assert "google-secret" not in response.text
    assert seen == {"state": "valid-state", "code": "private-one-time-code"}
    assert saved[0][0] == "youtube"
    assert saved[0][1]["YOUTUBE_CHANNEL_ID"] == "UC-demo-channel"
    assert saved[0][1]["YOUTUBE_REFRESH_TOKEN"] == "private-refresh-token"
    assert checked[0][0] == ("youtube", True)


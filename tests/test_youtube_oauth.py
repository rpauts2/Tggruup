from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeYouTubeOAuthClient:
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
            "access_token": "access-private",
            "refresh_token": "refresh-private",
        })

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({
            "items": [{"id": "UC_demo_123", "snippet": {"title": "Demo Channel"}}]
        })


@pytest.mark.asyncio
async def test_youtube_oauth_uses_state_offline_access_and_verified_channel(monkeypatch):
    from tgfarm import youtube_oauth

    youtube_oauth._PENDING.clear()
    FakeYouTubeOAuthClient.calls = []
    monkeypatch.setattr(youtube_oauth.httpx, "AsyncClient", FakeYouTubeOAuthClient)
    redirect = "http://127.0.0.1:8765/api/platforms/youtube/auth/callback"

    started = await youtube_oauth.begin_youtube_oauth(
        "google-client-id", "google-client-secret", redirect
    )
    parsed = urlsplit(started["authorize_url"])
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "accounts.google.com"
    assert query["client_id"] == ["google-client-id"]
    assert query["redirect_uri"] == [redirect]
    assert query["response_type"] == ["code"]
    assert "youtube.upload" in query["scope"][0]
    assert query["access_type"] == ["offline"]
    assert query["state"] and len(query["state"][0]) >= 32

    credentials = await youtube_oauth.complete_youtube_oauth(
        query["state"][0], "one-time-code"
    )
    assert credentials == {
        "client_id": "google-client-id",
        "client_secret": "google-client-secret",
        "refresh_token": "refresh-private",
        "channel_id": "UC_demo_123",
        "channel_title": "Demo Channel",
        "account": "Demo Channel",
    }
    token_request = FakeYouTubeOAuthClient.calls[0]
    assert token_request[0] == "POST"
    assert token_request[1] == youtube_oauth.TOKEN_URL
    assert token_request[2]["data"]["grant_type"] == "authorization_code"
    channel_request = FakeYouTubeOAuthClient.calls[1]
    assert channel_request[1] == youtube_oauth.CHANNELS_URL
    assert channel_request[2]["params"] == {"part": "id,snippet", "mine": "true"}
    assert "access-private" not in repr(credentials)
    with pytest.raises(ValueError, match="истекла или уже использована"):
        await youtube_oauth.complete_youtube_oauth(query["state"][0], "replay-code")


@pytest.mark.parametrize(
    "redirect",
    [
        "http://example.com/oauth/callback",
        "https://trusted.example/oauth/callback?code=leak",
        "https://user:password@trusted.example/oauth/callback",
        "javascript://trusted.example/oauth",
    ],
)
def test_youtube_oauth_rejects_untrusted_redirect_urls(redirect):
    from tgfarm.youtube_oauth import _validate_redirect_uri

    with pytest.raises(ValueError):
        _validate_redirect_uri(redirect)


@pytest.mark.asyncio
async def test_youtube_oauth_does_not_accept_code_without_state():
    from tgfarm import youtube_oauth

    with pytest.raises(ValueError, match="неполный"):
        await youtube_oauth.complete_youtube_oauth("", "valid-code")

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


class FakeTikTokOAuthClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/oauth/token/"):
            return FakeResponse({
                "data": {
                    "access_token": "tiktok-access-private",
                    "refresh_token": "tiktok-refresh-private",
                    "open_id": "open-user-123",
                    "expires_in": 86400,
                    "refresh_expires_in": 7776000,
                }
            })
        if url.endswith("/creator_info/query/"):
            return FakeResponse({
                "error": {"code": "ok"},
                "data": {"creator_username": "demo_creator"},
            })
        raise AssertionError("unexpected TikTok OAuth URL: " + url)


@pytest.mark.asyncio
async def test_tiktok_oauth_uses_state_and_verifies_creator_publish_scope(monkeypatch):
    from tgfarm import tiktok_oauth

    with tiktok_oauth._PENDING_LOCK:
        tiktok_oauth._PENDING.clear()
    FakeTikTokOAuthClient.calls = []
    monkeypatch.setattr(tiktok_oauth.httpx, "AsyncClient", FakeTikTokOAuthClient)
    redirect = "http://127.0.0.1:8765/api/platforms/tiktok/auth/callback"

    started = await tiktok_oauth.begin_tiktok_oauth(
        "tiktok-client-key", "tiktok-client-secret", redirect
    )
    parsed = urlsplit(started["authorize_url"])
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "www.tiktok.com"
    assert query["client_key"] == ["tiktok-client-key"]
    assert query["redirect_uri"] == [redirect]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["user.info.basic,video.publish"]
    assert query["state"] and len(query["state"][0]) >= 32
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 64
    assert all(char in "0123456789abcdef" for char in query["code_challenge"][0])
    pending = tiktok_oauth._PENDING[query["state"][0]]
    verifier = pending["code_verifier"]
    assert 43 <= len(verifier) <= 128
    import hashlib
    assert hashlib.sha256(verifier.encode("ascii")).hexdigest() == query["code_challenge"][0]

    credentials = await tiktok_oauth.complete_tiktok_oauth(
        query["state"][0], "one-time-code"
    )
    assert credentials["account"] == "@demo_creator"
    assert credentials["open_id"] == "open-user-123"
    assert credentials["refresh_token"] == "tiktok-refresh-private"
    assert int(credentials["token_expires_at"]) > 0
    assert int(credentials["refresh_token_expires_at"]) > int(credentials["token_expires_at"])
    assert "tiktok-access-private" not in repr({
        key: value for key, value in credentials.items() if key != "access_token"
    })
    assert FakeTikTokOAuthClient.calls[0][1] == tiktok_oauth.TOKEN_URL
    assert FakeTikTokOAuthClient.calls[0][2]["data"]["code_verifier"] == verifier
    creator_call = FakeTikTokOAuthClient.calls[1]
    assert creator_call[1] == tiktok_oauth.CREATOR_INFO_URL
    assert creator_call[2]["headers"]["Authorization"] == "Bearer tiktok-access-private"

    with pytest.raises(ValueError, match="истекла или уже использована"):
        await tiktok_oauth.complete_tiktok_oauth(query["state"][0], "replay-code")


@pytest.mark.parametrize(
    "redirect",
    [
        "http://public.example/oauth/callback",
        "https://trusted.example/oauth/callback?state=leak",
        "https://user:password@trusted.example/oauth/callback",
        "javascript://trusted.example/oauth",
    ],
)
def test_tiktok_oauth_rejects_untrusted_redirect_urls(redirect):
    from tgfarm.tiktok_oauth import _validate_redirect_uri

    with pytest.raises(ValueError):
        _validate_redirect_uri(redirect)

from __future__ import annotations
from urllib.parse import parse_qs, urlparse
import pytest

class FakeResponse:
    status_code = 200
    def __init__(self, payload): self._payload = payload
    def json(self): return self._payload

class FakeClient:
    calls = []
    def __init__(self, *args, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/oauth/access_token/"):
            return FakeResponse({"data":{"error_code":0,"access_token":"access-private","refresh_token":"refresh-private","open_id":"open-123","expires_in":1296000,"refresh_expires_in":2592000,"scope":"user_info,video.create"}})
        if url.endswith("/oauth/userinfo/"):
            return FakeResponse({"data":{"error_code":"0","open_id":"open-123","nickname":"Demo Creator"}})
        raise AssertionError(url)

def test_douyin_redirect_validation():
    from tgfarm.douyin_oauth import _validate_redirect_uri
    assert _validate_redirect_uri("http://127.0.0.1:8000/api/platforms/douyin/auth/callback")
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_redirect_uri("http://farm.example/api/platforms/douyin/auth/callback")

@pytest.mark.asyncio
async def test_douyin_oauth_uses_one_time_state_and_current_scopes(monkeypatch):
    import httpx
    from tgfarm import douyin_oauth
    FakeClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    begun = await douyin_oauth.begin_douyin_oauth("client","secret","https://farm.example/api/platforms/douyin/auth/callback")
    query = parse_qs(urlparse(str(begun["authorize_url"])).query)
    assert query["scope"] == ["user_info,video.create"]
    state = query["state"][0]
    credentials = await douyin_oauth.complete_douyin_oauth(state,"one-use-code")
    assert credentials["open_id"] == "open-123"
    assert credentials["account"] == "Demo Creator"
    assert FakeClient.calls[0][1]["data"]["grant_type"] == "authorization_code"
    assert FakeClient.calls[1][1]["headers"]["access-token"] == "access-private"
    with pytest.raises(ValueError, match="истекла или уже использована"):
        await douyin_oauth.complete_douyin_oauth(state,"one-use-code")

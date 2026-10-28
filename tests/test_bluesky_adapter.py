from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            request = httpx.Request("POST", "https://bsky.social/xrpc/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("provider error", request=request, response=response)


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("createSession"):
            return FakeResponse({"accessJwt": "not-returned", "did": "did:plc:abc", "handle": "demo.bsky.social"})
        return FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/xyz"})


@pytest.mark.asyncio
async def test_bluesky_adapter_creates_session_and_native_post(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        bluesky_handle="demo.bsky.social", bluesky_app_password="app-password"
    ))
    monkeypatch.setattr(adapters.httpx, "AsyncClient", FakeClient, raising=False) if hasattr(adapters, "httpx") else None
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await adapters.BlueskyAdapter().publish(CrosspostPayload(
        platform="bluesky", title="Hello Bluesky", text="A useful short post"
    ))

    assert result.status == "posted"
    assert result.remote_id == "at://did:plc:abc/app.bsky.feed.post/xyz"
    assert result.url == "https://bsky.app/profile/demo.bsky.social/post/xyz"
    assert len(FakeClient.calls) == 2
    create_url, create_kwargs = FakeClient.calls[0]
    assert create_url.endswith("com.atproto.server.createSession")
    assert create_kwargs["json"]["identifier"] == "demo.bsky.social"
    post_url, post_kwargs = FakeClient.calls[1]
    assert post_url.endswith("com.atproto.repo.createRecord")
    assert post_kwargs["json"]["collection"] == "app.bsky.feed.post"
    assert "Hello Bluesky" in post_kwargs["json"]["record"]["text"]
    assert "accessJwt" not in repr(result)


@pytest.mark.asyncio
async def test_bluesky_adapter_skips_without_credentials(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        bluesky_handle="", bluesky_app_password=""
    ))
    result = await adapters.BlueskyAdapter().publish(CrosspostPayload(
        platform="bluesky", title="Hello", text="Body"
    ))
    assert result.status == "skipped"

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://graph.instagram.com/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("provider rejected request", request=request, response=response)


class FakeInstagramClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/media"):
            return FakeResponse({"id": "container-123"})
        if url.endswith("/media_publish"):
            return FakeResponse({"id": "media-789"})
        raise AssertionError("unexpected Instagram POST " + url)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/container-123"):
            return FakeResponse({"status_code": "FINISHED"})
        if url.endswith("/media-789"):
            return FakeResponse({"id": "media-789", "permalink": "https://www.instagram.com/p/abc123/"})
        raise AssertionError("unexpected Instagram GET " + url)


def _settings():
    return SimpleNamespace(
        instagram_access_token="saved-token",
        instagram_user_id="ig-user-1",
        public_base_url="https://farm.example",
    )


@pytest.mark.asyncio
async def test_instagram_adapter_publishes_image_container(monkeypatch):
    from tgfarm import instagram_oauth
    from tgfarm.s9_growth import instagram

    FakeInstagramClient.calls = []
    monkeypatch.setattr(instagram, "get_settings", _settings)

    async def get_token():
        return "access-token"

    monkeypatch.setattr(instagram_oauth, "get_valid_instagram_access_token", get_token)
    monkeypatch.setattr(instagram, "_ensure_jpeg", lambda path: path)
    monkeypatch.setattr(instagram, "_signed_media_url", lambda path, post_id, kind: "https://farm.example/signed.jpg")
    monkeypatch.setattr(httpx, "AsyncClient", FakeInstagramClient)

    result = await instagram.InstagramAdapter().publish(CrosspostPayload(
        platform="instagram",
        title="A useful title",
        text="A short factual caption",
        image_path="/media/cover.png",
        meta={"post_public_id": "post-1"},
    ))

    assert result.status == "posted"
    assert result.remote_id == "media-789"
    assert result.url == "https://www.instagram.com/p/abc123/"
    create = next(call for call in FakeInstagramClient.calls if call[0] == "POST" and call[1].endswith("/media"))
    assert create[2]["data"]["image_url"] == "https://farm.example/signed.jpg"
    assert create[2]["data"]["caption"] == "A useful title\n\nA short factual caption"
    assert "media_type" not in create[2]["data"]
    publish = next(call for call in FakeInstagramClient.calls if call[0] == "POST" and call[1].endswith("/media_publish"))
    assert publish[2]["data"]["creation_id"] == "container-123"


@pytest.mark.asyncio
async def test_instagram_adapter_publishes_reel_only_after_processing_finishes(monkeypatch):
    from tgfarm import instagram_oauth
    from tgfarm.s9_growth import instagram

    FakeInstagramClient.calls = []
    monkeypatch.setattr(instagram, "get_settings", _settings)

    async def get_token():
        return "access-token"

    monkeypatch.setattr(instagram_oauth, "get_valid_instagram_access_token", get_token)
    monkeypatch.setattr(instagram, "_signed_media_url", lambda path, post_id, kind: "https://farm.example/signed.mp4")
    monkeypatch.setattr(instagram, "POLL_ATTEMPTS", 1)
    monkeypatch.setattr(httpx, "AsyncClient", FakeInstagramClient)

    result = await instagram.InstagramAdapter().publish(CrosspostPayload(
        platform="instagram",
        title="A useful title",
        text="A Reel caption",
        video_path="/media/video.mp4",
        meta={"post_public_id": "post-2"},
    ))

    assert result.status == "posted"
    assert result.remote_id == "media-789"
    create = next(call for call in FakeInstagramClient.calls if call[0] == "POST" and call[1].endswith("/media"))
    assert create[2]["data"]["media_type"] == "REELS"
    assert create[2]["data"]["video_url"] == "https://farm.example/signed.mp4"
    assert create[2]["data"]["share_to_feed"] == "true"
    status_index = next(i for i, call in enumerate(FakeInstagramClient.calls) if call[0] == "GET" and call[1].endswith("/container-123"))
    publish_index = next(i for i, call in enumerate(FakeInstagramClient.calls) if call[0] == "POST" and call[1].endswith("/media_publish"))
    assert status_index < publish_index


def test_instagram_adapter_fails_closed_without_public_https_origin(monkeypatch):
    from tgfarm.s9_growth.instagram import InstagramAdapter

    monkeypatch.setattr(
        "tgfarm.s9_growth.instagram.get_settings",
        lambda: SimpleNamespace(
            instagram_access_token="saved-token",
            instagram_user_id="ig-user-1",
            public_base_url="http://localhost:8000",
        ),
    )
    assert InstagramAdapter().configured() is False

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
            request = httpx.Request("POST", "https://graph.facebook.com/v26.0/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Graph API rejected request", request=request, response=response)


class FakeFacebookClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/feed"):
            return FakeResponse({"id": "page_123"})
        if url.endswith("/photos"):
            return FakeResponse({"id": "photo-123", "post_id": "page_456"})
        if url.endswith("/videos"):
            return FakeResponse({"id": "video-123"})
        raise AssertionError("unexpected Facebook POST " + url)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/video-123"):
            return FakeResponse({"id": "video-123", "status": {"video_status": "ready"}})
        raise AssertionError("unexpected Facebook GET " + url)


def _settings():
    return SimpleNamespace(
        facebook_page_id="123456789012345",
        facebook_page_access_token="saved-page-token",
    )


@pytest.mark.asyncio
async def test_facebook_adapter_publishes_page_feed_post(monkeypatch):
    from tgfarm import facebook_oauth
    from tgfarm.s9_growth import facebook

    FakeFacebookClient.calls = []
    monkeypatch.setattr(facebook, "get_settings", _settings)

    async def get_token():
        return "page-access-token"

    monkeypatch.setattr(facebook_oauth, "get_valid_facebook_page_token", get_token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeFacebookClient)

    result = await facebook.FacebookPageAdapter().publish(CrosspostPayload(
        platform="facebook",
        title="Page update",
        text="A useful factual summary.",
        link="https://example.test/source",
    ))

    assert result.status == "posted"
    assert result.remote_id == "page_123"
    assert result.url == "https://www.facebook.com/page_123"
    url, kwargs = FakeFacebookClient.calls[0]
    assert url == "https://graph.facebook.com/v26.0/123456789012345/feed"
    assert kwargs["headers"]["Authorization"] == "Bearer page-access-token"
    assert kwargs["data"] == {
        "message": "Page update\n\nA useful factual summary.",
        "link": "https://example.test/source",
    }


@pytest.mark.asyncio
async def test_facebook_adapter_publishes_photo_from_signed_media_url(monkeypatch):
    from tgfarm import facebook_oauth
    from tgfarm.s9_growth import facebook

    FakeFacebookClient.calls = []
    monkeypatch.setattr(facebook, "get_settings", _settings)

    async def get_token():
        return "page-access-token"

    async def signed_url(path, post_public_id, kind):
        return "https://farm.example/signed-photo.jpg"

    monkeypatch.setattr(facebook_oauth, "get_valid_facebook_page_token", get_token)
    monkeypatch.setattr(facebook.FacebookPageAdapter, "_prepare_image_url", lambda self, *args: signed_url(*args))
    monkeypatch.setattr(httpx, "AsyncClient", FakeFacebookClient)

    result = await facebook.FacebookPageAdapter().publish(CrosspostPayload(
        platform="facebook",
        title="Photo update",
        text="A useful caption",
        image_path="/media/cover.png",
        meta={"post_public_id": "post-1"},
    ))

    assert result.status == "posted"
    assert result.remote_id == "page_456"
    assert result.url == "https://www.facebook.com/page_456"
    url, kwargs = FakeFacebookClient.calls[0]
    assert url.endswith("/123456789012345/photos")
    assert kwargs["data"]["url"] == "https://farm.example/signed-photo.jpg"
    assert kwargs["data"]["caption"] == "Photo update\n\nA useful caption"
    assert kwargs["data"]["published"] == "true"


@pytest.mark.asyncio
async def test_facebook_adapter_publishes_video_only_after_graph_reports_ready(monkeypatch):
    from tgfarm import facebook_oauth
    from tgfarm.s9_growth import facebook

    FakeFacebookClient.calls = []
    monkeypatch.setattr(facebook, "get_settings", _settings)

    async def get_token():
        return "page-access-token"

    async def signed_url(path, post_public_id, kind):
        return "https://farm.example/signed-video.mp4"

    monkeypatch.setattr(facebook_oauth, "get_valid_facebook_page_token", get_token)
    monkeypatch.setattr(facebook.FacebookPageAdapter, "_prepare_video_url", lambda self, *args: signed_url(*args))
    monkeypatch.setattr(facebook, "POLL_ATTEMPTS", 1)
    monkeypatch.setattr(httpx, "AsyncClient", FakeFacebookClient)

    result = await facebook.FacebookPageAdapter().publish(CrosspostPayload(
        platform="facebook",
        title="Video update",
        text="A short explainer",
        video_path="/media/short.mp4",
        meta={"post_public_id": "post-2"},
    ))

    assert result.status == "posted"
    assert result.remote_id == "video-123"
    assert result.url == "https://www.facebook.com/123456789012345/videos/video-123/"
    post = next(call for call in FakeFacebookClient.calls if call[0] == "POST")
    check = next(call for call in FakeFacebookClient.calls if call[0] == "GET")
    assert post[1].endswith("/123456789012345/videos")
    assert post[2]["data"]["file_url"] == "https://farm.example/signed-video.mp4"
    assert post[2]["data"]["published"] == "true"
    assert check[2]["params"] == {"fields": "status"}


def test_facebook_adapter_requires_a_page_token_and_numeric_page_id(monkeypatch):
    from tgfarm.s9_growth.facebook import FacebookPageAdapter

    monkeypatch.setattr(
        "tgfarm.s9_growth.facebook.get_settings",
        lambda: SimpleNamespace(facebook_page_id="not-a-page", facebook_page_access_token=""),
    )
    assert FacebookPageAdapter().configured() is False

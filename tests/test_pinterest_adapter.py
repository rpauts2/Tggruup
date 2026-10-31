from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    status_code = 201
    content = b'{"id":"987654321012345678","board_id":"123456789012345678"}'

    def json(self):
        return {"id": "987654321012345678", "board_id": "123456789012345678"}

    def raise_for_status(self):
        return None


class FakePinterestClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


@pytest.mark.asyncio
async def test_pinterest_adapter_creates_image_pin_on_selected_board(monkeypatch):
    from tgfarm import pinterest_oauth
    from tgfarm.s9_growth import instagram, pinterest

    FakePinterestClient.calls = []
    monkeypatch.setattr(
        pinterest,
        "get_settings",
        lambda: SimpleNamespace(
            pinterest_app_id="app-id",
            pinterest_app_secret="app-secret",
            pinterest_access_token="saved-token",
            pinterest_refresh_token="refresh-token",
            pinterest_board_id="123456789012345678",
        ),
    )

    async def get_token():
        return "access-token"

    def signed_media(*args):
        return "https://farm.example/public/media/signed-image.jpg"

    monkeypatch.setattr(pinterest_oauth, "get_valid_pinterest_access_token", get_token)
    monkeypatch.setattr(instagram, "_signed_media_url", signed_media)
    monkeypatch.setattr(instagram, "_ensure_jpeg", lambda path: path)
    monkeypatch.setattr(httpx, "AsyncClient", FakePinterestClient)

    result = await pinterest.PinterestAdapter().publish(CrosspostPayload(
        platform="pinterest",
        title="A useful Pin",
        text="A factual description",
        link="https://example.test/source",
        image_path="/tmp/cover.png",
        meta={"post_public_id": "post-1"},
    ))

    assert result.status == "posted"
    assert result.remote_id == "987654321012345678"
    assert result.url == "https://www.pinterest.com/pin/987654321012345678/"
    url, kwargs = FakePinterestClient.calls[0]
    assert url == "https://api.pinterest.com/v5/pins"
    assert kwargs["headers"]["Authorization"] == "Bearer access-token"
    body = kwargs["json"]
    assert body["board_id"] == "123456789012345678"
    assert body["title"] == "A useful Pin"
    assert body["description"] == "A factual description"
    assert body["link"] == "https://example.test/source"
    assert body["media_source"] == {
        "source_type": "image_url",
        "url": "https://farm.example/public/media/signed-image.jpg",
        "is_standard": True,
    }


def test_pinterest_adapter_requires_oauth_and_a_board(monkeypatch):
    from tgfarm.s9_growth.pinterest import PinterestAdapter

    monkeypatch.setattr(
        "tgfarm.s9_growth.pinterest.get_settings",
        lambda: SimpleNamespace(
            pinterest_app_id="",
            pinterest_app_secret="",
            pinterest_access_token="",
            pinterest_refresh_token="",
            pinterest_board_id="",
        ),
    )
    assert PinterestAdapter().configured() is False



class FakeVideoResponse:
    def __init__(self, payload=None, status_code=200, content=b"{}"):
        self._payload = payload or {}
        self.status_code = status_code
        self.content = content
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakePinterestVideoClient:
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
            return FakeVideoResponse({
                "media_id": "video-media-123",
                "upload_url": "https://pinterest-media-upload.s3-accelerate.amazonaws.com/",
                "upload_parameters": {"key": "uploads/video.mp4", "x-amz-signature": "signed"},
            }, status_code=201)
        if url == "https://pinterest-media-upload.s3-accelerate.amazonaws.com/":
            return FakeVideoResponse({}, status_code=204, content=b"")
        if url.endswith("/pins"):
            return FakeVideoResponse({"id": "video-pin-456"}, status_code=201)
        raise AssertionError("unexpected Pinterest POST " + url)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/media/video-media-123"):
            return FakeVideoResponse({"media_id": "video-media-123", "status": "succeeded"})
        raise AssertionError("unexpected Pinterest GET " + url)


@pytest.mark.asyncio
async def test_pinterest_adapter_uploads_video_waits_for_ready_and_creates_video_pin(
    tmp_path, monkeypatch
):
    from tgfarm import pinterest_oauth
    from tgfarm.s9_growth import instagram, pinterest

    video = tmp_path / "short.mp4"
    video.write_bytes(b"fake-video-content")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"fake-cover-content")
    FakePinterestVideoClient.calls = []

    monkeypatch.setattr(
        pinterest,
        "get_settings",
        lambda: SimpleNamespace(
            pinterest_app_id="app-id",
            pinterest_app_secret="app-secret",
            pinterest_access_token="saved-token",
            pinterest_refresh_token="refresh-token",
            pinterest_board_id="board-123",
            video_max_mb=45,
        ),
    )

    async def get_token():
        return "access-token"

    monkeypatch.setattr(pinterest_oauth, "get_valid_pinterest_access_token", get_token)
    monkeypatch.setattr(instagram, "_ensure_jpeg", lambda path: path)
    monkeypatch.setattr(
        instagram,
        "_signed_media_url",
        lambda path, post_id, kind: "https://farm.example/signed-cover.jpg",
    )
    monkeypatch.setattr(pinterest, "POLL_ATTEMPTS", 1)
    monkeypatch.setattr(httpx, "AsyncClient", FakePinterestVideoClient)

    result = await pinterest.PinterestAdapter().publish(CrosspostPayload(
        platform="pinterest",
        title="A video Pin",
        text="A short video explanation",
        image_path=str(cover),
        video_path=str(video),
        meta={"post_public_id": "post-video-1"},
    ))

    assert result.status == "posted"
    assert result.remote_id == "video-pin-456"
    assert result.url == "https://www.pinterest.com/pin/video-pin-456/"
    register_index = next(i for i, call in enumerate(FakePinterestVideoClient.calls) if call[0] == "POST" and call[1].endswith("/media"))
    upload_index = next(i for i, call in enumerate(FakePinterestVideoClient.calls) if call[0] == "POST" and "pinterest-media-upload" in call[1])
    poll_index = next(i for i, call in enumerate(FakePinterestVideoClient.calls) if call[0] == "GET")
    create_index = next(i for i, call in enumerate(FakePinterestVideoClient.calls) if call[0] == "POST" and call[1].endswith("/pins"))
    assert register_index < upload_index < poll_index < create_index
    assert FakePinterestVideoClient.calls[upload_index][2]["data"]["key"] == "uploads/video.mp4"
    assert FakePinterestVideoClient.calls[upload_index][2]["files"]["file"][0] == "short.mp4"
    pin_body = FakePinterestVideoClient.calls[create_index][2]["json"]
    assert pin_body["media_source"] == {
        "source_type": "video_id",
        "cover_image_url": "https://farm.example/signed-cover.jpg",
        "media_id": "video-media-123",
    }

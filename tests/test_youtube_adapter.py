from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeYouTubeClient:
    calls = []
    put_error = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(
            status_code=200,
            headers={"Location": "https://upload.youtube.test/session/123"},
        )

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        if self.put_error:
            raise self.put_error
        return FakeResponse({"id": "youtube-video-123"})


def settings(**overrides):
    values = {
        "youtube_refresh_token": "saved-refresh-token",
        "youtube_client_id": "google-client",
        "youtube_client_secret": "google-secret",
        "youtube_channel_id": "UC-demo",
        "youtube_privacy_status": "private",
        "youtube_category_id": "25",
        "video_max_mb": 45,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "short.mp4"
    path.write_bytes(bytes.fromhex("000000186674797069736f6d") + b"mock-video-data")
    return path


@pytest.mark.asyncio
async def test_youtube_adapter_uses_resumable_upload_private_by_default(
    monkeypatch, video_file
):
    import httpx
    from tgfarm import youtube_oauth
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth import adapters

    FakeYouTubeClient.calls = []
    FakeYouTubeClient.put_error = None
    monkeypatch.setattr(adapters, "get_settings", lambda: settings())
    monkeypatch.setattr(httpx, "AsyncClient", FakeYouTubeClient)

    async def token():
        return "private-access-token"

    monkeypatch.setattr(youtube_oauth, "get_valid_youtube_access_token", token)
    payload = CrosspostPayload(
        platform="youtube",
        title="Тестовый Short",
        text="Описание ролика #Новости #AI",
        video_path=str(video_file),
        link="https://example.test/story",
    )

    result = await adapters.ADAPTERS["youtube"].publish(payload)

    assert result.status == "posted"
    assert result.remote_id == "youtube-video-123"
    assert result.url == "https://www.youtube.com/watch?v=youtube-video-123"
    init = next(call for call in FakeYouTubeClient.calls if call[0] == "POST")
    assert init[1] == adapters.ADAPTERS["youtube"].upload_url
    assert init[2]["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    assert init[2]["headers"]["Authorization"] == "Bearer private-access-token"
    assert init[2]["headers"]["X-Upload-Content-Length"] == str(video_file.stat().st_size)
    metadata = init[2]["json"]
    assert metadata["snippet"]["title"] == "Тестовый Short"
    assert metadata["snippet"]["categoryId"] == "25"
    assert metadata["snippet"]["tags"] == ["Новости", "AI"]
    assert metadata["status"]["privacyStatus"] == "private"

    upload = next(call for call in FakeYouTubeClient.calls if call[0] == "PUT")
    assert upload[1] == "https://upload.youtube.test/session/123"
    assert upload[2]["content"] == video_file.read_bytes()
    assert upload[2]["headers"]["Content-Type"] == "video/mp4"


@pytest.mark.asyncio
async def test_youtube_adapter_never_publishes_text_when_short_render_failed(monkeypatch):
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: settings())
    result = await adapters.ADAPTERS["youtube"].publish(
        CrosspostPayload(
            platform="youtube",
            title="Some title",
            text="Some text",
            meta={"video_render_error": "RuntimeError: FFmpeg not found"},
        )
    )
    assert result.status == "permanent"
    assert "FFmpeg not found" in result.error


@pytest.mark.asyncio
async def test_youtube_adapter_marks_ambiguous_upload_timeout_unknown(
    monkeypatch, video_file
):
    from tgfarm import youtube_oauth
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth import adapters

    FakeYouTubeClient.calls = []
    FakeYouTubeClient.put_error = httpx.ReadTimeout("upload timed out")

    monkeypatch.setattr(adapters, "get_settings", lambda: settings())
    monkeypatch.setattr(httpx, "AsyncClient", FakeYouTubeClient)

    async def token():
        return "private-access-token"

    monkeypatch.setattr(youtube_oauth, "get_valid_youtube_access_token", token)
    result = await adapters.ADAPTERS["youtube"].publish(
        CrosspostPayload(
            platform="youtube",
            title="Ambiguous upload",
            text="Details",
            video_path=str(video_file),
        )
    )
    assert result.status == "unknown"
    assert "verify Studio" in result.error
    assert len([call for call in FakeYouTubeClient.calls if call[0] == "PUT"]) == 1

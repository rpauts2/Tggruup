from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self.request = httpx.Request("POST", "https://www.googleapis.com/upload/youtube/v3/videos")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("provider rejected request", request=self.request, response=response)

    def json(self):
        return self._payload


class FakeYouTubeUploadClient:
    calls = []
    fail_put = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(headers={"location": "https://upload.example.test/resumable-session"})

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        if self.fail_put:
            request = httpx.Request("PUT", url)
            raise httpx.ReadTimeout("upload response timed out", request=request)
        return FakeResponse({"id": "short123"})


def _settings(**overrides):
    base = {
        "youtube_client_id": "google-client-id",
        "youtube_client_secret": "google-client-secret",
        "youtube_refresh_token": "refresh-private",
        "youtube_channel_id": "UC_demo_123",
        "youtube_privacy_status": "unlisted",
        "youtube_category_id": "28",
        "video_max_mb": 45,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_youtube_adapter_uploads_mp4_as_unlisted_synthetic_short(tmp_path, monkeypatch):
    from tgfarm import youtube_oauth
    from tgfarm.s9_growth import adapters

    video = tmp_path / "short.mp4"
    video.write_bytes(b"original-mp4-bytes")
    FakeYouTubeUploadClient.calls = []
    FakeYouTubeUploadClient.fail_put = False
    monkeypatch.setattr(adapters, "get_settings", lambda: _settings())
    monkeypatch.setattr(youtube_oauth, "get_youtube_access_token", lambda: _access_token())
    monkeypatch.setattr(httpx, "AsyncClient", FakeYouTubeUploadClient)

    result = await adapters.YouTubeAdapter().publish(CrosspostPayload(
        platform="youtube",
        title="A useful title",
        text="A concise description",
        video_path=str(video),
        hashtags=["#AI", "AI", "#Automation"],
    ))

    assert result.status == "posted"
    assert result.remote_id == "short123"
    assert result.url == "https://www.youtube.com/watch?v=short123"
    init_call, upload_call = FakeYouTubeUploadClient.calls
    assert init_call[0] == "POST"
    assert init_call[2]["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    assert init_call[2]["headers"]["Authorization"] == "Bearer access-private"
    body = init_call[2]["json"]
    assert body["snippet"]["title"] == "A useful title"
    assert body["snippet"]["description"] == "A concise description"
    assert body["snippet"]["tags"] == ["AI", "Automation"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["containsSyntheticMedia"] is True
    assert upload_call[0] == "PUT"
    assert upload_call[2]["content"] == b"original-mp4-bytes"
    assert "refresh-private" not in repr(result)


async def _access_token():
    return "access-private"


@pytest.mark.asyncio
async def test_youtube_adapter_marks_ambiguous_media_upload_unknown(tmp_path, monkeypatch):
    from tgfarm import youtube_oauth
    from tgfarm.s9_growth import adapters

    video = tmp_path / "short.mp4"
    video.write_bytes(b"original-mp4-bytes")
    FakeYouTubeUploadClient.calls = []
    FakeYouTubeUploadClient.fail_put = True
    monkeypatch.setattr(adapters, "get_settings", lambda: _settings())
    monkeypatch.setattr(youtube_oauth, "get_youtube_access_token", _access_token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeYouTubeUploadClient)

    result = await adapters.YouTubeAdapter().publish(CrosspostPayload(
        platform="youtube",
        title="A useful title",
        text="A concise description",
        video_path=str(video),
    ))

    assert result.status == "unknown"
    assert "Проверь YouTube Studio" in (result.error or "")
    assert result.remote_id is None


@pytest.mark.asyncio
async def test_youtube_adapter_skips_without_credentials(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: _settings(youtube_refresh_token=""))
    result = await adapters.YouTubeAdapter().publish(CrosspostPayload(
        platform="youtube", title="Title", text="Body", video_path="missing.mp4"
    ))
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_youtube_adapter_rejects_missing_video_without_fake_success(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: _settings())
    result = await adapters.YouTubeAdapter().publish(CrosspostPayload(
        platform="youtube", title="Title", text="Body", video_path="missing.mp4"
    ))
    assert result.status == "failed"
    assert result.remote_id is None

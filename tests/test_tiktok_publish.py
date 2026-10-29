from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, url="https://open.tiktokapis.com/v2/test"):
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self.request = httpx.Request("POST", url)

    def json(self):
        return self._payload


class FakeTikTokDirectPostClient:
    calls = []
    init_status = 200
    status_sequence = ["PUBLISH_COMPLETE"]

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/video/init/"):
            return FakeResponse(
                {"data": {"publish_id": "publish-123", "upload_url": "https://upload.example.test/session"}},
                status_code=self.init_status,
                headers={"Retry-After": "45"},
                url=url,
            )
        if url.endswith("/status/fetch/"):
            index = min(
                len([call for call in self.calls if call[1].endswith("/status/fetch/")]) - 1,
                len(self.status_sequence) - 1,
            )
            state = self.status_sequence[index]
            data = {"status": state}
            if state == "PUBLISH_COMPLETE":
                data["publicaly_available_post_id"] = ["video-789"]
            return FakeResponse({"error": {"code": "ok"}, "data": data}, url=url)
        raise AssertionError("unexpected TikTok POST URL: " + url)

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse({"ok": True}, status_code=201, url=url)


def _settings(**overrides):
    base = {"video_max_mb": 45}
    base.update(overrides)
    return SimpleNamespace(**base)


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "short.mp4"
    path.write_bytes(b"original-tiktok-video")
    path.with_suffix(".json").write_text(
        json.dumps({"asset": {"duration_s": 4.5}}),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_tiktok_direct_post_requires_fresh_creator_options_and_uploads_once(tmp_path, monkeypatch):
    from tgfarm import tiktok_oauth, tiktok_publish

    path = _video(tmp_path)
    FakeTikTokDirectPostClient.calls = []
    FakeTikTokDirectPostClient.init_status = 200
    FakeTikTokDirectPostClient.status_sequence = ["PUBLISH_COMPLETE"]
    monkeypatch.setattr(tiktok_publish, "get_settings", lambda: _settings())
    monkeypatch.setattr(tiktok_oauth, "get_valid_tiktok_access_token", _token)
    monkeypatch.setattr(
        tiktok_oauth,
        "get_tiktok_creator_info",
        lambda **kwargs: _creator_info(),
    )
    monkeypatch.setattr(httpx, "AsyncClient", FakeTikTokDirectPostClient)
    monkeypatch.setattr(tiktok_publish, "POLL_ATTEMPTS", 1)

    result = await tiktok_publish.publish_tiktok_video(
        path,
        caption="A useful short",
        privacy_level="SELF_ONLY",
    )

    assert result.status == "posted"
    assert result.remote_id == "video-789"
    assert result.url == "https://www.tiktok.com/@demo_creator/video/video-789"
    init_call = next(call for call in FakeTikTokDirectPostClient.calls if call[1].endswith("/video/init/"))
    body = init_call[2]["json"]
    assert body["post_mode"] == "DIRECT_POST"
    assert body["media_type"] == "VIDEO"
    assert body["is_aigc"] is True
    assert body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert body["post_info"]["title"] == "A useful short"
    assert body["source_info"]["source"] == "FILE_UPLOAD"
    upload = next(call for call in FakeTikTokDirectPostClient.calls if call[0] == "PUT")
    assert upload[2]["headers"]["Content-Range"] == f"bytes 0-{path.stat().st_size - 1}/{path.stat().st_size}"


async def _token():
    return "access-private"


async def _creator_info():
    return {
        "username": "demo_creator",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        "max_video_post_duration_sec": 60,
        "comment_disabled": False,
        "duet_disabled": True,
        "stitch_disabled": False,
    }


@pytest.mark.asyncio
async def test_tiktok_direct_post_rejects_privacy_not_returned_by_creator_info(tmp_path, monkeypatch):
    from tgfarm import tiktok_oauth, tiktok_publish

    path = _video(tmp_path)
    FakeTikTokDirectPostClient.calls = []
    monkeypatch.setattr(tiktok_publish, "get_settings", lambda: _settings())
    monkeypatch.setattr(tiktok_oauth, "get_valid_tiktok_access_token", _token)
    monkeypatch.setattr(tiktok_oauth, "get_tiktok_creator_info", lambda **kwargs: _creator_info())
    monkeypatch.setattr(httpx, "AsyncClient", FakeTikTokDirectPostClient)

    result = await tiktok_publish.publish_tiktok_video(
        path,
        caption="A useful short",
        privacy_level="FOLLOWER_OF_CREATOR",
    )

    assert result.status == "permanent"
    assert "изменил доступные настройки приватности" in (result.error or "")
    assert FakeTikTokDirectPostClient.calls == []


@pytest.mark.asyncio
async def test_tiktok_direct_post_unknown_processing_result_is_not_reported_as_success(tmp_path, monkeypatch):
    from tgfarm import tiktok_oauth, tiktok_publish

    path = _video(tmp_path)
    FakeTikTokDirectPostClient.calls = []
    FakeTikTokDirectPostClient.init_status = 200
    FakeTikTokDirectPostClient.status_sequence = ["PROCESSING_UPLOAD"]
    monkeypatch.setattr(tiktok_publish, "get_settings", lambda: _settings())
    monkeypatch.setattr(tiktok_oauth, "get_valid_tiktok_access_token", _token)
    monkeypatch.setattr(tiktok_oauth, "get_tiktok_creator_info", lambda **kwargs: _creator_info())
    monkeypatch.setattr(httpx, "AsyncClient", FakeTikTokDirectPostClient)
    monkeypatch.setattr(tiktok_publish, "POLL_ATTEMPTS", 1)
    monkeypatch.setattr(tiktok_publish, "POLL_DELAY_SECONDS", 0)

    result = await tiktok_publish.publish_tiktok_video(
        path,
        caption="A useful short",
        privacy_level="SELF_ONLY",
    )

    assert result.status == "unknown"
    assert result.remote_id == "publish-123"
    assert "не подтверждён" in (result.error or "")
    assert sum(1 for call in FakeTikTokDirectPostClient.calls if call[1].endswith("/video/init/")) == 1


@pytest.mark.asyncio
async def test_tiktok_direct_post_keeps_retry_after_when_init_is_rate_limited(tmp_path, monkeypatch):
    from tgfarm import tiktok_oauth, tiktok_publish

    path = _video(tmp_path)
    FakeTikTokDirectPostClient.calls = []
    FakeTikTokDirectPostClient.init_status = 429
    monkeypatch.setattr(tiktok_publish, "get_settings", lambda: _settings())
    monkeypatch.setattr(tiktok_oauth, "get_valid_tiktok_access_token", _token)
    monkeypatch.setattr(tiktok_oauth, "get_tiktok_creator_info", lambda **kwargs: _creator_info())
    monkeypatch.setattr(httpx, "AsyncClient", FakeTikTokDirectPostClient)

    result = await tiktok_publish.publish_tiktok_video(
        path,
        caption="A useful short",
        privacy_level="SELF_ONLY",
    )

    assert result.status == "failed"
    assert result.retry_after_seconds == 45
    assert FakeTikTokDirectPostClient.calls[0][1].endswith("/video/init/")


@pytest.mark.asyncio
async def test_tiktok_direct_post_refuses_missing_or_empty_caption_before_upload(tmp_path, monkeypatch):
    from tgfarm import tiktok_oauth, tiktok_publish

    path = _video(tmp_path)
    FakeTikTokDirectPostClient.calls = []
    monkeypatch.setattr(tiktok_publish, "get_settings", lambda: _settings())
    monkeypatch.setattr(tiktok_oauth, "get_valid_tiktok_access_token", _token)
    monkeypatch.setattr(tiktok_oauth, "get_tiktok_creator_info", lambda **kwargs: _creator_info())
    monkeypatch.setattr(httpx, "AsyncClient", FakeTikTokDirectPostClient)

    result = await tiktok_publish.publish_tiktok_video(path, caption="  ", privacy_level="SELF_ONLY")

    assert result.status == "permanent"
    assert "подпись" in (result.error or "").lower()
    assert FakeTikTokDirectPostClient.calls == []

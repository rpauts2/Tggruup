from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest

class FakeResponse:
    status_code = 200
    def __init__(self,payload,status_code=200): self._payload=payload; self.status_code=status_code
    def json(self): return self._payload

class FakeClient:
    calls = []
    create_error = None
    def __init__(self,*args,**kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    async def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        if url.endswith("/video/upload/"):
            return FakeResponse({"data":{"error_code":0,"video":{"video_id":"encrypted-video-123"}}})
        if url.endswith("/video/create/"):
            if self.create_error: raise self.create_error
            return FakeResponse({"data":{"error_code":0,"item_id":"douyin-item-456"}})
        raise AssertionError(url)

async def token(): return "access-private"

@pytest.mark.asyncio
async def test_douyin_upload_then_create(monkeypatch,tmp_path):
    from tgfarm import douyin_oauth,douyin_publish
    video = tmp_path/"clip.mp4"; video.write_bytes(b"original-video")
    monkeypatch.setattr(douyin_publish,"get_settings",lambda:SimpleNamespace(video_max_mb=45,douyin_open_id="open-123"))
    monkeypatch.setattr(douyin_oauth,"get_valid_douyin_access_token",token)
    monkeypatch.setattr(httpx,"AsyncClient",FakeClient)
    FakeClient.calls=[]; FakeClient.create_error=None
    result = await douyin_publish.publish_douyin_video(video,caption="A concise title")
    assert result.status == "posted" and result.remote_id == "douyin-item-456"
    upload_url, upload = FakeClient.calls[0]; create_url, create = FakeClient.calls[1]
    assert upload_url == douyin_publish.UPLOAD_URL and upload["params"] == {"open_id":"open-123"}
    assert upload["headers"]["access-token"] == "access-private" and "video" in upload["files"]
    assert create_url == douyin_publish.CREATE_URL
    assert create["json"] == {"video_id":"encrypted-video-123","text":"A concise title"}

@pytest.mark.asyncio
async def test_douyin_ambiguous_create_timeout_is_unknown(monkeypatch,tmp_path):
    from tgfarm import douyin_oauth,douyin_publish
    video = tmp_path/"clip.mp4"; video.write_bytes(b"original-video")
    monkeypatch.setattr(douyin_publish,"get_settings",lambda:SimpleNamespace(video_max_mb=45,douyin_open_id="open-123"))
    monkeypatch.setattr(douyin_oauth,"get_valid_douyin_access_token",token)
    monkeypatch.setattr(httpx,"AsyncClient",FakeClient)
    FakeClient.calls=[]; FakeClient.create_error=httpx.ReadTimeout("timeout")
    result = await douyin_publish.publish_douyin_video(video,caption="Title")
    assert result.status == "unknown" and "не повторяй" in result.error.lower()
    assert len(FakeClient.calls) == 2

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=None):
        self._payload = payload
        self.status_code = status_code
        self.content = content if content is not None else (b"" if payload is None else b"json")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.ok.ru/fb.do")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("OK rejected request", request=request, response=response)


class FakeOKClient:
    calls = []
    permissions = {"GROUP_CONTENT": True, "PHOTO_CONTENT": True, "VIDEO_CONTENT": True}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/fb.do"):
            params = kwargs["data"]
            method = params["method"]
            if method == "users.hasAppPermission":
                return FakeResponse(self.permissions.get(params["ext_perm"], False))
            if method == "photosV2.getUploadUrl":
                return FakeResponse({"upload_url": "https://api.ok.ru/upload/photo"})
            if method == "video.getUploadUrl":
                return FakeResponse({
                    "upload_url": "https://api.ok.ru/upload/video",
                    "video_id": "video-123",
                })
            if method == "video.update":
                return FakeResponse(None, content=b"")
            if method == "mediatopic.post":
                return FakeResponse("topic-123")
            raise AssertionError("unexpected OK API method " + method)
        if url == "https://api.ok.ru/upload/photo":
            return FakeResponse({"photos": {"photo-encrypted-id": {"token": "photo-upload-token"}}})
        if url == "https://api.ok.ru/upload/video":
            return FakeResponse(None, content=b"")
        raise AssertionError("unexpected OK upload URL " + url)


@pytest.fixture
def ok_settings(monkeypatch):
    from tgfarm.s9_growth import odnoklassniki

    monkeypatch.setattr(
        odnoklassniki,
        "get_settings",
        lambda: SimpleNamespace(
            ok_application_key="public-key",
            ok_application_secret="private-app-secret",
            ok_access_token="long-access-token",
            ok_session_secret_key="session-secret",
            ok_group_id="987654321",
            video_max_mb=45,
        ),
    )
    FakeOKClient.calls = []
    FakeOKClient.permissions = {"GROUP_CONTENT": True, "PHOTO_CONTENT": True, "VIDEO_CONTENT": True}
    monkeypatch.setattr(httpx, "AsyncClient", FakeOKClient)
    return odnoklassniki


def test_ok_signature_uses_sorted_key_values_and_excludes_access_token():
    from tgfarm.s9_growth.odnoklassniki import build_ok_signature, build_ok_request_params

    params = {
        "method": "mediatopic.post",
        "format": "json",
        "application_key": "public-key",
        "access_token": "user-secret",
        "gid": "12345",
    }
    request = build_ok_request_params(
        "mediatopic.post",
        {"gid": "12345"},
        application_key="public-key",
        application_secret="app-secret",
        access_token="user-secret",
        session_secret_key="session-secret",
    )
    expected = hashlib.md5(
        ("application_key=public-keyformat=jsongid=12345method=mediatopic.postsession-secret").encode()
    ).hexdigest()
    assert request["sig"] == expected
    assert request["access_token"] == "user-secret"
    assert build_ok_signature(params, "session-secret") == expected


@pytest.mark.asyncio
async def test_ok_adapter_publishes_group_text_topic(ok_settings):
    result = await ok_settings.OdnoklassnikiAdapter().publish(CrosspostPayload(
        platform="odnoklassniki",
        title="Useful update",
        text="A clear summary with an explanation.",
        link="https://example.test/source",
    ))

    assert result.status == "posted"
    assert result.remote_id == "topic-123"
    assert result.url == "https://ok.ru/group/987654321/topic/topic-123"
    call = next(c for c in FakeOKClient.calls if c[1].endswith("/fb.do") and c[2]["data"]["method"] == "mediatopic.post")
    params = call[2]["data"]
    assert params["gid"] == "987654321"
    assert params["type"] == "GROUP_THEME"
    assert params["onBehalfOfGroup"] == "true"
    attachment = json.loads(params["attachment"])
    assert attachment["media"][0] == {
        "type": "text",
        "text": "Useful update\n\nA clear summary with an explanation.",
    }
    assert attachment["media"][1] == {"type": "link", "url": "https://example.test/source"}


@pytest.mark.asyncio
async def test_ok_adapter_uploads_photo_and_uses_group_photo_token(tmp_path, monkeypatch, ok_settings):
    from tgfarm.s9_growth import instagram

    image = tmp_path / "cover.jpg"
    image.write_bytes(b"jpeg-image-placeholder")
    monkeypatch.setattr(instagram, "_ensure_jpeg", lambda path: Path(path))

    result = await ok_settings.OdnoklassnikiAdapter().publish(CrosspostPayload(
        platform="odnoklassniki",
        title="Photo update",
        text="Image detail",
        image_path=str(image),
    ))

    assert result.status == "posted"
    photo_call = next(c for c in FakeOKClient.calls if c[1].endswith("/upload/photo"))
    assert "pic1" in photo_call[2]["files"]
    upload_url_call = next(
        c for c in FakeOKClient.calls
        if c[1].endswith("/fb.do") and c[2]["data"]["method"] == "photosV2.getUploadUrl"
    )
    assert upload_url_call[2]["data"]["gid"] == "987654321"
    assert upload_url_call[2]["data"]["count"] == "1"
    publish_call = next(
        c for c in FakeOKClient.calls
        if c[1].endswith("/fb.do") and c[2]["data"]["method"] == "mediatopic.post"
    )
    attachment = json.loads(publish_call[2]["data"]["attachment"])
    assert {"type": "photo", "list": [{"id": "photo-upload-token"}]} in attachment["media"]


@pytest.mark.asyncio
async def test_ok_adapter_uploads_and_finalizes_video_before_group_post(tmp_path, ok_settings):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * (20 * 1024))

    result = await ok_settings.OdnoklassnikiAdapter().publish(CrosspostPayload(
        platform="odnoklassniki",
        title="Video update",
        text="Watch the explanation",
        video_path=str(video),
    ))

    assert result.status == "posted"
    calls = FakeOKClient.calls
    init = next(i for i, c in enumerate(calls) if c[1].endswith("/fb.do") and c[2]["data"]["method"] == "video.getUploadUrl")
    upload = next(i for i, c in enumerate(calls) if c[1].endswith("/upload/video"))
    finish = next(i for i, c in enumerate(calls) if c[1].endswith("/fb.do") and c[2]["data"]["method"] == "video.update")
    publish = next(i for i, c in enumerate(calls) if c[1].endswith("/fb.do") and c[2]["data"]["method"] == "mediatopic.post")
    assert init < upload < finish < publish
    assert calls[init][2]["data"]["file_size"] == str(video.stat().st_size)
    assert calls[finish][2]["data"]["vid"] == "video-123"
    attachment = json.loads(calls[publish][2]["data"]["attachment"])
    assert {"type": "movie", "list": [{"id": "video-123"}]} in attachment["media"]


def test_ok_adapter_requires_app_credentials_and_group(monkeypatch):
    from tgfarm.s9_growth.odnoklassniki import OdnoklassnikiAdapter

    monkeypatch.setattr(
        "tgfarm.s9_growth.odnoklassniki.get_settings",
        lambda: SimpleNamespace(
            ok_application_key="",
            ok_application_secret="",
            ok_access_token="",
            ok_session_secret_key="",
            ok_group_id="",
        ),
    )
    assert OdnoklassnikiAdapter().configured() is False


def test_ok_upload_url_rejects_untrusted_host():
    from tgfarm.s9_growth.odnoklassniki import _upload_url_allowed

    assert _upload_url_allowed("https://api.ok.ru/upload/photo") is True
    assert _upload_url_allowed("https://uploads.odnoklassniki.ru/file") is True
    assert _upload_url_allowed("https://evil.example/file") is False
    assert _upload_url_allowed("http://api.ok.ru/upload") is False

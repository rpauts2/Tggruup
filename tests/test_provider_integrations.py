"""LongCat text and Cloudflare Workers AI image integrations."""

from __future__ import annotations

import base64
import io

import httpx
import pytest
from PIL import Image

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.llm.client import _LongCat, _OpenAI
from tgfarm.llm.images import CloudflareImageProvider, _parse_size, PollinationsImageProvider


class _Recorder:
    def __init__(self, payload: dict, status: int = 200, content: bytes | None = None) -> None:
        self.payload = payload
        self.status = status
        self.content = content
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": headers or {}})
        if self.content is not None:
            return httpx.Response(
                self.status,
                content=self.content,
                headers={"content-type": "image/jpeg"},
                request=httpx.Request("GET", url),
            )
    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        if self.content is not None:
            return httpx.Response(
                self.status,
                content=self.content,
                headers={"content-type": "image/jpeg"},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("POST", url))
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        if self.content is not None:
            return httpx.Response(
                self.status,
                content=self.content,
                headers={"content-type": "image/jpeg"},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("POST", url))


def _patch_httpx(monkeypatch, recorder):
    class _Fake:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return recorder

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)


def jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (30, 90, 160)).save(buffer, "JPEG")
    return buffer.getvalue()


OK = {"choices": [{"message": {"content": "ответ"}}], "usage": {"completion_tokens": 3}}


@pytest.mark.asyncio
async def test_longcat_uses_its_own_openai_endpoint(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    provider = _LongCat("key", "LongCat-2.5-Preview", 10.0)
    await provider.complete("sys", "hi", json_mode=True, max_tokens=64)

    call = recorder.calls[0]
    assert call["url"] == "https://api.longcat.chat/openai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer key"
    assert call["json"]["model"] == "LongCat-2.5-Preview"
    assert call["json"]["thinking"] == {"type": "disabled"}
    assert "response_format" not in call["json"]


@pytest.mark.asyncio
async def test_longcat_thinking_is_opt_in(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    await _LongCat("key", "LongCat-2.0", 10.0, thinking=True).complete(
        "sys", "hi", json_mode=False, max_tokens=32
    )
    assert recorder.calls[0]["json"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_a_quota_error_demotes_the_provider(monkeypatch):
    recorder = _Recorder({"error": {"message": "Usage limit reached"}}, status=402)
    _patch_httpx(monkeypatch, recorder)

    provider = _OpenAI("key", "m", 10.0, base_url="https://api.example/v1")
    with pytest.raises(ProviderUnavailable):
        await provider.complete("s", "p", json_mode=False, max_tokens=16)
    assert await provider.available() is False


def test_cloudflare_size_is_clamped():
    assert _parse_size("1280x720") == (1280, 720)
    assert _parse_size("100x100") == (256, 256)
    assert _parse_size("5000x5000") == (2048, 2048)
    assert _parse_size("bad") == (1024, 1024)


@pytest.mark.asyncio
async def test_cloudflare_image_is_posted_to_workers_ai(monkeypatch):
    recorder = _Recorder({"success": True, "result": {"image": base64.b64encode(jpeg_bytes()).decode()}})
    _patch_httpx(monkeypatch, recorder)

    provider = CloudflareImageProvider(
        "account-id", "cf-key", "@cf/black-forest-labs/flux-1-schnell", "1280x720", 4, 30.0
    )
    result = await provider.generate("a cyberpunk lizard", "text, letters")

    call = recorder.calls[0]
    assert call["url"].startswith(
        "https://api.cloudflare.com/client/v4/accounts/account-id/ai/run/"
    )
    assert call["headers"]["Authorization"] == "Bearer cf-key"
    assert call["json"]["width"] == 1280
    assert call["json"]["height"] == 720
    assert call["json"]["steps"] == 4
    assert result.path.suffix == ".jpg"
    assert (result.width, result.height) == (64, 32)
    assert result.path.exists()


@pytest.mark.asyncio
async def test_cloudflare_accepts_raw_image_response(monkeypatch):
    _patch_httpx(monkeypatch, _Recorder({}, content=jpeg_bytes()))
    provider = CloudflareImageProvider("a", "k", "@cf/model", "1024x1024", 8, 30.0)
    result = await provider.generate("prompt")
    assert result.path.suffix == ".jpg"


@pytest.mark.asyncio
async def test_cloudflare_auth_failure_is_reported(monkeypatch):
    _patch_httpx(monkeypatch, _Recorder({"success": False}, status=403))
    provider = CloudflareImageProvider("a", "k", "@cf/model", "1024x1024", 4, 30.0)
    with pytest.raises(ProviderUnavailable, match="auth failed"):
        await provider.generate("prompt")

@pytest.mark.asyncio
async def test_pollinations_image_is_fetched_keyless(monkeypatch):
    recorder = _Recorder({}, content=jpeg_bytes())
    _patch_httpx(monkeypatch, recorder)

    provider = PollinationsImageProvider("flux", "1280x720", 30.0)
    result = await provider.generate("a cyberpunk lizard", "text, letters")

    call = recorder.calls[0]
    assert "image.pollinations.ai/prompt/" in call["url"]
    assert "model=flux" in call["url"]
    assert "width=1280" in call["url"]
    assert "height=720" in call["url"]
    assert result.path.suffix == ".jpg"
    assert (result.width, result.height) == (64, 32)

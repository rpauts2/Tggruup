"""The OpenAI-compatible provider.

One code path is meant to cover Gemini, Groq, OpenRouter, Together, Fireworks
and any local vLLM, so what matters is that the base URL is actually used, the
key is sent the way each expects, and a local server works without a key.
"""

from __future__ import annotations

import httpx
import pytest

from tgfarm.llm.client import LLMClient, _is_loopback, _OpenAI


class _Recorder:
    """Stands in for httpx.AsyncClient and remembers what was sent."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("POST", url))


def _patch_httpx(monkeypatch, recorder):

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return recorder

        async def __aenter__(self):
            return recorder

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)


OK = {"choices": [{"message": {"content": "ответ"}}], "usage": {"completion_tokens": 7}}


# ------------------------------------------------------------------ loopback rule
@pytest.mark.parametrize(
    ("base", "local"),
    [
        ("http://127.0.0.1:8000/v1", True),
        ("http://localhost:11434/v1", True),
        ("https://api.openai.com/v1", False),
        ("https://openrouter.ai/api/v1", False),
        ("https://generativelanguage.googleapis.com/v1beta/openai", False),
        ("", True),
    ],
)
def test_loopback_detection(base, local):
    assert _is_loopback(base) is local


# ------------------------------------------------------------------- behaviour
@pytest.mark.asyncio
async def test_the_configured_base_url_is_used(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    provider = _OpenAI("key", "model-x", 10.0, base_url="https://groq.example/v1/")
    assert await provider.complete("sys", "hi", json_mode=False, max_tokens=64) == "ответ"
    assert recorder.calls[0]["url"] == "https://groq.example/v1/chat/completions"


@pytest.mark.asyncio
async def test_the_bearer_token_is_sent(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    provider = _OpenAI("secret-key", "m", 10.0, base_url="https://api.example/v1")
    await provider.complete("s", "p", json_mode=False, max_tokens=32)
    assert recorder.calls[0]["headers"]["Authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_a_local_server_is_called_without_a_key(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    provider = _OpenAI("", "local-model", 10.0, base_url="http://127.0.0.1:8080/v1")
    assert await provider.available() is True
    await provider.complete("s", "p", json_mode=False, max_tokens=32)
    assert "Authorization" not in recorder.calls[0]["headers"]


@pytest.mark.asyncio
async def test_a_hosted_provider_without_a_key_is_skipped():
    provider = _OpenAI("", "m", 10.0, base_url="https://openrouter.ai/api/v1")
    assert await provider.available() is False


@pytest.mark.asyncio
async def test_json_mode_asks_for_json(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    await _OpenAI("k", "m", 10.0).complete("s", "p", json_mode=True, max_tokens=128)
    body = recorder.calls[0]["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 128


@pytest.mark.asyncio
async def test_an_explicit_model_overrides_the_configured_one(monkeypatch):
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)

    provider = _OpenAI("k", "configured", 10.0)
    await provider.complete("s", "p", json_mode=False, max_tokens=32, model="per-call")
    assert recorder.calls[0]["json"]["model"] == "per-call"


@pytest.mark.asyncio
async def test_rate_limit_marks_the_provider_down(monkeypatch):
    _patch_httpx(monkeypatch, _Recorder({"error": "slow down"}, status=429))
    provider = _OpenAI("k", "m", 10.0)

    from tgfarm.core.errors import ProviderRateLimited

    with pytest.raises(ProviderRateLimited):
        await provider.complete("s", "p", json_mode=False, max_tokens=32)
    assert await provider.available() is False


# --------------------------------------------------------------------- chain wiring
def test_compatible_is_registered_from_settings(monkeypatch):
    from tgfarm.core import settings as settings_mod

    settings = settings_mod.get_settings()
    monkeypatch.setattr(
        settings_mod.get_settings(),
        "llm_providers",
        "compatible,offline",
    )
    monkeypatch.setattr(settings_mod.get_settings(), "llm_base_url", "https://groq.example/v1")
    monkeypatch.setattr(settings_mod.get_settings(), "llm_api_key", "k")
    monkeypatch.setattr(settings_mod.get_settings(), "llm_model", "llama-3.3-70b")

    client = LLMClient()
    providers = [p.name for p in client._providers]
    assert "compatible" in providers
    configured = next(p for p in client._providers if p.name == "compatible")
    assert configured._base == "https://groq.example/v1"
    assert configured._model == "llama-3.3-70b"
    assert isinstance(settings, type(settings))
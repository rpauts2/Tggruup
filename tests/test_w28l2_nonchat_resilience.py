"""W28-L2: the non-chat LLM call sites inherit the retry discipline.

`tgfarm/llm/embedding.py` and `tgfarm/llm/images.py` post straight to the
provider instead of going through the chat client, so a 503 burst used to fail
every embedding batch and every image generation on the first bad response.
These tests pin the replacement behaviour, fully offline: every outbound call
is a monkeypatched stub and conftest turns a real socket into a failure.

Proved here:
  * 429/5xx with `Retry-After` waits exactly that (capped) amount and retries;
  * a non-retryable 4xx is sent once and never retried;
  * an exhausted retry raises ProviderUnavailable — never a fabricated result —
    and the image budget records the failed outcome;
  * the documented offline provenance still holds (local embedder, template
    fallback, zero network).
"""

from __future__ import annotations

import base64
import io
import types

import httpx
import pytest
from PIL import Image

import tgfarm.llm.embedding as embedding_mod
import tgfarm.llm.image_budget as image_budget_mod
import tgfarm.llm.images as images_mod
from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.ratelimit import RATE
from tgfarm.llm.embedding import LocalHashEmbedder, OpenAIEmbedder, get_embedder
from tgfarm.llm.images import ImageProvider, available, generate_cover, get_provider


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _resp(status: int, *, headers: dict | None = None, payload=None, url: str = "https://provider.invalid/x"):
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        json=payload if payload is not None else {"detail": "stub"},
        request=httpx.Request("POST", url),
    )


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    """The shared image bucket would otherwise make tests wait ~2s per attempt."""

    async def _noop(tokens: float = 1.0) -> None:
        return None

    monkeypatch.setattr(RATE.image, "take", _noop)


@pytest.fixture()
def image_delays(monkeypatch):
    """Capture the backoff sleeps instead of performing them."""
    delays: list[float] = []

    async def _fake_sleep(delay):
        delays.append(float(delay))

    monkeypatch.setattr(images_mod, "asyncio", types.SimpleNamespace(sleep=_fake_sleep))
    return delays


@pytest.fixture()
def embedding_delays(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(embedding_mod, "time", types.SimpleNamespace(sleep=delays.append))
    return delays


def _patch_image_http(monkeypatch, script):
    """Script httpx.AsyncClient.request; `script` entries are (status, headers, payload)."""
    state: dict = {"calls": [], "timeouts": [], "constructed": 0}

    class _Client:
        def __init__(self, **kwargs):
            state["constructed"] += 1
            state["timeouts"].append(kwargs.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            state["calls"].append({"method": method, "url": url, **kwargs})
            status, headers, payload = script.pop(0)
            return _resp(status, headers=headers, payload=payload, url=url)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return state


def _patch_embedding_post(monkeypatch, script):
    state: dict = {"calls": [], "timeouts": []}

    def _fake_post(url, headers=None, json=None, timeout=None):
        state["calls"].append({"url": url, "json": json})
        state["timeouts"].append(timeout)
        status, headers_, payload = script.pop(0)
        return _resp(status, headers=headers_, payload=payload, url=url)

    monkeypatch.setattr(httpx, "post", _fake_post)
    return state


def _stub_image_budget(monkeypatch):
    """Keep the daily-budget DB rows out of these tests; capture the outcomes."""
    finished: list[tuple] = []
    monkeypatch.setattr(image_budget_mod, "reserve_image_call", lambda provider, model: (7, True))
    monkeypatch.setattr(
        image_budget_mod,
        "finish_image_call",
        lambda usage_id, status, error_kind="": finished.append((usage_id, status, error_kind)),
    )
    return finished


def _provider() -> ImageProvider:
    return ImageProvider(
        base_url="https://provider.invalid/v1",
        api_key="test-key",
        model="test-model",
        size="1024x1024",
        quality="standard",
        timeout=7.5,
    )


_OK_IMAGE = (200, {}, {"data": [{"b64_json": _png_b64()}]})


# ----------------------------------------------------------------- images: retry
@pytest.mark.asyncio
async def test_image_503_with_retry_after_is_honoured_then_succeeds(monkeypatch, image_delays):
    state = _patch_image_http(monkeypatch, [(503, {"Retry-After": "9"}, None), _OK_IMAGE])
    generated = await ImageProvider.generate.__wrapped__(_provider(), "prompt")

    assert image_delays == [9.0]
    assert len(state["calls"]) == 2
    assert all(t == 7.5 for t in state["timeouts"]), "the bounded timeout must stay on every attempt"
    assert generated.path.exists()


@pytest.mark.asyncio
async def test_image_429_retry_after_is_capped(monkeypatch, image_delays):
    _patch_image_http(monkeypatch, [(429, {"Retry-After": "3600"}, None), _OK_IMAGE])
    generated = await ImageProvider.generate.__wrapped__(_provider(), "prompt")

    assert image_delays == [min(3600.0, images_mod._RETRY_AFTER_CAP_S)]
    assert generated.path.exists()


@pytest.mark.asyncio
async def test_image_non_retryable_400_is_sent_once(monkeypatch, image_delays):
    state = _patch_image_http(monkeypatch, [(400, {}, None)])

    with pytest.raises(ProviderUnavailable):
        await ImageProvider.generate.__wrapped__(_provider(), "prompt")

    assert len(state["calls"]) == 1
    assert image_delays == []


@pytest.mark.asyncio
async def test_image_exhausted_503_raises_and_budget_records_failure(monkeypatch, image_delays):
    finished = _stub_image_budget(monkeypatch)
    state = _patch_image_http(monkeypatch, [(503, {}, None)] * images_mod._MAX_ATTEMPTS)
    provider = _provider()

    with pytest.raises(ProviderUnavailable):
        await provider.generate("prompt")  # full decorated path

    assert len(state["calls"]) == images_mod._MAX_ATTEMPTS
    assert image_delays == [0.5, 1.0], "exponential backoff when no Retry-After is sent"
    assert finished == [(7, "failed", "ProviderUnavailable")], "outcome recorded, not swallowed"


@pytest.mark.asyncio
async def test_generate_cover_degrades_to_none_after_persistent_503(monkeypatch, image_delays):
    _stub_image_budget(monkeypatch)
    _patch_image_http(monkeypatch, [(503, {}, None)] * images_mod._MAX_ATTEMPTS)
    monkeypatch.setattr(images_mod, "get_provider", lambda: _provider())

    assert await generate_cover("headline", "sub", "tech", {}) is None


@pytest.mark.asyncio
async def test_offline_images_never_touch_the_network(monkeypatch):
    def _never(*a, **kw):
        raise AssertionError("offline mode must not build an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _never)
    assert available() is False
    assert get_provider() is None
    assert await generate_cover("headline", "sub", "tech", {}) is None


# --------------------------------------------------------------- embedding: retry
@pytest.mark.asyncio
async def test_embedding_503_with_retry_after_is_honoured_then_succeeds(monkeypatch, embedding_delays):
    ok = (200, {}, {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})
    state = _patch_embedding_post(monkeypatch, [(503, {"Retry-After": "4"}, None), ok])
    embedder = OpenAIEmbedder("test-key", "model", 3)

    vectors = embedder.embed_many(["some text"])

    assert embedding_delays == [4.0]
    assert len(state["calls"]) == 2
    assert state["timeouts"][0] == embedding_mod._REQUEST_TIMEOUT_S
    assert vectors.shape == (1, 3)


@pytest.mark.asyncio
async def test_embedding_non_retryable_4xx_is_sent_once(monkeypatch, embedding_delays):
    state = _patch_embedding_post(monkeypatch, [(400, {}, None)])
    embedder = OpenAIEmbedder("test-key", "model", 3)

    with pytest.raises(ProviderUnavailable):
        embedder.embed_many(["some text"])

    assert len(state["calls"]) == 1
    assert embedding_delays == []


@pytest.mark.asyncio
async def test_embedding_exhausted_503_raises_instead_of_fabricating_vectors(monkeypatch, embedding_delays):
    state = _patch_embedding_post(monkeypatch, [(503, {}, None)] * embedding_mod._MAX_ATTEMPTS)
    embedder = OpenAIEmbedder("test-key", "model", 3)

    with pytest.raises(ProviderUnavailable):
        embedder.embed_many(["some text"])

    assert len(state["calls"]) == embedding_mod._MAX_ATTEMPTS
    assert embedding_delays == [0.5, 1.0]


def test_offline_embedding_provenance_is_local_and_deterministic():
    embedding_mod.reset_embedder()
    embedder = get_embedder()
    assert isinstance(embedder, LocalHashEmbedder)
    first = embedder.embed("один и тот же текст")
    second = embedder.embed("один и тот же текст")
    assert first.tolist() == second.tolist()
    embedding_mod.reset_embedder()


def test_openai_embedder_refuses_to_run_without_a_key():
    with pytest.raises(ProviderUnavailable):
        OpenAIEmbedder("", "model", 8)

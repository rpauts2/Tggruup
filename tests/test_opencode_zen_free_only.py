"""OPENCODE_ZEN_FREE_ONLY must actually stop Zen spending money.

The field was declared, documented in `.env.example` and read by one property
(`Settings.zen_configured`) that nothing called — so turning the dial changed
nothing, and the farm kept calling paid models. `OpenCodeZen` already had the
free-tier logic (`resolve_model(want_free_only=True)`), with no caller.

Both directions are pinned here, as the dead-knob rule requires:

* at the code default the provider asks for exactly the configured model and
  behaves as it did before the wiring;
* with the knob on, the same provider object — not a rebuilt one — resolves a
  `-free` catalogue model and refuses any paid one.

The second point is what makes this a call-time read: the panel rewrites `.env`
and resets the settings cache, but it does not rebuild the LLM chain, so a value
frozen in `__init__` would only take effect after a restart.
"""

from __future__ import annotations

import httpx
import pytest

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.llm.client import LLMClient
from tgfarm.llm.opencode import OpenCodeZen, free_only_setting

PAID = "claude-sonnet-4-6"
FREE = "kimi-k2-free"
CATALOGUE = [{"id": PAID}, {"id": FREE}, {"id": "glm-4.6"}]
OK = {"choices": [{"message": {"content": "ответ"}}], "content": [{"type": "text", "text": "ответ"}]}


class _Recorder:
    """Stands in for httpx.AsyncClient and remembers the model actually asked for."""

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


def _patch_httpx(monkeypatch, recorder: _Recorder) -> None:
    class _Fake:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return recorder

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)


def _provider_with_catalogue(monkeypatch, *, token: str = "sk-test", model: str = PAID) -> OpenCodeZen:
    """A Zen provider whose catalogue is known, so no model listing is needed."""
    provider = OpenCodeZen(token, model, 10.0)

    async def _listing(_token: str = "") -> list[dict]:
        return CATALOGUE

    monkeypatch.setattr(provider, "list_models", _listing)
    return provider


def _set_free_only(monkeypatch, value: bool) -> None:
    from tgfarm.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.get_settings(), "opencode_zen_free_only", value)


# ------------------------------------------------------------------ the read
@pytest.mark.asyncio
async def test_default_off_is_the_untouched_behaviour(monkeypatch):
    """With the knob at its declared default, the configured model is requested."""
    _set_free_only(monkeypatch, False)
    assert free_only_setting(False) is False
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)
    provider = _provider_with_catalogue(monkeypatch)

    text = await provider.complete("s", "p", json_mode=False, max_tokens=32)

    assert text == "ответ"
    assert recorder.calls, "the request must still go out at the default"
    assert recorder.calls[0]["json"]["model"] == PAID
    assert provider._resolved_model == PAID


@pytest.mark.asyncio
async def test_knob_on_restricts_the_same_provider_object(monkeypatch):
    """Turning it on moves the next call, without rebuilding the provider."""
    _set_free_only(monkeypatch, False)
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)
    provider = _provider_with_catalogue(monkeypatch)
    await provider.complete("s", "p", json_mode=False, max_tokens=32)
    assert recorder.calls[0]["json"]["model"] == PAID

    _set_free_only(monkeypatch, True)
    assert free_only_setting(False) is True
    await provider.complete("s", "p", json_mode=False, max_tokens=32)

    assert [call["json"]["model"] for call in recorder.calls] == [PAID, FREE], (
        "the knob was read once and frozen: the second call should have used a "
        "`-free` model on the very next request"
    )


@pytest.mark.asyncio
async def test_free_only_refuses_a_paid_model_instead_of_billing_it(monkeypatch):
    """No `-free` model resolvable means no request at all, not a paid fallback."""
    _set_free_only(monkeypatch, True)
    recorder = _Recorder(OK)
    _patch_httpx(monkeypatch, recorder)
    provider = _provider_with_catalogue(monkeypatch)

    async def _empty(_token: str = "") -> list[dict]:
        return []

    monkeypatch.setattr(provider, "list_models", _empty)

    with pytest.raises(ProviderUnavailable, match="free-only"):
        await provider.complete("s", "p", json_mode=False, max_tokens=32)
    assert recorder.calls == [], "a spend cap that still sends the request is not a cap"


@pytest.mark.asyncio
async def test_free_only_refuses_an_explicit_paid_model(monkeypatch):
    """A caller asking for a paid model by name does not get past the cap."""
    _set_free_only(monkeypatch, True)
    _patch_httpx(monkeypatch, _Recorder(OK))
    provider = _provider_with_catalogue(monkeypatch)

    with pytest.raises(ProviderUnavailable, match="free-only"):
        await provider.complete("s", "p", json_mode=False, max_tokens=32, model=PAID)


@pytest.mark.asyncio
async def test_free_cache_does_not_relist_the_catalogue_every_call(monkeypatch):
    """The resolved free model is reused; a cap must not cost a request per call."""
    _set_free_only(monkeypatch, True)
    _patch_httpx(monkeypatch, _Recorder(OK))
    provider = _provider_with_catalogue(monkeypatch)
    listings = []

    async def _counted(_token: str = "") -> list[dict]:
        listings.append(1)
        return CATALOGUE

    monkeypatch.setattr(provider, "list_models", _counted)

    await provider.complete("s", "p", json_mode=False, max_tokens=32)
    await provider.complete("s", "p", json_mode=False, max_tokens=32)
    assert len(listings) == 1


# -------------------------------------------------------------------- fallback
def test_unreadable_settings_keep_the_value_the_chain_was_built_with(monkeypatch):
    """A stubbed `get_settings` must not silently lift the cap off."""
    from types import SimpleNamespace

    import tgfarm.llm.opencode as zen

    monkeypatch.setattr(zen, "get_settings", lambda: SimpleNamespace())
    assert free_only_setting(True) is True
    assert free_only_setting(False) is False
    monkeypatch.setattr(zen, "get_settings", lambda: SimpleNamespace(opencode_zen_free_only="true"))
    assert free_only_setting(False) is True


# ----------------------------------------------------------------- chain wiring
def test_llm_chain_passes_the_knob_to_the_provider(monkeypatch):
    """`LLMClient` builds Zen with the setting, so a fresh process starts capped."""
    from tgfarm.core import settings as settings_mod

    _set_free_only(monkeypatch, True)
    monkeypatch.setattr(settings_mod.get_settings(), "llm_providers", "zen,offline")
    monkeypatch.setattr(settings_mod.get_settings(), "opencode_zen_token", "sk-test")

    provider = next(p for p in LLMClient()._providers if p.name == "opencode-zen")
    assert provider._free_only is True

    _set_free_only(monkeypatch, False)
    provider = next(p for p in LLMClient()._providers if p.name == "opencode-zen")
    assert provider._free_only is False

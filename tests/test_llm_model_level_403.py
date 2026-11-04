"""A consent refusal for one free model must not kill the whole gateway.

Live incident (2026-10-27..11-04): every draft came from the offline composer.
Root cause: ``unorouter`` answers 403 ``consent_required`` for the *first* free
model in the chain, and the client treated any 403 as a dead provider — so all
remaining free models were skipped for an hour, over and over.
"""

from __future__ import annotations

import asyncio

import httpx

from tgfarm.llm import client as mod


class _FakeHTTP:
    """httpx.AsyncClient stand-in that answers per requested model."""

    def __init__(self, responses: dict[str, tuple[int, dict]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):  # when replacing the class itself
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        model = str((json or {}).get("model"))
        self.calls.append(model)
        status, payload = self.responses.get(model, (500, {"error": "no fake"}))
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


_OK = (200, {"choices": [{"message": {"content": "тест"}}], "usage": {"completion_tokens": 1}})
_CONSENT = (403, {"error": {"code": "consent_required", "message": "needs consent for lf"}})


def test_consent_403_parks_only_the_model(monkeypatch):
    fake = _FakeHTTP({"first:free": _CONSENT, "second:free": _OK})
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    provider = mod._OpenAI(
        api_key="k",
        model="first:free,second:free",
        timeout=5.0,
        base_url="https://example.org/v1",
    )
    out = asyncio.run(provider.complete("sys", "prompt", json_mode=False, max_tokens=8))

    assert out == "тест"
    assert fake.calls[0] == "first:free"
    assert "second:free" in fake.calls
    # The provider is still usable: only the consenting model is parked.
    assert provider._down == 0.0
    assert "first:free" in provider._model_cooldown
    assert "second:free" not in provider._model_cooldown


def test_parked_model_is_skipped_on_the_next_call(monkeypatch):
    fake = _FakeHTTP({"first:free": _CONSENT, "second:free": _OK})
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    provider = mod._OpenAI(
        api_key="k", model="first:free,second:free", timeout=5.0, base_url="https://example.org/v1"
    )
    asyncio.run(provider.complete("sys", "p", json_mode=False, max_tokens=4))
    fake.calls.clear()
    asyncio.run(provider.complete("sys", "p", json_mode=False, max_tokens=4))
    assert fake.calls == ["second:free"]


def test_bad_key_still_demotes_the_whole_provider(monkeypatch):
    fake = _FakeHTTP({"first:free": (401, {"error": {"message": "invalid api key"}})})
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    provider = mod._OpenAI(
        api_key="k", model="first:free,second:free", timeout=5.0, base_url="https://example.org/v1"
    )
    try:
        asyncio.run(provider.complete("sys", "p", json_mode=False, max_tokens=4))
    except mod.ProviderUnavailable:
        pass
    else:  # pragma: no cover - the call must fail
        raise AssertionError("a 401 must raise ProviderUnavailable")
    assert provider._down > 0.0
    assert fake.calls == ["first:free"]  # no pointless walk over dead-key models

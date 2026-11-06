"""Offline transport and client regressions: no credentials or external calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.errors import ProviderRateLimited, ProviderUnavailable
from tgfarm.llm import reliability
from tgfarm.llm.client import LLMClient, _OpenAI


class Provider:
    name = "fake"
    _model = "test"

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def available(self):
        return True

    async def complete(self, *args, **kwargs):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome


def client(provider, retries=3, timeout=10):
    instance = object.__new__(LLMClient)
    instance._settings = SimpleNamespace(llm_timeout_s=timeout, llm_max_retries=retries)
    instance._providers = [provider]
    instance._resolved = None
    return instance


@pytest.fixture(autouse=True)
def no_rate_wait(monkeypatch):
    async def take():
        pass

    monkeypatch.setattr("tgfarm.llm.client.RATE.llm.take", take)


@pytest.fixture
def delays(monkeypatch):
    values = []

    async def sleep(delay):
        values.append(delay)

    monkeypatch.setattr(reliability.asyncio, "sleep", sleep)
    monkeypatch.setattr(reliability.random, "uniform", lambda *_: 0)
    return values


async def test_transient_success_has_provenance(delays):
    provider = Provider([ProviderUnavailable("503"), "ok"])
    response = await client(provider).complete_text("s", "p")
    assert response.text == "ok"
    assert response.provenance == "retry_succeeded"
    assert provider.calls == 2
    assert delays == [0.5]


async def test_bounded_failure_opens_and_recovers(delays):
    provider = Provider([ProviderUnavailable("503")] * 3 + ["recovered"])
    instance = client(provider, retries=8)
    with pytest.raises(ProviderUnavailable):
        await instance.complete_text("s", "p")
    assert provider.calls == 3
    with pytest.raises(ProviderUnavailable):
        await instance.complete_text("s", "p")
    assert provider.calls == 3
    instance._circuits[provider] = (3, 0, False)
    assert (await instance.complete_text("s", "p")).text == "recovered"
    assert instance._circuits[provider] == (0, 0, False)


async def test_retry_after_and_permanent_failure(delays):
    provider = Provider([ProviderRateLimited("429", retry_after=2), "ok"])
    await client(provider).complete_text("s", "p")
    assert delays == [2]
    provider = Provider([reliability.PermanentProviderError("400")])
    with pytest.raises(reliability.PermanentProviderError):
        await client(provider).complete_text("s", "p")
    assert provider.calls == 1


async def test_timeout_honors_total_deadline():
    event = asyncio.Event()
    provider = Provider([event.wait])
    with pytest.raises(ProviderUnavailable):
        await client(provider, timeout=0.01).complete_text("s", "p")
    assert provider.calls == 1


async def test_cancellation_releases_probe():
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def hang():
        entered.set()
        await blocker.wait()

    provider = Provider([hang])
    instance = client(provider)
    instance._circuits = {provider: (3, 0, False)}
    task = asyncio.create_task(instance.complete_text("s", "p", offline_fallback="not success"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instance._circuits[provider][2] is False


async def test_invalid_json_fallback_is_not_online(delays):
    provider = Provider(["not json"] * 3)
    parsed, response = await client(provider).complete_json("s", "p", offline_fallback={"draft": True})
    assert parsed == {"draft": True}
    assert response.provider == "offline"
    assert response.provenance == "deterministic_fallback"
    assert provider.calls == 3


async def test_open_circuit_does_not_starve_backup():
    broken, backup = Provider([]), Provider(["backup"])
    instance = client(broken)
    instance._providers.append(backup)
    instance._circuits = {broken: (3, float("inf"), False)}
    assert (await instance.complete_text("s", "p")).text == "backup"
    assert broken.calls == 0


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_real_transport_permanent_errors_are_not_retried(monkeypatch, status):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "TEST_SECRET_SENTINEL"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    provider = _OpenAI("fake", "model", 1)
    with pytest.raises(reliability.PermanentProviderError) as error:
        await client(provider).complete_text("s", "p")
    assert len(calls) == 1
    assert "TEST_SECRET_SENTINEL" not in str(error.value)


async def test_real_transport_honors_retry_after(monkeypatch, delays):
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    response = await client(_OpenAI("fake", "model", 1)).complete_text("s", "p")
    assert response.text == "ok"
    assert delays == [0]
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [
    reliability.PermanentProviderError("400"),
    ProviderUnavailable("503"),
    ProviderRateLimited("429", retry_after=120),
])
async def test_failed_primary_does_not_starve_backup(failure, delays):
    primary, backup = Provider([failure] * 3), Provider(["backup"])
    instance = client(primary)
    instance._providers.append(backup)
    response = await instance.complete_text("s", "p")
    assert response.text == "backup"
    assert primary.calls == backup.calls == 1
    assert delays == []


def test_retry_after_formats():
    assert reliability.retry_after("12") == 12
    assert reliability.retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert reliability.retry_after("nan") == 60
    assert reliability.retry_after("bad") == 60

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
    clock = [reliability.time.monotonic()]

    async def sleep(delay):
        values.append(delay)
        clock[0] += delay

    monkeypatch.setattr(reliability, "time", SimpleNamespace(monotonic=lambda: clock[0]))
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


async def test_primary_cooldown_survives_failed_backup(delays):
    primary = Provider([ProviderRateLimited("429", retry_after=120), "too early"])
    backup = Provider([ProviderUnavailable("503"), "backup"])
    instance = client(primary)
    instance._providers.append(backup)
    assert (await instance.complete_text("s", "p")).text == "backup"
    assert primary.calls == 1


async def test_half_open_claim_during_availability_uses_backup():
    primary, backup = Provider(["wrong"]), Provider(["backup"])
    instance = client(primary)
    instance._providers.append(backup)
    instance._circuits = {primary: (3, 0, False)}

    async def concurrent_claim():
        # Another task claims the probe while the availability I/O is pending.
        instance._circuits[primary] = (3, 0, True)
        return True

    primary.available = concurrent_claim
    assert (await instance.complete_text("s", "p")).text == "backup"
    assert primary.calls == 0


async def test_expired_deadline_does_not_probe_backup():
    blocker = asyncio.Event()
    primary, backup = Provider([blocker.wait]), Provider(["backup"])
    instance = client(primary, timeout=0.01)
    instance._providers.append(backup)
    probes = []

    async def slow_probe():
        probes.append(True)
        await asyncio.sleep(0.1)
        return True

    backup.available = slow_probe
    with pytest.raises(ProviderUnavailable):
        await instance.complete_text("s", "p")
    assert probes == []


async def test_ollama_permanent_error_is_secret_safe_and_not_retried(monkeypatch, delays):
    from tgfarm.llm.client import _Ollama

    calls = []

    async def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={})
        calls.append(request)
        return httpx.Response(400, text="TEST_SECRET_SENTINEL")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    provider = _Ollama("http://localhost:11434", "model", 1)
    with pytest.raises(reliability.PermanentProviderError) as error:
        await client(provider).complete_text("s", "p")
    assert len(calls) == 1
    assert "TEST_SECRET_SENTINEL" not in str(error.value)


async def test_permanent_backup_does_not_abort_primary_retry(delays):
    primary = Provider([ProviderUnavailable("503"), "recovered"])
    backup = Provider([reliability.PermanentProviderError("400")])
    instance = client(primary)
    instance._providers.append(backup)
    response = await instance.complete_text("s", "p")
    assert response.text == "recovered"
    assert primary.calls == 2
    assert backup.calls == 1


def test_retry_after_formats():
    assert reliability.retry_after("12") == 12
    assert reliability.retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert reliability.retry_after("nan") == 60
    assert reliability.retry_after("bad") == 60

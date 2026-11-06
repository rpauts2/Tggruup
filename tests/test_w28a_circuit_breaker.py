"""The circuit breaker: stop hammering a provider that already said no.

36 hours of the live log say what happens without one: 6 437 503s and 8 506
offline fallbacks from the same unreachable free-tier gateway, because the
only parking the old client had was `_down` on 429/401 — a 503 was retried
forever. The breaker is what actually starves that loop: after a few
consecutive failed *calls* it refuses to call at all, and the stage falls to
the deterministic composer in about zero seconds instead of 42 minutes.

The rule these tests defend is the project's standing one: a refusal must
never be reported as a success. An open breaker produces
`provider=offline`, never a model name.

Fake clock, injected sleeper, no network, no real sleeping.
"""

from __future__ import annotations

import pytest

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.llm import resilience as res
from tgfarm.llm.client import LLMClient, get_stats


@pytest.fixture(autouse=True)
def _clean_resilience():
    res.reset_resilience_state()
    yield
    res.reset_resilience_state()


class _Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleeper(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.t += float(seconds)


def _install(clock: _Clock) -> None:
    res.set_clock(clock.now)
    res.set_sleeper(clock.sleeper)


class _Scripted:
    name = "scripted"
    supports_deadline = True

    def __init__(self, results: list[object], model: str = "m1", models: int = 12) -> None:
        # A full twelve-model catalogue, like the live `LLM_MODEL`, so the
        # per-stage budget (one sweep) is never the thing that stops the loop
        # in these tests: only the breaker is.
        self._models = [f"{model}-{i}" for i in range(models)]
        self._model = model
        self.results = list(results)
        self.calls = 0

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, *, json_mode, max_tokens, model=None, deadline=None):
        self.calls += 1
        slot = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(slot, BaseException):
            raise slot
        return slot


def _client_with(*providers) -> LLMClient:
    client = LLMClient()
    client._providers = list(providers)
    return client


def _fails(count: int) -> list[object]:
    return [ProviderUnavailable("503 providers busy")] * count


# ------------------------------------------------------------- unit rules ----
def test_the_breaker_opens_on_exactly_the_threshold():
    breaker = res.CircuitBreaker(threshold=3, cooldown_s=60.0, max_cooldown_s=600.0)
    res.set_clock(lambda: 100.0)

    assert breaker.on_failure("p") is False
    assert breaker.on_failure("p") is False
    assert breaker.allow("p") is True
    assert breaker.on_failure("p") is True
    assert breaker.allow("p") is False
    assert breaker.state("p") == res.OPEN


def test_a_success_resets_the_consecutive_counter():
    """Consecutive is the word: one good call in four buys four more chances."""
    breaker = res.CircuitBreaker(threshold=3, cooldown_s=60.0, max_cooldown_s=600.0)
    res.set_clock(lambda: 100.0)
    breaker.on_failure("p")
    breaker.on_failure("p")
    breaker.on_success("p")
    assert breaker.state("p") == res.CLOSED
    assert breaker.on_failure("p") is False


def test_the_cool_off_then_admits_exactly_one_probe():
    """Half-open means one request to test the water, not the whole stage back."""
    clock = _Clock(start=100.0)
    _install(clock)
    breaker = res.CircuitBreaker(threshold=1, cooldown_s=60.0, max_cooldown_s=600.0)

    assert breaker.on_failure("p") is True
    assert breaker.allow("p") is False

    clock.t += 59.0
    assert breaker.allow("p") is False, "re-opened before the cool-off ended"

    clock.t += 2.0
    assert breaker.state("p") == res.HALF_OPEN
    assert breaker.allow("p") is True
    assert breaker.allow("p") is False, "half-open admitted more than one probe"


def test_a_failed_probe_re_opens_with_a_doubled_cool_off():
    clock = _Clock(start=100.0)
    _install(clock)
    breaker = res.CircuitBreaker(threshold=1, cooldown_s=60.0, max_cooldown_s=600.0)

    breaker.on_failure("p")
    first = breaker.retry_in("p")
    assert first == pytest.approx(60.0)

    clock.t += 61.0
    assert breaker.state("p") == res.HALF_OPEN
    breaker.on_failure("p")
    assert breaker.retry_in("p") == pytest.approx(120.0)

    for _ in range(6):
        clock.t += breaker.retry_in("p") + 1.0
        breaker.state("p")
        breaker.on_failure("p")
    assert breaker.retry_in("p") <= 600.0, "the cool-off grew past its ceiling"


def test_breaker_state_is_per_key():
    breaker = res.CircuitBreaker(threshold=1, cooldown_s=60.0, max_cooldown_s=600.0)
    res.set_clock(lambda: 100.0)
    breaker.on_failure("broken")
    assert breaker.allow("broken") is False
    assert breaker.allow("healthy") is True


def test_the_snapshot_reports_the_state_it_claims():
    breaker = res.CircuitBreaker(threshold=2, cooldown_s=45.0, max_cooldown_s=90.0)
    res.set_clock(lambda: 500.0)
    breaker.on_failure("compatible")
    breaker.on_failure("compatible")
    snap = breaker.snapshot()["compatible"]
    assert snap["state"] == res.OPEN
    assert snap["failures"] == 2
    assert snap["cooldown_s"] == pytest.approx(45.0)
    assert snap["retry_in_s"] == pytest.approx(45.0)


# ------------------------------------------------------- client integration --
@pytest.mark.asyncio
async def test_the_chain_stops_calling_a_provider_that_kept_failing(monkeypatch):
    """Item 2, mutant "breaker never opens".

    Without a breaker this is `llm_max_retries` requests forever; with one the
    provider is called exactly `threshold` times and then refused.
    """
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(_fails(50))
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)

    response = await client.complete_text("s", "p", offline_fallback="offline text")

    assert response.provider == "offline"
    assert provider.calls == res.BREAKER.threshold, provider.calls
    assert "breaker_open" in response.offline_reason
    assert response.is_model_generation is False


@pytest.mark.asyncio
async def test_an_open_breaker_shortcuts_without_a_single_request(monkeypatch):
    """The doomed call is not even attempted, and the reason says who refused."""
    clock = _Clock()
    _install(clock)
    provider = _Scripted(_fails(50))
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)

    await client.complete_text("s", "p", offline_fallback="x")
    calls_after_first = provider.calls

    blocked_before = get_stats().get("scripted", {}).get("breaker_blocked", 0.0)
    response = await client.complete_text("s", "p", offline_fallback="x")

    assert provider.calls == calls_after_first, "an open breaker still made a request"
    assert response.attempts == 0
    assert get_stats()["scripted"]["breaker_blocked"] - blocked_before >= 1.0


@pytest.mark.asyncio
async def test_the_next_provider_in_the_chain_still_gets_traffic(monkeypatch):
    """A breaker is per provider, so one dead gateway must not offline the farm."""
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    dead = _Scripted(_fails(50), model="dead-model")
    dead.name = "dead"
    alive = _Scripted(["настоящий текст"], model="alive-model")
    alive.name = "alive"
    client = _client_with(dead, alive)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)

    response = await client.complete_text("s", "p", offline_fallback="x")

    assert response.provider == "alive"
    assert response.model == "alive-model"
    assert response.is_model_generation is True
    assert dead.calls == res.BREAKER.threshold


@pytest.mark.asyncio
async def test_after_the_cool_off_one_probe_reopens_the_chain(monkeypatch):
    """Recovery is by evidence: the provider that answers closes the breaker."""
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted([*_fails(res.BREAKER.threshold), "ок после восстановления"])
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)

    first = await client.complete_text("s", "p", offline_fallback="x")
    assert first.provider == "offline"

    clock.t += res.BREAKER.retry_in("scripted") + 1.0
    second = await client.complete_text("s", "p", offline_fallback="x")

    assert second.provider == "scripted"
    assert second.is_model_generation is True
    assert res.BREAKER.state("scripted") == res.CLOSED


@pytest.mark.asyncio
async def test_a_tripped_breaker_counts_itself_in_the_stats(monkeypatch):
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(_fails(50))
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)

    trips_before = res.BREAKER.trips
    await client.complete_text("s", "p", offline_fallback="x")

    assert res.BREAKER.trips - trips_before == 1


@pytest.mark.asyncio
async def test_a_rate_limit_instruction_is_not_double_punished(monkeypatch):
    """`Retry-After: 30` already parked the provider; the breaker must still be
    the thing that stops the loop when the wait is longer than our budget."""
    from tgfarm.core.errors import ProviderRateLimited

    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted([ProviderRateLimited("429", retry_after=5.0)] * 50)
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)

    response = await client.complete_text("s", "p", offline_fallback="x")

    assert provider.calls == res.BREAKER.threshold
    # every wait is the told number, never our guess, and never after the trip
    assert clock.slept and set(clock.slept) == {5.0}, clock.slept
    assert "breaker_open" in response.offline_reason


@pytest.mark.asyncio
async def test_the_breaker_never_reports_a_model_it_did_not_call(monkeypatch):
    """The honesty half of item 2: an open breaker is not a generation."""
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(_fails(50), model="gemini-flash-latest:free")
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 50, raising=False)

    await client.complete_text("s", "p", offline_fallback="x")
    payload, response = await client.complete_json("s", "p", offline_fallback={"hook": "x"})

    assert isinstance(payload, dict)
    assert response.provider == "offline"
    assert response.model == "none"
    assert response.is_model_generation is False
    assert "gemini" not in str(response.model)

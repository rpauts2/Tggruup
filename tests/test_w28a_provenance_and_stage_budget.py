"""Provenance that reports the failover, and a per-stage call budget.

Two live-log facts drive this file. First: 4 039 draft lines in 36 hours,
every single one `provider=offline` — not 42 % of drafts, all of them — while
the same window logged 20 784 `llm provider: compatible` lines. An operator
reading that cannot tell an unmeasured thing from a measured one, and the
standing rule of this project is that they must never have to.

Second: 8 506 offline fallbacks burned tens of thousands of doomed HTTP
requests against an `AI_DAILY_BUDGET=2000` that never came close to filling,
because `llm/policy.py` only charges calls that *produced text*. The budget
below is the missing view of that same ledger — attempts spent — not a second
one an operator has to remember.

Fake clock, injected sleeper, no network.
"""

from __future__ import annotations

import pytest

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.llm import resilience as res
from tgfarm.llm.client import (
    OFFLINE_MODEL,
    OFFLINE_PROVIDER,
    LLMClient,
    LLMResponse,
    stage_scope,
)


@pytest.fixture(autouse=True)
def _clean_resilience():
    res.reset_resilience_state()
    yield
    res.reset_resilience_state()


class _Clock:
    def __init__(self, start: float = 50_000.0) -> None:
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


def _client_with(*providers, budget: int = 2000) -> LLMClient:
    client = LLMClient()
    client._providers = list(providers)
    client._settings.ai_daily_budget = budget
    return client


# ------------------------------------------------- provenance of a failover --
@pytest.mark.asyncio
async def test_the_provider_that_answered_is_the_one_reported(monkeypatch):
    """Item 3: a fallback to another provider must be visible, not silent."""
    _install(_Clock())
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    first = _Scripted([ProviderUnavailable("gateway busy")], model="a")
    first.name = "compatible"
    second = _Scripted(["второй провайдер"], model="b")
    second.name = "compatible2"
    client = _client_with(first, second)
    monkeypatch.setattr(client._settings, "llm_max_retries", 3, raising=False)

    response = await client.complete_text("s", "p", offline_fallback="x")

    assert response.provider == "compatible2", "the answering provider is hidden"
    assert response.model == "b"
    assert response.is_model_generation is True
    assert response.attempts == 2
    assert any(step.startswith("compatible:") for step in response.tried)
    assert any(step.startswith("compatible2:") for step in response.tried)


@pytest.mark.asyncio
async def test_a_model_override_is_reported_as_the_model_that_ran(monkeypatch):
    _install(_Clock())
    provider = _Scripted(["текст"], model="default-model")
    client = _client_with(provider)

    response = await client.complete_text("s", "p", offline_fallback="x", model="critic-model")

    assert response.model == "critic-model"


@pytest.mark.asyncio
async def test_the_json_pair_contract_survives_the_offline_path(monkeypatch):
    """Callers unpack `payload, response`; an offline return of a bare response
    is a TypeError in every stage, which is how a fallback becomes a crash."""
    _install(_Clock())
    provider = _Scripted([ProviderUnavailable("busy")], model="m")
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 2, raising=False)

    payload, response = await client.complete_json("s", "p", offline_fallback={"hook": "готово"})

    assert payload == {"hook": "готово"}
    assert response.provider == OFFLINE_PROVIDER
    assert response.model == OFFLINE_MODEL


@pytest.mark.asyncio
async def test_a_malformed_json_body_is_not_reported_as_a_generation(monkeypatch):
    """Prose where an object was required is a failed call, honestly offline."""
    _install(_Clock())
    provider = _Scripted(["это не json вовсе"], model="m")
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 2, raising=False)

    payload, response = await client.complete_json("s", "p", offline_fallback={"hook": "x"})

    assert response.provider == OFFLINE_PROVIDER
    assert response.is_model_generation is False
    assert payload == {"hook": "x"}


def test_an_offline_response_carrying_a_model_name_is_corrected(caplog):
    """Item 3's honesty rule, asserted on the object itself.

    `s4_generation` stores `generation_model = response.model or ...`, so an
    offline draft that carries `gemini-flash-latest:free` would be an
    unmeasured thing recorded as a measured one. The dataclass refuses it.
    """
    with caplog.at_level("WARNING"):
        response = LLMResponse("текст", OFFLINE_PROVIDER, "gemini-flash-latest:free", 12)

    assert response.model == OFFLINE_MODEL
    assert response.is_model_generation is False
    assert "offline result carrying model=" in caplog.text


def test_a_real_generation_is_never_marked_offline():
    response = LLMResponse("текст", "compatible", "gemini-flash-latest:free", 12)
    assert response.model == "gemini-flash-latest:free"
    assert response.is_model_generation is True
    assert not response.offline_reason


@pytest.mark.asyncio
async def test_the_offline_reason_names_the_guard_that_refused(monkeypatch):
    """`caplog` is the operator's only window: the reason must be in it."""
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted([ProviderUnavailable("503 providers busy")] * 30, model="m")
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 30, raising=False)

    await client.complete_text("s", "p", offline_fallback="x")  # trips the breaker
    response = await client.complete_text("s", "p", offline_fallback="x")

    assert response.provider == OFFLINE_PROVIDER
    assert response.offline_reason.startswith("breaker_open")
    assert response.attempts == 0


# ---------------------------------------------------- per-stage call budget --
@pytest.mark.asyncio
async def test_one_stage_cannot_spend_the_whole_cycle_on_doomed_calls(monkeypatch):
    """Item 4, mutant "the call budget is not enforced".

    The allowance is one sweep of the configured catalogue: with twelve models
    a stage may prove all twelve unusable, and the thirteenth attempt is
    refused. The live stage made thousands.
    """
    _install(_Clock())
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(["текст"] * 30, model="m", models=12)
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 1, raising=False)

    with stage_scope("s4_generate"):
        results = [await client.complete_text("s", "p", offline_fallback="x") for _ in range(15)]

    assert provider.calls == 12, f"the stage spent {provider.calls} attempts of its 12"
    assert res.BUDGET.spent("s4_generate") == 12
    refused = [r for r in results if r.provider == OFFLINE_PROVIDER]
    assert len(refused) == 3
    assert all("budget spent" in r.offline_reason for r in refused), refused[-1].offline_reason
    assert res.BUDGET.denied() == 3


@pytest.mark.asyncio
async def test_the_stage_bucket_is_per_stage_not_global(monkeypatch):
    _install(_Clock())
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(["текст"] * 30, model="m", models=12)
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 1, raising=False)

    with stage_scope("s5_quality"):
        for _ in range(12):
            await client.complete_text("s", "p", offline_fallback="x")
    with stage_scope("s8_analytics"):
        after = await client.complete_text("s", "p", offline_fallback="x")

    assert after.provider == "scripted", "a starved stage starved its neighbours too"
    assert res.BUDGET.spent("s8_analytics") == 1


@pytest.mark.asyncio
async def test_the_stage_budget_cooperates_with_ai_daily_budget(monkeypatch):
    """One operator knob bounds both views of the ledger.

    `AI_DAILY_BUDGET=2` means two online calls today, whoever asks for them:
    the stage cap (12) may not buy a third, and it may not buy one per stage
    either.
    """
    _install(_Clock())
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(["текст"] * 30, model="m", models=12)
    client = _client_with(provider, budget=2)
    monkeypatch.setattr(client._settings, "llm_max_retries", 1, raising=False)

    with stage_scope("s4_generate"):
        await client.complete_text("s", "p", offline_fallback="x")
        await client.complete_text("s", "p", offline_fallback="x")
        with stage_scope("s5_quality"):
            third = await client.complete_text("s", "p", offline_fallback="x")

    assert provider.calls == 2, provider.calls
    assert third.provider == OFFLINE_PROVIDER
    assert res.BUDGET.spent_all() == 2


@pytest.mark.asyncio
async def test_a_budget_of_zero_spends_nothing(monkeypatch):
    _install(_Clock())
    provider = _Scripted(["текст"] * 5, model="m")
    client = _client_with(provider, budget=0)
    monkeypatch.setattr(client._settings, "llm_max_retries", 1, raising=False)

    with stage_scope("s4_generate"):
        response = await client.complete_text("s", "p", offline_fallback="x")

    assert provider.calls == 0
    assert response.provider == OFFLINE_PROVIDER
    assert "budget" in response.offline_reason


def test_the_allowance_is_one_catalogue_sweep_within_its_bounds():
    ledger = res.AttemptBudget()
    res.set_clock(lambda: 1.0)
    # A twelve-model catalogue: exactly one sweep.
    assert ledger.stage_allowance("s4_generate", daily_budget=2000, model_count=12) == 12
    # A single-model provider still gets the floor of a few tries.
    assert ledger.stage_allowance("s4_generate", daily_budget=2000, model_count=1) == 4
    # And no stage is ever allowed past the hard ceiling, however big the list.
    assert ledger.stage_allowance("s4_generate", daily_budget=2000, model_count=400) == 24
    # The day ceiling composes: the smaller view wins.
    assert ledger.stage_allowance("s4_generate", daily_budget=3, model_count=12) == 3


def test_the_ledger_rolls_over_on_a_new_day():
    ledger = res.AttemptBudget()
    day = ["20260101"]
    ledger._today = lambda: day[0]  # type: ignore[method-assign]
    assert ledger.try_spend("s4_generate", daily_budget=1, model_count=12) is True
    assert ledger.try_spend("s4_generate", daily_budget=1, model_count=12) is False
    day[0] = "20260102"
    assert ledger.try_spend("s4_generate", daily_budget=1, model_count=12) is True, "new day"
    assert ledger.spent("s4_generate") == 1


@pytest.mark.asyncio
async def test_an_unnamed_stage_still_gets_its_own_bucket(monkeypatch):
    """`pipeline.py` does not wrap stages yet.

    The default bucket is `unknown`, which still bounds: an unwrapped caller
    gets a cycle-wide allowance rather than no allowance at all. Fail-safe in
    the direction of spending less.
    """
    _install(_Clock())
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 600.0)
    provider = _Scripted(["текст"] * 30, model="m", models=12)
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 1, raising=False)

    for _ in range(14):
        await client.complete_text("s", "p", offline_fallback="x")

    assert res.BUDGET.spent("unknown") == 12
    assert provider.calls == 12

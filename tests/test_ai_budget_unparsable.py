"""A 200 whose body could not be parsed still cost money — charge the reservation.

`_obtain` treats "provider answered, body is not JSON" as a failed attempt and
falls back to the deterministic composer with `provider=offline`. Call sites
settled the reservation with `used = response.provider != "offline"`, which
released it: the provider generated (and billed) tokens, yet the daily cap never
saw the call, so a farm stuck on a chatty model could spend past its budget.

The fallback must stay offline — that provenance rule is not relaxed — but the
reservation has to be charged, and it must not be reported as a used draft.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from tgfarm.db.models import AIUsage
from tgfarm.db.session import session_scope
from tgfarm.llm import policy
from tgfarm.llm import resilience as res
from tgfarm.llm.client import LLMClient


@pytest.fixture(autouse=True)
def _clean_state():
    res.reset_resilience_state()
    with session_scope() as session:
        session.execute(delete(AIUsage))
    yield
    res.reset_resilience_state()
    with session_scope() as session:
        session.execute(delete(AIUsage))


class _AnswersProse:
    """A live provider: HTTP 200, tokens billed, no JSON object in the body."""

    name = "scripted"
    supports_deadline = False
    _model = "scripted-1"

    def __init__(self) -> None:
        self.calls = 0

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, *, json_mode, max_tokens, model=None):
        self.calls += 1
        return "Sure! Here is my answer in plain prose, not an object."


class _Refuses:
    """A provider that never answered: nothing was billed."""

    name = "scripted"
    supports_deadline = False
    _model = "scripted-1"

    def __init__(self) -> None:
        self.calls = 0

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, *, json_mode, max_tokens, model=None):
        self.calls += 1
        raise TimeoutError("connect timeout")


def _client(provider) -> LLMClient:
    client = LLMClient()
    client._providers = [provider]
    client._settings = SimpleNamespace(
        llm_max_retries=1,
        llm_call_budget_s=5.0,
        ai_daily_budget=5,
    )
    return client


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        online=True,
        ai_policy="always",
        provider_chain=["scripted", "offline"],
        ai_daily_budget=5,
    )


async def test_unparsable_response_keeps_the_reservation_charged(monkeypatch):
    monkeypatch.setattr(policy, "get_settings", _settings)
    client = _client(_AnswersProse())
    decision = policy.reserve("s4_draft", purpose="unparsable")
    assert decision.allowed is True

    parsed, response = await client.complete_json(
        "system", "prompt", max_tokens=64, offline_fallback={"hook": ""}
    )

    assert parsed == {"hook": ""}
    assert response.provider == "offline", "the fallback must not claim a model"
    assert response.is_model_generation is False

    # The settlement every call site performs.
    policy.finalize(
        decision.usage_id,
        provider=response.audit_provider,
        model=response.audit_model,
        latency_ms=response.latency_ms,
        used=response.is_model_generation,
        billed=response.billed,
    )

    assert policy.charged_today() == 1, (
        "the provider answered and billed; releasing the reservation spends past the cap"
    )
    with session_scope() as session:
        row = session.get(AIUsage, decision.usage_id)
        assert row is not None
        assert row.charged is True
        assert row.used is False, "an unusable answer is not a used draft"
        assert row.reason == "billed:unusable"
        assert row.provider == "scripted"
        assert row.model == "scripted-1"


async def test_a_provider_that_never_answered_still_releases(monkeypatch):
    monkeypatch.setattr(policy, "get_settings", _settings)
    client = _client(_Refuses())
    decision = policy.reserve("s4_draft", purpose="timeout")
    assert decision.allowed is True

    _, response = await client.complete_json(
        "system", "prompt", max_tokens=64, offline_fallback={"hook": ""}
    )

    policy.finalize(
        decision.usage_id,
        provider=response.audit_provider,
        model=response.audit_model,
        latency_ms=response.latency_ms,
        used=response.is_model_generation,
        billed=response.billed,
    )

    assert response.billed is False
    assert policy.charged_today() == 0
    with session_scope() as session:
        row = session.get(AIUsage, decision.usage_id)
        assert row is not None
        assert row.charged is False
        assert row.reason == "fallback:offline"

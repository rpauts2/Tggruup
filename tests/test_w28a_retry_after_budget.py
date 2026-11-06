"""Retry-After is honoured, backoff is bounded, and one call cannot overran.

The live farm answered 6 437 `compatible 503` in 36 hours and logged
`stage s4_generate failed after 2 527 331ms` (42 min median) while doing it.
Nothing in the old client read the `Retry-After` header, and the only bound on
a call was `llm_timeout_s` applied per *request* — multiplied by twelve
catalogue models and three client-level retries. These tests pin the two things
that replaced it: the told number wins over the guessed one, and a total
wall-clock budget wins over both.

No network (the session conftest makes one a failure) and no real sleeping: the
clock and the sleeper are injected, so every assertion is about the *arguments*
handed to the sleeper or to httpx, never about elapsed wall time.
"""

from __future__ import annotations

import httpx
import pytest

from tgfarm.core.errors import ProviderRateLimited, ProviderUnavailable
from tgfarm.llm import resilience as res
from tgfarm.llm.client import (
    LLMClient,
    _OpenAI,
    _request_timeout,
    _retry_after,
    _server_error,
    get_stats,
)


@pytest.fixture(autouse=True)
def _clean_resilience():
    """Own process state: a neighbour's open breaker is not our starting point."""
    res.reset_resilience_state()
    yield
    res.reset_resilience_state()


class _Clock:
    """A monotonic clock the test drives, plus the sleeps it was asked to take."""

    def __init__(self, start: float = 1_000.0) -> None:
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


class _Transport:
    """Stands in for `httpx.AsyncClient`; replays scripted responses."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.timeouts: list[object] = []
        self._index = 0

    def make_client(self, *args, **kwargs):
        """`httpx.AsyncClient(...)` must land here as a factory returning a client."""
        self.timeouts.append(kwargs.get("timeout"))
        transport = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                slot = transport.responses[min(transport._index, len(transport.responses) - 1)]
                transport._index += 1
                return httpx.Response(
                    slot["status"],
                    json=slot.get("json", {"error": "nope"}),
                    headers=slot.get("headers") or {},
                    request=httpx.Request("POST", url),
                )

        return _Client()


def _patch_httpx(monkeypatch, transport: _Transport) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", transport.make_client)


def _response(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": "x"}, headers=headers or {})


# --------------------------------------------------------------- header rules
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("0", 0.0),
        ("7", 7.0),
        ("2.5", 2.5),
        ("not-a-number", None),
        ("-4", None),
        ("7; select=20", None),
    ],
)
def test_retry_after_delta_form_is_parsed(header, expected):
    got = res.parse_retry_after(header)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_retry_after_http_date_form_is_parsed_as_a_wait():
    """A gateway that means "until the window rolls over" must not be retried early."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) + timedelta(seconds=42)
    got = res.parse_retry_after(format_datetime(when, usegmt=True))
    assert got is not None
    assert 38.0 <= got <= 44.0


def test_retry_after_in_the_past_is_zero_not_negative():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) - timedelta(seconds=90)
    assert res.parse_retry_after(format_datetime(when, usegmt=True)) == 0.0


def test_header_absent_falls_back_to_the_default_only():
    assert _retry_after(_response(503), default=None) is None
    assert _retry_after(_response(503), default=60.0) == pytest.approx(60.0)


# --------------------------------------------------------- told vs guessed
@pytest.mark.asyncio
async def test_a_429_retry_after_header_beats_the_hardcoded_guess(monkeypatch):
    """Single-sided mutant for item 1: "keep retry_after=60 regardless".

    The old `_OpenAI` line was `ProviderRateLimited(..., retry_after=60)` —
    always sixty, whatever the provider said. With `Retry-After: 7` the parked
    window must be seven seconds, not sixty.
    """
    transport = _Transport([{"status": 429, "headers": {"Retry-After": "7"}}])
    _patch_httpx(monkeypatch, transport)
    clock = _Clock()
    _install(clock)
    provider = _OpenAI("k", "m1", 60.0, base_url="https://api.example/v1")

    with pytest.raises(ProviderRateLimited) as caught:
        await provider.complete("s", "p", json_mode=False, max_tokens=32)

    assert caught.value.retry_after == pytest.approx(7.0)
    # and the candidate is parked for exactly the told window
    assert provider._model_cooldown["m1"] == pytest.approx(clock.now() + 7.0)


@pytest.mark.asyncio
async def test_a_503_retry_after_header_changes_the_exception_type(monkeypatch):
    """503 + `Retry-After` is a scheduling instruction, not bare unavailability."""
    transport = _Transport([{"status": 503, "headers": {"Retry-After": "11"}}])
    _patch_httpx(monkeypatch, transport)
    clock = _Clock()
    _install(clock)
    provider = _OpenAI("k", "m1", 60.0, base_url="https://api.example/v1")

    with pytest.raises(ProviderRateLimited) as caught:
        await provider.complete("s", "p", json_mode=False, max_tokens=32)

    assert caught.value.retry_after == pytest.approx(11.0)


def test_503_without_a_header_stays_a_plain_unavailability():
    exc = _server_error("compatible 503 on m1", _retry_after(_response(503), default=None))
    assert isinstance(exc, ProviderUnavailable)
    assert not isinstance(exc, ProviderRateLimited)


# --------------------------------------------------------- timeout clamping
def test_request_timeout_is_clamped_to_the_remaining_budget():
    """The mutant this kills: `timeout=self._timeout` restored everywhere."""
    assert _request_timeout(60.0, None) == 60.0
    clock = _Clock(start=500.0)
    res.set_clock(clock.now)
    # 12 s of budget left against a configured 60 s: the budget is the answer.
    assert _request_timeout(60.0, 512.0) == pytest.approx(12.0)
    # Never non-positive: httpx rejects it, and a zero timeout is not "hurry".
    assert _request_timeout(60.0, 400.0) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_a_doomed_walk_clamps_every_request_timeout(monkeypatch):
    """Twelve candidates used to mean twelve full 60 s timeouts in one call.

    That is the arithmetic behind `stage s4_generate failed after 2 815 575ms`:
    not a slow model, an unbounded walk. Every request now gets only what the
    call budget still has.
    """
    clock = _Clock()
    _install(clock)
    models = ",".join(f"m{i}" for i in range(12))
    transport = _Transport([{"status": 503, "json": {"error": "providers busy"}}] * 12)
    _patch_httpx(monkeypatch, transport)
    provider = _OpenAI("k", models, 60.0, base_url="https://api.example/v1")

    deadline = res.now() + 20.0
    with pytest.raises(ProviderUnavailable):
        await provider.complete("s", "p", json_mode=False, max_tokens=32, deadline=deadline)

    timeouts = [t for t in transport.timeouts if isinstance(t, (int, float))]
    assert timeouts, "the fake transport never saw a timeout"
    assert max(timeouts) <= 20.0
    assert len(timeouts) <= 12


@pytest.mark.asyncio
async def test_a_slow_walk_stops_at_the_deadline(monkeypatch):
    """A provider whose requests each eat seconds must not finish the catalogue."""
    clock = _Clock()
    _install(clock)
    provider = _OpenAI("k", "m1,m2,m3,m4,m5,m6", 60.0, base_url="https://api.example/v1")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            clock.t += 4.0  # every doomed request burns four seconds
            return httpx.Response(503, json={"error": "busy"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(ProviderUnavailable):
        await provider.complete("s", "p", json_mode=False, max_tokens=32, deadline=clock.now() + 12.0)

    assert clock.t - 1_000.0 <= 16.0, "the walk kept starting requests past its deadline"


# ------------------------------------------------------- client loop rules
class _ScriptedProvider:
    """A provider with no transport at all: only the loop's behaviour is under test."""

    name = "scripted"
    supports_deadline = True

    def __init__(self, results: list[object], model: str = "scripted-model") -> None:
        self._models = [model]
        self._model = model
        self.results = list(results)
        self.calls: list[dict] = []

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, *, json_mode, max_tokens, model=None, deadline=None):
        self.calls.append({"deadline": deadline, "json_mode": json_mode})
        slot = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(slot, BaseException):
            raise slot
        return slot


class _SlowFailingProvider(_ScriptedProvider):
    """Each attempt eats `seconds` of the fake clock, then refuses."""

    def __init__(self, results, model="scripted-model", seconds: float = 2.0) -> None:
        super().__init__(results, model)
        self.seconds = seconds

    async def complete(self, system, prompt, *, json_mode, max_tokens, model=None, deadline=None):
        self.calls.append({"deadline": deadline})
        await res.sleep(self.seconds)
        raise ProviderUnavailable("still busy")


def _client_with(provider) -> LLMClient:
    client = LLMClient()
    client._providers = [provider]
    return client


@pytest.mark.asyncio
async def test_the_loop_sleeps_the_told_wait_and_retries_once_more(monkeypatch):
    """Single-sided mutant: "sleep the ladder, then call again regardless of Retry-After"."""
    clock = _Clock()
    _install(clock)
    provider = _ScriptedProvider([ProviderRateLimited("nope", retry_after=7.0), "текст"])
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 3, raising=False)

    response = await client.complete_text("s", "p", offline_fallback="offline text")

    assert clock.slept == [7.0], clock.slept
    assert response.provider == "scripted"
    assert response.attempts == 2


@pytest.mark.asyncio
async def test_an_absurd_retry_after_is_refused_instead_of_slept(monkeypatch):
    """`Retry-After: 86400` from a stranger must not become an 86400 s sleep.

    The honest alternative to waiting a day is to say so and go offline inside
    this call, which is what a scheduler tick can still act on.
    """
    clock = _Clock()
    _install(clock)
    provider = _ScriptedProvider([ProviderRateLimited("nope", retry_after=86_400.0)])
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 3, raising=False)

    before = get_stats().get("offline", {}).get("calls", 0.0)
    response = await client.complete_text("s", "p", offline_fallback="offline text")

    assert clock.slept == [], f"waited out an absurd instruction: {clock.slept}"
    assert response.provider == "offline"
    assert "exceeds" in response.offline_reason
    assert len(provider.calls) == 1
    assert get_stats()["offline"]["calls"] - before == 1.0


@pytest.mark.asyncio
async def test_the_backoff_ladder_is_exponential_with_jitter(monkeypatch):
    clock = _Clock()
    _install(clock)
    res.set_rng(lambda: 1.0)  # deterministic top of the equal-jitter band
    provider = _ScriptedProvider(
        [ProviderUnavailable("a"), ProviderUnavailable("b"), ProviderUnavailable("c")]
    )
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 3, raising=False)

    await client.complete_text("s", "p", offline_fallback="x")

    assert len(clock.slept) == 2, f"expected two waits before giving up, got {clock.slept}"
    assert clock.slept[1] == pytest.approx(clock.slept[0] * 2.0)
    assert clock.slept[0] <= res.BACKOFF_CAP_S


@pytest.mark.asyncio
async def test_the_ladder_keeps_half_its_step_even_with_zero_rng(monkeypatch):
    """Full jitter would let a mutant sleep 0 and hammer; equal jitter cannot."""
    clock = _Clock()
    _install(clock)
    res.set_rng(lambda: 0.0)
    provider = _ScriptedProvider([ProviderUnavailable("a"), ProviderUnavailable("b")])
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 2, raising=False)

    await client.complete_text("s", "p", offline_fallback="x")

    assert clock.slept == [pytest.approx(res.BACKOFF_BASE_S / 2.0)]


@pytest.mark.asyncio
async def test_one_call_cannot_outlive_its_time_budget(monkeypatch):
    """The hard total-time budget: the reason a stage may not eat the cycle."""
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 5.0)
    provider = _SlowFailingProvider([ProviderUnavailable("busy")])
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 9, raising=False)

    started = clock.now()
    response = await client.complete_text("s", "p", offline_fallback="offline text")

    assert response.provider == "offline"
    assert clock.now() - started <= 5.0 + 1e-6, "a call outlived its budget"
    assert response.attempts == len(provider.calls)


@pytest.mark.asyncio
async def test_the_provider_is_handed_the_call_deadline(monkeypatch):
    """Without the deadline the clamp cannot happen, so it is asserted directly."""
    clock = _Clock()
    _install(clock)
    monkeypatch.setattr(LLMClient, "_call_budget_s", lambda self: 20.0)
    provider = _ScriptedProvider(["текст"])
    client = _client_with(provider)

    await client.complete_text("s", "p", offline_fallback="x")

    assert provider.calls[0]["deadline"] == pytest.approx(1_000.0 + 20.0)


# ------------------------------------------------------ the default, unpumped
@pytest.mark.asyncio
async def test_the_unpatched_client_carries_the_documented_call_budget(monkeypatch):
    """Mutant (f): `_call_budget_s` returning a huge number.

    Every other test in this file patches `_call_budget_s`, which would make
    that mutant invisible, so this one patches nothing: it asserts the default
    the production client actually uses, and that the default is what makes a
    42-minute stage impossible rather than what a test decided it should be.
    """
    from tgfarm.llm import resilience as resilience_mod

    client = _client_with(_ScriptedProvider(["текст"]))
    assert client._call_budget_s() == pytest.approx(resilience_mod.CALL_BUDGET_S)
    # Below the 45-minute stale window by orders of magnitude, and below one
    # catalogue sweep at the configured per-request timeout.
    assert resilience_mod.CALL_BUDGET_S <= 30.0
    assert resilience_mod.MAX_WAIT_S <= resilience_mod.CALL_BUDGET_S


@pytest.mark.asyncio
async def test_a_slow_provider_is_cut_off_at_the_default_budget(monkeypatch):
    """Behaviourally, without patching the budget method itself."""
    clock = _Clock()
    _install(clock)
    provider = _SlowFailingProvider([ProviderUnavailable("busy")], seconds=4.0)
    client = _client_with(provider)
    monkeypatch.setattr(client._settings, "llm_max_retries", 9, raising=False)
    # The breaker and the stage budget must be taken out of the picture, or
    # they stop the ladder first and this test proves nothing about the time
    # budget. Without these two lines the test passes with the budget removed
    # entirely — found by mutating `_call_budget_s` to 1e9.
    monkeypatch.setattr(res.BREAKER, "threshold", 10_000)
    monkeypatch.setattr(client, "_daily_budget", lambda: 10_000)
    monkeypatch.setattr(client, "_model_count", lambda: 10_000)

    started = clock.now()
    response = await client.complete_text("s", "p", offline_fallback="offline text")

    spent = clock.now() - started
    assert response.provider == "offline"
    assert spent <= res.CALL_BUDGET_S + 4.0, f"a call outlived the default budget: {spent}s"
    assert response.attempts <= 6, f"the budget did not stop the ladder: {response.attempts}"


@pytest.mark.asyncio
async def test_a_provider_that_has_not_opted_in_keeps_its_old_signature(monkeypatch):
    """`OpenCodeZen` is not mine to edit: it must be called exactly as before."""
    clock = _Clock()
    _install(clock)

    class _Legacy:
        name = "legacy"
        supports_deadline = False
        _models = ["legacy-model"]
        _model = "legacy-model"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def available(self) -> bool:
            return True

        async def complete(self, system, prompt, *, json_mode, max_tokens, model=None):
            self.calls.append({"json_mode": json_mode})
            return "старый текст"

    provider = _Legacy()
    client = _client_with(provider)
    before = get_stats().get("legacy", {}).get("deadline_not_supported", 0.0)

    response = await client.complete_text("s", "p", offline_fallback="x")

    assert response.text == "старый текст"
    assert len(provider.calls) == 1
    assert get_stats()["legacy"]["deadline_not_supported"] - before == 1.0

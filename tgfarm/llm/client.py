"""LLM client with provider failover and a deterministic offline composer.

Chain order comes from `LLM_PROVIDERS`. Each provider reports availability; the
first available one wins per call, and a mid-call failure demotes it for the
rest of the process. If nothing is available the `offline` composer takes over,
so the whole farm stays runnable (and testable) with an empty .env.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from tgfarm.core.errors import ProviderRateLimited, ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.settings import get_settings
from tgfarm.core.text import (
    content_tokens,
    detect_lang,
    first_sentence,
    strip_html,
    truncate,
)
from tgfarm.llm import resilience as res
from tgfarm.llm.extract import extract_json

log = get_logger(__name__)

_STATS: dict[str, dict[str, float]] = {}

#: Which stage is paying for the calls right now. `pipeline.py` owns the stage
#: loop, so this is ambient rather than a new required argument: `stage_scope()`
#: wraps a stage and a caller that never wraps one lands in the `unknown`
#: bucket, which still bounds the calls — it just bounds the cycle instead of
#: the stage. Fail-safe in the direction of spending less.
CURRENT_STAGE: ContextVar[str] = ContextVar("llm_current_stage", default="unknown")


@contextmanager
def stage_scope(name: str):
    """Ambient stage name for the per-stage call budget."""
    token = CURRENT_STAGE.set(name or "unknown")
    try:
        yield
    finally:
        CURRENT_STAGE.reset(token)


def record_stat(provider: str, key: str, value: float = 1.0) -> None:
    """Per-provider counters surfaced by `tgfarm doctor` and the dashboard."""
    bucket = _STATS.setdefault(provider, {})
    bucket[key] = bucket.get(key, 0.0) + value


def get_stats() -> dict[str, dict[str, float]]:
    return {k: dict(v) for k, v in _STATS.items()}


#: The only two spellings of "this text did not come from a model".
OFFLINE_PROVIDER = "offline"
OFFLINE_MODEL = "none"


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None
    #: Provenance of the *decision*, not just of the answer. `provider` and
    #: `model` already name who answered; these say who was asked and refused,
    #: how many tries it took, and — when nobody answered — which guard sent us
    #: to the deterministic composer. An operator cannot audit a fallback they
    #: cannot distinguish from a generation.
    attempts: int = 0
    tried: tuple[str, ...] = ()
    offline_reason: str = ""
    #: True when a provider returned a body for this call and was therefore
    #: billed. `provider` names who produced the *usable* answer (or `offline`
    #: when nobody did): an unusable answer is not a generation. Money is a
    #: different question from provenance, so a caller that rejects the body must
    #: still charge the reservation through `billed`.
    billed: bool = False
    billed_provider: str = ""
    billed_model: str = ""

    def __post_init__(self) -> None:
        # An offline draft must never carry a model name. Not cosmetic:
        # `s4_generation` writes `generation_model = response.model or ...`,
        # and a draft stored as `gemini-...:free` when the composer wrote it is
        # an unmeasured thing reported as a measured one. Corrected loudly.
        if self.provider == OFFLINE_PROVIDER:
            if self.model and self.model != OFFLINE_MODEL:
                log.warning(
                    "provenance: offline result carrying model=%s corrected to %s",
                    self.model,
                    OFFLINE_MODEL,
                )
                record_stat("provenance", "offline_model_corrected")
            self.model = OFFLINE_MODEL

    @property
    def is_model_generation(self) -> bool:
        """True only when a real provider produced `text`.

        The existing contract across the pipeline is
        `used = response.provider != "offline"`; this is the same rule with a
        name, so a caller cannot half-implement it. It deliberately says nothing
        about money: see `billed`.
        """
        return self.provider != OFFLINE_PROVIDER

    @property
    def audit_provider(self) -> str:
        """Who to record on the reservation: the billed call, if there was one."""
        return self.billed_provider or self.provider

    @property
    def audit_model(self) -> str:
        return self.billed_model or self.model


class _Provider:
    name = "base"
    #: Whether this provider accepts the `deadline` keyword. The client only
    #: passes a deadline to providers that say they handle it, so a provider
    #: owned by another work package cannot be broken by the call budget.
    supports_deadline = False

    async def available(self) -> bool:
        raise NotImplementedError

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        json_mode: bool,
        max_tokens: int,
        model: str | None = None,
        deadline: float | None = None,
    ) -> str:
        raise NotImplementedError


class _Anthropic(_Provider):
    name = "anthropic"
    supports_deadline = True

    def __init__(self, api_key: str, model: str, timeout: float, proxy: str = "") -> None:
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._proxy = proxy or ""
        self._down = 0.0

    async def available(self) -> bool:
        return bool(self._key) and res.now() > self._down

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        json_mode: bool,
        max_tokens: int,
        model: str | None = None,
        deadline: float | None = None,
    ) -> str:
        import httpx

        await RATE.llm.take()
        body = {
            "model": model or self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            body["system"] = system + (
                "\n\nRespond with a single valid JSON object and nothing else. "
                "No prose, no markdown fences."
            )
        async with httpx.AsyncClient(
            timeout=_request_timeout(self._timeout, deadline), proxy=self._proxy or None
        ) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
        if resp.status_code == 429:
            wait = _retry_after(resp, default=60.0)
            self._down = res.now() + wait
            raise ProviderRateLimited("anthropic 429", retry_after=wait)
        if resp.status_code >= 500:
            wait = _retry_after(resp, default=None)
            self._down = res.now() + (wait if wait is not None else 30)
            raise _server_error(f"anthropic {resp.status_code}", wait)
        if resp.status_code != 200:
            raise ProviderUnavailable(f"anthropic {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        chunks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        record_stat(self.name, "tokens_out", float(data.get("usage", {}).get("output_tokens", 0)))
        return "\n".join(chunks)


def _request_timeout(timeout_s: float, deadline: float | None) -> float:
    """The httpx timeout for one request given the call's remaining budget.

    `deadline` is an absolute monotonic instant. A 60 s request timeout inside a
    20 s call budget is not a timeout, it is an permission slip for the stage
    that outlived its cycle window: the live `stage s4_generate failed after
    2 815 575ms` is what that reads like. The 0.5 s floor keeps the value
    usable — httpx rejects a non-positive timeout.
    """
    if deadline is None:
        return float(timeout_s)
    remaining = float(deadline) - res.now()
    return max(0.5, min(float(timeout_s), remaining))


def _retry_after(resp: Any, *, default: float | None) -> float | None:
    """`Retry-After` from a response, or `default` when it says nothing."""
    header = ""
    try:
        header = resp.headers.get("Retry-After") or ""
    except Exception:  # noqa: BLE001 - a malformed header is "no instruction"
        header = ""
    parsed = res.parse_retry_after(header)
    if parsed is not None:
        record_stat("resilience", "retry_after_honoured")
        return parsed
    return default


def _server_error(detail: str, wait: float | None) -> Exception:
    """A 5xx as the exception it actually is.

    503 with a `Retry-After` is a scheduling instruction, which is what
    `ProviderRateLimited` means; without one it is plain unavailability. The
    distinction is what lets the retry loop wait the told amount instead of
    inventing one.
    """
    if wait is None:
        return ProviderUnavailable(detail)
    return ProviderRateLimited(detail, retry_after=wait)


def _is_loopback(base_url: str) -> bool:
    """True when the base URL points at this machine."""
    host = urlparse(base_url or "").hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")


def _split_models(value: str) -> list[str]:
    """`LLM_MODEL` accepts a comma-separated list of candidates.

    Free catalogues rotate weekly and rate-limit per model, so a single id is
    a single point of failure; the provider walks the list and remembers the
    model that answered.
    """
    models = [m.strip() for m in (value or "").split(",") if m.strip()]
    return models or [""]


class _OpenAI(_Provider):
    """Anything that speaks the OpenAI chat-completions protocol.

    One implementation covers a surprising amount: OpenAI itself, Google Gemini's
    OpenAI-compatible endpoint, Groq, OpenRouter, Together, Fireworks, and any
    local vLLM or llama.cpp server. They differ only in base URL, key and model,
    which is what `LLM_BASE_URL` supplies.
    """

    name = "openai"
    supports_deadline = True

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float,
        base_url: str = "https://api.openai.com/v1",
        name: str | None = None,
        *,
        send_json_mode: bool = True,
        extra_body: dict[str, Any] | None = None,
        proxy: str = "",
    ) -> None:
        self._key = api_key.strip()
        self._models = _split_models(model)
        self._model = self._models[0]
        self._model_index = 0
        # Per-model cooldown after a 429: the free tier allows one request per
        # minute per model, so a known-hot candidate is skipped instead of
        # burned. Also the base for the per-model usage counters.
        self._model_cooldown: dict[str, float] = {}
        self._timeout = timeout
        self._base = base_url.rstrip("/")
        self._proxy = proxy or ""
        self._down = 0.0
        self._send_json_mode = send_json_mode
        self._extra_body = extra_body or {}
        if name:
            self.name = name

    async def available(self) -> bool:
        if res.now() <= self._down:
            return False
        # Hosted providers reject an unauthenticated call, but a local server
        # (vLLM, llama.cpp, LM Studio) needs no key at all.
        return bool(self._key) or _is_loopback(self._base)

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        json_mode: bool,
        max_tokens: int,
        model: str | None = None,
        deadline: float | None = None,
    ) -> str:
        """One call, rotating through the candidate models.

        Rules of the walk: 429 and unknown-model errors move to the next
        candidate (the free tier rate-limits per model, and catalogues rotate);
        a model that rejects `response_format` gets one retry without it, the
        JSON extractor handles prose anyway; 401/402/403 are key-level and take
        the whole provider down, because trying another model with a dead key is
        waste. The model that answered is remembered for the next calls.

        `deadline` is an absolute monotonic instant the walk must not pass. With
        a twelve-model catalogue the walk is twelve sequential requests, each
        carrying the full `llm_timeout_s` (60 by default): that is how one
        generation reached 42 minutes in the live log. Without a deadline the
        walk behaves exactly as before.
        """
        import httpx

        await RATE.llm.take()
        if model:
            candidates = [model]
        else:
            rotated = self._models[self._model_index :] + self._models[: self._model_index]
            now = res.now()
            candidates = [
                m
                for m in rotated
                if m and now >= self._model_cooldown.get(m, 0.0)
            ] or [m for m in rotated if m]
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"

        last_error: Exception | None = None
        for candidate in candidates:
            if deadline is not None and res.now() >= deadline:
                # Out of budget. Starting a request the cycle cannot wait for
                # is not patience, it is the next stage going hungry.
                if last_error is None:
                    last_error = ProviderUnavailable(
                        f"{self.name}: call budget spent before {candidate}"
                    )
                break
            # attempt_json only controls the response_format flag: gateways that
            # do not advertise it (LongCat) get a single plain attempt.
            json_attempts = (True, False) if (json_mode and self._send_json_mode) else (False,)
            for attempt_json in json_attempts:
                if deadline is not None and res.now() >= deadline:
                    break
                body: dict[str, Any] = {
                    "model": candidate,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.4,
                }
                body.update(self._extra_body)
                if attempt_json:
                    body["response_format"] = {"type": "json_object"}
                async with httpx.AsyncClient(
                    timeout=_request_timeout(self._timeout, deadline), proxy=self._proxy or None
                ) as client:
                    resp = await client.post(
                        f"{self._base}/chat/completions",
                        headers=headers,
                        json=body,
                    )
                if resp.status_code == 429:
                    record_stat(self.name, "rate_limited")
                    record_stat(f"{self.name}:{candidate}", "rate_limited")
                    # One request per minute per model on the free tier — but
                    # when the gateway says when, its number beats our guess.
                    wait = _retry_after(resp, default=60.0)
                    self._model_cooldown[candidate] = res.now() + wait
                    last_error = ProviderRateLimited(
                        f"{self.name} 429 on {candidate}", retry_after=wait
                    )
                    break  # next candidate; this one is out of quota
                if resp.status_code in (401, 402, 403):
                    body_text = resp.text.lower()
                    if resp.status_code == 403 and any(
                        marker in body_text
                        for marker in (
                            "consent_required",
                            "consent required",
                            "model_not_found",
                            "unknown model",
                        )
                    ):
                        # A free model may demand a one-off consent for its
                        # upstream provider (unorouter returns 403
                        # consent_required) or simply not exist in this
                        # catalogue. The key is fine, so park only this
                        # candidate for an hour and keep walking: taking the
                        # whole gateway down here is what turned every draft
                        # into offline text for days.
                        self._model_cooldown[candidate] = res.now() + 3600
                        record_stat(f"{self.name}:{candidate}", "consent_required")
                        last_error = ProviderUnavailable(
                            f"{self.name} 403 for {candidate}: {resp.text[:160]}"
                        )
                        break
                    # Bad key, revoked access, or an exhausted prepaid quota:
                    # retrying this provider in the same process will not help.
                    self._down = res.now() + 3600
                    raise ProviderUnavailable(f"{self.name} {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 500:
                    # An overloaded model (503 "providers busy") is not a dead
                    # provider: rotate to the next candidate instead of parking
                    # the whole gateway for 30 seconds.
                    record_stat(self.name, "provider_busy")
                    wait = _retry_after(resp, default=None)
                    if wait is not None:
                        # "come back in N seconds" applies to this candidate, so
                        # the rotation skips it for exactly that long — this is
                        # the only way a 503 with a header changes behaviour
                        # while we are still walking the catalogue.
                        self._model_cooldown[candidate] = res.now() + wait
                    last_error = _server_error(
                        f"{self.name} {resp.status_code} on {candidate}", wait
                    )
                    break  # next candidate
                if resp.status_code == 400 and attempt_json and "response_format" in body:
                    record_stat(self.name, "json_mode_rejected")
                    continue  # same model, plain text + extractor
                if resp.status_code != 200:
                    record_stat(self.name, "model_rejected")
                    last_error = ProviderUnavailable(
                        f"{self.name} {resp.status_code} on {candidate}: {resp.text[:200]}"
                    )
                    break  # unknown model id — try the next one
                data = resp.json()
                if not model and candidate in self._models:
                    self._model_index = self._models.index(candidate)
                    self._model = candidate
                record_stat(
                    self.name, "tokens_out", float(data.get("usage", {}).get("completion_tokens", 0))
                )
                record_stat(f"{self.name}:{candidate}", "calls")
                return data["choices"][0]["message"]["content"]

        if last_error is not None:
            if isinstance(last_error, ProviderRateLimited):
                # Every candidate is out of quota: park the provider for a
                # minute so the client loop does not hammer it immediately.
                self._down = res.now() + 60
            raise last_error
        raise ProviderUnavailable(f"{self.name}: no usable model in the list")


class _LongCat(_OpenAI):
    """LongCat's OpenAI-compatible endpoint.

    It does not advertise `response_format`, so JSON requests rely on the
    prompt and the extractor instead. Thinking is disabled by default: the
    pipeline wants prose, not a hidden reasoning trace, and enabled thinking
    fixes the temperature at 1.0.
    """

    name = "longcat"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float,
        *,
        thinking: bool = False,
        proxy: str = "",
    ) -> None:
        super().__init__(
            api_key,
            model,
            timeout,
            base_url="https://api.longcat.chat/openai/v1",
            name="longcat",
            send_json_mode=False,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
            proxy=proxy,
        )


class _Ollama(_Provider):
    name = "ollama"
    supports_deadline = True

    def __init__(self, url: str, model: str, timeout: float, proxy: str = "") -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._proxy = proxy or ""
        self._ready: bool | None = None

    async def available(self) -> bool:
        if self._ready is False:
            return False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0, proxy=self._proxy or None) as client:
                resp = await client.get(f"{self._url}/api/tags")
            self._ready = resp.status_code == 200
        except Exception:
            self._ready = False
        return bool(self._ready)

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        json_mode: bool,
        max_tokens: int,
        model: str | None = None,
        deadline: float | None = None,
    ) -> str:
        import httpx

        await RATE.llm.take()
        body = {
            "model": model or self._model,
            "stream": False,
            "format": "json" if json_mode else "",
            "options": {"temperature": 0.4, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        async with httpx.AsyncClient(
            timeout=_request_timeout(self._timeout, deadline), proxy=self._proxy or None
        ) as client:
            resp = await client.post(f"{self._url}/api/chat", json=body)
        if resp.status_code == 429:
            raise ProviderRateLimited("ollama 429", retry_after=_retry_after(resp, default=60.0))
        if resp.status_code >= 500:
            raise _server_error(f"ollama {resp.status_code}", _retry_after(resp, default=None))
        if resp.status_code != 200:
            raise ProviderUnavailable(f"ollama {resp.status_code}: {resp.text[:200]}")
        return resp.json()["message"]["content"]


class LLMClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._providers: list[_Provider] = []
        timeout = settings.llm_timeout_s
        proxy = settings.llm_proxy
        for name in settings.provider_chain:
            if name == "anthropic":
                self._providers.append(
                    _Anthropic(
                        settings.anthropic_api_key,
                        settings.anthropic_model,
                        timeout,
                        proxy=proxy,
                    )
                )
            elif name == "openai":
                self._providers.append(
                    _OpenAI(
                        settings.openai_api_key,
                        settings.openai_model,
                        timeout,
                        proxy=proxy,
                    )
                )
            elif name in ("compatible", "openai_compatible"):
                # Gemini / Groq / OpenRouter / Together / Fireworks / vLLM — all
                # of them speak the same protocol and differ only by base URL.
                self._providers.append(
                    _OpenAI(
                        settings.llm_api_key,
                        settings.llm_model,
                        timeout,
                        base_url=settings.llm_base_url,
                        name="compatible",
                        proxy=proxy,
                    )
                )
            elif name == "longcat":
                self._providers.append(
                    _LongCat(
                        settings.longcat_api_key,
                        settings.longcat_model,
                        timeout,
                        thinking=settings.longcat_thinking,
                        proxy=proxy,
                    )
                )
            elif name in ("opencode", "zen"):
                from tgfarm.llm.opencode import OpenCodeZen

                self._providers.append(
                    OpenCodeZen(
                        settings.opencode_zen_token,
                        settings.opencode_zen_model,
                        timeout,
                        free_only=settings.opencode_zen_free_only,
                    )
                )
            elif name == "compatible2":
                # Second echelon: an independent gateway. Skipped without a
                # base URL, so an unused slot costs nothing.
                if settings.llm2_base_url:
                    self._providers.append(
                        _OpenAI(
                            settings.llm2_api_key,
                            settings.llm2_model,
                            timeout,
                            base_url=settings.llm2_base_url,
                            name="compatible2",
                            proxy=proxy,
                        )
                    )
            elif name == "ollama":
                self._providers.append(
                    _Ollama(
                        settings.ollama_url,
                        settings.ollama_model,
                        timeout,
                        proxy=proxy,
                    )
                )
        self._resolved: _Provider | None = None

    @property
    def offline(self) -> bool:
        return self._resolved is None

    def _model_count(self) -> int:
        """How many model candidates the chain actually holds.

        The per-stage budget is expressed in units of one catalogue sweep, so
        it needs the sweep size. Providers that do not rotate report one.
        """
        counts = [len(prov._models) for prov in self._providers if getattr(prov, "_models", None)]
        return max(counts) if counts else 1

    def _call_budget_s(self) -> float:
        """Wall-clock allowance for one logical generation call.

        Read from settings when an operator adds the knob, otherwise the
        module default. It can never exceed `llm_timeout_s` by more than the
        ladder allows, and it is what makes a 42-minute stage impossible.
        """
        value = getattr(self._settings, "llm_call_budget_s", None)
        try:
            budget = float(value) if value else res.CALL_BUDGET_S
        except (TypeError, ValueError):
            budget = res.CALL_BUDGET_S
        return max(1.0, budget)

    def _daily_budget(self) -> int:
        """The operator's own daily figure, passed through untouched.

        `ai_daily_budget` is the single number for "how many online calls per
        day". `llm/policy.py` charges it only for calls that produced text;
        `resilience.AttemptBudget` also counts the attempts that never did,
        which is the half the live log proved is unbounded (8 506 doomed calls
        against a 2 000/day budget that never filled). One budget, two views:
        the subtraction of what is already spent lives in the budget object,
        so this cannot double-charge.
        """
        try:
            return max(0, int(getattr(self._settings, "ai_daily_budget", 0) or 0))
        except (TypeError, ValueError):
            return 0

    async def _resolve(
        self,
        blocked: list[str] | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> _Provider | None:
        """First provider that is configured, not parked and not breaker-open.

        `blocked` collects *why* a provider was skipped, so the operator who
        reads `provider=offline` can tell "the gateway refused" from "we
        stopped asking". Skipping a breaker-open provider is not a failure: it
        is the refusal to keep hammering a thing that already said no.
        """
        if self._resolved is not None and self._resolved.name not in exclude:
            provider = self._resolved
            if not res.BREAKER.allow(provider.name):
                if blocked is not None:
                    blocked.append(f"breaker_open:{provider.name}")
                record_stat(provider.name, "breaker_blocked")
                self._resolved = None
            else:
                try:
                    if await provider.available():
                        return provider
                except Exception as exc:  # noqa: BLE001 - a probe must never raise
                    log.debug("provider %s re-probe failed: %s", provider.name, exc)
                self._resolved = None
        for provider in self._providers:
            if provider.name in exclude:
                # Already refused inside this call: retrying it would spend the
                # attempt on the provider that just said no, which is why the
                # old chain never reached a healthy second gateway.
                continue
            if not res.BREAKER.allow(provider.name):
                if blocked is not None:
                    blocked.append(
                        f"breaker_open:{provider.name}:{res.BREAKER.retry_in(provider.name):.0f}s"
                    )
                record_stat(provider.name, "breaker_blocked")
                continue
            try:
                if await provider.available():
                    self._resolved = provider
                    log.info("llm provider: %s", provider.name)
                    return provider
            except Exception as exc:  # noqa: BLE001 - probing must never raise
                log.debug("provider %s probe failed: %s", provider.name, exc)
            # Chosen by `allow()` as the half-open probe but never used: give
            # the slot back, or the breaker would stay locked on a probe that
            # was never made.
            res.BREAKER.release_probe(provider.name)
        if exclude and blocked is not None:
            blocked.append(f"excluded_this_call:{'|'.join(sorted(exclude))}")
        return None

    async def ensure_available(self) -> bool:
        """A live check for callers deciding whether to ship generated text.

        The `offline` property only remembers the last resolve; after a failed
        rotation it is stale in both directions. This actually re-probes.
        """
        provider = await self._resolve()
        return provider is not None

    async def probe(self) -> dict:
        """One tiny real call; `available()` only proves a key is present.

        A key can be valid and the endpoint still refuse the request (OpenRouter
        answers 403 from blocked regions, a provider can be out of credit). A
        doctor that reports "ok" because the key is non-empty is worse than no
        doctor at all.
        """
        blocked: list[str] = []
        provider = await self._resolve(blocked)
        if provider is None:
            return {
                "ok": False,
                "provider": None,
                "detail": "offline (deterministic composer)",
                "blocked": blocked,
            }
        try:
            started = res.now()
            text = await provider.complete(
                "Отвечай одним словом.", "ping", json_mode=False, max_tokens=8
            )
            latency = int((res.now() - started) * 1000)
            return {
                "ok": True,
                "provider": provider.name,
                "model": getattr(provider, "_model", "?"),
                "latency_ms": latency,
                "detail": f"{provider.name} ответил за {latency} мс: {truncate(text.strip(), 40)}",
            }
        except Exception as exc:  # noqa: BLE001 - a failed probe is the report
            return {
                "ok": False,
                "provider": provider.name,
                "detail": f"{provider.name}: {exc}"[:220],
            }

    async def _call_provider(
        self, provider: _Provider, system: str, prompt: str, *, json_mode: bool, max_tokens: int,
        model: str | None, deadline: float,
    ) -> str:
        """One provider call, deadline-clamped for providers that opt in.

        Providers outside this work package (currently `OpenCodeZen`) do not
        accept `deadline`; they are called exactly as before rather than being
        edited into the new signature, and the seam is reported instead.
        """
        if provider.supports_deadline:
            return await provider.complete(
                system, prompt, json_mode=json_mode, max_tokens=max_tokens, model=model,
                deadline=deadline,
            )
        record_stat(provider.name, "deadline_not_supported")
        return await provider.complete(
            system, prompt, json_mode=json_mode, max_tokens=max_tokens, model=model
        )

    async def _obtain(
        self,
        system: str,
        prompt: str,
        *,
        json_mode: bool,
        max_tokens: int,
        model: str | None,
    ) -> dict[str, Any]:
        """Bounded, backed-off, breaker-guarded attempt loop. Shared by text/json.

        Every `complete_*` call used to run its own three-try loop with a
        hardcoded sleep; the two loops drifted. One loop means one set of
        guarantees, and the guarantees are:
          * at most `llm_max_retries` provider calls,
          * at most `llm_call_budget_s` seconds of wall clock, in total,
          * a `Retry-After` is honoured up to `MAX_WAIT_S` and only if the
            remaining budget covers it — otherwise we stop rather than wait,
          * a provider that failed `BREAKER_FAILURE_THRESHOLD` calls in a row
            is not called again until its cool-off ends,
          * the stage may not spend more attempts than its budget allows.
        Failure is reported as a reason; nothing in here returns success.
        """
        stage = CURRENT_STAGE.get()
        budget = res.TimeBudget.start(self._call_budget_s())
        attempts = max(1, int(self._settings.llm_max_retries or 1))
        tried: list[str] = []
        blocked: list[str] = []
        # Providers that refused inside this call. Without the exclusion the
        # retry loop re-resolved to provider[0] every attempt — `_down` on a
        # 429 or a breaker trip was the only thing that ever moved a call to
        # the next gateway, so a three-attempt call could spend all three on
        # the same refusing provider and never fail over.
        refused: set[str] = set()
        spent = 0
        reason = "no provider configured"
        last_error: Exception | None = None
        #: The last provider that actually returned a body. A 200 whose JSON
        #: does not parse is a billed call that produced nothing usable: it must
        #: still reach the daily cap, or the farm spends past its budget.
        billed_provider = ""
        billed_model = ""

        for attempt in range(attempts):
            blocked.clear()
            provider = await self._resolve(blocked, exclude=frozenset(refused))
            if provider is None and refused:
                # The whole chain refused: start the round again, otherwise a
                # single-provider farm would get one attempt per call instead
                # of `llm_max_retries`.
                refused.clear()
                provider = await self._resolve(blocked)
            if provider is None:
                if blocked:
                    # The breaker is the whole answer: we are not calling
                    # anyone, and the operator must see that in the metadata.
                    reason = ";".join(blocked)
                    if spent == 0:
                        record_stat("resilience", "breaker_shortcut")
                else:
                    reason = "no provider available"
                break
            if not res.BUDGET.try_spend(
                stage, daily_budget=self._daily_budget(), model_count=self._model_count()
            ):
                reason = f"stage budget spent ({stage})"
                record_stat("resilience", "stage_budget_denied")
                log.warning(
                    "llm stage budget spent for %s after %d attempt(s); going offline",
                    stage,
                    spent,
                )
                break
            spent += 1
            tried.append(f"{provider.name}:{getattr(provider, '_model', '?')}")
            try:
                started = res.now()
                text = await self._call_provider(
                    provider, system, prompt, json_mode=json_mode, max_tokens=max_tokens,
                    model=model, deadline=budget.started_at + budget.limit_s,
                )
                billed_provider = provider.name
                billed_model = model or getattr(provider, "_model", "") or "?"
                parsed: Any = None
                if json_mode:
                    # Inside the try on purpose: `extract_json` raises when the
                    # body holds no object at all, and that must go down the
                    # same ladder as a refused call — a prose answer from a
                    # live model is a failed generation, not a crash in S4.
                    parsed = extract_json(text)
                    if not isinstance(parsed, dict):
                        record_stat(provider.name, "json_unparsable")
                        raise ProviderUnavailable("expected a JSON object")
            except ProviderRateLimited as exc:
                last_error = exc
                tripped = res.BREAKER.on_failure(provider.name)
                record_stat(provider.name, "rate_limited_call")
                self._resolved = None
                refused.add(provider.name)
                told = max(0.0, float(getattr(exc, "retry_after", 0.0) or 0.0))
                wait = min(told, res.MAX_WAIT_S, budget.remaining())
                if told > res.MAX_WAIT_S:
                    # The provider asked for more than we are ever willing to
                    # give inside one call. Honouring it *is* refusing to retry
                    # now — say which, instead of sleeping the difference.
                    reason = f"retry_after {told:.0f}s exceeds cap {res.MAX_WAIT_S:.0f}s"
                    log.warning(
                        "llm %s asked for %ss, capped at %ss: giving up this call",
                        provider.name, told, res.MAX_WAIT_S,
                    )
                    break
                if wait >= budget.remaining() and budget.remaining() <= 1.0:
                    reason = f"call budget spent before retry_after {told:.0f}s"
                    break
                if tripped:
                    reason = f"breaker_open:{provider.name}"
                    log.warning(
                        "circuit breaker open for %s after %d consecutive failures; "
                        "cooling off %.0fs",
                        provider.name,
                        res.BREAKER.threshold,
                        res.BREAKER.retry_in(provider.name),
                    )
                    # Do not end the call: the next attempt re-resolves, skips
                    # the now-open provider and tries the rest of the chain.
                    # Ending here is how one dead gateway offlined a healthy
                    # second one.
                if attempt + 1 < attempts:
                    await res.sleep(wait)
                continue
            except Exception as exc:  # noqa: BLE001 - failover is the point
                last_error = exc
                tripped = res.BREAKER.on_failure(provider.name)
                record_stat(provider.name, "errors")
                self._resolved = None
                refused.add(provider.name)
                remaining = budget.remaining()
                wait = res.backoff_delay(attempt, ceiling=min(res.BACKOFF_CAP_S, remaining))
                if remaining <= wait + 0.5:
                    reason = f"call budget spent ({exc})"
                    break
                if attempt + 1 < attempts:
                    await res.sleep(wait)
                if tripped:
                    reason = f"breaker_open:{provider.name}"
                    log.warning(
                        "circuit breaker open for %s after %d consecutive failures; "
                        "cooling off %.0fs",
                        provider.name,
                        res.BREAKER.threshold,
                        res.BREAKER.retry_in(provider.name),
                    )
                continue
            res.BREAKER.on_success(provider.name)
            record_stat(provider.name, "calls")
            record_stat(provider.name, "retries", float(attempt))
            answered = model or getattr(provider, "_model", "") or "?"
            return {
                "ok": True,
                "text": text,
                "parsed": parsed,
                "provider": provider.name,
                "model": answered,
                "latency_ms": int((res.now() - started) * 1000),
                "attempts": spent,
                "tried": tuple(tried),
                "billed": True,
                "billed_provider": provider.name,
                "billed_model": answered,
            }

        if last_error is not None and reason == "no provider configured":
            reason = str(last_error)
        return {
            "ok": False,
            "provider": OFFLINE_PROVIDER,
            "model": OFFLINE_MODEL,
            "attempts": spent,
            "tried": tuple(tried),
            "reason": reason,
            "error": last_error,
            "billed": bool(billed_provider),
            "billed_provider": billed_provider,
            "billed_model": billed_model,
        }

    async def complete_text(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1500,
        offline_fallback: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        outcome = await self._obtain(
            system, prompt, json_mode=False, max_tokens=max_tokens, model=model
        )
        if outcome["ok"]:
            return LLMResponse(
                outcome["text"],
                outcome["provider"],
                outcome["model"],
                outcome["latency_ms"],
                attempts=outcome["attempts"],
                tried=outcome["tried"],
                billed=bool(outcome.get("billed")),
                billed_provider=str(outcome.get("billed_provider") or ""),
                billed_model=str(outcome.get("billed_model") or ""),
            )
        return self._offline_response(
            outcome, offline_fallback, kind="text", max_tokens=max_tokens
        )

    async def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 2000,
        offline_fallback: dict | None = None,
        model: str | None = None,
    ) -> tuple[dict, LLMResponse]:
        outcome = await self._obtain(
            system, prompt, json_mode=True, max_tokens=max_tokens, model=model
        )
        if outcome["ok"]:
            return outcome["parsed"], LLMResponse(
                outcome["text"],
                outcome["provider"],
                outcome["model"],
                outcome["latency_ms"],
                attempts=outcome["attempts"],
                tried=outcome["tried"],
                billed=bool(outcome.get("billed")),
                billed_provider=str(outcome.get("billed_provider") or ""),
                billed_model=str(outcome.get("billed_model") or ""),
            )
        response = self._offline_response(
            outcome, offline_fallback, kind="json", max_tokens=max_tokens
        )
        # The pair contract is the offline payload plus the honest provenance of
        # how it was reached; callers unpack it, so returning the bare response
        # here is a TypeError in every stage that fell back.
        return (offline_fallback if isinstance(offline_fallback, dict) else {}), response

    def _offline_response(
        self, outcome: dict[str, Any], fallback: Any, *, kind: str, max_tokens: int
    ) -> LLMResponse:
        """The one way a call ends without a model, said out loud.

        `provider=offline` and `model=none` are the same values the old code
        used, so nothing downstream changes — what is added is the reason and
        the attempt count, because 8 506 offline fallbacks against a
        `provider=compatible` line that said nothing is how an unmeasured thing
        got reported as a measured one for 36 hours.
        """
        reason = str(outcome.get("reason") or "all llm providers failed")
        attempts = int(outcome.get("attempts") or 0)
        label = "llm json failed" if kind == "json" else "llm failed"
        log.warning(
            "%s (%s) after %d attempt(s), using offline fallback",
            label,
            str(outcome.get("error") or reason)[:200],
            attempts,
        )
        if fallback is None:
            raise ProviderUnavailable(f"all llm providers failed: {reason}")
        record_stat("offline", "calls")
        text = "" if kind == "json" else (fallback if isinstance(fallback, str) else "")
        response = LLMResponse(
            text,
            OFFLINE_PROVIDER,
            OFFLINE_MODEL,
            0,
            attempts=attempts,
            tried=tuple(outcome.get("tried") or ()),
            offline_reason=reason[:300],
            billed=bool(outcome.get("billed")),
            billed_provider=str(outcome.get("billed_provider") or ""),
            billed_model=str(outcome.get("billed_model") or ""),
        )
        response.usage = {"attempts": attempts, "max_tokens": max_tokens}
        response.raw = {"offline_reason": response.offline_reason, "tried": list(response.tried)}
        return response

_CLIENT: LLMClient | None = None


def get_llm() -> LLMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient()
    return _CLIENT


def reset_llm() -> None:
    global _CLIENT
    _CLIENT = None


# --- offline deterministic composer ----------------------------------------
_ANGLE_LEADS = {
    "news": "{title}",
    "analysis": "Разбираем, что произошло и почему это важно",
    "why_it_matters": "Почему это касается каждого",
    "comparison": "Сравнили два подхода — вот разница",
    "checklist": "Короткий чек-лист по теме",
    "history": "Как это развивалось",
}


def _matching_language(items: list, language: str) -> list:
    """Keep only sources the offline composer can use without translating.

    The deterministic composer is extractive: it copies sentences from sources.
    Mixing an English feed into a Russian channel produces English prose with a
    Russian angle suffix, so the language must be enforced before a sentence is
    picked. `uz` and other Latin-script languages are left untouched because the
    cheap detector cannot tell them apart from English.
    """
    if language not in ("ru", "en"):
        return list(items)
    return [
        item
        for item in items
        if detect_lang(f"{item.title} {item.text}") == language
    ]


def _split_sentences(text: str) -> list[str]:
    cleaned = strip_html(text or "").strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?…])\s+", cleaned)
    return [p.strip() for p in parts if len(p.strip()) > 25]


def _distinct_sentences(hook: str, pool: list[str], limit: int = 4) -> list[str]:
    """Pick sentences that actually add information.

    Feed summaries routinely restate the headline, and headlines are routinely
    reused verbatim as the first body line. Overlap is measured on content
    tokens so paraphrases get filtered too — a body that repeats the hook fails
    the S5 redundancy gate and gets rejected, so this has to be right here.
    """
    seen = set(content_tokens(hook))
    chosen: list[str] = []
    for sentence in pool:
        tokens = set(content_tokens(sentence))
        if len(tokens) < 4:
            continue
        overlap = len(tokens & seen) / len(tokens)
        if overlap > 0.55:
            continue
        seen |= tokens
        chosen.append(sentence)
        if len(chosen) >= limit:
            break
    return chosen


def compose_offline_post(
    cluster_items: list,
    persona: dict,
    angle: str = "news",
    format_plan=None,
) -> dict:
    """Deterministic fallback composer.

    Not a toy: it produces a valid, sourced, publishable post from the cluster so
    the farm never stalls on an LLM outage. `format_plan` (S10) sets the length
    budget and the opening move, so the offline path respects the same format
    decision the live path would.
    """
    tone = persona.get("tone", ["factual"])
    lang = persona.get("language", "ru")
    cluster_items = _matching_language(cluster_items, lang)
    if not cluster_items:
        return {
            "hook": "",
            "body": "",
            "sources": [],
            "hashtags": [],
            "cta": None,
            "claims": [],
            "language": lang,
        }

    lead = cluster_items[0]

    # the hook must be a complete sentence — S5 gate `structure` checks this,
    # so the offline composer has to honour it or it gates itself out
    hook_title = first_sentence(lead.title, 108).strip()
    if not re.search(r"[.!?…]$", hook_title):
        hook_title += "."

# S10 decides the opening move and the budget; the offline path follows it
    # exactly so a provider outage cannot change the shape of the post.
    fmt = getattr(format_plan, "fmt", "news")
    if fmt == "flash":
        hook = truncate(hook_title, 140)
    elif fmt == "stat":
        hook = truncate(f"{hook_title} Цифра, которая всё объясняет.", 140)
    elif fmt == "video":
        hook = truncate(f"{hook_title} Смотрите, что произошло.", 140)
    elif fmt == "article":
        hook = truncate(f"{hook_title} Разбираемся по порядку.", 140)
    elif angle == "why_it_matters":
        hook = truncate(f"{hook_title} Разбираемся, что это меняет на практике.", 115)
    elif angle == "checklist":
        hook = truncate(f"{hook_title} Коротко, по пунктам.", 115)
    elif angle == "comparison":
        hook = truncate(f"{hook_title} Два подхода, одна цель.", 115)
    elif angle == "analysis":
        hook = truncate(f"{hook_title} Что за этим стоит.", 115)
    elif angle == "history":
        hook = truncate(f"{hook_title} Как мы к этому пришли.", 115)
    else:
        hook = truncate(hook_title, 115)

    # body: harvest distinct sentences from every member, longest first
    pool: list[str] = []
    for item in cluster_items[:5]:
        sentences = _split_sentences(item.text)
        pool.extend(sorted(sentences, key=len, reverse=True))
    limit = 12 if persona.get("platform") == "dzen" else (8 if fmt == "article" else 6)
    picked = _distinct_sentences(hook, pool, limit=limit)

    cta_hint = str(persona.get("cta") or "")
    cta_enabled = persona.get("cta_policy") == "always" and cta_hint
    cta_cost = len(cta_hint) + 1 if cta_enabled else 0

    if format_plan is not None:
        total_budget = int(format_plan.max_chars)
        floor = int(format_plan.min_chars)
    else:
        total_budget = int(persona.get("max_chars", 600))
        floor = int(persona.get("min_chars", 150))
    budget = max(80, total_budget - len(hook) - cta_cost)
    min_budget = max(0, floor - len(hook))

    body_parts: list[str] = []
    used = 0
    for sentence in picked:
        piece = truncate(sentence, max(60, budget - used))
        cost = len(piece) + (2 if body_parts else 0)
        if used + cost > budget:
            break
        body_parts.append(piece)
        used += cost
        if used >= max(min_budget, budget * 0.85):
            break

    if not body_parts:
        body_parts = [hook_title]
    body = "\n\n".join(body_parts)
    if persona.get("platform") == "dzen" and len(body) < min_budget:
        body_parts.append("Подробности и первоисточники — в ссылках ниже.")
        body = "\n\n".join(body_parts)

    sources = []
    seen: set[str] = set()
    for item in cluster_items[:3]:
        if item.url in seen:
            continue
        seen.add(item.url)
        sources.append({"title": item.title[:160], "url": item.url, "source": item.source_id})

    claims = [
        {"claim": first_sentence(item.text, 200), "source_url": item.url, "verified": True}
        for item in cluster_items[:2]
        if item.text
    ]

    tags = list(persona.get("hashtags", []))
    if not tags:
        from tgfarm.core.text import keyword_set

        tags = [t.capitalize() for t in list(keyword_set(lead.title + " " + lead.text, 3))]

    cta = persona.get("cta") if persona.get("cta_policy") == "always" else None

    return {
        "hook": hook,
        "body": body,
        "sources": sources,
        "hashtags": tags,
        "cta": cta,
        "claims": claims,
        "angle": angle,
        "language": lang,
        "tone": tone,
    }

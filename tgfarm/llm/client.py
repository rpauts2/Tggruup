"""LLM client with provider failover and a deterministic offline composer.

Chain order comes from `LLM_PROVIDERS`. Each provider reports availability; the
first available one wins per call, and a mid-call failure demotes it for the
rest of the process. If nothing is available the `offline` composer takes over,
so the whole farm stays runnable (and testable) with an empty .env.
"""

from __future__ import annotations

import asyncio
import re
import time
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
from tgfarm.llm.extract import extract_json

log = get_logger(__name__)

_STATS: dict[str, dict[str, float]] = {}


def record_stat(provider: str, key: str, value: float = 1.0) -> None:
    """Per-provider counters surfaced by `tgfarm doctor` and the dashboard."""
    bucket = _STATS.setdefault(provider, {})
    bucket[key] = bucket.get(key, 0.0) + value


def get_stats() -> dict[str, dict[str, float]]:
    return {k: dict(v) for k, v in _STATS.items()}


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class _Provider:
    name = "base"

    async def available(self) -> bool:
        raise NotImplementedError

    async def complete(
        self, system: str, prompt: str, *, json_mode: bool, max_tokens: int, model: str | None = None
    ) -> str:
        raise NotImplementedError


class _Anthropic(_Provider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float, proxy: str = "") -> None:
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._proxy = proxy or ""
        self._down = 0.0

    async def available(self) -> bool:
        return bool(self._key) and time.monotonic() > self._down

    async def complete(
        self, system: str, prompt: str, *, json_mode: bool, max_tokens: int, model: str | None = None
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
        async with httpx.AsyncClient(timeout=self._timeout, proxy=self._proxy or None) as client:
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
            self._down = time.monotonic() + 60
            raise ProviderRateLimited("anthropic 429", retry_after=60)
        if resp.status_code >= 500:
            self._down = time.monotonic() + 30
            raise ProviderUnavailable(f"anthropic {resp.status_code}")
        if resp.status_code != 200:
            raise ProviderUnavailable(f"anthropic {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        chunks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        record_stat(self.name, "tokens_out", float(data.get("usage", {}).get("output_tokens", 0)))
        return "\n".join(chunks)


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
        if time.monotonic() <= self._down:
            return False
        # Hosted providers reject an unauthenticated call, but a local server
        # (vLLM, llama.cpp, LM Studio) needs no key at all.
        return bool(self._key) or _is_loopback(self._base)

    async def complete(
        self, system: str, prompt: str, *, json_mode: bool, max_tokens: int, model: str | None = None
    ) -> str:
        """One call, rotating through the candidate models.

        Rules of the walk: 429 and unknown-model errors move to the next
        candidate (the free tier rate-limits per model, and catalogues rotate);
        a model that rejects `response_format` gets one retry without it, the
        JSON extractor handles prose anyway; 401/402/403 are key-level and take
        the whole provider down, because trying another model with a dead key is
        waste. The model that answered is remembered for the next calls.
        """
        import httpx

        await RATE.llm.take()
        if model:
            candidates = [model]
        else:
            rotated = self._models[self._model_index :] + self._models[: self._model_index]
            now = time.monotonic()
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
            # attempt_json only controls the response_format flag: gateways that
            # do not advertise it (LongCat) get a single plain attempt.
            json_attempts = (True, False) if (json_mode and self._send_json_mode) else (False,)
            for attempt_json in json_attempts:
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
                async with httpx.AsyncClient(timeout=self._timeout, proxy=self._proxy or None) as client:
                    resp = await client.post(
                        f"{self._base}/chat/completions",
                        headers=headers,
                        json=body,
                    )
                if resp.status_code == 429:
                    record_stat(self.name, "rate_limited")
                    record_stat(f"{self.name}:{candidate}", "rate_limited")
                    # One request per minute per model on the free tier: park
                    # this candidate and let the rotation move on.
                    self._model_cooldown[candidate] = time.monotonic() + 60
                    last_error = ProviderRateLimited(
                        f"{self.name} 429 on {candidate}", retry_after=60
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
                        self._model_cooldown[candidate] = time.monotonic() + 3600
                        record_stat(f"{self.name}:{candidate}", "consent_required")
                        last_error = ProviderUnavailable(
                            f"{self.name} 403 for {candidate}: {resp.text[:160]}"
                        )
                        break
                    # Bad key, revoked access, or an exhausted prepaid quota:
                    # retrying this provider in the same process will not help.
                    self._down = time.monotonic() + 3600
                    raise ProviderUnavailable(f"{self.name} {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 500:
                    # An overloaded model (503 "providers busy") is not a dead
                    # provider: rotate to the next candidate instead of parking
                    # the whole gateway for 30 seconds.
                    record_stat(self.name, "provider_busy")
                    last_error = ProviderUnavailable(f"{self.name} {resp.status_code} on {candidate}")
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
                self._down = time.monotonic() + 60
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
        self, system: str, prompt: str, *, json_mode: bool, max_tokens: int, model: str | None = None
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
        async with httpx.AsyncClient(timeout=self._timeout, proxy=self._proxy or None) as client:
            resp = await client.post(f"{self._url}/api/chat", json=body)
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

    async def _resolve(self) -> _Provider | None:
        if self._resolved is not None:
            try:
                if await self._resolved.available():
                    return self._resolved
            except Exception as exc:  # noqa: BLE001 - a probe must never raise
                log.debug("provider %s re-probe failed: %s", self._resolved.name, exc)
            self._resolved = None
        for provider in self._providers:
            try:
                if await provider.available():
                    self._resolved = provider
                    log.info("llm provider: %s", provider.name)
                    return provider
            except Exception as exc:  # noqa: BLE001 - probing must never raise
                log.debug("provider %s probe failed: %s", provider.name, exc)
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
        provider = await self._resolve()
        if provider is None:
            return {
                "ok": False,
                "provider": None,
                "detail": "offline (deterministic composer)",
            }
        try:
            started = time.monotonic()
            text = await provider.complete(
                "Отвечай одним словом.", "ping", json_mode=False, max_tokens=8
            )
            latency = int((time.monotonic() - started) * 1000)
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

    async def complete_text(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1500,
        offline_fallback: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        last_error: Exception | None = None
        for attempt in range(self._settings.llm_max_retries):
            provider = await self._resolve()
            if provider is None:
                break
            try:
                started = time.monotonic()
                text = await provider.complete(
                    system, prompt, json_mode=False, max_tokens=max_tokens, model=model
                )
                record_stat(provider.name, "calls")
                record_stat(provider.name, "retries", float(attempt))
                return LLMResponse(
                    text,
                    provider.name,
                    model or getattr(provider, "_model", "?"),
                    int((time.monotonic() - started) * 1000),
                )
            except ProviderRateLimited as exc:
                last_error = exc
                self._resolved = None
                await asyncio.sleep(min(exc.retry_after, 20))
            except Exception as exc:  # noqa: BLE001 - failover is the point
                last_error = exc
                record_stat(provider.name, "errors")
                self._resolved = None
                await asyncio.sleep(0.5 * (2**attempt))

        if offline_fallback is not None:
            log.warning("llm failed (%s), using offline fallback", last_error)
            record_stat("offline", "calls")
            return LLMResponse(offline_fallback, "offline", "none", 0)
        raise ProviderUnavailable(f"all llm providers failed: {last_error}")

    async def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 2000,
        offline_fallback: dict | None = None,
        model: str | None = None,
    ) -> tuple[dict, LLMResponse]:
        last_error: Exception | None = None
        for attempt in range(self._settings.llm_max_retries):
            provider = await self._resolve()
            if provider is None:
                break
            try:
                started = time.monotonic()
                raw = await provider.complete(
                    system, prompt, json_mode=True, max_tokens=max_tokens, model=model
                )
                parsed = extract_json(raw)
                if not isinstance(parsed, dict):
                    raise ProviderUnavailable("expected a JSON object")
                record_stat(provider.name, "calls")
                return parsed, LLMResponse(
                    raw,
                    provider.name,
                    model or getattr(provider, "_model", "?"),
                    int((time.monotonic() - started) * 1000),
                )
            except ProviderRateLimited as exc:
                last_error = exc
                self._resolved = None
                await asyncio.sleep(min(exc.retry_after, 20))
            except Exception as exc:  # noqa: BLE001 - retry then failover
                last_error = exc
                record_stat(provider.name, "errors")
                self._resolved = None
                await asyncio.sleep(0.5 * (2**attempt))

        if offline_fallback is not None:
            log.warning("llm json failed (%s), using offline fallback", last_error)
            record_stat("offline", "calls")
            return offline_fallback, LLMResponse("", "offline", "none", 0)
        raise ProviderUnavailable(f"all llm providers failed: {last_error}")


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

"""OpenCode Zen (OpenCode Console inference) provider.

Zen is an OpenAI/Anthropic/Gemini-compatible gateway. Every model is served by
exactly one API family, so the provider routes on the model id:

    gpt-*            -> POST /inference/openai/v1/responses
    claude-*, qwen-* -> POST /inference/anthropic/v1/messages
    gemini-*         -> POST /inference/google/v1beta/models/<m>:generateContent
    everything else  -> POST /inference/openai/v1/chat/completions

Auth is `Authorization: Bearer <token>`. Free chat models work without a token,
which is what makes this provider usable in the farm's degrade-gracefully chain.
"""

from __future__ import annotations

import json
import time

from tgfarm.core.errors import ProviderRateLimited, ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.settings import get_settings
from tgfarm.llm.client import _Provider, record_stat
from tgfarm.llm.extract import extract_json

log = get_logger(__name__)

BASE_URL = "https://opencode.ai/inference"
MODELS_URL = f"{BASE_URL}/v1/models"

# Model id prefix -> API family. Longest prefix wins.
FAMILY_PREFIXES: list[tuple[str, str]] = [
    ("gpt-", "openai_responses"),
    ("o1", "openai_responses"),
    ("o3", "openai_responses"),
    ("o4", "openai_responses"),
    ("claude-", "anthropic"),
    ("qwen", "anthropic"),
    ("gemini-", "gemini"),
]

DEFAULT_MODEL = "claude-sonnet-4-6"
# Cheap, fast, good enough for gates and routing.
FAST_MODEL = "claude-haiku-4-5"
# Used when the operator wants generation quality over cost.
QUALITY_MODEL = "claude-sonnet-4-6"


def api_family(model: str) -> str:
    lowered = (model or "").lower()
    for prefix, family in FAMILY_PREFIXES:
        if lowered.startswith(prefix):
            return family
    return "openai_chat"


def is_paid(model: str) -> bool:
    """Free models carry `-free` in their id. Even those need a key here: the
    free tier is restricted to requests made from inside OpenCode itself."""
    return "-free" not in (model or "").lower()


def free_only_setting(fallback: bool = False) -> bool:
    """`OPENCODE_ZEN_FREE_ONLY`, read when a request is built rather than at import.

    The panel rewrites `.env` and resets the settings cache, but the built LLM
    chain is not rebuilt, so a value frozen in `__init__` would only take effect
    on a restart — a knob that is alive for a new process and dead for the one
    the operator is looking at. `fallback` is the value the provider was built
    with, used when the settings object cannot answer (a partially stubbed
    `get_settings` in a test), so a stub never silently disables the spend cap.
    """
    try:
        return bool(get_settings().opencode_zen_free_only)
    except (AttributeError, TypeError, ValueError):
        return bool(fallback)


class OpenCodeZen(_Provider):
    name = "opencode-zen"

    def __init__(self, token: str, model: str, timeout: float, free_only: bool = False) -> None:
        self._token = token.strip()
        self._model = model or DEFAULT_MODEL
        self._timeout = timeout
        # The value the chain was built with. The live setting wins on every
        # call; this is the fallback when settings cannot be read.
        self._free_only = bool(free_only)
        self._down = 0.0
        self._resolved_model = ""

    async def available(self) -> bool:
        # Without a service-account key every call fails: paid models answer 401,
        # and the free tier answers 403 "free tier can only be used from within
        # OpenCode". A standalone service has to authenticate, so skip the
        # provider outright instead of paying a round trip per LLM call.
        return bool(self._token) and time.monotonic() > self._down

    async def resolve_model(self, want_free_only: bool = False) -> str:
        """Pick a concrete model id, preferring the configured one."""
        if self._resolved_model and not want_free_only:
            return self._resolved_model
        # A resolved free model stays cached too: re-listing the catalogue on
        # every completion to prove the same fact would cost a request per call.
        if self._resolved_model and not is_paid(self._resolved_model):
            return self._resolved_model
        candidates = await self.list_models(self._token)
        if not candidates:
            self._resolved_model = self._model
            return self._resolved_model

        ids = [c["id"] for c in candidates]
        if want_free_only:
            free = [i for i in ids if i.endswith("-free")]
            self._resolved_model = free[0] if free else (ids[0] if ids else self._model)
        elif self._model in ids:
            self._resolved_model = self._model
        else:
            # configured model was retired — fall back to the quality default
            self._resolved_model = QUALITY_MODEL if QUALITY_MODEL in ids else ids[0]
            log.warning("model %s unavailable on Zen, using %s", self._model, self._resolved_model)
        return self._resolved_model

    async def list_models(self, token: str = "") -> list[dict]:
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            await RATE.http.take()
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(MODELS_URL, headers=headers)
                resp.raise_for_status()
                return resp.json().get("data", [])
        except Exception as exc:  # noqa: BLE001 - listing is best-effort
            log.debug("zen model list failed: %s", exc)
            return []

    # ------------------------------------------------------------------ routing
    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        json_mode: bool,
        max_tokens: int,
        model: str | None = None,
    ) -> str:
        import httpx

        free_only = free_only_setting(self._free_only)
        model = model or await self.resolve_model(free_only)
        if free_only and is_paid(model):
            # A spend cap that silently falls back to the paid configured model
            # is not a cap: the catalogue could not name a `-free` model, so name
            # it down and let the chain move on instead of charging for it.
            raise ProviderUnavailable(f"zen free-only mode refuses paid model {model}")
        family = api_family(model)
        await RATE.llm.take()

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        if not self._token:
            headers.pop("Authorization")

        if json_mode:
            system = system + (
                "\n\nRespond with a single valid JSON object and nothing else. "
                "No prose, no markdown fences."
            )

        if family == "anthropic":
            url = f"{BASE_URL}/anthropic/v1/messages"
            headers["anthropic-version"] = "2023-06-01"
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        elif family == "openai_responses":
            url = f"{BASE_URL}/openai/v1/responses"
            body = {"model": model, "input": prompt, "instructions": system,
                    "max_output_tokens": max_tokens}
        elif family == "gemini":
            url = f"{BASE_URL}/google/v1beta/models/{model}:generateContent"
            body = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "systemInstruction": {"parts": [{"text": system}]},
                "generationConfig": {
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json" if json_mode else "text/plain",
                },
            }
        else:
            url = f"{BASE_URL}/openai/v1/chat/completions"
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.4,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=body)

        if resp.status_code == 429:
            self._down = time.monotonic() + 60
            raise ProviderRateLimited(f"zen 429 for {model}", retry_after=60)
        if resp.status_code in (401, 403):
            self._down = time.monotonic() + 300
            raise ProviderUnavailable(f"zen auth failed ({resp.status_code}) for {model}")
        if resp.status_code >= 500:
            self._down = time.monotonic() + 30
            raise ProviderUnavailable(f"zen {resp.status_code} for {model}")
        if resp.status_code != 200:
            raise ProviderUnavailable(
                f"zen {resp.status_code} for {model}: {resp.text[:200]}"
            )

        text = self._extract(resp.json(), family)
        record_stat(self.name, "tokens_out", float(len(text) / 3.6))
        return text

    @staticmethod
    def _extract(data: dict, family: str) -> str:
        if family == "anthropic":
            chunks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "\n".join(chunks)

        if family == "openai_responses":
            chunks: list[str] = []
            for item in data.get("output", []) or []:
                for block in item.get("content", []) or []:
                    if block.get("type") in ("output_text", "text"):
                        chunks.append(block.get("text", ""))
            if chunks:
                return "\n".join(chunks)
            return data.get("output_text", "") or ""

        if family == "gemini":
            candidates = data.get("candidates", []) or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", []) or []
            return "\n".join(p.get("text", "") for p in parts)

        choices = data.get("choices", []) or []
        if not choices:
            return ""
        message = choices[0].get("message", {}) or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # some gateways return content parts
            return "\n".join(part.get("text", "") for part in content)
        return ""


def prompt_json(system: str, user: str) -> str:
    """Helper for callers that want a validated JSON object out of Zen."""
    payload = extract_json(user)
    if not isinstance(payload, dict):
        raise ProviderUnavailable("zen returned a non-object payload")
    return json.dumps(payload, ensure_ascii=False)
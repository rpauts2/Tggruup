"""Bounded client-level attempts; providers own only model/format negotiation."""
from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tgfarm.core.errors import ProviderRateLimited, ProviderUnavailable


class PermanentProviderError(ProviderUnavailable):
    """A request/auth error for which retrying the same call cannot help."""


def retry_after(value: str | None, default: float = 60.0) -> float:
    """Accept HTTP delta-seconds or date, ignoring malformed/non-finite input."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(value)
            delay = (moment.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    return max(0.0, delay) if math.isfinite(delay) else default


def circuit_available(client, provider) -> bool:
    failures, until, probing = getattr(client, "_circuits", {}).get(provider, (0, 0.0, False))
    return not probing and time.monotonic() >= until


async def attempt(client, system, prompt, *, json_mode, max_tokens, model, parse):
    """One total deadline, bounded backoff, and one half-open recovery probe."""
    deadline = time.monotonic() + max(0.01, float(client._settings.llm_timeout_s))
    attempts = max(1, int(client._settings.llm_max_retries))
    last_error = None
    failed = set()
    permanent = set()
    if not hasattr(client, "_circuits"):
        client._circuits = {}
    for number in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        provider = None
        claimed_probe = False
        try:
            async with asyncio.timeout(remaining):
                provider = await client._resolve(exclude=failed)
                if provider is None and failed:
                    # All alternatives were tried; only transient failures may repeat.
                    failed = {p for p in failed if p in permanent}
                    provider = await client._resolve(exclude=failed)
                if provider is None:
                    break
                if not circuit_available(client, provider):
                    break
                failures, until, _ = client._circuits.get(provider, (0, 0.0, False))
                if failures >= 3:
                    client._circuits[provider] = (failures, until, True)
                    claimed_probe = True
                started = time.monotonic()
                raw = await provider.complete(
                    system, prompt, json_mode=json_mode, max_tokens=max_tokens, model=model
                )
                parsed = parse(raw)
                client._circuits[provider] = (0, 0.0, False)
                return parsed, raw, provider, int((time.monotonic() - started) * 1000), number
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Retry only within the same finite deadline.
            last_error = exc
            if provider is not None:
                failures, _, _ = client._circuits.get(provider, (0, 0.0, False))
                failures += 1
                cooldown = max(0.0, exc.retry_after) if isinstance(exc, ProviderRateLimited) else 0.0
                if failures >= 3:
                    cooldown = max(cooldown, 30.0)
                client._circuits[provider] = (
                    failures, time.monotonic() + cooldown if cooldown else 0.0, False
                )
            client._resolved = None
            if provider is not None:
                failed.add(provider)
                if isinstance(exc, PermanentProviderError):
                    permanent.add(provider)
            if number + 1 >= attempts:
                break
            # Availability probes share the same total deadline as completion.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(remaining):
                    backup = await client._resolve(exclude=failed)
            except TimeoutError:
                break
            if backup is not None:
                continue
            if isinstance(exc, PermanentProviderError):
                if failed - permanent:
                    # A rejected backup must not consume a transient primary's
                    # remaining attempts. Permanent providers stay excluded.
                    continue
                raise
            delay = (max(0.0, exc.retry_after) if isinstance(exc, ProviderRateLimited)
                     else min(8.0, 0.5 * 2**number) + random.uniform(0, 0.25))
            remaining = deadline - time.monotonic()
            if delay >= remaining:
                break
            await asyncio.sleep(delay)
        finally:
            # Cancellation and permanent errors must not strand a half-open probe.
            if claimed_probe:
                failures, until, _ = client._circuits[provider]
                client._circuits[provider] = (failures, until, False)
    raise ProviderUnavailable(
        "provider unavailable (" + (type(last_error).__name__ if last_error else "no provider") + ")"
    )

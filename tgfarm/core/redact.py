"""Log-layer secret redaction (W24-S).

Call sites are unreliable guards: the pipeline already learned to wrap some
provider failures in ``core.security.redact_sensitive_text``, but dozens of
``log.warning("... %s", exc)`` sites still interpolate raw exception text, and
an ``exc_info`` traceback can carry a credential-bearing URL in the exception
message. This module moves the guard into the logging layer itself: every
formatted log line passes through :func:`redact_log_text` before it reaches a
handler, regardless of which call site produced it.

Deliberate stdout output (``tgfarm api-token``, the serve banner) does not go
through a logging formatter, so it is unaffected by construction.
"""

from __future__ import annotations

import logging
import os
import re
import threading

from tgfarm.core.security import redact_sensitive_text

REDACTION = "[REDACTED]"

# Registered/derived secrets shorter than this are ignored: tiny values would
# shred ordinary log text and none of the real tokens are this short.
_MIN_SECRET_LEN = 12

# Environment variables whose values are secrets by naming convention.
_ENV_SECRET_NAME = re.compile(r"(?i)(token|secret|api_?key|password|cookie)")

# Header-shaped patterns the core.security patterns do not cover.
_LOG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*[^\r\n\"';]+"), r"\1: " + REDACTION),
    (re.compile(r"(?i)(x-tgfarm-token[\"']?\s*[:=]\s*[\"']?)\S+"), r"\1" + REDACTION),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), REDACTION),
)

_lock = threading.Lock()
_secrets: set[str] = set()
_autoloaded = False


def register_secret(value: str | None) -> None:
    """Add an exact secret value so raw occurrences are always suppressed."""
    text = (value or "").strip()
    if len(text) >= _MIN_SECRET_LEN:
        with _lock:
            _secrets.add(text)


def clear_registry() -> None:
    """Forget all secrets and re-arm the one-time environment autoload."""
    global _autoloaded
    with _lock:
        _secrets.clear()
        _autoloaded = False


def registered_secrets() -> frozenset[str]:
    with _lock:
        return frozenset(_secrets)


def _discover_secret_values() -> set[str]:
    """Secrets knowable without touching the call sites: env vars and the token file."""
    values: set[str] = set()
    for name, value in os.environ.items():
        if _ENV_SECRET_NAME.search(name):
            text = value.strip()
            if len(text) >= _MIN_SECRET_LEN:
                values.add(text)
    try:
        from tgfarm.core.apiauth import token_path

        raw = token_path().read_text(encoding="utf-8").strip()
    except Exception:  # no settings, no file, unpickable path: nothing to register
        raw = ""
    if len(raw) >= _MIN_SECRET_LEN:
        values.add(raw)
    return values


def refresh_registry() -> None:
    """Rebuild the secret registry from the environment and the admin token file."""
    clear_registry()
    found = _discover_secret_values()
    with _lock:
        _secrets.update(found)
        global _autoloaded
        _autoloaded = True


def _ensure_autoloaded() -> None:
    global _autoloaded
    with _lock:
        if _autoloaded:
            return
        _autoloaded = True
        _secrets.update(_discover_secret_values())


def redact_known_secrets(text: str) -> str:
    """Replace exact registered secret values wherever they appear."""
    _ensure_autoloaded()
    with _lock:
        secrets = tuple(_secrets)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTION)
    return text


def redact_log_text(text: str) -> str:
    """Full logging-layer chain: exact values, shared patterns, log-specific shapes."""
    if not text:
        return text
    text = redact_known_secrets(text)
    # limit keeps the shared patterns while disabling the length clamp.
    text = redact_sensitive_text(text, limit=len(text))
    for pattern, replacement in _LOG_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Wrap any base formatter and scrub its final output before handlers write it."""

    def __init__(self, base: logging.Formatter) -> None:
        super().__init__()
        self._base = base

    @property
    def base(self) -> logging.Formatter:
        return self._base

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(self._base.format(record))

    def usesTime(self) -> bool:
        return self._base.usesTime()


__all__ = [
    "REDACTION",
    "RedactingFormatter",
    "clear_registry",
    "redact_known_secrets",
    "redact_log_text",
    "refresh_registry",
    "register_secret",
    "registered_secrets",
]

"""Redaction helpers for logs and operator-facing diagnostics.

Never persist a raw provider exception: HTTP clients may include credential-bearing
URLs, Authorization headers, or provider response snippets in their messages.
"""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram Bot API tokens and MAX's /bot<TOKEN>/ URL convention.
    (re.compile(r"(?i)(https?://[^/\\s]+/bot)[^/\\s]+"), r"\\1[REDACTED]"),
    (re.compile(r"\\b\\d{5,}:[A-Za-z0-9_-]{20,}\\b"), "[REDACTED_TELEGRAM_BOT_TOKEN]"),
    # HTTP authentication headers and URL user-info.
    (re.compile(r"(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(https?://[^:\\s/@]+:)[^@\\s]+(@)"), r"\\1[REDACTED]\\2"),
    # Common credential-bearing query parameters.
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|client_secret|client_id|"
            r"api_key|apikey|password|passwd|secret|token|code)=)[^&#\\s]+"
        ),
        r"\\1[REDACTED]",
    ),
    # JSON, form, and debug key/value snippets including AT Protocol JWT names.
    (
        re.compile(
            r"(?i)([\\"']?(?:access_token|refresh_token|accessjwt|refreshjwt|"
            r"client_secret|client_id|api_key|apikey|password|passwd|"
            r"authorization|app_password|token|secret)[\\"']?\\s*[:=]\\s*[\\"']?)"
            r"[^\\"'\\s,;}]+"
        ),
        r"\\1[REDACTED]",
    ),
)


def redact_sensitive_text(value: Any, *, limit: int = 400) -> str:
    """Return bounded text with common credential forms removed."""
    text = str(value or "")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    # Keep any malformed/hostile provider response from bloating logs and DB rows.
    return text[: max(0, int(limit))]

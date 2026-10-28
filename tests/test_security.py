from __future__ import annotations

import pytest

from tgfarm.core.security import redact_sensitive_text


@pytest.mark.parametrize(
    ("value", "secrets"),
    [
        (
            "HTTP error for https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456/sendMessage",
            ["123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456"],
        ),
        (
            "Request failed Authorization: Bearer abc.def_123 and access_token=top-secret&next=1",
            ["abc.def_123", "top-secret"],
        ),
        (
            'provider body {"refresh_token":"refresh-secret", "app_password":"app-secret"}',
            ["refresh-secret", "app-secret"],
        ),
        (
            "MAX endpoint https://botapi.max.ru/botMAX_TOKEN_123/sendMessage returned 500",
            ["MAX_TOKEN_123"],
        ),
    ],
)
def test_redact_sensitive_text_removes_common_credentials(value: str, secrets: list[str]) -> None:
    redacted = redact_sensitive_text(value)
    for secret in secrets:
        assert secret not in redacted
    assert "[REDACTED]" in redacted or "[REDACTED_TELEGRAM_BOT_TOKEN]" in redacted


def test_redact_sensitive_text_bounds_output() -> None:
    assert len(redact_sensitive_text("x" * 1000, limit=100)) == 100

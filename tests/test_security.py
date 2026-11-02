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


def test_api_query_token_is_removed_without_losing_other_parameters() -> None:
    from tgfarm.core.apiauth import strip_token_from_query

    cleaned = strip_token_from_query(
        b"token=secret-value&limit=20&query=hello+world&token=second-secret"
    )
    assert cleaned == b"limit=20&query=hello+world"
    assert b"secret-value" not in cleaned
    assert b"second-secret" not in cleaned


def test_api_query_token_is_removed_when_key_is_url_encoded() -> None:
    from tgfarm.core.apiauth import strip_token_from_query

    cleaned = strip_token_from_query(b"%74oken=secret-value&limit=20")
    assert cleaned == b"limit=20"
    assert b"secret-value" not in cleaned


def test_api_query_without_token_is_preserved_verbatim() -> None:
    from tgfarm.core.apiauth import strip_token_from_query

    query = b"limit=20&query=hello%20world"
    assert strip_token_from_query(query) == query


def test_malformed_excessive_query_drops_possible_token() -> None:
    from tgfarm.core.apiauth import strip_token_from_query

    query = b"token=secret&" + b"&".join([b"x=1"] * 1025)
    assert strip_token_from_query(query) == b""


def test_public_max_webhook_host_is_not_trusted_for_the_dashboard():
    from tgfarm.api import _is_trusted_host_request

    trusted = {"127.0.0.1", "localhost", "::1"}
    public_host = "farm.example"

    assert _is_trusted_host_request(
        "localhost", trusted, public_host, method="GET", path="/"
    )
    assert _is_trusted_host_request(
        public_host,
        trusted,
        public_host,
        method="POST",
        path="/api/platforms/max/webhook",
    )
    assert not _is_trusted_host_request(
        public_host, trusted, public_host, method="GET", path="/"
    )
    assert not _is_trusted_host_request(
        public_host, trusted, public_host, method="GET", path="/api/status"
    )
    assert not _is_trusted_host_request(
        public_host,
        trusted,
        public_host,
        method="POST",
        path="/api/platforms/max/webhook/register",
    )

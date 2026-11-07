"""Retry-After has two legal encodings, and Graph throttling hides in HTTP 400.

Pinterest and Instagram parsed only the delta-seconds form, so a provider that
answered with an HTTP-date lost its cooldown. Instagram also classified every
HTTP 400 as permanent, including the Graph application-throttling codes that
Meta returns when a request is rejected before anything is created.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx


def _error(status: int, *, headers: dict | None = None, body: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.test/resource")
    response = httpx.Response(
        status,
        request=request,
        headers=headers or {},
        json=body if body is not None else {},
    )
    return httpx.HTTPStatusError("provider rejected request", request=request, response=response)


def test_pinterest_honours_an_http_date_retry_after():
    from tgfarm.s9_growth import pinterest

    retry_at = datetime.now(UTC) + timedelta(hours=2)
    result = pinterest._error_result(
        _error(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
        "create_pin",
    )

    assert result.status == "failed"
    assert result.retry_after_seconds is not None
    assert 7000 <= result.retry_after_seconds <= 7300


def test_instagram_honours_an_http_date_retry_after():
    from tgfarm.s9_growth import instagram

    retry_at = datetime.now(UTC) + timedelta(hours=2)
    result = instagram._http_result(
        "instagram",
        _error(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
        "create_media",
    )

    assert result.status == "failed"
    assert result.retry_after_seconds is not None
    assert 7000 <= result.retry_after_seconds <= 7300


def test_instagram_delta_seconds_still_work():
    from tgfarm.s9_growth import instagram

    result = instagram._http_result(
        "instagram",
        _error(429, headers={"Retry-After": "90"}),
        "create_media",
    )

    assert result.status == "failed"
    assert result.retry_after_seconds == 90


def test_instagram_graph_throttling_400_is_retryable():
    from tgfarm.s9_growth import instagram

    for code in (4, 17, 32, 613):
        result = instagram._http_result(
            "instagram",
            _error(400, body={"error": {"code": code, "message": "throttled"}}),
            "create_media",
        )
        assert result.status == "failed", code


def test_instagram_a_plain_400_stays_permanent():
    from tgfarm.s9_growth import instagram

    result = instagram._http_result(
        "instagram",
        _error(400, body={"error": {"code": 100, "message": "invalid parameter"}}),
        "create_media",
    )

    assert result.status == "permanent"


def test_instagram_a_malformed_error_body_does_not_raise():
    from tgfarm.s9_growth import instagram

    request = httpx.Request("POST", "https://api.example.test/resource")
    response = httpx.Response(400, request=request, content=b"not json")
    error = httpx.HTTPStatusError("bad body", request=request, response=response)

    assert instagram._http_result("instagram", error, "create_media").status == "permanent"

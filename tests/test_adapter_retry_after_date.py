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


def test_facebook_honours_an_http_date_retry_after():
    """Facebook kept its own int-only parser after the other adapters were fixed.

    A Graph 429 that answers with a date is a cooldown the publisher must not
    shorten to its own default backoff.
    """
    from tgfarm.s9_growth import facebook

    retry_at = datetime.now(UTC) + timedelta(hours=2)
    result = facebook._error_result(
        _error(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
        "feed",
    )

    assert result.status == "failed"
    assert result.retry_after_seconds is not None
    assert 7000 <= result.retry_after_seconds <= 7300


def test_facebook_delta_seconds_still_work():
    from tgfarm.s9_growth import facebook

    result = facebook._error_result(_error(429, headers={"Retry-After": "45"}), "feed")

    assert result.retry_after_seconds == 45


async def test_tiktok_honours_an_http_date_retry_after():
    """TikTok read `Retry-After` with int(float(...)) and dropped the date form."""
    import pytest

    from tgfarm.tiktok_publish import TikTokProviderError, _provider_json

    request = httpx.Request("POST", "https://open.tiktokapis.com/v2/video/init/")
    retry_at = datetime.now(UTC) + timedelta(hours=2)
    response = httpx.Response(
        429,
        request=request,
        headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
        content=b"",
    )

    with pytest.raises(TikTokProviderError) as caught:
        await _provider_json(response, "video/init")

    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds is not None
    assert 7000 <= caught.value.retry_after_seconds <= 7300

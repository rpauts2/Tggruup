"""A failed Tumblr token refresh is not automatically a permanent failure.

The adapter used to record every refresh exception as `permanent`, including a
network timeout or a 503 from the token endpoint - states in which no publish
request exists at all. The distinction that matters is whether the grant is
dead (4xx from the token endpoint, operator must reconnect) or the exchange
merely failed (safe to retry).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload
from tgfarm.tumblr_oauth import TumblrOAuthError


def _settings():
    return SimpleNamespace(
        tumblr_client_id="client-id",
        tumblr_client_secret="client-secret",
        tumblr_blog_identifier="demo-blog",
        tumblr_access_token="access-secret",
        tumblr_refresh_token="refresh-secret",
        tumblr_blog_url="https://demo-blog.tumblr.com/",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TumblrOAuthError("invalid_grant", status_code=400), "permanent"),
        (TumblrOAuthError("revoked", status_code=401), "permanent"),
        (TumblrOAuthError("token endpoint down", status_code=503), "failed"),
        (RuntimeError("network error"), "failed"),
    ],
)
async def test_token_refresh_failure_keeps_its_retryability(monkeypatch, error, expected):
    from tgfarm.s9_growth import adapters, tumblr

    monkeypatch.setattr(tumblr, "get_settings", _settings)

    async def token():
        raise error

    monkeypatch.setattr(tumblr, "get_valid_tumblr_access_token", token)

    result = await adapters.ADAPTERS["tumblr"].publish(
        CrosspostPayload(platform="tumblr", title="T", text="Body")
    )

    assert result.status == expected
    assert result.remote_id is None


class _RefreshClient:
    status_code = 400

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        return httpx.Response(self.status_code, request=httpx.Request("POST", url), json={
            "error": "invalid_grant",
        })


@pytest.mark.asyncio
async def test_the_token_endpoint_status_reaches_the_adapter(monkeypatch):
    from tgfarm import tumblr_oauth

    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(
            tumblr_access_token="expired",
            tumblr_refresh_token="refresh",
            tumblr_client_id="client-id",
            tumblr_client_secret="client-secret",
            tumblr_token_expires_at="1",
        ),
    )
    monkeypatch.setattr(tumblr_oauth.httpx, "AsyncClient", _RefreshClient)

    with pytest.raises(TumblrOAuthError) as caught:
        await tumblr_oauth.get_valid_tumblr_access_token()

    assert caught.value.status_code == 400

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse({
            "id": "123456",
            "url": "https://mastodon.example/@demo/123456",
        })


@pytest.mark.asyncio
async def test_mastodon_adapter_posts_native_status_with_idempotency_key(monkeypatch):
    import httpx
    from tgfarm.s9_growth import adapters

    FakeClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        mastodon_instance="https://mastodon.example",
        mastodon_access_token="private-token",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await adapters.MastodonAdapter().publish(CrosspostPayload(
        platform="mastodon", title="Hello Mastodon", text="A useful short post"
    ))

    assert result.status == "posted"
    assert result.remote_id == "123456"
    assert result.url == "https://mastodon.example/@demo/123456"
    assert len(FakeClient.calls) == 1
    url, kwargs = FakeClient.calls[0]
    assert url == "https://mastodon.example/api/v1/statuses"
    assert kwargs["headers"]["Authorization"] == "Bearer private-token"
    assert kwargs["headers"]["Idempotency-Key"]
    assert kwargs["data"]["status"] == "Hello Mastodon\n\nA useful short post"
    assert "private-token" not in repr(result)


@pytest.mark.asyncio
async def test_mastodon_adapter_skips_without_oauth_credentials(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        mastodon_instance="", mastodon_access_token=""
    ))
    result = await adapters.MastodonAdapter().publish(CrosspostPayload(
        platform="mastodon", title="Hello", text="Body"
    ))
    assert result.status == "skipped"

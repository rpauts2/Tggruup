from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeMAXResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeMAXClient:
    calls = []
    init_kwargs = {}

    def __init__(self, *args, **kwargs):
        self.__class__.init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.__class__.calls.append((url, kwargs))
        return FakeMAXResponse({
            "message": {
                "body": {"mid": "max-message-77"},
                "recipient": {"chat_id": 10001},
            }
        })


def _payload():
    from tgfarm.core.schemas import CrosspostPayload

    return CrosspostPayload(
        platform="max",
        title="MAX publication",
        text="A source-backed message.",
        link=None,
        image_path=None,
        hashtags=[],
        meta={"post_public_id": "max-test-post"},
    )


@pytest.mark.asyncio
async def test_max_adapter_uses_current_messages_endpoint_and_never_puts_token_in_url(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMAXClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        max_access_token="max-private-token",
        max_channel_id="10001",
    ))
    monkeypatch.setattr("httpx.AsyncClient", FakeMAXClient)

    result = await adapters.MaxAdapter().publish(_payload())

    assert result.status == "posted"
    assert result.remote_id == "max-message-77"
    assert FakeMAXClient.calls[0][0] == "https://platform-api2.max.ru/messages"
    params = FakeMAXClient.calls[0][1]["params"]
    assert params["chat_id"] == "10001"
    assert FakeMAXClient.calls[0][1]["json"]["text"] == "MAX publication\n\nA source-backed message."
    assert FakeMAXClient.init_kwargs["headers"]["Authorization"] == "max-private-token"
    assert "max-private-token" not in FakeMAXClient.calls[0][0]
    assert "max-private-token" not in str(params)

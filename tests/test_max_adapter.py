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
    response_payload = {
        "message": {
            "body": {"mid": "max-message-77"},
            "recipient": {"chat_id": 10001},
        }
    }

    def __init__(self, *args, **kwargs):
        self.__class__.init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.__class__.calls.append((url, kwargs))
        return FakeMAXResponse(self.__class__.response_payload)


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
        max_channel_blocked=False,
    ))
    async def verify_permission(_token, _chat_id):
        return {"id": "10001", "name": "News channel", "role": "admin"}

    monkeypatch.setattr("tgfarm.max_api.select_max_chat", verify_permission)
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


@pytest.mark.asyncio
async def test_max_adapter_refuses_to_publish_after_target_permission_revocation(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMAXClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        max_access_token="max-private-token",
        max_channel_id="10001",
        max_channel_blocked=True,
    ))
    monkeypatch.setattr("httpx.AsyncClient", FakeMAXClient)

    result = await adapters.MaxAdapter().publish(_payload())

    assert result.status == "skipped"
    assert FakeMAXClient.calls == []


@pytest.mark.asyncio
async def test_max_adapter_does_not_claim_posted_without_message_id(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMAXClient.calls = []
    FakeMAXClient.response_payload = {"message": {"body": {"text": "sent"}}}
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        max_access_token="max-private-token",
        max_channel_id="10001",
        max_channel_blocked=False,
    ))
    monkeypatch.setattr("httpx.AsyncClient", FakeMAXClient)

    result = await adapters.MaxAdapter().publish(_payload())

    assert result.status == "unknown"
    assert not result.remote_id


@pytest.mark.asyncio
async def test_max_adapter_treats_explicit_provider_rejection_as_permanent(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMAXClient.calls = []
    FakeMAXClient.response_payload = {"success": False, "message": "permission denied"}
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        max_access_token="max-private-token",
        max_channel_id="10001",
        max_channel_blocked=False,
    ))
    monkeypatch.setattr("httpx.AsyncClient", FakeMAXClient)

    result = await adapters.MaxAdapter().publish(_payload())

    assert result.status == "permanent"
    assert "rejected" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_max_adapter_refuses_to_send_when_write_permission_is_revoked(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMAXClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        max_access_token="max-private-token",
        max_channel_id="10001",
        max_channel_blocked=False,
    ))

    async def no_permission(_token, _chat_id):
        raise RuntimeError("MAX permission denied")

    monkeypatch.setattr("tgfarm.max_api.select_max_chat", no_permission)
    monkeypatch.setattr("httpx.AsyncClient", FakeMAXClient)

    result = await adapters.MaxAdapter().publish(_payload())

    assert result.status == "permanent"
    assert "write permission" in (result.error or "").lower()
    assert FakeMAXClient.calls == []


@pytest.mark.asyncio
async def test_max_adapter_caps_total_text_not_just_body(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMAXClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        max_access_token="max-private-token",
        max_channel_id="10001",
        max_channel_blocked=False,
    ))

    async def verify_permission(_token, _chat_id):
        return {"id": "10001", "name": "News channel", "role": "admin"}

    monkeypatch.setattr("tgfarm.max_api.select_max_chat", verify_permission)
    monkeypatch.setattr("httpx.AsyncClient", FakeMAXClient)
    payload = _payload()
    payload.title = "Title " * 100
    payload.text = "Body " * 1000

    result = await adapters.MaxAdapter().publish(payload)

    assert result.status == "posted"
    assert len(FakeMAXClient.calls[0][1]["json"]["text"]) <= 4000

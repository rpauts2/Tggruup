from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    status_code = 200
    content = b'{"id":"123456789012345678","guild_id":"223456789012345678","channel_id":"323456789012345678"}'

    def json(self):
        return {
            "id": "123456789012345678",
            "guild_id": "223456789012345678",
            "channel_id": "323456789012345678",
        }

    def raise_for_status(self):
        return None


class FakeDiscordClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


@pytest.mark.asyncio
async def test_discord_adapter_sends_confirmed_message_without_mass_mentions(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeDiscordClient.calls = []
    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(
            discord_webhook_url="https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopQRSTUV"
        ),
    )
    monkeypatch.setattr(httpx, "AsyncClient", FakeDiscordClient)

    result = await adapters.ADAPTERS["discord"].publish(CrosspostPayload(
        platform="discord",
        title="TgFarm update",
        text="A useful announcement",
    ))

    assert result.status == "posted"
    assert result.remote_id == "123456789012345678"
    assert result.url == "https://discord.com/channels/223456789012345678/323456789012345678/123456789012345678"
    url, kwargs = FakeDiscordClient.calls[0]
    assert url.startswith("https://discord.com/api/webhooks/")
    assert kwargs["params"] == {"wait": "true"}
    assert kwargs["json"]["content"] == "TgFarm update\n\nA useful announcement"
    assert kwargs["json"]["allowed_mentions"] == {"parse": []}


def test_discord_adapter_rejects_non_discord_webhook_hosts(monkeypatch):
    from tgfarm.s9_growth.adapters import DiscordAdapter

    monkeypatch.setattr(
        "tgfarm.s9_growth.adapters.get_settings",
        lambda: SimpleNamespace(
            discord_webhook_url="https://attacker.example/api/webhooks/123456789012345678/abcdefghijklmnopQRSTUV"
        ),
    )
    assert DiscordAdapter().configured() is False

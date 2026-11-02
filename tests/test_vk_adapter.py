from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeVKClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse({"response": {"post_id": 987654}})


def _payload():
    from tgfarm.core.schemas import CrosspostPayload

    return CrosspostPayload(
        platform="vk",
        title="Проверка публикации",
        text="Тестируем правильный адресат.",
        link=None,
        image_path=None,
        hashtags=[],
        meta={"post_public_id": "vk-test-post"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["123456789", "-123456789"])
async def test_vk_wall_post_uses_negative_owner_id_for_community(monkeypatch, group_id):
    from tgfarm.s9_growth import adapters

    FakeVKClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        vk_access_token="vk-access-token",
        vk_group_id=group_id,
    ))
    monkeypatch.setattr("httpx.AsyncClient", FakeVKClient)

    result = await adapters.VkAdapter().publish(_payload())

    assert result.status == "posted"
    assert result.remote_id == "987654"
    assert result.url == "https://vk.com/wall-123456789_987654"
    assert len(FakeVKClient.calls) == 1
    url, kwargs = FakeVKClient.calls[0]
    assert url == "https://api.vk.com/method/wall.post"
    assert kwargs["data"]["owner_id"] == -123456789
    assert kwargs["data"]["from_group"] == 1


@pytest.mark.asyncio
async def test_vk_wall_post_refuses_non_numeric_community_id(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeVKClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        vk_access_token="vk-access-token",
        vk_group_id="not-a-number",
    ))
    monkeypatch.setattr("httpx.AsyncClient", FakeVKClient)

    result = await adapters.VkAdapter().publish(_payload())

    assert result.status == "permanent"
    assert "numeric" in (result.error or "").lower()
    assert FakeVKClient.calls == []

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest


class FakeVKResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeVKReadClient:
    calls = []
    groups = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/method/groups.get"):
            return FakeVKResponse({
                "response": {
                    "count": len(self.groups),
                    "items": self.groups,
                }
            })
        return FakeVKResponse({"error": {"error_code": 100}})


@pytest.mark.asyncio
async def test_vk_group_discovery_returns_only_admin_and_editor_communities(monkeypatch):
    from tgfarm import platform_auth

    FakeVKReadClient.calls = []
    FakeVKReadClient.groups = [
        {"id": 101, "name": "Owner group", "screen_name": "owner_group", "is_admin": 1, "is_editor": 0},
        {"id": 202, "name": "Editor group", "screen_name": "editor_group", "is_admin": 0, "is_editor": 1},
        {"id": 303, "name": "Member only", "screen_name": "member_only", "is_admin": 0, "is_editor": 0},
    ]
    monkeypatch.setattr(platform_auth, "get_settings", lambda: SimpleNamespace(vk_access_token="private-vk-token"))
    monkeypatch.setattr(httpx, "AsyncClient", FakeVKReadClient)

    groups = await platform_auth.list_vk_groups()

    assert [item["id"] for item in groups] == ["101", "202"]
    assert [item["role"] for item in groups] == ["admin", "editor"]
    assert all(item["can_publish"] is True for item in groups)
    assert all("token" not in item for item in groups)
    url, kwargs = FakeVKReadClient.calls[0]
    assert url == "https://api.vk.com/method/groups.get"
    assert kwargs["params"]["filter"] == "admin,editor"
    assert kwargs["params"]["access_token"] == "private-vk-token"


@pytest.mark.asyncio
async def test_vk_group_selection_rechecks_current_manage_role(monkeypatch):
    from tgfarm import platform_auth

    FakeVKReadClient.groups = [
        {"id": 101, "name": "Owner group", "is_admin": 1, "is_editor": 0},
    ]
    monkeypatch.setattr(platform_auth, "get_settings", lambda: SimpleNamespace(vk_access_token="vk-token"))
    monkeypatch.setattr(httpx, "AsyncClient", FakeVKReadClient)

    selected = await platform_auth.select_vk_group("101")
    assert selected == {"id": "101", "name": "Owner group", "role": "admin"}

    with pytest.raises(RuntimeError, match="роль"):
        await platform_auth.select_vk_group("202")


@pytest.mark.asyncio
async def test_vk_group_selection_rejects_non_numeric_ids():
    from tgfarm.platform_auth import select_vk_group

    with pytest.raises(ValueError, match="числовым"):
        await select_vk_group("https://vk.com/public123")

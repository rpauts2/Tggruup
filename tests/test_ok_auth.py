from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_ok_auth_check_verifies_user_group_and_permissions(monkeypatch):
    from tgfarm import platform_auth

    calls = []

    async def fake_api(client, method, params=None):
        calls.append((method, params or {}))
        if method == "users.getCurrentUser":
            return "user-123"
        if method == "group.getInfo":
            return [{"uid": "group-456", "name": "TgFarm group", "url": "ok.ru/tgfarm"}]
        raise AssertionError("unexpected OK method " + method)

    permissions = {
        "GROUP_CONTENT": True,
        "PHOTO_CONTENT": True,
        "VIDEO_CONTENT": False,
    }

    async def fake_permission(client, permission):
        return permissions[permission]

    monkeypatch.setattr(
        platform_auth,
        "get_settings",
        lambda: SimpleNamespace(
            ok_application_key="key",
            ok_application_secret="secret",
            ok_access_token="token",
            ok_session_secret_key="session-secret",
            ok_group_id="group-456",
        ),
    )
    from tgfarm.s9_growth import odnoklassniki
    monkeypatch.setattr(odnoklassniki, "ok_api_call", fake_api)
    monkeypatch.setattr(odnoklassniki, "ok_has_permission", fake_permission)
    monkeypatch.setattr(
        platform_auth,
        "record_auth_check",
        lambda *args, **kwargs: {"checked_at": "2026-10-31T00:00:00+00:00"},
    )

    result = await platform_auth.check_platform_auth("odnoklassniki")

    assert result["authorized"] is True
    assert result["account"] == "OK · TgFarm group · user user-123"
    assert "GROUP_CONTENT" in result["detail"]
    assert "PHOTO_CONTENT доступен" in result["detail"]
    assert "VIDEO_CONTENT не выдан" in result["detail"]
    assert [method for method, _ in calls] == ["users.getCurrentUser", "group.getInfo"]

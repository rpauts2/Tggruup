from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_facebook_auth_check_verifies_selected_page_and_content_task(monkeypatch):
    from tgfarm import facebook_oauth, platform_auth

    async def get_page_token():
        return "page-token"

    async def response_json(client, url, params=None):
        assert url.endswith("/v26.0/123456789012345")
        assert params == {"fields": "id,name,tasks"}
        return {
            "id": "123456789012345",
            "name": "TgFarm Page",
            "tasks": ["CREATE_CONTENT", "MODERATE"],
        }

    monkeypatch.setattr(
        platform_auth,
        "get_settings",
        lambda: SimpleNamespace(
            facebook_page_id="123456789012345",
            facebook_page_access_token="saved-page-token",
            facebook_page_name="TgFarm Page",
        ),
    )
    monkeypatch.setattr(facebook_oauth, "get_valid_facebook_page_token", get_page_token)
    monkeypatch.setattr(platform_auth, "_response_json", response_json)
    monkeypatch.setattr(
        platform_auth,
        "record_auth_check",
        lambda *args, **kwargs: {"checked_at": "2026-10-31T00:00:00+00:00"},
    )

    result = await platform_auth.check_platform_auth("facebook")

    assert result["authorized"] is True
    assert result["account"] == "TgFarm Page"
    assert "CREATE_CONTENT" in result["detail"]


@pytest.mark.asyncio
async def test_facebook_auth_check_rejects_different_page_id(monkeypatch):
    from tgfarm import facebook_oauth, platform_auth

    async def get_page_token():
        return "page-token"

    async def response_json(client, url, params=None):
        return {
            "id": "999999999999999",
            "name": "Not the selected Page",
            "tasks": ["CREATE_CONTENT"],
        }

    monkeypatch.setattr(
        platform_auth,
        "get_settings",
        lambda: SimpleNamespace(
            facebook_page_id="123456789012345",
            facebook_page_access_token="saved-page-token",
            facebook_page_name="TgFarm Page",
        ),
    )
    monkeypatch.setattr(facebook_oauth, "get_valid_facebook_page_token", get_page_token)
    monkeypatch.setattr(platform_auth, "_response_json", response_json)
    monkeypatch.setattr(
        platform_auth,
        "record_auth_check",
        lambda *args, **kwargs: {"checked_at": "2026-10-31T00:00:00+00:00"},
    )

    result = await platform_auth.check_platform_auth("facebook")

    assert result["authorized"] is False
    assert "выбранную Page" in result["detail"]

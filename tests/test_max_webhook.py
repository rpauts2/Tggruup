from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest


def test_max_webhook_url_requires_public_https_origin_on_443():
    from tgfarm.max_api import build_max_webhook_url

    assert build_max_webhook_url("https://farm.example") == (
        "https://farm.example/api/platforms/max/webhook"
    )
    assert build_max_webhook_url("https://farm.example/") == (
        "https://farm.example/api/platforms/max/webhook"
    )
    for value in (
        "http://farm.example",
        "https://localhost",
        "https://127.0.0.1",
        "https://farm.example:8443",
        "https://farm.example/path",
        "https://farm.example?token=secret",
    ):
        with pytest.raises(ValueError):
            build_max_webhook_url(value)


def test_max_webhook_event_tracker_adds_renames_and_removes_only_group_targets():
    from tgfarm.max_api import apply_max_webhook_event

    added, changed = apply_max_webhook_event([], {
        "update_type": "bot_added",
        "chat_id": 10001,
        "is_channel": True,
    })
    assert changed is True
    assert added[0]["id"] == "10001"
    assert added[0]["kind"] == "channel"
    assert added[0]["needs_permission_check"] is True

    private_dialog, changed = apply_max_webhook_event([], {
        "update_type": "bot_started",
        "chat_id": 10002,
    })
    assert private_dialog == []
    assert changed is False

    renamed, changed = apply_max_webhook_event(added, {
        "update_type": "chat_title_changed",
        "chat_id": 10001,
        "title": "News channel",
    })
    assert changed is True
    assert renamed[0]["name"] == "News channel"
    assert renamed[0]["needs_permission_check"] is True

    removed, changed = apply_max_webhook_event(renamed, {
        "update_type": "bot_removed",
        "chat_id": 10001,
    })
    assert changed is True
    assert removed == []


def test_max_webhook_secret_uses_constant_time_match():
    from tgfarm.max_api import verify_max_webhook_secret

    assert verify_max_webhook_secret("good_secret_123", "good_secret_123")
    assert not verify_max_webhook_secret("wrong_secret", "good_secret_123")
    assert not verify_max_webhook_secret("anything", "")


class FakeMAXResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeMAXClient:
    calls = []
    headers = {}

    def __init__(self, *args, **kwargs):
        self.__class__.headers = kwargs.get("headers") or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        self.__class__.calls.append((method, url, kwargs))
        if url.endswith("/me"):
            return FakeMAXResponse({"user_id": 77, "name": "Farm bot"})
        if url.endswith("/subscriptions"):
            return FakeMAXResponse({"success": True})
        if url.endswith("/chats/10001/members/me"):
            return FakeMAXResponse({
                "is_admin": True,
                "is_owner": False,
                "permissions": ["read_all_messages", "write"],
            })
        if url.endswith("/chats/10001"):
            return FakeMAXResponse({"chat_id": 10001, "title": "News channel", "type": "channel"})
        return FakeMAXResponse({"success": False, "message": "unknown"})


@pytest.mark.asyncio
async def test_max_webhook_registration_uses_authorization_header_and_provider_secret(monkeypatch):
    from tgfarm import max_api

    FakeMAXClient.calls = []
    monkeypatch.setattr(max_api.httpx, "AsyncClient", FakeMAXClient)
    result = await max_api.register_max_webhook(
        "max-private-token",
        "https://farm.example/api/platforms/max/webhook",
        "webhook-secret_123456",
    )

    assert result["success"] is True
    assert result["bot_name"] == "Farm bot"
    assert FakeMAXClient.headers["Authorization"] == "max-private-token"
    assert all("max-private-token" not in call[1] for call in FakeMAXClient.calls)
    subscription = next(call for call in FakeMAXClient.calls if call[0] == "POST")
    assert subscription[1] == "https://platform-api2.max.ru/subscriptions"
    assert subscription[2]["json"]["url"] == "https://farm.example/api/platforms/max/webhook"
    assert set(subscription[2]["json"]["update_types"]) == set(max_api.WEBHOOK_EVENTS)
    assert subscription[2]["json"]["secret"] == "webhook-secret_123456"


@pytest.mark.asyncio
async def test_max_chat_selection_rechecks_write_permission(monkeypatch):
    from tgfarm import max_api

    FakeMAXClient.calls = []
    monkeypatch.setattr(max_api.httpx, "AsyncClient", FakeMAXClient)
    selected = await max_api.select_max_chat("max-private-token", "10001")
    assert selected == {
        "id": "10001",
        "name": "News channel",
        "is_channel": True,
        "role": "admin",
        "permissions": ["read_all_messages", "write"],
    }
    member_call = next(call for call in FakeMAXClient.calls if call[1].endswith("/members/me"))
    assert member_call[0] == "GET"
    assert member_call[1] == "https://platform-api2.max.ru/chats/10001/members/me"

    async def no_write(*args, **kwargs):
        return {"is_admin": True, "is_owner": False, "permissions": ["read_all_messages"]}

    async def fake_client(*args, **kwargs):
        raise AssertionError("the real client fake below should be used")

    class NoWriteClient(FakeMAXClient):
        async def request(self, method, url, **kwargs):
            if url.endswith("/members/me"):
                return FakeMAXResponse(await no_write())
            return FakeMAXResponse({"title": "News channel", "type": "channel"})

    monkeypatch.setattr(max_api.httpx, "AsyncClient", NoWriteClient)
    with pytest.raises(RuntimeError, match="право write"):
        await max_api.select_max_chat("max-private-token", "10001")


def test_max_webhook_public_callback_is_secret_protected_and_persists_events(client, monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.core.apiauth import is_public_path

    state = {"chats": "[]"}
    saved = []

    def fake_settings():
        return SimpleNamespace(
            max_webhook_secret="max-hook-secret",
            max_discovered_chats=state["chats"],
            max_access_token="",
            public_base_url="https://farm.example",
        )

    async def inline_io(function, *args, **kwargs):
        return function(*args, **kwargs)

    def save_credentials(platform_id, values):
        saved.append((platform_id, values))
        if "MAX_DISCOVERED_CHATS" in values:
            state["chats"] = values["MAX_DISCOVERED_CHATS"]
        return {}

    monkeypatch.setattr(api_module, "get_settings", fake_settings)
    monkeypatch.setattr(api_module, "io", inline_io)
    monkeypatch.setattr("tgfarm.platforms.save_platform_credentials", save_credentials)

    assert is_public_path("/api/platforms/max/webhook")
    path = "/api/platforms/max/webhook"
    update = {"update_type": "bot_added", "chat_id": 10001, "is_channel": True}

    denied = client.post(path, json=update, headers={"X-Max-Bot-Api-Secret": "wrong"})
    assert denied.status_code == 401

    accepted = client.post(path, json=update, headers={"X-Max-Bot-Api-Secret": "max-hook-secret"})
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    discovered = json.loads(state["chats"])
    assert discovered[0]["id"] == "10001"
    assert discovered[0]["is_channel"] is True
    assert saved[-1][0] == "max"
    assert "access_token" not in accepted.text


def test_max_webhook_setup_and_destinations_require_dashboard_auth(client):
    assert client.post("/api/platforms/max/webhook/register", json={}).status_code == 401
    assert client.get("/api/platforms/max/destinations").status_code == 401
    assert client.post("/api/platforms/max/destination", json={"chat_id": "10001"}).status_code == 401


def test_max_webhook_setup_registers_public_url_and_never_returns_secret(client, monkeypatch):
    import tgfarm.api as api_module
    import tgfarm.platforms as platform_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    state = SimpleNamespace(
        max_access_token="max-private-token",
        max_webhook_secret="",
        public_base_url="https://farm.example",
    )
    saved = []
    checks = []

    async def inline_io(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def fake_register(token, url, secret):
        assert token == "max-private-token"
        assert url == "https://farm.example/api/platforms/max/webhook"
        assert len(secret) >= 32
        return {"success": True, "bot_name": "Farm bot", "events": ["bot_added"]}

    def save_credentials(platform_id, values):
        saved.append((platform_id, values))
        if "MAX_WEBHOOK_SECRET" in values:
            state.max_webhook_secret = values["MAX_WEBHOOK_SECRET"]
        if "MAX_WEBHOOK_URL" in values:
            state.max_webhook_url = values["MAX_WEBHOOK_URL"]
        return {}

    def record_auth(platform_id, authorized, **kwargs):
        checks.append((platform_id, authorized, kwargs))
        return {}

    monkeypatch.setattr(api_module, "get_settings", lambda: state)
    monkeypatch.setattr(api_module, "io", inline_io)
    monkeypatch.setattr(api_module, "_operator", lambda *args, **kwargs: None)
    monkeypatch.setattr(platform_module, "save_platform_credentials", save_credentials)
    monkeypatch.setattr(platform_module, "record_auth_check", record_auth)
    monkeypatch.setattr("tgfarm.max_api.register_max_webhook", fake_register)

    response = client.post(
        "/api/platforms/max/webhook/register",
        json={},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 200
    assert response.json()["bot_name"] == "Farm bot"
    assert state.max_webhook_secret
    assert "max_webhook_secret" not in response.text.lower()
    assert state.max_webhook_url == "https://farm.example/api/platforms/max/webhook"
    assert checks[0][0:2] == ("max", None)


def test_max_destination_api_rechecks_write_permission_before_saving(client, monkeypatch):
    import tgfarm.api as api_module
    import tgfarm.platforms as platform_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    state = SimpleNamespace(max_access_token="max-private-token")
    saved = []
    checks = []

    async def inline_io(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def fake_select(token, chat_id):
        assert token == "max-private-token"
        assert chat_id == "10001"
        return {
            "id": "10001",
            "name": "News channel",
            "is_channel": True,
            "role": "admin",
            "permissions": ["write"],
        }

    def save_credentials(platform_id, values):
        saved.append((platform_id, values))
        return {}

    def record_auth(platform_id, authorized, **kwargs):
        checks.append((platform_id, authorized, kwargs))
        return {}

    monkeypatch.setattr(api_module, "get_settings", lambda: state)
    monkeypatch.setattr(api_module, "io", inline_io)
    monkeypatch.setattr(api_module, "_operator", lambda *args, **kwargs: None)
    monkeypatch.setattr(platform_module, "save_platform_credentials", save_credentials)
    monkeypatch.setattr(platform_module, "record_auth_check", record_auth)
    monkeypatch.setattr("tgfarm.max_api.select_max_chat", fake_select)

    response = client.post(
        "/api/platforms/max/destination",
        json={"chat_id": "10001"},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 200
    assert response.json()["destination"] == {
        "id": "10001",
        "name": "News channel",
        "kind": "channel",
        "role": "admin",
    }
    assert saved == [("max", {
        "MAX_CHANNEL_ID": "10001",
        "MAX_CHANNEL_NAME": "News channel",
        "MAX_CHANNEL_IS_CHANNEL": "true",
    })]
    assert checks[0][0:2] == ("max", True)

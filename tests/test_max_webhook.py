from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


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
    subscription_url = ""
    subscription_events = []

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
        if url.endswith("/subscriptions") and method == "POST":
            self.__class__.subscription_url = str((kwargs.get("json") or {}).get("url") or "")
            self.__class__.subscription_events = list((kwargs.get("json") or {}).get("update_types") or [])
            return FakeMAXResponse({"success": True})
        if url.endswith("/subscriptions") and method == "GET":
            return FakeMAXResponse({
                "subscriptions": [{
                    "url": self.__class__.subscription_url,
                    "time": 1760000000000,
                    "update_types": self.__class__.subscription_events,
                }] if self.__class__.subscription_url else [],
            })
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

    observed = []
    allow_write = True

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers") or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def fake_request(client, method, path, *, params=None, json_body=None):
        observed.append((method, path, client.headers))
        if path.endswith("/members/me"):
            return {
                "is_admin": True,
                "is_owner": False,
                "permissions": ["read_all_messages", "write"] if allow_write else ["read_all_messages"],
            }
        return {"chat_id": 10001, "title": "News channel", "type": "channel"}

    monkeypatch.setattr(max_api.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(max_api, "_json_request", fake_request)

    selected = await max_api.select_max_chat("max-private-token", "10001")
    assert selected == {
        "id": "10001",
        "name": "News channel",
        "is_channel": True,
        "role": "admin",
        "permissions": ["read_all_messages", "write"],
    }
    assert observed[0][0:2] == ("GET", "/chats/10001/members/me")
    assert observed[0][2]["Authorization"] == "max-private-token"

    allow_write = False
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
        "MAX_CHANNEL_BLOCKED": "false",
    })]
    assert checks[0][0:2] == ("max", True)


def test_max_permission_change_blocks_the_current_target_until_reselected(client, monkeypatch):
    import json
    import tgfarm.api as api_module
    import tgfarm.platforms as platform_module

    state = SimpleNamespace(
        max_webhook_secret="max-hook-secret",
        max_discovered_chats=json.dumps([{
            "id": "10001",
            "name": "News channel",
            "is_channel": True,
            "needs_permission_check": False,
        }]),
        max_channel_id="10001",
        max_channel_name="News channel",
        max_channel_blocked=False,
        max_bot_id="77",
    )
    saved = []
    checks = []

    async def inline_io(function, *args, **kwargs):
        return function(*args, **kwargs)

    def fake_save(platform_id, values):
        saved.append((platform_id, values))
        if "MAX_CHANNEL_BLOCKED" in values:
            state.max_channel_blocked = values["MAX_CHANNEL_BLOCKED"] == "true"
        if "MAX_DISCOVERED_CHATS" in values:
            state.max_discovered_chats = values["MAX_DISCOVERED_CHATS"]
        return {}

    def fake_check(platform_id, authorized, **kwargs):
        checks.append((platform_id, authorized, kwargs))
        return {}

    monkeypatch.setattr(api_module, "get_settings", lambda: state)
    monkeypatch.setattr(api_module, "io", inline_io)
    monkeypatch.setattr(platform_module, "save_platform_credentials", fake_save)
    monkeypatch.setattr(platform_module, "record_auth_check", fake_check)

    response = client.post(
        "/api/platforms/max/webhook",
        json={"update_type": "bot_admin_permissions_changed", "chat_id": 10001, "bot_id": 77},
        headers={"X-Max-Bot-Api-Secret": "max-hook-secret"},
    )

    assert response.status_code == 200
    assert state.max_channel_blocked is True
    assert ("max", {"MAX_CHANNEL_BLOCKED": "true"}) in saved
    assert checks[0][0:2] == ("max", False)


def test_max_permission_change_for_another_bot_does_not_block_selected_target(client, monkeypatch):
    import json
    import tgfarm.api as api_module
    import tgfarm.platforms as platform_module

    state = SimpleNamespace(
        max_webhook_secret="max-hook-secret",
        max_discovered_chats=json.dumps([{
            "id": "10001",
            "name": "News channel",
            "is_channel": True,
            "needs_permission_check": False,
        }]),
        max_channel_id="10001",
        max_channel_name="News channel",
        max_channel_blocked=False,
        max_bot_id="77",
    )
    saved = []

    async def inline_io(function, *args, **kwargs):
        return function(*args, **kwargs)

    def fake_save(platform_id, values):
        saved.append((platform_id, values))
        if "MAX_CHANNEL_BLOCKED" in values:
            state.max_channel_blocked = values["MAX_CHANNEL_BLOCKED"] == "true"
        return {}

    monkeypatch.setattr(api_module, "get_settings", lambda: state)
    monkeypatch.setattr(api_module, "io", inline_io)
    monkeypatch.setattr(platform_module, "save_platform_credentials", fake_save)

    response = client.post(
        "/api/platforms/max/webhook",
        json={"update_type": "bot_admin_permissions_changed", "chat_id": 10001, "bot_id": 88},
        headers={"X-Max-Bot-Api-Secret": "max-hook-secret"},
    )

    assert response.status_code == 200
    assert state.max_channel_blocked is False
    assert not any("MAX_CHANNEL_BLOCKED" in values for _, values in saved)


@pytest.mark.asyncio
async def test_max_api_classifies_rate_limit_and_service_outage_as_temporary(monkeypatch):
    from tgfarm import max_api

    class ErrorResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"message": "temporary"}

    class ErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, *args, **kwargs):
            return ErrorResponse(429)

    with pytest.raises(max_api.MaxTemporaryError):
        await max_api._json_request(ErrorClient(), "GET", "/me")

    class ServerErrorClient(ErrorClient):
        async def request(self, *args, **kwargs):
            return ErrorResponse(503)

    with pytest.raises(max_api.MaxTemporaryError):
        await max_api._json_request(ServerErrorClient(), "GET", "/me")


@pytest.mark.asyncio
async def test_max_api_classifies_missing_write_permission_as_permanent(monkeypatch):
    from tgfarm import max_api

    class ErrorResponse:
        status_code = 403

        def json(self):
            return {"message": "forbidden"}

    class ErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, *args, **kwargs):
            return ErrorResponse()

    with pytest.raises(max_api.MaxPermissionError):
        await max_api._json_request(ErrorClient(), "GET", "/chats/10001/members/me")

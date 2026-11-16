"""OK (Odnoklassniki) server OAuth: the access token is captured via login, never typed.

The flow follows the official OK server authorization (connect.ok.ru/oauth/authorize
-> api.ok.ru/oauth/token.do). The signing secret is derived as MD5(access_token +
application_secret_key), exactly as the OK API requires.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

REDIRECT = "https://farm.example/api/platforms/odnoklassniki/auth/callback"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeOKClient:
    """Route token.do and fb.do to canned responses; record every call."""

    calls: list = []
    token_payload: dict = {
        "access_token": "ok-access",
        "refresh_token": "ok-refresh",
        "expires_in": "2592000",
        "token_type": "session",
    }
    profile_payload: dict = {"uid": "12345", "name": "Owner Name"}

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/oauth/token.do"):
            return FakeResponse(self.token_payload)
        return FakeResponse(self.profile_payload)


def _state_from(result: dict) -> str:
    query = parse_qs(urlsplit(result["authorize_url"]).query)
    return query["state"][0]


@pytest.mark.asyncio
async def test_begin_builds_the_authorize_url_with_semicolon_scope():
    from tgfarm.ok_oauth import begin_ok_oauth

    result = await begin_ok_oauth("APPKEY", "APPSECRET", REDIRECT)
    parts = urlsplit(result["authorize_url"])
    assert parts.scheme == "https"
    assert parts.netloc == "connect.ok.ru"
    assert parts.path == "/oauth/authorize"
    query = parse_qs(parts.query)
    assert query["client_id"] == ["APPKEY"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["state"]
    scope = query["scope"][0]
    assert ";" in scope
    assert "GROUP_CONTENT" in scope.split(";")
    assert "LONG_ACCESS_TOKEN" in scope.split(";")
    assert result["expires_in_s"] == 600


@pytest.mark.asyncio
async def test_begin_requires_application_credentials():
    from tgfarm.ok_oauth import begin_ok_oauth

    with pytest.raises(ValueError):
        await begin_ok_oauth("", "APPSECRET", REDIRECT)
    with pytest.raises(ValueError):
        await begin_ok_oauth("APPKEY", "  ", REDIRECT)


@pytest.mark.asyncio
async def test_begin_rejects_a_non_loopback_http_redirect():
    from tgfarm.ok_oauth import begin_ok_oauth

    with pytest.raises(ValueError):
        await begin_ok_oauth("APPKEY", "APPSECRET", "http://evil.example/cb")


@pytest.mark.asyncio
async def test_complete_exchanges_the_code_and_derives_the_session_secret(monkeypatch):
    from tgfarm.ok_oauth import begin_ok_oauth, complete_ok_oauth

    state = _state_from(await begin_ok_oauth("APPKEY", "APPSECRET", REDIRECT))
    FakeOKClient.calls = []
    FakeOKClient.token_payload = {
        "access_token": "ok-access",
        "refresh_token": "ok-refresh",
        "expires_in": "2592000",
        "token_type": "session",
    }
    FakeOKClient.profile_payload = {"uid": "12345", "name": "Owner Name"}
    monkeypatch.setattr(httpx, "AsyncClient", FakeOKClient)

    result = await complete_ok_oauth(state, "the-code")

    assert result["access_token"] == "ok-access"
    assert result["application_key"] == "APPKEY"
    assert result["application_secret"] == "APPSECRET"
    expected_secret = hashlib.md5(b"ok-accessAPPSECRET").hexdigest()
    assert result["session_secret_key"] == expected_secret
    assert result["account"]

    token_url, token_request = FakeOKClient.calls[0]
    assert token_url.endswith("/oauth/token.do")
    data = token_request["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"
    assert data["client_id"] == "APPKEY"
    assert data["client_secret"] == "APPSECRET"
    assert data["redirect_uri"] == REDIRECT

    profile_url, profile_request = FakeOKClient.calls[1]
    assert profile_url.endswith("/fb.do")
    assert profile_request["data"]["method"] == "users.getCurrentUser"
    assert profile_request["data"]["application_key"] == "APPKEY"
    assert profile_request["data"]["sig"]


@pytest.mark.asyncio
async def test_complete_rejects_an_unknown_state():
    from tgfarm.ok_oauth import complete_ok_oauth

    with pytest.raises(ValueError):
        await complete_ok_oauth("not-a-real-state", "code")


@pytest.mark.asyncio
async def test_complete_raises_when_ok_rejects_the_code(monkeypatch):
    from tgfarm.ok_oauth import begin_ok_oauth, complete_ok_oauth

    state = _state_from(await begin_ok_oauth("APPKEY", "APPSECRET", REDIRECT))
    FakeOKClient.calls = []
    FakeOKClient.token_payload = {"error": "invalid_grant", "error_description": "Invalid code"}
    monkeypatch.setattr(httpx, "AsyncClient", FakeOKClient)

    with pytest.raises(RuntimeError):
        await complete_ok_oauth(state, "the-code")


@pytest.mark.asyncio
async def test_complete_maps_a_transport_failure_to_runtime_error(monkeypatch):
    from tgfarm.ok_oauth import begin_ok_oauth, complete_ok_oauth

    class TimeoutClient(FakeOKClient):
        async def post(self, url, **kwargs):
            raise httpx.ConnectTimeout("timeout")

    state = _state_from(await begin_ok_oauth("APPKEY", "APPSECRET", REDIRECT))
    monkeypatch.setattr(httpx, "AsyncClient", TimeoutClient)

    with pytest.raises(RuntimeError):
        await complete_ok_oauth(state, "the-code")


@pytest.mark.asyncio
async def test_cancel_discards_the_pending_transaction():
    from tgfarm.ok_oauth import begin_ok_oauth, cancel_ok_oauth, complete_ok_oauth

    state = _state_from(await begin_ok_oauth("APPKEY", "APPSECRET", REDIRECT))
    cancel_ok_oauth(state)
    with pytest.raises(ValueError):
        await complete_ok_oauth(state, "the-code")


def test_ok_oauth_callback_does_not_echo_code_or_token(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm import ok_oauth, platforms
    from tgfarm.api import create_app

    saved = []
    checked = []
    seen = {}

    async def complete(state, code):
        seen["state"] = state
        seen["code"] = code
        return {
            "application_key": "APPKEY",
            "application_secret": "private-app-secret",
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "session_secret_key": "private-session-secret",
            "uid": "123456789",
            "account": "Наука",
        }

    monkeypatch.setattr(ok_oauth, "complete_ok_oauth", complete)
    monkeypatch.setattr(
        platforms, "save_platform_credentials",
        lambda platform_id, values: saved.append((platform_id, values)) or {"status": "authorized"},
    )
    monkeypatch.setattr(
        platforms, "record_auth_check",
        lambda *args, **kwargs: checked.append((args, kwargs)) or {"status": "authorized"},
    )
    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1:8765") as client:
        response = client.get(
            "/api/platforms/odnoklassniki/auth/callback?code=do-not-echo-code&state=valid-state"
        )
    assert response.status_code == 200
    assert "Наука" in response.text
    assert "do-not-echo-code" not in response.text
    assert "private-access-token" not in response.text
    assert "private-app-secret" not in response.text
    assert "private-session-secret" not in response.text
    assert seen == {"state": "valid-state", "code": "do-not-echo-code"}
    assert saved[0][0] == "odnoklassniki"
    assert saved[0][1]["OK_ACCESS_TOKEN"] == "private-access-token"
    assert saved[0][1]["OK_SESSION_SECRET_KEY"] == "private-session-secret"
    assert checked[0][0] == ("odnoklassniki", True)
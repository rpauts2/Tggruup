from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakePinterestOAuthClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse({
            "access_token": "pina-access-token",
            "refresh_token": "pinr-refresh-token",
            "expires_in": 2592000,
            "refresh_token_expires_in": 5184000,
        })

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({
            "id": "board-123",
            "name": "My Ideas",
            "owner": {"username": "pinner"},
        })


@pytest.mark.asyncio
async def test_pinterest_oauth_verifies_selected_board_and_returns_rotating_tokens(monkeypatch):
    from tgfarm import pinterest_oauth

    FakePinterestOAuthClient.calls = []
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(pinterest_board_id="board-123"),
    )
    monkeypatch.setattr(pinterest_oauth.httpx, "AsyncClient", FakePinterestOAuthClient)

    started = await pinterest_oauth.begin_pinterest_oauth(
        "app-123",
        "secret-456",
        "https://farm.example/api/platforms/pinterest/auth/callback",
    )
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert query["client_id"] == ["app-123"]
    assert set(query["scope"][0].split()) == {"boards:read", "pins:read", "pins:write"}

    result = await pinterest_oauth.complete_pinterest_oauth(query["state"][0], "auth-code-xyz")
    assert result["access_token"] == "pina-access-token"
    assert result["refresh_token"] == "pinr-refresh-token"
    assert result["board_id"] == "board-123"
    assert result["board_name"] == "My Ideas"
    assert result["account"] == "@pinner · My Ideas"
    token_call = FakePinterestOAuthClient.calls[0]
    assert token_call[1] == "https://api.pinterest.com/v5/oauth/token"
    assert token_call[2]["auth"] == ("app-123", "secret-456")
    board_call = FakePinterestOAuthClient.calls[1]
    assert board_call[1] == "https://api.pinterest.com/v5/boards/board-123"
    assert board_call[2]["headers"]["Authorization"] == "Bearer pina-access-token"


def test_pinterest_oauth_callback_requires_https_outside_loopback():
    from tgfarm.pinterest_oauth import _validate_redirect_uri

    with pytest.raises(ValueError):
        _validate_redirect_uri("http://farm.example/api/platforms/pinterest/auth/callback")
    assert _validate_redirect_uri(
        "http://127.0.0.1:8000/api/platforms/pinterest/auth/callback"
    ).startswith("http://127.0.0.1")

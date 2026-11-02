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


@pytest.mark.asyncio
async def test_pinterest_oauth_can_finish_before_a_board_is_selected(monkeypatch):
    from tgfarm import pinterest_oauth

    FakePinterestOAuthClient.calls = []
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(pinterest_board_id=""),
    )
    monkeypatch.setattr(pinterest_oauth.httpx, "AsyncClient", FakePinterestOAuthClient)

    started = await pinterest_oauth.begin_pinterest_oauth(
        "app-123",
        "secret-456",
        "https://farm.example/api/platforms/pinterest/auth/callback",
    )
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    result = await pinterest_oauth.complete_pinterest_oauth(state, "auth-code-xyz")

    assert result["access_token"] == "pina-access-token"
    assert result["refresh_token"] == "pinr-refresh-token"
    assert result["board_id"] == ""
    assert result["account"] == "Доска ещё не выбрана"
    discovery_call = FakePinterestOAuthClient.calls[1]
    assert discovery_call[1] == "https://api.pinterest.com/v5/boards"
    assert discovery_call[2]["params"] == {"page_size": "1"}


@pytest.mark.asyncio
async def test_pinterest_board_discovery_uses_bounded_pagination_and_never_returns_tokens(monkeypatch):
    from tgfarm import pinterest_oauth

    class BoardClient(FakePinterestOAuthClient):
        async def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            bookmark = (kwargs.get("params") or {}).get("bookmark")
            if bookmark:
                return FakeResponse({
                    "items": [{"id": "board-2", "name": "Ideas 2", "url": "https://www.pinterest.com/pinner/ideas-2/"}],
                    "bookmark": None,
                })
            return FakeResponse({
                "items": [
                    {"id": "board-1", "name": "Ideas 1", "access_token": "never-return", "url": "https://www.pinterest.com/pinner/ideas-1/"},
                    {"id": "board-1", "name": "Duplicate", "url": "https://www.pinterest.com/pinner/ideas-1/"},
                ],
                "bookmark": "next-page",
            })

    FakePinterestOAuthClient.calls = []
    monkeypatch.setattr(pinterest_oauth.httpx, "AsyncClient", BoardClient)
    monkeypatch.setattr(pinterest_oauth, "get_valid_pinterest_access_token", lambda: None)

    async def token():
        return "pina-access-token"

    monkeypatch.setattr(pinterest_oauth, "get_valid_pinterest_access_token", token)
    boards = await pinterest_oauth.list_pinterest_boards(page_size=1, max_pages=3)

    assert [board["id"] for board in boards] == ["board-1", "board-2"]
    assert all("access_token" not in board for board in boards)
    assert len(BoardClient.calls) == 2


@pytest.mark.asyncio
async def test_pinterest_board_selection_revalidates_id_and_owner_access(monkeypatch):
    from tgfarm import pinterest_oauth

    FakePinterestOAuthClient.calls = []
    monkeypatch.setattr(pinterest_oauth.httpx, "AsyncClient", FakePinterestOAuthClient)

    async def token():
        return "pina-access-token"

    monkeypatch.setattr(pinterest_oauth, "get_valid_pinterest_access_token", token)
    board = await pinterest_oauth.select_pinterest_board("board-123")

    assert board["id"] == "board-123"
    assert board["name"] == "My Ideas"
    assert all("access_token" not in value for value in board.values())

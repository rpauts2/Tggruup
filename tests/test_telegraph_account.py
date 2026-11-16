"""Telegraph createAccount auto-provisioning: the token is captured, never typed."""

from __future__ import annotations

import httpx
import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.telegra.ph/createAccount")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Telegraph HTTP error", request=request, response=response)


class FakeAccountClient:
    calls = []
    payload = {"ok": True, "result": {"access_token": "provisioned-token", "short_name": "TgFarm"}}

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.kwargs))
        return FakeResponse(self.payload)


@pytest.mark.asyncio
async def test_create_account_posts_short_name_and_returns_the_token(monkeypatch):
    from tgfarm.s9_growth import telegraph

    FakeAccountClient.calls = []
    FakeAccountClient.payload = {
        "ok": True,
        "result": {"access_token": "provisioned-token", "short_name": "TgFarm", "auth_url": "https://t.me/..."},
    }
    monkeypatch.setattr(httpx, "AsyncClient", FakeAccountClient)

    result = await telegraph.create_telegraph_account("TgFarm")

    assert result["access_token"] == "provisioned-token"
    assert result["short_name"] == "TgFarm"
    url, request, client_kwargs = FakeAccountClient.calls[0]
    assert url == "https://api.telegra.ph/createAccount"
    assert request["data"]["short_name"] == "TgFarm"
    assert client_kwargs["headers"]["User-Agent"].startswith("TgFarm/")


@pytest.mark.asyncio
async def test_create_account_rejects_an_empty_short_name():
    from tgfarm.s9_growth import telegraph

    with pytest.raises(ValueError, match="short_name"):
        await telegraph.create_telegraph_account("   ")


@pytest.mark.asyncio
async def test_create_account_raises_when_the_api_rejects(monkeypatch):
    from tgfarm.s9_growth import telegraph

    FakeAccountClient.calls = []
    FakeAccountClient.payload = {"ok": False, "error": "SHORT_NAME_REQUIRED"}
    monkeypatch.setattr(httpx, "AsyncClient", FakeAccountClient)

    with pytest.raises(ValueError, match="SHORT_NAME_REQUIRED"):
        await telegraph.create_telegraph_account("TgFarm")


@pytest.mark.asyncio
async def test_create_account_raises_when_no_token_is_returned(monkeypatch):
    from tgfarm.s9_growth import telegraph

    FakeAccountClient.calls = []
    FakeAccountClient.payload = {"ok": True, "result": {"short_name": "TgFarm"}}
    monkeypatch.setattr(httpx, "AsyncClient", FakeAccountClient)

    with pytest.raises(ValueError, match="access token"):
        await telegraph.create_telegraph_account("TgFarm")


@pytest.mark.asyncio
async def test_create_account_maps_a_transport_failure_to_value_error(monkeypatch):
    from tgfarm.s9_growth import telegraph

    class TimeoutClient(FakeAccountClient):
        async def post(self, url, **kwargs):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", TimeoutClient)

    with pytest.raises(ValueError, match="createAccount"):
        await telegraph.create_telegraph_account("TgFarm")
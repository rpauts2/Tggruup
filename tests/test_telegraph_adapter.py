from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.telegra.ph/createPage")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Telegraph HTTP error", request=request, response=response)


class FakeTelegraphClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.kwargs))
        return FakeResponse({
            "ok": True,
            "result": {
                "path": "Autonomous-Publishing-11-03",
                "url": "https://telegra.ph/Autonomous-Publishing-11-03",
            },
        })


@pytest.mark.asyncio
async def test_telegraph_adapter_creates_native_article_with_form_content(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeTelegraphClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        telegraph_access_token="telegraph-token-secret",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", FakeTelegraphClient)

    result = await adapters.ADAPTERS["telegraph"].publish(CrosspostPayload(
        platform="telegraph",
        title="Autonomous publishing",
        text=(
            "## The pipeline\n\n"
            "TgFarm chooses **the best destination** automatically.\n\n"
            "- Read rules\n- Adapt the article\n\n"
            "Reference: [official docs](https://telegra.ph/api)"
        ),
    ))

    assert result.status == "posted"
    assert result.remote_id == "Autonomous-Publishing-11-03"
    assert result.url == "https://telegra.ph/Autonomous-Publishing-11-03"
    url, request, client_kwargs = FakeTelegraphClient.calls[0]
    assert url == "https://api.telegra.ph/createPage"
    assert request["data"]["access_token"] == "telegraph-token-secret"
    assert client_kwargs["headers"]["User-Agent"].startswith("TgFarm/")
    nodes = json.loads(request["data"]["content"])
    assert nodes[0]["tag"] == "h3"
    assert nodes[1]["tag"] == "p"
    assert nodes[2]["tag"] == "ul"
    assert nodes[3]["children"][1]["tag"] == "a"


@pytest.mark.asyncio
async def test_telegraph_adapter_skips_without_access_token(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(telegraph_access_token=""))
    result = await adapters.ADAPTERS["telegraph"].publish(CrosspostPayload(
        platform="telegraph", title="Test", text="Text"
    ))
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_telegraph_adapter_marks_unconfirmed_timeout_as_unknown(monkeypatch):
    from tgfarm.s9_growth import adapters

    class TimeoutClient(FakeTelegraphClient):
        async def post(self, url, **kwargs):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        telegraph_access_token="telegraph-token-secret",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", TimeoutClient)

    result = await adapters.ADAPTERS["telegraph"].publish(CrosspostPayload(
        platform="telegraph", title="Test", text="Text"
    ))
    assert result.status == "unknown"
    assert "secret" not in str(result.error)


@pytest.mark.asyncio
async def test_telegraph_adapter_marks_an_unconfirmed_server_error_as_unknown(monkeypatch):
    """`createPage` may have created the page before the gateway gave up.

    Telegraph used to answer `failed` for every 5xx and `permanent` for a 408.
    `failed` is the status the planner re-offers on a timer, so a 502 that had
    already created the page produced a second one on the next tick; `permanent`
    for a 408 threw away a retryable timeout. Both now read like every other
    adapter: 5xx/408/425 are ambiguous, 4xx is a definite rejection, 429 is a
    rate limit.
    """
    from tgfarm.s9_growth import adapters

    def _client_raising(code: int):
        class StatusClient(FakeTelegraphClient):
            async def post(self, url, **kwargs):
                request = httpx.Request("POST", url)
                raise httpx.HTTPStatusError(
                    "Telegraph HTTP error",
                    request=request,
                    response=httpx.Response(code, request=request),
                )

        return StatusClient

    for code in (500, 502, 503, 408, 425):
        monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
            telegraph_access_token="telegraph-token-secret",
        ))
        monkeypatch.setattr(httpx, "AsyncClient", _client_raising(code))

        result = await adapters.ADAPTERS["telegraph"].publish(CrosspostPayload(
            platform="telegraph", title="Test", text="Text"
        ))
        assert result.status == "unknown", f"HTTP {code} was classified {result.status}"


@pytest.mark.asyncio
async def test_telegraph_adapter_still_rejects_a_client_error_permanently(monkeypatch):
    """A 401/400 is a definite rejection: the page was not created."""
    from tgfarm.s9_growth import adapters

    class UnauthorizedClient(FakeTelegraphClient):
        async def post(self, url, **kwargs):
            raise httpx.HTTPStatusError(
                "Telegraph HTTP error",
                request=httpx.Request("POST", url),
                response=httpx.Response(401, request=httpx.Request("POST", url)),
            )

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        telegraph_access_token="telegraph-token-secret",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", UnauthorizedClient)

    result = await adapters.ADAPTERS["telegraph"].publish(CrosspostPayload(
        platform="telegraph", title="Test", text="Text"
    ))
    assert result.status == "permanent"


def test_telegraph_adapter_uses_a_safe_utf8_character_cap():
    from tgfarm.s9_growth.adapters import ADAPTERS

    assert ADAPTERS["telegraph"].max_body == 20_000


def test_telegraph_node_builder_enforces_the_documented_content_limit():
    from tgfarm.s9_growth.telegraph import MAX_CONTENT_BYTES, build_telegraph_content

    nodes = build_telegraph_content("Title", "Paragraph\n\n- first\n- second")
    assert nodes[0]["tag"] == "p"
    assert nodes[1]["tag"] == "ul"

    with pytest.raises(ValueError, match="64 KB"):
        build_telegraph_content("Title", "x" * (MAX_CONTENT_BYTES + 1))

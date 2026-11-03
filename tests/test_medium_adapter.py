from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("GET", "https://api.medium.com/v1/me")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("request failed", request=request, response=response)

    def json(self):
        return self._payload


class FakeMediumClient:
    calls = []
    responses = []

    def __init__(self, *args, **kwargs):
        self.headers = kwargs.get("headers", {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs, self.headers))
        return self.responses.pop(0)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs, self.headers))
        return self.responses.pop(0)


def _payload():
    from tgfarm.core.schemas import CrosspostPayload

    return CrosspostPayload(
        platform="medium",
        title="A practical article about resilient automation",
        text="This is an evidence-based explanation with clear steps and a useful conclusion.",
        link=None,
        image_path=None,
        hashtags=["python", "#automation", "x" * 26, "python", "engineering"],
        meta={},
    )


@pytest.mark.asyncio
async def test_medium_publishes_markdown_using_authenticated_legacy_author(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMediumClient.calls = []
    FakeMediumClient.responses = [
        FakeResponse({"data": {"id": "author123", "username": "writer"}}),
        FakeResponse({"data": {"id": "article-123", "url": "https://medium.com/@writer/article-123"}}),
    ]
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(medium_access_token="legacy-token"))
    monkeypatch.setattr("httpx.AsyncClient", FakeMediumClient)

    result = await adapters.MediumAdapter().publish(_payload())

    assert result.status == "posted"
    assert result.remote_id == "article-123"
    assert result.url == "https://medium.com/@writer/article-123"
    assert [(method, url) for method, url, *_ in FakeMediumClient.calls] == [
        ("GET", "https://api.medium.com/v1/me"),
        ("POST", "https://api.medium.com/v1/users/author123/posts"),
    ]
    request = FakeMediumClient.calls[1][2]["json"]
    assert request["title"] == "A practical article about resilient automation"
    assert request["contentFormat"] == "markdown"
    assert request["content"].startswith("# A practical article about resilient automation\n\n")
    assert request["publishStatus"] == "public"
    assert request["notifyFollowers"] is False
    assert request["tags"] == ["python", "automation", "engineering"]
    assert FakeMediumClient.calls[0][3]["Authorization"] == "Bearer legacy-token"


@pytest.mark.asyncio
async def test_medium_rejects_revoked_legacy_token_before_create(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMediumClient.calls = []
    FakeMediumClient.responses = [FakeResponse({"errors": [{"message": "unauthorized"}]}, status_code=401)]
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(medium_access_token="revoked-token"))
    monkeypatch.setattr("httpx.AsyncClient", FakeMediumClient)

    result = await adapters.MediumAdapter().publish(_payload())

    assert result.status == "permanent"
    assert len(FakeMediumClient.calls) == 1
    assert FakeMediumClient.calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_medium_missing_token_is_skipped_without_network(monkeypatch):
    from tgfarm.s9_growth import adapters

    FakeMediumClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(medium_access_token=""))
    monkeypatch.setattr("httpx.AsyncClient", FakeMediumClient)

    result = await adapters.MediumAdapter().publish(_payload())

    assert result.status == "skipped"
    assert FakeMediumClient.calls == []


@pytest.mark.asyncio
async def test_medium_does_not_silently_truncate_long_titles(monkeypatch):
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth import adapters

    FakeMediumClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(medium_access_token="legacy-token"))
    monkeypatch.setattr("httpx.AsyncClient", FakeMediumClient)
    payload = CrosspostPayload(
        platform="medium", title="T" * 101, text="Body", link=None, image_path=None, hashtags=[], meta={}
    )

    result = await adapters.MediumAdapter().publish(payload)

    assert result.status == "permanent"
    assert FakeMediumClient.calls == []

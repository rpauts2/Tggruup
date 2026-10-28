from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    status_code = 201

    def __init__(self, payload=None, headers=None):
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("LinkedIn API failure")


class FakeLinkedInPostClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.default_headers = kwargs.get("headers") or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.default_headers))
        return FakeResponse(headers={"x-restli-id": "urn:li:share:555"})


@pytest.mark.asyncio
async def test_linkedin_adapter_publishes_member_text_post(monkeypatch):
    import httpx

    from tgfarm import linkedin_oauth
    from tgfarm.s9_growth import adapters

    FakeLinkedInPostClient.calls = []
    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(
            linkedin_member_id="member-123",
            linkedin_access_token="private-linkedin-token",
            linkedin_member_name="TgFarm Demo",
        ),
    )

    async def get_token():
        return "private-linkedin-token"

    monkeypatch.setattr(linkedin_oauth, "get_valid_linkedin_access_token", get_token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeLinkedInPostClient)

    result = await adapters.LinkedInAdapter().publish(CrosspostPayload(
        platform="linkedin",
        title="Hello LinkedIn",
        text="A useful professional post",
    ))

    assert result.status == "posted"
    assert result.remote_id == "urn:li:share:555"
    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:555/"
    assert "private-linkedin-token" not in repr(result)
    assert len(FakeLinkedInPostClient.calls) == 1

    url, kwargs, headers = FakeLinkedInPostClient.calls[0]
    assert url == "https://api.linkedin.com/rest/posts"
    assert headers["Authorization"] == "Bearer private-linkedin-token"
    assert headers["LinkedIn-Version"] == "202610"
    assert headers["X-Restli-Protocol-Version"] == "2.0.0"
    post = kwargs["json"]
    assert post["author"] == "urn:li:person:member-123"
    assert post["commentary"] == "Hello LinkedIn\n\nA useful professional post"
    assert post["visibility"] == "PUBLIC"
    assert post["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert post["lifecycleState"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_linkedin_adapter_skips_without_connected_member(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(
            linkedin_member_id="",
            linkedin_access_token="",
            linkedin_member_name="",
        ),
    )
    result = await adapters.LinkedInAdapter().publish(CrosspostPayload(
        platform="linkedin", title="Hello", text="Body"
    ))
    assert result.status == "skipped"

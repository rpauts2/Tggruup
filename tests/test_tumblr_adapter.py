from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    status_code = 201
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.tumblr.com/v2/blog/demo/posts")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Tumblr HTTP error", request=request, response=response)


class FakeTumblrPublishClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.kwargs))
        return FakeResponse({"meta": {"status": 201}, "response": {"id": "123456789"}})


@pytest.mark.asyncio
async def test_tumblr_adapter_publishes_native_npf_with_tags_and_source(monkeypatch):
    from tgfarm import tumblr_oauth
    from tgfarm.s9_growth import adapters

    FakeTumblrPublishClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        tumblr_client_id="client-id",
        tumblr_client_secret="client-secret",
        tumblr_blog_identifier="demo-blog",
        tumblr_access_token="access-secret",
        tumblr_refresh_token="refresh-secret",
        tumblr_blog_url="https://demo-blog.tumblr.com/",
    ))
    async def token():
        return "access-secret"
    monkeypatch.setattr(adapters, "get_valid_tumblr_access_token", token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeTumblrPublishClient)

    result = await adapters.ADAPTERS["tumblr"].publish(CrosspostPayload(
        platform="tumblr",
        title="A good heading",
        text="First paragraph.\n\nSecond paragraph.\n\nПодробнее: https://example.com/story\n\n#AI #Science",
        link="https://example.com/story",
        hashtags=["AI", "Science"],
    ))

    assert result.status == "posted"
    assert result.remote_id == "123456789"
    assert result.url == "https://demo-blog.tumblr.com/post/123456789"
    url, request, client_kwargs = FakeTumblrPublishClient.calls[0]
    assert url == "https://api.tumblr.com/v2/blog/demo-blog/posts"
    assert client_kwargs["headers"]["Authorization"] == "Bearer access-secret"
    assert client_kwargs["headers"]["User-Agent"] == tumblr_oauth.USER_AGENT
    body = request["json"]
    assert body["state"] == "published"
    assert body["tags"] == "AI, Science"
    assert body["source_url"] == "https://example.com/story"
    assert body["content"] == [
        {"type": "text", "text": "A good heading"},
        {"type": "text", "text": "First paragraph."},
        {"type": "text", "text": "Second paragraph."},
    ]


@pytest.mark.asyncio
async def test_tumblr_adapter_skips_without_credentials(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        tumblr_client_id="", tumblr_client_secret="", tumblr_blog_identifier="",
        tumblr_access_token="", tumblr_refresh_token="",
    ))
    result = await adapters.ADAPTERS["tumblr"].publish(
        CrosspostPayload(platform="tumblr", title="Test", text="Text")
    )
    assert result.status == "skipped"


def test_tumblr_npf_builder_splits_long_paragraphs_into_safe_blocks():
    from tgfarm.s9_growth.tumblr import MAX_BLOCK_CHARS, build_npf_content

    content = build_npf_content("Heading", "A" * (MAX_BLOCK_CHARS + 50))
    assert content[0] == {"type": "text", "text": "Heading"}
    assert all(len(block["text"]) <= MAX_BLOCK_CHARS for block in content)

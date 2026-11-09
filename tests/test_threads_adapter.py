from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgfarm.core.schemas import CrosspostPayload


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("provider error")


class FakeThreadsClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/threads_publish"):
            return FakeResponse({"id": "published-post-123"})
        if url.endswith("/threads"):
            return FakeResponse({"id": "container-456"})
        raise AssertionError("unexpected Threads POST URL: " + url)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({
            "id": "published-post-123",
            "permalink": "https://www.threads.net/@tgfarm_demo/post/abc123",
        })


@pytest.mark.asyncio
async def test_threads_adapter_creates_and_publishes_text_container(monkeypatch):
    import httpx

    from tgfarm import threads_oauth
    from tgfarm.s9_growth import adapters

    FakeThreadsClient.calls = []
    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(
            threads_user_id="threads-user-123",
            threads_access_token="private-token",
            threads_username="tgfarm_demo",
        ),
    )

    async def get_token():
        return "private-token"

    monkeypatch.setattr(threads_oauth, "get_valid_threads_access_token", get_token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeThreadsClient)

    result = await adapters.ThreadsAdapter().publish(CrosspostPayload(
        platform="threads", title="Hello Threads", text="A useful short post"
    ))

    assert result.status == "posted"
    assert result.remote_id == "published-post-123"
    assert result.url == "https://www.threads.net/@tgfarm_demo/post/abc123"
    assert "private-token" not in repr(result)
    assert len(FakeThreadsClient.calls) == 3

    create = FakeThreadsClient.calls[0]
    assert create[0] == "POST"
    assert create[1] == "https://graph.threads.net/v1.0/threads-user-123/threads"
    assert create[2]["data"]["media_type"] == "TEXT"
    assert create[2]["data"]["text"] == "Hello Threads\n\nA useful short post"

    publish = FakeThreadsClient.calls[1]
    assert publish[0] == "POST"
    assert publish[1] == "https://graph.threads.net/v1.0/threads-user-123/threads_publish"
    assert publish[2]["data"]["creation_id"] == "container-456"

    lookup = FakeThreadsClient.calls[2]
    assert lookup[0] == "GET"
    assert lookup[2]["params"]["fields"] == "id,permalink"


@pytest.mark.asyncio
async def test_threads_adapter_skips_without_credentials(monkeypatch):
    from tgfarm.s9_growth import adapters

    monkeypatch.setattr(
        adapters,
        "get_settings",
        lambda: SimpleNamespace(
            threads_user_id="",
            threads_access_token="",
            threads_username="",
        ),
    )
    result = await adapters.ThreadsAdapter().publish(CrosspostPayload(
        platform="threads", title="Hello", text="Body"
    ))
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_threads_adapter_clips_to_limit_on_a_word_boundary(monkeypatch):
    import httpx

    from tgfarm import threads_oauth
    from tgfarm.s9_growth import adapters

    FakeThreadsClient.calls = []
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        threads_user_id="u", threads_access_token="t", threads_username="n",
    ))

    async def get_token():
        return "t"

    monkeypatch.setattr(threads_oauth, "get_valid_threads_access_token", get_token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeThreadsClient)
    await adapters.ThreadsAdapter().publish(CrosspostPayload(
        platform="threads", title="Заголовок", text="длинноеслово " * 80,
    ))
    sent = FakeThreadsClient.calls[0][2]["data"]["text"]
    assert len(sent) <= 500
    assert sent.endswith("…")
    assert not sent[:-1].endswith("длинноесло")  # cut at a word end, not mid-word


@pytest.mark.asyncio
@pytest.mark.parametrize("module,adapter_name", [
    ("threads_oauth", "ThreadsAdapter"),
    ("linkedin_oauth", "LinkedInAdapter"),
])
async def test_temporary_token_refresh_failure_is_not_recorded_as_permanent(
    monkeypatch, module, adapter_name
):
    import importlib

    import httpx

    from tgfarm.s9_growth import adapters

    oauth = importlib.import_module(f"tgfarm.{module}")
    getter = next(n for n in dir(oauth) if n.startswith("get_valid_") and n.endswith("_access_token"))
    temporary = next(n for n in dir(oauth) if n.endswith("OAuthTemporaryError"))
    monkeypatch.setattr(adapters, "get_settings", lambda: SimpleNamespace(
        threads_user_id="u", threads_access_token="t", threads_username="n",
        linkedin_member_id="m", linkedin_access_token="t", linkedin_member_name="n",
    ))

    async def temporary_failure():
        raise getattr(oauth, temporary)("provider is down")

    async def revoked():
        raise RuntimeError("grant revoked")

    monkeypatch.setattr(httpx, "AsyncClient", FakeThreadsClient)
    payload = CrosspostPayload(platform="x", title="T", text="body")
    adapter = getattr(adapters, adapter_name)()
    monkeypatch.setattr(oauth, getter, temporary_failure)
    assert (await adapter.publish(payload)).status == "failed"
    monkeypatch.setattr(oauth, getter, revoked)
    assert (await adapter.publish(payload)).status == "permanent"

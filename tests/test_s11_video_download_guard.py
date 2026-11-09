"""The video downloader and the S11 facade: the byte cap, the SSRF guard and the
refusal to fetch at all when the pipeline is off.

``tgfarm/s11_video/__init__.py`` owns two functions the suite never exercised —
``download`` (stream to disk with a hard byte cap) and ``fetch_video_for`` (the
facade the publisher calls) — while the licensing gate around them was already
covered by ``tests/test_format_video.py``. That left three properties unguarded,
and each of them is the kind that fails silently in production:

* **The byte cap.** ``download`` reads ``content-length`` when the server sends
  it, but a chunked response has no such header, so the cap has to be enforced
  while writing. A stream that ignores it fills the disk instead of refusing.
* **The SSRF guard.** ``assert_public_http_url`` runs before the socket opens. A
  candidate pointing at a private address must be refused, not fetched — the
  test asserts the refusal *and* that no request was attempted.
* **The off switch.** ``fetch_video_for`` returns ``None`` without touching the
  network when ``video_enabled`` is false, which is the shipped default.

Hermetic: every test replaces ``httpx.AsyncClient`` with a fake, and
``video_root()`` with ``tmp_path``, so nothing leaves the process and no file
survives the run. The DNS resolver is stubbed for the download tests because
``site.invalid`` cannot resolve and the SSRF guard denies unresolvable hosts by
design — the guard itself is tested separately, against ``127.0.0.1``, with the
real function in place.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgfarm.core.media import REFUSAL_REASONS, MediaCandidate
from tgfarm.s11_video import download, fetch_video_for


class _FakeStream:
    """An `async with client.stream(...)` that yields whatever the test asked for."""

    def __init__(self, *, status: int = 200, headers: dict | None = None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aiter_bytes(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    """Records every request so a test can assert that none was made."""

    requests: list[str] = []
    next_stream: _FakeStream | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, _method, url, **_kwargs):
        type(self).requests.append(str(url))
        return type(self).next_stream or _FakeStream()


def _patch_client(monkeypatch, stream: _FakeStream | None = None):
    """`download` imports httpx inside the function, so the patch goes on the
    real module — patching `tgfarm.s11_video.httpx` would create an attribute the
    module never had and the local import would miss."""
    import httpx

    class Client(_FakeClient):
        next_stream = stream

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    _FakeClient.requests = []
    return Client


@pytest.fixture
def video_env(monkeypatch, tmp_path):
    """Point the downloader at a throwaway directory and a known size limit.

    The public-host check is stubbed here on purpose: ``site.invalid`` cannot
    resolve, and ``host_is_private`` denies unresolvable hosts by design, so
    leaving it in place would test the resolver rather than the downloader. The
    guard has its own test below, with the real function and a real private IP.
    """
    import tgfarm.core.net as net
    import tgfarm.s11_video as s11

    settings = SimpleNamespace(
        video_enabled=True,
        video_allow_unknown=False,
        video_max_mb=1,
        video_root=lambda: tmp_path,
        user_agent="test-agent",
        http_timeout_s=5.0,
    )
    monkeypatch.setattr(s11, "get_settings", lambda: settings)
    monkeypatch.setattr(net, "assert_public_http_url", lambda url: url)
    return settings


def test_a_chunked_response_is_cut_off_by_the_byte_cap(video_env, monkeypatch):
    """No `content-length` means the cap has to be enforced while writing.

    The header-only shape would have written any size. Two 800 KiB chunks
    against a 1 MiB limit must stop at the second chunk, refuse the file, and
    leave nothing behind on disk.
    """
    _patch_client(monkeypatch, _FakeStream(chunks=[b"x" * (800 * 1024), b"x" * (800 * 1024)]))
    candidate = MediaCandidate(url="https://site.invalid/clip.mp4", kind="video")

    result = asyncio.run(download(candidate))

    assert result.refusal == REFUSAL_REASONS["too_large"]
    assert result.local_path is None, "a refused file must not stay on disk"
    assert result.bytes > 1024 * 1024
    leftovers = list(video_env.video_root().glob("*.mp4"))
    assert leftovers == [], f"partial download survived: {leftovers}"


def test_a_declared_oversize_is_refused_before_reading_the_body(video_env, monkeypatch):
    client = _patch_client(
        monkeypatch, _FakeStream(headers={"content-length": str(5 * 1024 * 1024)})
    )
    candidate = MediaCandidate(url="https://site.invalid/huge.mp4", kind="video")

    result = asyncio.run(download(candidate))

    assert result.refusal == REFUSAL_REASONS["too_large"]
    assert client.requests == ["https://site.invalid/huge.mp4"]


def test_a_private_address_is_refused_and_never_requested(monkeypatch, tmp_path):
    """The real guard, not a stub: `assert_public_http_url` runs before the socket
    opens, so the refusal must happen first and the fake client must see nothing."""
    import tgfarm.s11_video as s11

    settings = SimpleNamespace(
        video_enabled=True,
        video_allow_unknown=False,
        video_max_mb=1,
        video_root=lambda: tmp_path,
        user_agent="test-agent",
        http_timeout_s=5.0,
    )
    monkeypatch.setattr(s11, "get_settings", lambda: settings)
    client = _patch_client(monkeypatch)
    candidate = MediaCandidate(url="http://127.0.0.1/clip.mp4", kind="video")

    result = asyncio.run(download(candidate))

    assert result.refusal and result.refusal.startswith("url refused")
    assert result.local_path is None or not result.bytes
    assert client.requests == [], "a refused URL must not reach the network"


def test_a_non_200_response_is_refused(video_env, monkeypatch):
    _patch_client(monkeypatch, _FakeStream(status=404))
    candidate = MediaCandidate(url="https://site.invalid/gone.mp4", kind="video")

    result = asyncio.run(download(candidate))

    assert result.refusal == "HTTP 404"


def test_a_clean_download_reports_the_bytes_it_wrote(video_env, monkeypatch):
    _patch_client(monkeypatch, _FakeStream(chunks=[b"a" * 100, b"b" * 200]))
    candidate = MediaCandidate(url="https://site.invalid/ok.mp4", kind="video")

    result = asyncio.run(download(candidate))

    assert not result.refusal, f"unexpected refusal: {result.refusal!r}"
    assert result.bytes == 300
    assert result.local_path is not None
    with open(result.local_path, "rb") as handle:
        assert handle.read() == b"a" * 100 + b"b" * 200


def test_the_facade_does_nothing_when_the_pipeline_is_off(monkeypatch):
    """`video_enabled` is False in the shipped settings, and that is the switch
    the whole S11 stage answers to: no discovery, no fetch, no refusal logged."""
    import tgfarm.s11_video as s11

    monkeypatch.setattr(
        s11,
        "get_settings",
        lambda: SimpleNamespace(video_enabled=False, video_allow_unknown=False),
    )
    client = _patch_client(monkeypatch)
    item = SimpleNamespace(url="https://site.invalid/a", media_links=[], text="")

    assert asyncio.run(fetch_video_for(item)) is None
    assert client.requests == []


def test_the_facade_refuses_a_platform_video_even_with_the_unknown_override(
    video_env, monkeypatch
):
    """`VIDEO_ALLOW_UNKNOWN` widens «no licence marker», never «platform video»:
    a YouTube link is refused by the host rule before the override is consulted."""
    video_env.video_allow_unknown = True
    _patch_client(monkeypatch)
    item = SimpleNamespace(
        url="https://site.invalid/a",
        media_links=["https://www.youtube.com/watch?v=abc"],
        text="",
        html="",
    )

    assert asyncio.run(fetch_video_for(item)) is None

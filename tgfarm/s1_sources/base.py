"""Collector interface + shared HTTP plumbing."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx

from tgfarm.config import SourceSpec
from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.schemas import RawItem
from tgfarm.core.settings import get_settings
from tgfarm.core.text import detect_lang, strip_html

log = get_logger(__name__)


def external_id_for(url: str, guid: str | None = None) -> str:
    if guid:
        return hashlib.sha1(guid.encode("utf-8")).hexdigest()[:32]
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:32]


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    from dateutil import parser as date_parser

    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class Collector(Protocol):
    kind: str

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]: ...


def build_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.http_timeout_s,
        follow_redirects=True,
        headers={
            "User-Agent": settings.user_agent,
            "Accept-Language": "ru,en;q=0.8",
        },
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


# Hosts that answer 429 far more eagerly than the rest, and need a wider gap.
# Raise these when the farm shares an IP with other Reddit clients — a shared
# address is what usually turns into a hard block rather than a slow-down.
HOST_INTERVALS = {
    "www.reddit.com": 10.0,
    "old.reddit.com": 10.0,
    "api.gdeltproject.org": 6.0,  # documented: one request per five seconds
}


class HostPacer:
    """Serialise requests that share a host, with a minimum gap.

    Collectors run concurrently, so two sources pointing at one host — two Reddit
    subreddits, say — hit it within milliseconds of each other and the site
    answers 429. One source's failure is survivable; two is a hole in coverage.
    Requests to *different* hosts are never blocked.
    """

    def __init__(self, interval: float = 2.0) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def interval_for(self, host: str) -> float:
        return HOST_INTERVALS.get(host, self._interval)

    async def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        if not host:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            previous = self._last.get(host)
            if previous is not None:
                delay = self.interval_for(host) - (time.monotonic() - previous)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


PACE = HostPacer()


async def get_text(client: httpx.AsyncClient, url: str, max_bytes: int = 4_000_000) -> str:
    await RATE.http.take()
    await PACE.wait(url)
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise ProviderUnavailable(f"GET {url[:120]} -> {resp.status_code}")
    content = resp.content[:max_bytes]
    resp.raise_for_status()
    # feedparser copes better with a decoded string than with raw bytes when the
    # server lies about the charset
    try:
        return content.decode(resp.encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def to_raw(
    spec: SourceSpec,
    *,
    title: str,
    url: str,
    text: str = "",
    guid: str | None = None,
    published: datetime | None = None,
    media_links: list[str] | None = None,
    media_kind: str | None = None,
) -> RawItem | None:
    title = strip_html(title or "").strip()
    url = (url or "").strip()
    if len(title) < 8 or not url.lower().startswith("http"):
        return None
    clean_text = strip_html(text or "")[:6000]
    return RawItem(
        source_id=spec.id,
        external_id=external_id_for(url, guid),
        url=url,
        title=title[:400],
        text=clean_text,
        lang=spec.lang if spec.lang else detect_lang(title + " " + clean_text[:200]),
        published_at=published,
        niche_hint=list(spec.niches),
        source_weight=spec.weight,
        media_links=media_links or [],
        media_kind=media_kind,
    )


async def collect_all(fetchers: dict[str, Collector], specs: list[SourceSpec]) -> list[RawItem]:
    """Fetch every source concurrently, isolating per-source failures."""
    async with build_client() as client:

        async def _one(spec: SourceSpec) -> list[RawItem]:
            fetcher = fetchers.get(spec.kind)
            if fetcher is None:
                log.warning("no collector for kind=%s (source %s)", spec.kind, spec.id)
                return []
            try:
                items = await fetcher.fetch(spec, client)
                log.info("collected %-22s %3d items", spec.id, len(items))
                return items
            except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
                log.warning("collector %s failed: %s", spec.id, exc)
                return []

        results = await asyncio.gather(*(_one(spec) for spec in specs), return_exceptions=True)

    flat: list[RawItem] = []
    for result in results:
        if isinstance(result, list):
            flat.extend(result)
    return flat
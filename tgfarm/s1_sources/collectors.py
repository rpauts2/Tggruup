"""S1 collectors: RSS/Atom, Google News query feeds, GDELT article lists."""

from __future__ import annotations

import json
import urllib.parse

import feedparser
import httpx

from tgfarm.config import SourceSpec
from tgfarm.core.logging import get_logger
from tgfarm.core.media import discover_from_feed, dominant_kind
from tgfarm.core.ratelimit import RATE
from tgfarm.core.schemas import RawItem
from tgfarm.core.settings import get_settings
from tgfarm.core.text import strip_html
from tgfarm.s1_sources.base import Collector, get_text, parse_date, to_raw
from tgfarm.s1_sources.extra_sources import (
    GitHubCollector,
    HackerNewsCollector,
    RedditCollector,
    WebSearchCollector,
    YouTubeCollector,
)
from tgfarm.s1_sources.telegram_source import TelegramCollector

log = get_logger(__name__)

# feedparser has no hard cap; we do
MAX_ITEMS_PER_FEED = 60


class RssCollector:
    """Any static RSS/Atom feed."""

    kind = "rss"

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        payload = await get_text(client, spec.url)
        parsed = feedparser.parse(payload)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"feed parse failed: {parsed.bozo_exception}")

        items: list[RawItem] = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            media = media_of(entry)
            raw = to_raw(
                spec,
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                text=_entry_text(entry),
                guid=entry.get("id") or entry.get("guid"),
                published=parse_date(
                    entry.get("published") or entry.get("updated") or entry.get("pubDate")
                ),
                media_links=media[0],
                media_kind=media[1],
            )
            if raw:
                items.append(raw)
        return items


def media_of(entry) -> tuple[list[str], str | None]:
    """Media a feed entry carries, plus the kind that decides layout.

    Most headline feeds attach nothing, so this is the cheap common path; the
    video stage only pays for an article fetch when it comes back empty.
    """
    candidates = discover_from_feed(entry)
    return [c.url for c in candidates], dominant_kind(candidates)


def _entry_text(entry) -> str:
    """Prefer real content; fall back to the summary. Many feeds put the whole
    article in `content` and only a teaser in `summary`."""
    contents = entry.get("content") or []
    for block in contents:
        value = block.get("value")
        if value and len(value) > 120:
            return strip_html(value)
    summary = entry.get("summary") or entry.get("description") or ""
    return strip_html(summary)


class GoogleNewsCollector:
    """Google News RSS, which turns a query into a live filtered feed.

    This is the workhorse: one query template per niche gives fresh, localised,
    properly-dated news without any API key.

    The substituted query has to be percent-encoded. Handing httpx a raw URL with
    spaces and Cyrillic in it yields a parseable feed with zero entries — the
    request "succeeds", so the failure looks like "no news today" rather than a
    bug.
    """

    kind = "gnews"

    def render_url(self, spec: SourceSpec) -> str:
        params = spec.params or {}
        substitutions = {
            "Q": params.get("Q", ""),
            "Q1": params.get("Q1", ""),
            "CITY": params.get("CITY", _city_from_config()),
            "NICHE": spec.niches[0] if spec.niches else "",
        }
        rendered = spec.url
        for key, value in substitutions.items():
            rendered = rendered.replace("{{" + key + "}}", urllib.parse.quote(str(value), safe=""))
        return rendered

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        url = self.render_url(spec)
        payload = await get_text(client, url)
        parsed = feedparser.parse(payload)
        items: list[RawItem] = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            # Google News wraps the real publisher URL; keep the google one —
            # it is stable and clickable — but take the title/summary from feed
            media = media_of(entry)
            raw = to_raw(
                spec,
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                text=_entry_text(entry),
                guid=entry.get("id") or entry.get("guid"),
                published=parse_date(entry.get("published") or entry.get("pubDate")),
                media_links=media[0],
                media_kind=media[1],
            )
            if raw:
                items.append(raw)
        return items


class GdeltCollector:
    """GDELT DOC 2.0 — free, 65 languages, no key, near-real-time.

    GDELT entries have only a headline, which is fine: the farm's job is to
    detect that a story is heating up, and the quality gate then pulls the
    article body from the linked publisher.
    """

    kind = "gdelt"

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        params = spec.params or {}
        query = params.get("query", "artificial intelligence")
        query_params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(int(params.get("maxrecords", 40))),
            "timespan": params.get("timespan", "6h"),
            "sort": "datedesc",
        }
        url = f"{spec.url}?{urllib.parse.urlencode(query_params)}"
        await RATE.http.take()
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"gdelt {resp.status_code}: {resp.text[:160]}")
        payload = json.loads(resp.text)

        items: list[RawItem] = []
        for article in payload.get("articles", [])[:MAX_ITEMS_PER_FEED]:
            raw = to_raw(
                spec,
                title=article.get("title", ""),
                url=article.get("url", ""),
                text="",
                guid=article.get("url"),
                published=parse_date(article.get("seendate")),
            )
            if raw:
                raw.lang = article.get("language") or spec.lang
                items.append(raw)
        return items


def _city_from_config() -> str:
    from tgfarm.config import get_config

    for persona in get_config().personas:
        if persona.niche == "local" and persona.city:
            return persona.city
    return ""


async def collect(fetchers: dict[str, Collector], specs: list[SourceSpec]) -> list[RawItem]:
    from tgfarm.s1_sources.base import collect_all

    settings = get_settings()
    if not settings.online:
        log.warning("mode=off — skipping network collection")
        return []
    return await collect_all(fetchers, [s for s in specs if s.enabled])


FETCHERS: dict[str, Collector] = {
    "rss": RssCollector(),
    "gnews": GoogleNewsCollector(),
    "gdelt": GdeltCollector(),
    "reddit": RedditCollector(),
    "hackernews": HackerNewsCollector(),
    "youtube": YouTubeCollector(),
    "github": GitHubCollector(),
    "websearch": WebSearchCollector(),
    "telegram": TelegramCollector(),
}
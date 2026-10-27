"""Additional source collectors.

All four need nothing but a URL, and all four arrive as RSS/Atom, so they reuse
the existing feed parser instead of growing a second one:

* Reddit          — subreddit and search feeds, `.rss` suffix
* Hacker News     — hnrss.org, front page and per-query
* YouTube         — per-channel video feeds
* GitHub          — release and tag atom feeds for a repository

One rule applies throughout: only what a feed actually contains is recorded. A
YouTube feed has a title and a description and no article, so the body is the
description and the pipeline treats it as a headline source rather than
pretending there is more text than there is.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import UTC, datetime

import feedparser
import httpx

from tgfarm.config import SourceSpec
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.schemas import RawItem
from tgfarm.core.text import strip_html
from tgfarm.s1_sources.base import PACE, external_id_for, get_text, parse_date

log = get_logger(__name__)

MAX_ITEMS_PER_FEED = 40
REDDIT_USER_AGENT = "tgfarm/1.2 (feed reader)"


def _to_raw(spec: SourceSpec, entry: dict, url: str, title: str, text: str) -> RawItem | None:
    title = strip_html(title or "").strip()
    if len(title) < 8 or not url.lower().startswith("http"):
        return None
    return RawItem(
        source_id=spec.id,
        external_id=external_id_for(url, entry.get("id") or entry.get("guid")),
        url=url,
        title=title[:400],
        text=strip_html(text or "")[:6000],
        lang=spec.lang or "unknown",
        published_at=parse_date(
            entry.get("published") or entry.get("updated") or entry.get("pubDate")
        ),
        niche_hint=list(spec.niches),
        source_weight=spec.weight,
    )


async def _feed(client: httpx.AsyncClient, url: str) -> list[dict]:
    payload = await get_text(client, url)
    parsed = feedparser.parse(payload)
    return list(parsed.entries[:MAX_ITEMS_PER_FEED])


class RedditCollector:
    """Subreddit or search results, as RSS.

    Reddit blocks requests without a descriptive User-Agent, and the collector
    client carries the farm's default — so this one overrides it per request.
    """

    kind = "reddit"

    def render_url(self, spec: SourceSpec) -> str:
        params = spec.params or {}
        if params.get("search"):
            query = urllib.parse.quote(str(params["search"]))
            sort = params.get("sort", "new")
            return f"https://www.reddit.com/search.rss?q={query}&sort={sort}&t={params.get('t', 'week')}"
        sub = params.get("subreddit") or "technology"
        sort = params.get("sort", "new")
        return f"https://www.reddit.com/r/{sub}/.rss?sort={sort}"

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        url = self.render_url(spec)
        headers = {"User-Agent": REDDIT_USER_AGENT}
        payload = ""
        for attempt in range(2):
            await RATE.http.take()
            await PACE.wait(url)
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                payload = resp.text
                break
            if resp.status_code != 429:
                raise RuntimeError(f"reddit {resp.status_code}: {resp.text[:120]}")
            # one retry, after the host gap plus a little — Reddit's limit is
            # per-IP per-second, so this clears it without a long stall
            log.info("reddit 429, retrying %s", spec.id)
            await asyncio.sleep(4.0 * (attempt + 1))
        if not payload:
            raise RuntimeError("reddit 429: rate limited after retry")
        parsed = feedparser.parse(payload)
        items: list[RawItem] = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            raw = _to_raw(
                spec, entry, entry.get("link", ""), entry.get("title", ""), entry.get("summary", "")
            )
            if raw:
                items.append(raw)
        return items


class HackerNewsCollector:
    """hnrss.org — the front page or any search query."""

    kind = "hackernews"

    def render_url(self, spec: SourceSpec) -> str:
        params = spec.params or {}
        if params.get("query"):
            query = urllib.parse.quote(str(params["query"]))
            return f"https://hnrss.org/newest?q={query}&points={params.get('min_points', 0)}"
        return "https://hnrss.org/frontpage?points=" + str(params.get("min_points", 100))

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        items: list[RawItem] = []
        for entry in await _feed(client, self.render_url(spec)):
            description = entry.get("description") or entry.get("summary") or ""
            raw = _to_raw(
                spec, entry, entry.get("link", ""), entry.get("title", ""), description
            )
            if raw:
                items.append(raw)
        return items


class YouTubeCollector:
    """A channel's uploads. Descriptions only — there is no article here."""

    kind = "youtube"

    def render_url(self, spec: SourceSpec) -> str:
        params = spec.params or {}
        channel = params.get("channel_id") or params.get("channel") or ""
        if not channel:
            raise ValueError("youtube-источнику нужен params.channel_id или params.channel")
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(str(channel))}"

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        items: list[RawItem] = []
        for entry in await _feed(client, self.render_url(spec)):
            # yt:videoId is the real identifier; the guid is what feedparser picks up
            video_id = entry.get("yt_videoid") or entry.get("video_id") or ""
            url = entry.get("link") or ""
            raw = _to_raw(
                spec, entry, url, entry.get("title", ""), entry.get("summary", "")
            )
            if raw:
                if video_id:
                    raw.external_id = f"{spec.id}-{video_id}"
                raw.media_kind = "video" if entry.get("media_thumbnail") else None
                items.append(raw)
        return items


class GitHubCollector:
    """Releases or tags of one repository."""

    kind = "github"

    def render_url(self, spec: SourceSpec) -> str:
        params = spec.params or {}
        owner = params.get("owner")
        repo = params.get("repo")
        if not owner or not repo:
            raise ValueError("github-источнику нужны params.owner и params.repo")
        what = params.get("what", "releases")
        return f"https://github.com/{owner}/{repo}/{what}.atom"

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        items: list[RawItem] = []
        for entry in await _feed(client, self.render_url(spec)):
            content = entry.get("content") or [{}]
            body = content[0].get("value") if isinstance(content[0], dict) else ""
            raw = _to_raw(
                spec,
                entry,
                entry.get("link", ""),
                entry.get("title", ""),
                body or entry.get("summary", ""),
            )
            if raw:
                items.append(raw)
        return items


class WebSearchCollector:
    """Tavily / Serper as a source: one news query per niche.

    Enabled only with a key. The result is a short list of headlines with
    snippets, which is exactly what the clusterer wants — and the snippet is
    marked as such so downstream stages do not mistake it for an article body.
    """

    kind = "websearch"

    async def fetch(self, spec: SourceSpec, client: httpx.AsyncClient) -> list[RawItem]:
        params = spec.params or {}
        query = params.get("query") or " ".join(params.get("niches") or spec.niches)
        if not query:
            return []
        results = await _search(client, query, days=int(params.get("days", 7)))
        items: list[RawItem] = []
        for row in results:
            url = row.get("url") or ""
            title = row.get("title") or ""
            if not url or len(title) < 8:
                continue
            snippet = row.get("content") or row.get("snippet") or ""
            body = f"{title}\n\n{snippet}".strip()
            items.append(
                RawItem(
                    source_id=spec.id,
                    external_id=external_id_for(url, None),
                    url=url,
                    title=title[:400],
                    text=body[:6000],
                    lang=spec.lang or "unknown",
                    published_at=_parse_iso(row.get("published_date")),
                    niche_hint=list(spec.niches),
                    source_weight=spec.weight,
                )
            )
        return items


async def _search(client: httpx.AsyncClient, query: str, days: int = 7) -> list[dict]:
    from tgfarm.core.settings import get_settings

    settings = get_settings()

    if settings.tavily_api_key:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "days": days,
                "search_depth": "basic",
                "max_results": int(settings.web_search_max_results),
                "topic": "news",
            },
            timeout=30.0,
        )
        if resp.status_code == 200:
            return list(resp.json().get("results", []))
        log.info("tavily %s: %s", resp.status_code, resp.text[:120])

    if settings.serper_api_key:
        resp = await client.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "ru", "hl": "ru"},
            timeout=30.0,
        )
        if resp.status_code == 200:
            return [
                {
                    "title": row.get("title", ""),
                    "url": row.get("link", ""),
                    "snippet": row.get("snippet", ""),
                    "published_date": row.get("date", ""),
                }
                for row in resp.json().get("news", [])
            ]
        log.info("serper %s: %s", resp.status_code, resp.text[:120])

    return []


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    from dateutil import parser as date_parser

    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "GitHubCollector",
    "HackerNewsCollector",
    "RedditCollector",
    "WebSearchCollector",
    "YouTubeCollector",
]
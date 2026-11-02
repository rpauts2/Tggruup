"""Lead-photo selection for Dzen articles.

A feed image is the publisher's own lead illustration, so it is preferred over a
blind image search. When the feed carries no usable image, the article's
``og:image`` is used as a fallback. Every candidate is decoded, checked for a
usable size and aspect ratio, re-encoded, and stored under ``data/media``.

The module is deliberately separate from the Pillow cover renderer: this is the
only place that turns a remote photo into a local asset, and it can be disabled
with ``ARTICLE_IMAGE_ENABLED=false`` without touching the publishing path.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.media import (
    MediaCandidate,
    discover_from_feed,
    discover_in_html,
    kind_of,
)
from tgfarm.core.schemas import MediaAsset
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.models import Media as MediaRow
from tgfarm.db.models import Post as PostRow
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope

log = get_logger(__name__)

MIN_WIDTH = 400
MIN_HEIGHT = 200
MIN_ASPECT = 0.5
MAX_ASPECT = 2.8
MAX_PROBES_PER_POST = 3
MAX_INLINE_IMAGES = 3
HTML_LIMIT = 1_500_000


@dataclass
class SourceItem:
    url: str
    title: str = ""
    media_links: list[str] | None = None
    media_kind: str | None = None


def large_variants(url: str) -> list[str]:
    """Ask a CDN for the full-size sibling of a tiny feed thumbnail.

    phys.org feeds carry 90×90 ``/csz/news/tmb/`` images while the same asset is
    published at 800 px under ``/csz/news/800/``. Requesting the large sibling
    avoids fetching a 403 article page just to find a usable photo.
    """
    marker = "/csz/news/tmb/"
    if marker not in url:
        return [url]
    return [
        url.replace(marker, "/csz/news/800/"),
        url.replace(marker, "/csz/news/800a/"),
        url,
    ]


def image_candidates(item) -> list[MediaCandidate]:
    """Feed images in the order the publisher supplied them.

    Some CDNs serve extensionless image URLs (BeInCrypto, for example), so when
    collection already classified the entry as ``image`` every HTTP link is
    trusted as an image instead of being dropped for lacking a file suffix.
    """
    links = [
        unescape(str(link)).strip()
        for link in (getattr(item, "media_links", None) or [])
    ]
    links = [link for link in links if link.startswith(("http://", "https://"))]
    urls: list[str] = []
    seen: set[str] = set()
    if getattr(item, "media_kind", None) == "image":
        for link in links:
            if kind_of(link) == "video":
                continue
            for url in large_variants(link):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        return [MediaCandidate(url=url, kind="image") for url in urls]
    for link in links:
        for candidate in discover_from_feed({"links": [link]}):
            if candidate.kind != "image":
                continue
            for url in large_variants(candidate.url):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
    return [MediaCandidate(url=url, kind="image") for url in urls]


def absolute_candidates(candidates: list[MediaCandidate], base_url: str) -> list[MediaCandidate]:
    for candidate in candidates:
        candidate.url = urljoin(base_url, candidate.url)
    return candidates


def usable_image(image: Image.Image) -> bool:
    width, height = image.size
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return False
    aspect = width / max(1, height)
    return MIN_ASPECT <= aspect <= MAX_ASPECT


def normalise_image(data: bytes) -> tuple[bytes, int, int] | None:
    """Decode, orient, downscale and re-encode to JPEG.

    Re-encoding strips metadata (including GPS coordinates) and guarantees Dzen
    receives a format it can display inline.
    """
    settings = get_settings()
    try:
        with Image.open(io.BytesIO(data)) as raw:
            raw.load()
            image = ImageOps.exif_transpose(raw)
            if not usable_image(image):
                return None
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode == "RGBA":
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"), (0, 0))
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            limit = max(640, settings.article_image_max_width)
            image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=88, optimize=True, progressive=True)
            return buffer.getvalue(), image.width, image.height
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def save_image(data: bytes, source_url: str) -> MediaAsset | None:
    normalised = normalise_image(data)
    if normalised is None:
        return None
    payload, width, height = normalised
    digest = hashlib.sha1(payload).hexdigest()[:16]
    path = get_settings().media_root() / f"article_{digest}.jpg"
    if not path.exists():
        path.write_bytes(payload)
    host = urlparse(source_url).hostname or ""
    return MediaAsset(
        kind="article_image",
        path=str(path),
        width=width,
        height=height,
        sha=hashlib.sha1(payload).hexdigest(),
        caption=f"Фото: {host}" if host else None,
    )


async def _download(url: str) -> bytes | None:
    from tgfarm.core.net import UnsafeURLError, assert_public_http_url

    try:
        assert_public_http_url(url)
    except UnsafeURLError as exc:
        log.debug("article image url refused: %s", exc)
        return None

    settings = get_settings()
    limit = max(1, settings.article_image_max_mb) * 1024 * 1024
    headers = {"User-Agent": settings.user_agent}
    try:
        async with httpx.AsyncClient(
            timeout=min(settings.article_image_timeout_s, settings.http_timeout_s + 5),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                mime = (response.headers.get("content-type") or "").lower()
                if mime and not mime.startswith("image/"):
                    return None
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > limit:
                    return None
                buffer = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > limit:
                        return None
                return bytes(buffer)
    except Exception as exc:  # noqa: BLE001 - enrichment must never fail the cycle
        log.debug("article image download failed %s: %s", url[:90], exc)
        return None


async def _article_candidates(url: str) -> list[MediaCandidate]:
    from tgfarm.core.net import UnsafeURLError, assert_public_http_url

    try:
        assert_public_http_url(url)
    except UnsafeURLError as exc:
        log.debug("article page url refused: %s", exc)
        return []

    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=min(settings.article_image_timeout_s, settings.http_timeout_s),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return []
            html = response.text[:HTML_LIMIT]
            return absolute_candidates(discover_in_html(html, response.url), str(response.url))
    except Exception as exc:  # noqa: BLE001 - page fetch is optional
        log.debug("article page fetch failed %s: %s", url[:90], exc)
        return []


async def _web_image_candidates(title: str) -> list[str]:
    """Best-effort image search on Wikimedia Commons (no API key required)."""
    query = (title or "").strip()
    if not query:
        return []
    import httpx

    settings = get_settings()
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": "6",
        "prop": "imageinfo",
        "iiprop": "url|size",
        "format": "json",
    }
    urls: list[str] = []
    seen: set[str] = set()
    try:
        async with httpx.AsyncClient(
            timeout=min(settings.article_image_timeout_s, settings.http_timeout_s),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            resp = await client.get("https://commons.wikimedia.org/w/api.php", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            pages = (data.get("query") or {}).get("pages") or {}
            for page in pages.values():
                for info in (page.get("imageinfo") or []):
                    url = info.get("url")
                    if not url or url in seen:
                        continue
                    if not url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        continue
                    width = int(info.get("width") or 0)
                    height = int(info.get("height") or 0)
                    if width < MIN_WIDTH or height < MIN_HEIGHT:
                        continue
                    seen.add(url)
                    urls.append(url)
    except Exception as exc:  # noqa: BLE001
        log.debug("web image search failed: %s", exc)
    return urls[:MAX_PROBES_PER_POST]



async def fetch_for_item(item) -> MediaAsset | None:
    settings = get_settings()
    if not settings.article_image_enabled or not settings.online:
        return None

    candidates = image_candidates(item)
    for candidate in candidates[:MAX_PROBES_PER_POST]:
        data = await _download(candidate.url)
        if not data:
            continue
        asset = save_image(data, candidate.url)
        if asset is not None:
            log.info("article image for %s: %s", getattr(item, "url", "")[:90], asset.path)
            return asset

    article_url = getattr(item, "url", "") or ""
    if article_url:
        for candidate in (await _article_candidates(article_url))[:MAX_PROBES_PER_POST]:
            data = await _download(candidate.url)
            if not data:
                continue
            asset = save_image(data, candidate.url)
            if asset is not None:
                log.info(
                    "article og:image for %s: %s", article_url[:90], asset.path
                )
                return asset

    title = getattr(item, "title", "") or ""
    for url in await _web_image_candidates(title):
        data = await _download(url)
        if not data:
            continue
        asset = save_image(data, url)
        if asset is not None:
            log.info("article web image for %s: %s", title[:90], asset.path)
            return asset
    return None


def _load_posts(post_public_ids: list[str]) -> tuple[list[dict], dict[int, list[SourceItem]]]:
    if not post_public_ids:
        return [], {}
    config = get_config()
    with session_scope() as session:
        rows = session.execute(
            select(
                PostRow.id,
                PostRow.public_id,
                PostRow.channel_id,
                PostRow.cluster_public_id,
            ).where(PostRow.public_id.in_(post_public_ids))
        ).all()
        posts = [
            {"id": r[0], "public_id": r[1], "channel_id": r[2], "cluster_public_id": r[3]}
            for r in rows
        ]
        if not posts:
            return [], {}
        with_inline = {}
        for post_id, path in session.execute(
            select(MediaRow.post_id, MediaRow.path).where(
                MediaRow.post_id.in_([post["id"] for post in posts]),
                MediaRow.kind == "article_image",
            )
        ).all():
            if path and Path(path).exists():
                with_inline[post_id] = with_inline.get(post_id, 0) + 1
        wanted = [
            post
            for post in posts
            if with_inline.get(post["id"], 0) < MAX_INLINE_IMAGES
            and (persona := config.persona(post["channel_id"])) is not None
            and persona.platform == "dzen"
            and persona.enabled
        ]
        if not wanted:
            return [], {}

        cluster_ids = {post["cluster_public_id"] for post in wanted if post["cluster_public_id"]}
        clusters = {
            row.public_id: row.id
            for row in session.execute(
                select(ClusterRow).where(ClusterRow.public_id.in_(cluster_ids))
            ).scalars()
        }
        if not clusters:
            return [], {}
        items: dict[int, list[SourceItem]] = {}
        raw_rows = session.execute(
            select(
                RawItemRow.cluster_id,
                RawItemRow.url,
                RawItemRow.title,
                RawItemRow.media_links,
                RawItemRow.media_kind,
            )
            .where(RawItemRow.cluster_id.in_(clusters.values()))
            .order_by(RawItemRow.heat.desc(), RawItemRow.source_weight.desc())
        ).all()
        for row in raw_rows:
            items.setdefault(row.cluster_id, []).append(
                SourceItem(
                    url=row.url,
                    title=row.title,
                    media_links=list(row.media_links or []),
                    media_kind=row.media_kind,
                )
            )

        return (
            [
                {
                    "public_id": post.public_id,
                    "channel_id": post["channel_id"],
                    "cluster_id": clusters.get(post["cluster_public_id"]),
                    "inline_count": with_inline.get(post["id"], 0),
                }
                for post in wanted
            ],
            items,
        )


def _persist(public_id: str, asset: MediaAsset) -> None:
    from tgfarm.s6_visual import persist_media

    persist_media(public_id, asset, is_primary=False)


def _file_sha(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dzen_pending_without_inline() -> list[str]:
    """Scheduled Dzen posts that still have no inline photo.

    Transport backoff can park dozens of Dzen posts for hours; when they retry
    they republish exactly what they had — a text-template cover. Feeding them
    through the attach pass gives every retry a real illustration.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Channel as ChannelRow
    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(PostRow.public_id)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(
                ChannelRow.platform == "dzen",
                PostRow.status == "scheduled",
                ~select(MediaRow.id)
                .where(
                    MediaRow.post_id == PostRow.id,
                    MediaRow.kind == "article_image",
                )
                .exists(),
            )
            .order_by(PostRow.published_at.asc())
            .limit(40)
        ).scalars().all()
    return list(rows)


def _recent_image_shas(limit: int = 40) -> set[str]:
    """Shas of inline photos used by recent posts.

    The same picture twice in a feed is the fastest way to look templated; the
    guard is cheap, so it runs on every attach.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(MediaRow.sha)
            .where(MediaRow.kind == "article_image", MediaRow.sha.isnot(None))
            .order_by(MediaRow.id.desc())
            .limit(limit)
        ).scalars().all()
    return {str(row) for row in rows if row}


async def attach_inline_images(post_public_ids: list[str]) -> int:
    """Attach up to MAX_INLINE_IMAGES photos to each Dzen post that lacks them."""
    if not get_settings().article_image_enabled:
        return 0

    from tgfarm.core.run import to_thread

    try:
        backlog = await to_thread(_dzen_pending_without_inline)
    except Exception as exc:  # noqa: BLE001 - backlog enrichment is optional
        log.debug("backlog dzen lookup failed: %s", exc)
        backlog = []
    if backlog:
        post_public_ids = list(dict.fromkeys([*post_public_ids, *backlog]))

    loaded = await to_thread(_load_posts, post_public_ids)
    if not loaded:
        return 0
    posts, items_by_cluster = loaded
    semaphore = asyncio.Semaphore(3)
    attached_total = 0
    used_shas = await to_thread(_recent_image_shas)

    async def attach_one(post: dict) -> int:
        items = items_by_cluster.get(post["cluster_id"], [])
        if not items:
            return 0
        need = MAX_INLINE_IMAGES - int(post.get("inline_count") or 0)
        if need <= 0:
            return 0
        attached = 0
        seen_paths: set[str] = set()
        async with semaphore:
            for item in items:
                if attached >= need:
                    break
                asset = await fetch_for_item(item)
                if asset is None or not asset.path:
                    continue
                if asset.path in seen_paths:
                    continue
                sha = asset.sha or _file_sha(asset.path)
                if sha in used_shas:
                    continue
                seen_paths.add(asset.path)
                used_shas.add(sha)
                await to_thread(_persist, post["public_id"], asset)
                attached += 1
        return attached

    results = await asyncio.gather(*(attach_one(post) for post in posts))
    attached_total = sum(results)

    # Fallback: a Dzen post without a single photo still deserves an
    # illustration — the operator asked for images on every post, humor-toned.
    # The configured image provider (Pollinations is keyless) draws a light
    # editorial piece; if it fails, the post keeps its template cover.
    from tgfarm.config import get_config
    from tgfarm.core.schemas import MediaAsset

    config = get_config()
    generated = 0
    for post, attached in zip(posts, results):
        if attached > 0:
            continue
        persona = config.persona(post["channel_id"])
        palette = persona.palette if persona else {}
        niche = persona.niche if persona else "general"
        try:
            with session_scope() as session:
                hook = session.execute(
                    select(PostRow.hook).where(PostRow.public_id == post["public_id"])
                ).scalar_one_or_none() or ""
            from tgfarm.llm.images import generate_cover

            art = await generate_cover(
                (hook or "новость дня")[:120],
                "легкая ироничная иллюстрация к новости, без текста",
                niche,
                palette,
            )
            if art and art.path:
                await to_thread(
                    _persist,
                    post["public_id"],
                    MediaAsset(
                        kind="article_image",
                        path=str(art.path),
                        width=art.width,
                        height=art.height,
                        sha=art.sha,
                    ),
                )
                generated += 1
        except Exception as exc:  # noqa: BLE001 - a fallback must not fail the stage
            log.debug("humor image fallback failed for %s: %s", post["public_id"], exc)
    if generated:
        log.info("s6: generated %d humor fallback images for imageless posts", generated)

    attached_total += generated
    log.info("s6: attached %d inline article images", attached_total)
    return attached_total

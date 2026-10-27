"""Media discovery: pure parsing of feeds and article HTML.

Kept separate from `s11_video` because *every* stage needs it — S1 has to record
what media a feed entry carried at collection time, long before the video stage
decides whether the farm may reuse it. Nothing in this module touches the
network or the settings: it turns bytes into candidates and nothing else, so the
licensing decision stays in one place.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

# Platforms whose URLs are never a syndication licence.
REFUSED_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "vimeo.com", "www.vimeo.com", "player.vimeo.com",
    "dailymotion.com", "www.dailymotion.com",
    "twitch.tv", "www.twitch.tv",
    "twitter.com", "x.com", "nitter.net",
    "rutube.ru", "www.rutube.ru", "ok.ru", "vk.com", "dzen.ru",
    "odysee.com", "rumble.com",
    "ted.com", "www.ted.com", "coub.com",
    "streamable.com", "bitchute.com",
}

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mov", ".ogv", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}

VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime", "video/x-m4v", "video/ogg"}

CC_RE = re.compile(
    r"creative\s+commons|cc[\s\-]?by|public\s+domain|creativecommons\.org",
    re.IGNORECASE,
)
_VIDEO_URL_RE = re.compile(r"video|mp4|webm|m3u8", re.IGNORECASE)

REFUSAL_REASONS = {
    "platform_video": "ссылка на видеоплатформу, а не на файл",
    "cross_domain": "хост не совпадает с доменом статьи",
    "no_licence_marker": "нет явной лицензии на переиспользование",
    "too_large": "файл превышает лимит",
    "extension": "неподдерживаемый формат",
}

_VIDEO_TAG_RE = re.compile(
    r"""<(?:video|source)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["'][^>]*>""", re.I
)
_IMAGE_TAG_RE = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["'][^>]*>""", re.I
)
_OG_VIDEO_RE = re.compile(
    r"""<meta[^>]+property\s*=\s*["']og:video(?::secure_url|:url)?["'][^>]+"""
    r"""content\s*=\s*["']([^"']+)["']""",
    re.I,
)
_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property\s*=\s*["']og:image(?::secure_url|:url)?["'][^>]+"""
    r"""content\s*=\s*["']([^"']+)["']""",
    re.I,
)


@dataclass
class MediaCandidate:
    url: str
    kind: str = "video"  # video | image
    mime: str = ""
    title: str = ""
    license_hint: str = ""
    article_url: str = ""
    host: str = ""
    same_domain: bool = False
    allowed: bool = False
    refusal: str = ""
    bytes: int = 0
    local_path: str | None = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def extension_of(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 and len(path) - dot <= 6 else ""


def kind_of(url: str) -> str | None:
    """Classify a media URL by extension. None when the extension says nothing."""
    ext = extension_of(url)
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def _resolve_kind(url: str, kind: str | None, trusted: bool = False) -> str | None:
    """Decide the kind of one media URL.

    `trusted` means the source declared a video mime type, which is stronger than
    the file name: signed CDN URLs carry no extension but are genuinely video.
    Without that signal a `video` claim has to be backed by a video extension —
    otherwise `<source src=".../config.json">` becomes a "video" and the pipeline
    goes off to download a player manifest.
    """
    detected = kind_of(url)
    resolved = kind or detected
    if resolved == "video" and detected != "video" and not trusted:
        resolved = "image" if detected == "image" else None
    return resolved


def discover_in_html(html: str, base_url: str = "") -> list[MediaCandidate]:
    """Extract media references from raw HTML.

    The tag decides the kind: `<img>` is never a video. Where a tag is missing
    (a bare URL inside prose) the extension decides instead, and an unrecognised
    URL is dropped rather than guessed at.
    """
    out: list[MediaCandidate] = []
    seen: set[str] = set()

    def add(url: str, kind: str | None) -> None:
        url = url.strip()
        if not url:
            return
        if base_url:
            url = urljoin(base_url, url)
        if url in seen:
            return
        resolved = _resolve_kind(url, kind)
        if resolved is None:
            return
        seen.add(url)
        out.append(MediaCandidate(url=url, kind=resolved))

    for match in _OG_VIDEO_RE.finditer(html or ""):
        add(match.group(1), "video")
    for match in _OG_IMAGE_RE.finditer(html or ""):
        add(match.group(1), "image")
    for match in _VIDEO_TAG_RE.finditer(html or ""):
        add(match.group(1), "video")
    for match in _IMAGE_TAG_RE.finditer(html or ""):
        add(match.group(1), "image")
    return out


def discover_from_feed(entry: dict) -> list[MediaCandidate]:
    """Pull media references out of an RSS/Atom entry.

    feedparser normalises `<enclosure>` into `entry.enclosures` and `<media:content>`
    into `entry.media_content`, but not every feed uses the same shape, so plain
    `links`/`content` markup is scanned too.
    """
    found: dict[str, MediaCandidate] = {}

    def add(url: str, kind: str | None, mime: str = "", title: str = "") -> None:
        url = (url or "").strip()
        if not url.lower().startswith("http") or url in found:
            return
        resolved = _resolve_kind(url, kind, trusted=(mime or "").lower().startswith("video/"))
        if resolved is None:
            return
        found[url] = MediaCandidate(url=url, kind=resolved, mime=mime, title=title)

    for link in entry.get("links", []) or []:
        if isinstance(link, str):
            # no type to go on: the extension is the only honest signal
            add(link, kind_of(link))
            continue
        mime = (link.get("type") or "").lower()
        href = link.get("href") or ""
        if mime.startswith("video/"):
            add(href, "video", mime, link.get("title", ""))
        elif mime.startswith("image/"):
            add(href, "image", mime, link.get("title", ""))
        elif href:
            add(href, kind_of(href), mime, link.get("title", ""))

    for enclosure in entry.get("enclosures", []) or []:
        if not isinstance(enclosure, dict):
            continue
        mime = (enclosure.get("type") or "").lower()
        href = enclosure.get("href") or enclosure.get("url") or ""
        if mime.startswith("video/"):
            add(href, "video", mime, enclosure.get("title", ""))
        elif mime.startswith("image/"):
            add(href, "image", mime, enclosure.get("title", ""))

    for block in entry.get("media_content", []) or entry.get("media_thumbnail", []) or []:
        if isinstance(block, dict):
            mime = (block.get("type") or block.get("medium") or "").lower()
            href = block.get("url") or ""
            if mime.startswith("video/"):
                add(href, "video", mime)
            elif mime.startswith("image/"):
                add(href, "image", mime)
            else:
                add(href, kind_of(href), mime)

    for content in entry.get("content", []) or []:
        if isinstance(content, dict):
            for candidate in discover_in_html(content.get("value") or ""):
                add(candidate.url, candidate.kind, candidate.mime, candidate.title)

    for candidate in discover_in_html(entry.get("summary") or entry.get("description") or ""):
        add(candidate.url, candidate.kind, candidate.mime, candidate.title)

    return list(found.values())


def detect_licence_marker(html: str, meta: dict | None = None) -> str:
    """Look for an explicit reuse statement."""
    meta = meta or {}
    for key in ("license", "dcterms.license", "cc:license", "copyright"):
        value = meta.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value and CC_RE.search(str(value)):
            return str(value)[:200]

    blob = " ".join(
        [
            html or "",
            meta.get("license", "") if isinstance(meta.get("license"), str) else "",
            meta.get("copyright", "") if isinstance(meta.get("copyright"), str) else "",
        ]
    )
    match = CC_RE.search(blob)
    return match.group(0) if match else ""


def dominant_kind(candidates: list[MediaCandidate]) -> str | None:
    """The kind that decides layout: video wins, then image, else nothing."""
    kinds = {c.kind for c in candidates}
    if "video" in kinds:
        return "video"
    if "image" in kinds:
        return "image"
    return None


__all__ = [
    "CC_RE",
    "IMAGE_EXTENSIONS",
    "MediaCandidate",
    "REFUSED_HOSTS",
    "REFUSAL_REASONS",
    "VIDEO_EXTENSIONS",
    "VIDEO_MIMES",
    "detect_licence_marker",
    "discover_from_feed",
    "discover_in_html",
    "dominant_kind",
    "extension_of",
    "kind_of",
]
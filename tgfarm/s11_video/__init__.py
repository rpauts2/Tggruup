"""S11 — video pipeline.

News feeds often attach footage. This module finds it, decides whether the farm
is allowed to use it, downloads it, and hands it to the publisher.

The licensing gate is the point of this module, and it is deliberately strict.

Allowed
    * The media is served from the **same domain as the article** — the publisher
      is offering it as part of their own reporting.
    * The article page carries an explicit **Creative Commons** or
      **public-domain** statement in its metadata or body.

Refused, always
    * YouTube, TikTok, Instagram, Vimeo, Dailymotion, Twitch and every other
      platform video — those URLs are not a syndication licence, and extracting
      them is both a ToS violation and copyright infringement.
    * Any host not on the same domain as the source article, with no CC marker.

The refusals are counted and logged rather than hidden, so the operator can see
exactly how much the guard is costing. Turning it off is one env var
(`VIDEO_ALLOW_UNKNOWN=1`) and is documented as the legal risk it is.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

from tgfarm.core.logging import get_logger
from tgfarm.core.media import (
    IMAGE_EXTENSIONS,
    REFUSAL_REASONS,
    REFUSED_HOSTS,
    VIDEO_EXTENSIONS,
    MediaCandidate,
    detect_licence_marker,
    discover_from_feed,
    discover_in_html,
)
from tgfarm.core.settings import get_settings

log = get_logger(__name__)

_VIDEO_URL_RE = re.compile(r"video|mp4|webm|m3u8", re.IGNORECASE)

# Re-exported so callers can treat "media parsing" and "media licensing" as one
# module while the pure half stays importable from anywhere (S1 needs it).
__all__ = [
    "IMAGE_EXTENSIONS",
    "MediaCandidate",
    "REFUSED_HOSTS",
    "REFUSAL_REASONS",
    "VIDEO_EXTENSIONS",
    "detect_licence_marker",
    "discover_from_feed",
    "discover_in_html",
    "download",
    "evaluate",
    "fetch_video_for",
    "fits_telegram",
    "guard_summary",
    "is_video",
    "mime_for",
]


# -------------------------------------------------------------------- gating
def evaluate(candidate: MediaCandidate, article_url: str, licence_marker: str) -> MediaCandidate:
    """Decide whether this file may be reused. Fills `allowed` and `refusal`."""
    settings = get_settings()
    candidate.article_url = article_url
    candidate.host = (urlparse(candidate.url).netloc or "").lower()
    article_host = (urlparse(article_url).netloc or "").lower()
    candidate.same_domain = _same_domain(candidate.host, article_host)
    candidate.license_hint = licence_marker

    path = Path(urlparse(candidate.url).path).suffix.lower()
    if path and path not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
        candidate.refusal = REFUSAL_REASONS["extension"]
        return candidate
    candidate.kind = "image" if path in IMAGE_EXTENSIONS else candidate.kind

    if any(candidate.host == h or candidate.host.endswith("." + h) for h in REFUSED_HOSTS):
        candidate.refusal = REFUSAL_REASONS["platform_video"]
        return candidate

    if licence_marker:
        candidate.allowed = True
        return candidate

    if candidate.same_domain:
        candidate.allowed = True
        return candidate

    if getattr(settings, "video_allow_unknown", False):
        candidate.allowed = True
        candidate.meta["warning"] = "VIDEO_ALLOW_UNKNOWN=1: лицензия не проверена"
        return candidate

    candidate.refusal = REFUSAL_REASONS["no_licence_marker"]
    return candidate


# Registry boundaries that own nothing themselves: evil.co.uk and good.co.uk
# share "co.uk" but are unrelated owners. A bounded subset, not the full public
# suffix list - an unlisted multi-label registry still compares by two labels.
_PUBLIC_SECOND_LEVEL = frozenset({
    "ac.uk", "co.uk", "gov.uk", "ltd.uk", "me.uk", "net.uk", "org.uk", "plc.uk",
    "asn.au", "com.au", "edu.au", "gov.au", "id.au", "net.au", "org.au",
    "ac.jp", "co.jp", "go.jp", "ne.jp", "or.jp", "co.kr", "ne.kr", "or.kr",
    "com.br", "net.br", "org.br", "com.cn", "gov.cn", "net.cn", "org.cn",
    "com.hk", "com.my", "com.ph", "com.sg", "com.tw", "net.tw", "org.tw",
    "co.id", "or.id", "co.in", "net.in", "org.in", "co.th", "or.th", "com.vn",
    "com.bd", "com.pk", "com.sa", "com.eg", "com.ng", "co.ke", "or.ke", "co.za",
    "org.za", "web.za", "co.il", "org.il", "com.tr", "com.ua", "com.ru", "com.pl",
    "com.mx", "com.ar", "com.co", "com.pe", "com.ve", "com.ec", "com.uy",
    "com.py", "com.bo", "com.do", "co.nz", "net.nz", "org.nz",
})


def _registrable_domain(host: str) -> str:
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    tail = ".".join(labels[-2:])
    if tail not in _PUBLIC_SECOND_LEVEL:
        return tail
    if len(labels) < 3:
        return ""
    return ".".join(labels[-3:])


def _same_domain(host: str, article_host: str) -> bool:
    if not host or not article_host:
        return False
    if host == article_host:
        return True
    host = host.lower().rstrip(".")
    article_host = article_host.lower().rstrip(".")
    if host == article_host:
        return True
    shared = _registrable_domain(host)
    return bool(shared) and shared == _registrable_domain(article_host)


def is_video(candidate: MediaCandidate) -> bool:
    if candidate.kind != "video":
        return False
    return candidate.mime.startswith("video/") or _VIDEO_URL_RE.search(candidate.url) is not None


# ----------------------------------------------------------------- downloading
async def download(candidate: MediaCandidate, timeout: float = 120.0) -> MediaCandidate:
    """Stream to disk with a hard byte cap."""
    settings = get_settings()
    import httpx

    limit_bytes = max(1, settings.video_max_mb) * 1024 * 1024

    digest = hashlib.sha1(candidate.url.encode()).hexdigest()[:16]
    suffix = Path(urlparse(candidate.url).path).suffix.lower() or ".mp4"
    target = settings.video_root() / f"{digest}{suffix}"
    candidate.local_path = str(target)

    headers = {"User-Agent": get_settings().user_agent}
    from tgfarm.core.net import UnsafeURLError, assert_public_http_url

    try:
        assert_public_http_url(candidate.url)
    except UnsafeURLError as exc:
        candidate.refusal = f"url refused: {exc}"
        candidate.local_path = None
        return candidate

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", candidate.url, headers=headers) as resp:
                if resp.status_code != 200:
                    candidate.refusal = f"HTTP {resp.status_code}"
                    candidate.local_path = None
                    return candidate

                length_header = resp.headers.get("content-length")
                if length_header and int(length_header) > limit_bytes:
                    candidate.refusal = REFUSAL_REASONS["too_large"]
                    candidate.local_path = None
                    return candidate

                written = 0
                with target.open("wb") as handle:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > limit_bytes:
                            handle.close()
                            target.unlink(missing_ok=True)
                            candidate.refusal = REFUSAL_REASONS["too_large"]
                            candidate.bytes = written
                            # The file is gone, so the path must not survive: the
                            # facade decides on `local_path`, and a dead path there
                            # reads as a usable video.
                            candidate.local_path = None
                            return candidate
                        handle.write(chunk)
                candidate.bytes = written
    except Exception as exc:  # noqa: BLE001 - download failure is not fatal
        target.unlink(missing_ok=True)
        candidate.local_path = None
        candidate.refusal = f"download failed: {type(exc).__name__}"
        log.warning("video download failed %s: %s", candidate.url[:80], exc)
        return candidate

    if candidate.bytes == 0:
        target.unlink(missing_ok=True)
        candidate.local_path = None
        candidate.refusal = "empty file"
    return candidate


def mime_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "video/mp4"


def fits_telegram(candidate: MediaCandidate) -> bool:
    """Bot API rejects uploads over 50 MB and caps dimensions."""
    return candidate.bytes > 0 and candidate.bytes <= 50 * 1024 * 1024


# ------------------------------------------------------------------- facade
async def _article_html(url: str) -> str:
    """Fetch an article page so og:video / <video> tags can be inspected.

    Only called when the feed carried no media at all — a feed enclosure is
    cheaper and already trusted, while this costs one request per candidate item.
    """
    settings = get_settings()
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=min(30.0, settings.http_timeout_s),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            return resp.text[:1_500_000]
    except Exception as exc:  # noqa: BLE001 - enrichment is optional
        log.debug("article fetch failed %s: %s", url[:80], exc)
        return ""


async def fetch_video_for(item) -> MediaCandidate | None:
    """Best-effort: return an allowed, downloaded video for one source item, or
    None. Every refusal path is logged so the operator can see the cost of the
    guard.

    `item` may be a pydantic `RawItem` or an ORM row; both carry `media_links`
    and `media_kind` once S1 records what the feed contained.
    """
    settings = get_settings()
    if not settings.video_enabled:
        return None

    media_links = list(getattr(item, "media_links", None) or [])
    candidates = discover_from_feed({"links": media_links, "summary": getattr(item, "text", "") or ""})

    # The feed may carry only a headline — then the page itself is the only place
    # media and the licence statement can be found.
    article_html = getattr(item, "html", "") or ""
    if not any(c.kind == "video" for c in candidates):
        article_html = article_html or await _article_html(item.url)
        candidates.extend(discover_in_html(article_html))

    licence = detect_licence_marker(article_html)

    best: MediaCandidate | None = None
    for candidate in candidates:
        if candidate.kind != "video":
            continue
        evaluate(candidate, item.url, licence)
        if not candidate.allowed:
            log.info("video refused (%s): %s", candidate.refusal, candidate.url[:90])
            continue
        if best is None:
            best = candidate

    if best is None:
        return None

    downloaded = await download(best)
    if not downloaded.local_path:
        log.info("video unusable (%s): %s", downloaded.refusal, best.url[:90])
        return None
    if not fits_telegram(downloaded):
        log.info("video too large for Bot API: %.1f MB", downloaded.bytes / 1e6)
        Path(downloaded.local_path).unlink(missing_ok=True)
        downloaded.local_path = None
        return None
    return downloaded


def guard_summary() -> dict:
    """What the licensing gate is doing, for the dashboard."""
    settings = get_settings()
    return {
        "enabled": settings.video_enabled,
        "refused_hosts": sorted(REFUSED_HOSTS),
        "allowed": [
            "media served from the article's own domain",
            "explicit Creative Commons / public-domain marker",
        ],
        "limits": {
            "max_mb": settings.video_max_mb,
            "max_duration_s": settings.video_max_duration_s,
            "telegram_bot_api_max_mb": 50,
        },
        "allow_unknown": settings.video_allow_unknown,
    }

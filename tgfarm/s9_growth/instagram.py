"""Official Instagram Login Content Publishing adapter for S9.

Publishes one JPEG image or one Reel per post. Meta fetches assets from signed
HTTPS URLs served by TgFarm. Uncertain publish outcomes must be reconciled.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate

log = get_logger(__name__)

API_ROOT = "https://graph.instagram.com/v25.0"
POLL_ATTEMPTS = 60
POLL_DELAY_SECONDS = 5


def _http_result(platform: str, exc: BaseException, operation: str) -> CrosspostResult:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        result_status = "failed"
    elif status in (408, 425) or (isinstance(status, int) and status >= 500):
        result_status = "unknown" if operation == "publish" else "failed"
    elif isinstance(status, int) and 400 <= status < 500:
        result_status = "permanent"
    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        result_status = "failed"
    else:
        result_status = "unknown" if operation == "publish" else "failed"
    safe_error = (
        f"Instagram {operation} returned HTTP {status}."
        if isinstance(status, int)
        else f"Instagram {operation} failed ({type(exc).__name__})."
    )
    retry_after = None
    if status == 429 and response is not None:
        raw = response.headers.get("Retry-After")
        try:
            retry_after = min(86400, max(0, int(raw))) if raw is not None else None
        except (TypeError, ValueError):
            retry_after = None
    log.warning("Instagram %s %s: %s", operation, result_status, redact_sensitive_text(exc, limit=180))
    return CrosspostResult(
        platform=platform,
        status=result_status,
        error=safe_error,
        retry_after_seconds=retry_after,
    )


def _ensure_jpeg(path: str) -> Path:
    """Convert generated PNG/WebP covers to a stable JPEG asset for Instagram."""
    source = Path(path)
    if not source.is_file():
        raise RuntimeError("Instagram media file is missing.")
    if source.suffix.lower() in {".jpg", ".jpeg"}:
        return source

    from PIL import Image, ImageOps

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    target = source.parent / "instagram_media" / f"{digest}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            temp = target.with_suffix(".jpg.tmp")
            image.save(temp, format="JPEG", quality=91, optimize=True, progressive=True)
            os.replace(temp, target)
    return target


def _registered_media_id(path: Path, post_public_id: str, kind: str) -> str:
    """Ensure the asset is addressable by a signed, short-lived public URL."""
    from sqlalchemy import select

    from tgfarm.db.models import Media, Post
    from tgfarm.db.session import session_scope

    path_text = str(path)
    with session_scope() as session:
        post = session.execute(
            select(Post).where(
                Post.public_id == post_public_id,
                Post.status == "published",
            )
        ).scalar_one_or_none()
        if post is None:
            raise RuntimeError("Instagram can only publish media attached to a published TgFarm post.")
        row_id = session.execute(
            select(Media.id)
            .where(Media.post_id == post.id, Media.path == path_text)
            .order_by(Media.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row_id is not None:
            return str(row_id)

        width = height = 0
        try:
            from PIL import Image
            with Image.open(path) as image:
                width, height = image.size
        except Exception:
            pass
        checksum = hashlib.sha1(path.read_bytes()).hexdigest()
        row = Media(
            post_id=post.id,
            kind=kind,
            path=path_text,
            width=width,
            height=height,
            sha=checksum,
            is_primary=False,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def _signed_media_url(path: str, post_public_id: str, kind: str) -> str:
    from tgfarm.instagram_oauth import make_signed_instagram_media_url

    media_id = _registered_media_id(Path(path), post_public_id, kind)
    url = make_signed_instagram_media_url(media_id)
    if not url:
        settings = get_settings()
        parsed = urlsplit(str(settings.public_base_url or "").strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError(
                "Для Instagram нужен публичный HTTPS-домен в TGFARM_PUBLIC_BASE_URL."
            )
        raise RuntimeError("Не удалось создать безопасную ссылку на медиа Instagram.")
    return url


class InstagramAdapter:
    """Publish one native image or Reel to an Instagram professional account."""

    platform = "instagram"
    max_caption = 2200

    def configured(self) -> bool:
        settings = get_settings()
        parsed = urlsplit(str(settings.public_base_url or "").strip())
        host = (parsed.hostname or "").lower()
        return bool(
            settings.instagram_access_token
            and settings.instagram_user_id
            and parsed.scheme == "https"
            and host
            and host not in {"localhost", "127.0.0.1", "::1"}
            and not host.endswith(".local")
        )

    async def _media_url(self, path: str, post_public_id: str, kind: str) -> str:
        return await asyncio.to_thread(_signed_media_url, path, post_public_id, kind)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(
                platform=self.platform,
                status="skipped",
                error="Instagram OAuth and TGFARM_PUBLIC_BASE_URL must be configured.",
            )

        meta = payload.meta or {}
        post_public_id = str(meta.get("post_public_id") or "").strip()
        video_path = str(payload.video_path or meta.get("video_path") or "").strip()
        image_path = str(payload.image_path or meta.get("image_path") or "").strip()
        if not post_public_id:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Instagram delivery is missing the source post identifier.",
            )
        if not video_path and not image_path:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Instagram requires a generated image or vertical video.",
            )

        try:
            if video_path:
                media_url = await self._media_url(video_path, post_public_id, "video")
                media_type = "REELS"
            else:
                jpeg_path = await asyncio.to_thread(_ensure_jpeg, image_path)
                media_url = await self._media_url(str(jpeg_path), post_public_id, "article_image")
                media_type = "IMAGE"
        except Exception as exc:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=redact_sensitive_text(exc, limit=260) or "Instagram media preparation failed.",
            )

        from tgfarm.instagram_oauth import get_valid_instagram_access_token

        try:
            token = await get_valid_instagram_access_token()
        except Exception as exc:
            log.warning("Instagram OAuth refresh failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status="failed",
                error="Instagram OAuth token could not be refreshed. Reconnect the account.",
            )

        user_id = str(get_settings().instagram_user_id).strip()
        caption = truncate(
            "\n\n".join(
                part for part in (str(payload.title or "").strip(), str(payload.text or "").strip())
                if part
            ),
            self.max_caption,
        )
        if not caption:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Instagram captions cannot be empty.",
            )

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=35.0, follow_redirects=False) as client:
            create_data = {"caption": caption}
            if media_type == "REELS":
                create_data.update({
                    "media_type": "REELS",
                    "video_url": media_url,
                    "share_to_feed": "true",
                })
            else:
                create_data["image_url"] = media_url

            try:
                created = await client.post(
                    f"{API_ROOT}/{user_id}/media",
                    data=create_data,
                    headers=headers,
                )
                created.raise_for_status()
                creation_id = str((created.json() or {}).get("id") or "")
            except Exception as exc:
                return _http_result(self.platform, exc, "container creation")

            if not creation_id:
                return CrosspostResult(
                    platform=self.platform,
                    status="unknown",
                    error="Instagram did not return a media-container ID; reconcile before retrying.",
                )

            ready = media_type == "IMAGE"
            if not ready:
                for attempt in range(POLL_ATTEMPTS):
                    try:
                        status_response = await client.get(
                            f"{API_ROOT}/{creation_id}",
                            params={"fields": "status_code,status"},
                            headers=headers,
                        )
                        status_response.raise_for_status()
                        status_payload = status_response.json() or {}
                        status_code = str(status_payload.get("status_code") or "").upper()
                    except Exception as exc:
                        return _http_result(self.platform, exc, "container status check")
                    if status_code in {"FINISHED", "PUBLISHED"}:
                        ready = True
                        break
                    if status_code in {"ERROR", "EXPIRED"}:
                        return CrosspostResult(
                            platform=self.platform,
                            status="permanent",
                            remote_id=creation_id,
                            error=f"Instagram media processing ended with {status_code}.",
                        )
                    if attempt + 1 < POLL_ATTEMPTS:
                        await asyncio.sleep(POLL_DELAY_SECONDS)

            if not ready:
                return CrosspostResult(
                    platform=self.platform,
                    status="unknown",
                    remote_id=creation_id,
                    error="Instagram Reel processing was not confirmed. Check the account before retrying.",
                )

            try:
                published = await client.post(
                    f"{API_ROOT}/{user_id}/media_publish",
                    data={"creation_id": creation_id},
                    headers=headers,
                )
                published.raise_for_status()
                media_id = str((published.json() or {}).get("id") or "")
            except Exception as exc:
                return _http_result(self.platform, exc, "publish")

            if not media_id:
                return CrosspostResult(
                    platform=self.platform,
                    status="unknown",
                    remote_id=creation_id,
                    error="Instagram accepted the publish request without a verifiable media ID.",
                )

            permalink = None
            try:
                lookup = await client.get(
                    f"{API_ROOT}/{media_id}",
                    params={"fields": "id,permalink"},
                    headers=headers,
                )
                lookup.raise_for_status()
                permalink = str((lookup.json() or {}).get("permalink") or "") or None
            except Exception as exc:
                log.warning("Instagram permalink lookup failed (%s)", type(exc).__name__)

        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=media_id,
            url=permalink,
        )

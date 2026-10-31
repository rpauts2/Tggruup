"""Facebook Pages Graph API publisher for text, image and video posts."""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate

log = get_logger(__name__)

GRAPH_ROOT = "https://graph.facebook.com/v26.0"
POLL_ATTEMPTS = 60
POLL_DELAY_SECONDS = 5


def _error_result(exc: BaseException, operation: str) -> CrosspostResult:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        state = "failed"
    elif status in (408, 425) or (isinstance(status, int) and status >= 500):
        state = "unknown" if operation in {"feed", "photo", "video"} else "failed"
    elif isinstance(status, int) and 400 <= status < 500:
        state = "permanent"
    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        state = "failed"
    else:
        state = "unknown" if operation in {"feed", "photo", "video"} else "failed"
    retry_after = None
    if status == 429 and response is not None:
        raw = response.headers.get("Retry-After")
        try:
            retry_after = min(86400, max(0, int(raw))) if raw is not None else None
        except (TypeError, ValueError):
            retry_after = None
    log.warning("Facebook Page %s %s: %s", operation, state, redact_sensitive_text(exc, limit=180))
    return CrosspostResult(
        platform="facebook",
        status=state,
        error=f"Facebook {operation} returned HTTP {status}." if status else f"Facebook {operation} failed ({type(exc).__name__}).",
        retry_after_seconds=retry_after,
    )


def _page_credentials() -> tuple[str, str]:
    settings = get_settings()
    return (
        str(settings.facebook_page_id or "").strip(),
        str(settings.facebook_page_access_token or "").strip(),
    )


class FacebookPageAdapter:
    """Publish to an owner-managed Facebook Page, never to a personal profile."""

    platform = "facebook"
    max_message = 60_000

    def configured(self) -> bool:
        page_id, token = _page_credentials()
        return bool(page_id.isdigit() and token)

    async def _signed_media_url(self, path: str, post_public_id: str, kind: str) -> str:
        from tgfarm.instagram_oauth import make_signed_instagram_media_url
        from tgfarm.s9_growth.instagram import _registered_media_id

        media_id = await asyncio.to_thread(_registered_media_id, Path(path), post_public_id, kind)
        url = make_signed_instagram_media_url(media_id)
        if not url:
            raise RuntimeError("Facebook media needs a public HTTPS origin in TGFARM_PUBLIC_BASE_URL.")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("Facebook media URL must use HTTPS.")
        return url

    async def _prepare_image_url(self, path: str, post_public_id: str) -> str:
        from tgfarm.s9_growth.instagram import _ensure_jpeg

        jpeg = await asyncio.to_thread(_ensure_jpeg, path)
        return await self._signed_media_url(str(jpeg), post_public_id, "article_image")

    async def _prepare_video_url(self, path: str, post_public_id: str) -> str:
        return await self._signed_media_url(path, post_public_id, "video")

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        page_id, _saved_token = _page_credentials()
        meta = payload.meta or {}
        post_public_id = str(meta.get("post_public_id") or "").strip()
        title = str(payload.title or "").strip()
        body = str(payload.text or "").strip()
        message = truncate("\n\n".join(part for part in (title, body) if part), self.max_message)
        image_path = str(payload.image_path or meta.get("image_path") or "").strip()
        video_path = str(payload.video_path or meta.get("video_path") or "").strip()
        if not message and not payload.link and not image_path and not video_path:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Facebook Page posts need text, a link, a photo or a video.",
            )

        from tgfarm.facebook_oauth import get_valid_facebook_page_token

        try:
            token = await get_valid_facebook_page_token()
        except Exception as exc:
            log.warning("Facebook Page token refresh failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status="failed",
                error="Facebook Page access token is no longer valid. Reconnect the Page.",
            )

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
            if video_path:
                if not post_public_id:
                    return CrosspostResult(
                        platform=self.platform,
                        status="permanent",
                        error="Facebook video delivery is missing the source post identifier.",
                    )
                try:
                    video_url = await self._prepare_video_url(video_path, post_public_id)
                except Exception as exc:
                    return CrosspostResult(
                        platform=self.platform,
                        status="permanent",
                        error=redact_sensitive_text(exc, limit=260) or "Facebook video preparation failed.",
                    )
                try:
                    response = await client.post(
                        f"{GRAPH_ROOT}/{page_id}/videos",
                        headers=headers,
                        data={
                            "file_url": video_url,
                            "title": truncate(title or "Видео", 1000),
                            "description": truncate(body, 5000),
                            "published": "true",
                        },
                        timeout=180.0,
                    )
                    response.raise_for_status()
                    result = response.json() or {}
                    video_id = str(result.get("id") or "")
                    if not video_id:
                        return CrosspostResult(
                            platform=self.platform,
                            status="unknown",
                            error="Facebook accepted the video upload without returning a video ID.",
                        )
                except Exception as exc:
                    return _error_result(exc, "video")

                ready = False
                for attempt in range(POLL_ATTEMPTS):
                    try:
                        status_response = await client.get(
                            f"{GRAPH_ROOT}/{video_id}",
                            headers=headers,
                            params={"fields": "status"},
                        )
                        status_response.raise_for_status()
                        status_payload = status_response.json() or {}
                        status_obj = status_payload.get("status") or {}
                        video_status = str(status_obj.get("video_status") or "").lower()
                        if video_status == "ready":
                            ready = True
                            break
                        if video_status in {"error", "expired", "upload_failed"}:
                            return CrosspostResult(
                                platform=self.platform,
                                status="permanent",
                                remote_id=video_id,
                                error=f"Facebook video processing ended with status {video_status}.",
                            )
                    except Exception as exc:
                        # Video creation has already returned its ID. Do not create another upload.
                        return CrosspostResult(
                            platform=self.platform,
                            status="unknown",
                            remote_id=video_id,
                            error=f"Facebook video status could not be confirmed ({type(exc).__name__}). Check the Page before retrying.",
                        )
                    if attempt + 1 < POLL_ATTEMPTS:
                        await asyncio.sleep(POLL_DELAY_SECONDS)
                if not ready:
                    return CrosspostResult(
                        platform=self.platform,
                        status="unknown",
                        remote_id=video_id,
                        error="Facebook video processing is not confirmed yet. Check the Page before retrying.",
                    )
                return CrosspostResult(
                    platform=self.platform,
                    status="posted",
                    remote_id=video_id,
                    url=f"https://www.facebook.com/{page_id}/videos/{video_id}/",
                )

            if image_path:
                if not post_public_id:
                    return CrosspostResult(
                        platform=self.platform,
                        status="permanent",
                        error="Facebook image delivery is missing the source post identifier.",
                    )
                try:
                    image_url = await self._prepare_image_url(image_path, post_public_id)
                except Exception as exc:
                    return CrosspostResult(
                        platform=self.platform,
                        status="permanent",
                        error=redact_sensitive_text(exc, limit=260) or "Facebook image preparation failed.",
                    )
                try:
                    response = await client.post(
                        f"{GRAPH_ROOT}/{page_id}/photos",
                        headers=headers,
                        data={
                            "url": image_url,
                            "caption": truncate(message, 1000),
                            "published": "true",
                        },
                    )
                    response.raise_for_status()
                    result = response.json() or {}
                except Exception as exc:
                    return _error_result(exc, "photo")
                photo_id = str(result.get("id") or "")
                post_id = str(result.get("post_id") or "")
                remote_id = post_id or photo_id
                if not remote_id:
                    return CrosspostResult(
                        platform=self.platform,
                        status="unknown",
                        error="Facebook accepted the photo request without returning a post ID.",
                    )
                return CrosspostResult(
                    platform=self.platform,
                    status="posted",
                    remote_id=remote_id,
                    url=f"https://www.facebook.com/{remote_id}" if "_" in remote_id else None,
                )

            data = {"message": message}
            if payload.link:
                data["link"] = str(payload.link)
            try:
                response = await client.post(
                    f"{GRAPH_ROOT}/{page_id}/feed",
                    headers=headers,
                    data=data,
                )
                response.raise_for_status()
                result = response.json() or {}
            except Exception as exc:
                return _error_result(exc, "feed")
        post_id = str(result.get("id") or "")
        if not post_id:
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Facebook accepted the feed post without returning an ID.",
            )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=post_id,
            url=f"https://www.facebook.com/{post_id}",
        )

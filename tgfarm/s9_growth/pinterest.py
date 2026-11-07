"""Pinterest API v5 image and video Pin publisher for S9."""
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
API_ROOT = "https://api.pinterest.com/v5"
POLL_ATTEMPTS = 60
POLL_DELAY_SECONDS = 5
MAX_VIDEO_MB = 100


def _error_result(exc: BaseException, operation: str) -> CrosspostResult:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        state = "failed"
    elif status in (408, 425) or (isinstance(status, int) and status >= 500):
        state = "unknown" if operation == "create_pin" else "failed"
    elif isinstance(status, int) and 400 <= status < 500:
        state = "permanent"
    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        state = "failed"
    else:
        state = "unknown" if operation == "create_pin" else "failed"
    # Keep adapter behavior aligned with the shared S9 Retry-After parser.
    from tgfarm.s9_growth.adapters import crosspost_retry_after_seconds

    retry_after = crosspost_retry_after_seconds(exc)
    log.warning("Pinterest %s %s: %s", operation, state, redact_sensitive_text(exc, limit=180))
    return CrosspostResult(
        platform="pinterest",
        status=state,
        error=f"Pinterest {operation} returned HTTP {status}." if status else f"Pinterest {operation} failed ({type(exc).__name__}).",
        retry_after_seconds=retry_after,
    )


class PinterestAdapter:
    """Create one native image or video Pin on a board selected by the owner."""

    platform = "pinterest"
    max_title = 100
    max_body = 500

    def configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.pinterest_app_id
            and settings.pinterest_app_secret
            and settings.pinterest_access_token
            and settings.pinterest_refresh_token
            and settings.pinterest_board_id
        )

    async def _signed_cover(self, image_path: str, post_public_id: str) -> str:
        from tgfarm.s9_growth.instagram import _ensure_jpeg, _signed_media_url

        jpeg_path = await asyncio.to_thread(_ensure_jpeg, image_path)
        return await asyncio.to_thread(
            _signed_media_url, str(jpeg_path), post_public_id, "article_image"
        )

    async def _upload_video(self, client: httpx.AsyncClient, token: str, video_path: str) -> str:
        path = Path(video_path)
        if not path.is_file():
            raise RuntimeError("Pinterest video file is missing.")
        settings = get_settings()
        try:
            max_mb = min(MAX_VIDEO_MB, max(1, int(settings.video_max_mb or MAX_VIDEO_MB)))
        except (TypeError, ValueError):
            max_mb = MAX_VIDEO_MB
        size = path.stat().st_size
        if size <= 0 or size > max_mb * 1024 * 1024:
            raise RuntimeError(f"Pinterest video must be between 1 byte and {max_mb} MB.")

        response = await client.post(
            f"{API_ROOT}/media",
            headers={"Authorization": f"Bearer {token}"},
            json={"media_type": "video"},
        )
        response.raise_for_status()
        payload = response.json() or {}
        media_id = str(payload.get("media_id") or "")
        upload_url = str(payload.get("upload_url") or "")
        parameters = payload.get("upload_parameters") or {}
        parsed = urlsplit(upload_url)
        if (
            not media_id
            or parsed.scheme != "https"
            or parsed.hostname != "pinterest-media-upload.s3-accelerate.amazonaws.com"
            or not isinstance(parameters, dict)
            or not parameters
        ):
            raise RuntimeError("Pinterest returned an invalid video upload session.")

        form = {str(key): str(value) for key, value in parameters.items()}
        try:
            with path.open("rb") as handle:
                uploaded = await client.post(
                    upload_url,
                    data=form,
                    files={"file": (path.name, handle, "video/mp4")},
                    timeout=180.0,
                    follow_redirects=False,
                )
            uploaded.raise_for_status()
        except Exception as exc:
            raise RuntimeError("Pinterest rejected the video upload. No Pin was created.") from exc

        ready = False
        for attempt in range(POLL_ATTEMPTS):
            status_response = await client.get(
                f"{API_ROOT}/media/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            status_response.raise_for_status()
            status_payload = status_response.json() or {}
            state = str(status_payload.get("status") or "").lower()
            if state == "succeeded":
                ready = True
                break
            if state in {"failed", "error", "expired"}:
                raise RuntimeError(f"Pinterest video processing ended with status {state}.")
            if attempt + 1 < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_DELAY_SECONDS)
        if not ready:
            raise RuntimeError("Pinterest video upload was accepted but processing is not yet complete.")
        return media_id

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        image_path = str(payload.image_path or (payload.meta or {}).get("image_path") or "").strip()
        video_path = str(payload.video_path or (payload.meta or {}).get("video_path") or "").strip()
        post_public_id = str((payload.meta or {}).get("post_public_id") or "").strip()
        if not image_path or not post_public_id:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Pinterest Pins require a cover image and source post identifier.",
            )

        try:
            cover_url = await self._signed_cover(image_path, post_public_id)
        except Exception as exc:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=redact_sensitive_text(exc, limit=260) or "Pinterest cover preparation failed.",
            )

        from tgfarm.pinterest_oauth import get_valid_pinterest_access_token

        try:
            token = await get_valid_pinterest_access_token()
        except Exception as exc:
            log.warning("Pinterest OAuth refresh failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status="failed",
                error="Pinterest OAuth token could not be refreshed. Reconnect the account.",
            )

        settings = get_settings()
        title = truncate(str(payload.title or "").strip(), self.max_title)
        description = truncate(str(payload.text or "").strip(), self.max_body)
        if not title and not description:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Pinterest Pins need a title or description.",
            )

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            try:
                if video_path:
                    video_id = await self._upload_video(client, token, video_path)
                    media_source = {
                        "source_type": "video_id",
                        "cover_image_url": cover_url,
                        "media_id": video_id,
                    }
                else:
                    media_source = {
                        "source_type": "image_url",
                        "url": cover_url,
                        "is_standard": True,
                    }
            except httpx.HTTPStatusError as exc:
                return _error_result(exc, "media_upload")
            except Exception as exc:
                log.warning("Pinterest media upload failed (%s)", type(exc).__name__)
                return CrosspostResult(
                    platform=self.platform,
                    status="failed",
                    error=redact_sensitive_text(exc, limit=260) or "Pinterest media upload failed.",
                )

            body = {
                "board_id": str(settings.pinterest_board_id).strip(),
                "title": title or description[:self.max_title],
                "description": description,
                "media_source": media_source,
            }
            if payload.link:
                body["link"] = str(payload.link)
            try:
                response = await client.post(
                    f"{API_ROOT}/pins",
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
                result = response.json() or {}
            except Exception as exc:
                return _error_result(exc, "create_pin")

        pin_id = str(result.get("id") or "") if isinstance(result, dict) else ""
        if not pin_id:
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Pinterest accepted the create request without returning a Pin ID; reconcile before retrying.",
            )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=pin_id,
            url=f"https://www.pinterest.com/pin/{pin_id}/",
        )

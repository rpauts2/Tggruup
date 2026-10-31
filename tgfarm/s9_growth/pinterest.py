"""Pinterest API v5 image Pin publisher for S9."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx

from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate

log = get_logger(__name__)
API_ROOT = "https://api.pinterest.com/v5"


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
    retry_after = None
    if status == 429 and response is not None:
        raw = response.headers.get("Retry-After")
        try:
            retry_after = min(86400, max(0, int(raw))) if raw is not None else None
        except (TypeError, ValueError):
            retry_after = None
    log.warning("Pinterest %s %s: %s", operation, state, redact_sensitive_text(exc, limit=180))
    return CrosspostResult(
        platform="pinterest",
        status=state,
        error=f"Pinterest {operation} returned HTTP {status}." if status else f"Pinterest {operation} failed ({type(exc).__name__}).",
        retry_after_seconds=retry_after,
    )


class PinterestAdapter:
    """Create one native image Pin on a board selected by the owner."""

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

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        image_path = str(payload.image_path or (payload.meta or {}).get("image_path") or "").strip()
        post_public_id = str((payload.meta or {}).get("post_public_id") or "").strip()
        if not image_path or not post_public_id:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Pinterest image Pins require a cover image and source post identifier.",
            )

        try:
            from tgfarm.s9_growth.instagram import _ensure_jpeg, _signed_media_url
            jpeg_path = await __import__("asyncio").to_thread(_ensure_jpeg, image_path)
            image_url = await __import__("asyncio").to_thread(
                _signed_media_url, str(jpeg_path), post_public_id, "article_image"
            )
        except Exception as exc:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=redact_sensitive_text(exc, limit=260) or "Pinterest media preparation failed.",
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
            return CrosspostResult(platform=self.platform, status="permanent", error="Pinterest Pin needs a title or description.")

        body = {
            "board_id": str(settings.pinterest_board_id).strip(),
            "title": title or description[:self.max_title],
            "description": description,
            "media_source": {
                "source_type": "image_url",
                "url": image_url,
                "is_standard": True,
            },
        }
        if payload.link:
            body["link"] = str(payload.link)
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                response = await client.post(
                    f"{API_ROOT}/pins",
                    headers={"Authorization": f"Bearer {token}"},
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

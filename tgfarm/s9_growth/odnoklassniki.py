"""Odnoklassniki REST API group publisher with signed requests and media upload."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate

log = get_logger(__name__)

API_URL = "https://api.ok.ru/fb.do"
MAX_TEXT = 5000
MAX_VIDEO_BYTES = 1024 * 1024 * 1024


class OKAPIError(RuntimeError):
    """A sanitized rejection from the Odnoklassniki REST API."""

    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = bool(ambiguous)


def _credentials() -> tuple[str, str, str, str, str]:
    settings = get_settings()
    return (
        str(getattr(settings, "ok_application_key", "") or "").strip(),
        str(getattr(settings, "ok_application_secret", "") or "").strip(),
        str(getattr(settings, "ok_access_token", "") or "").strip(),
        str(getattr(settings, "ok_session_secret_key", "") or "").strip(),
        str(getattr(settings, "ok_group_id", "") or "").strip(),
    )


def ok_session_secret(access_token: str, application_secret: str, session_secret: str = "") -> str:
    """Return the API signing secret, preferring the session secret from the token pair."""
    supplied = str(session_secret or "").strip()
    if supplied:
        return supplied.lower()
    return hashlib.md5((str(access_token) + str(application_secret)).encode("utf-8")).hexdigest()


def build_ok_signature(params: dict[str, str], secret: str) -> str:
    """Sign OK API params as sorted key=value pairs plus the session secret."""
    excluded = {"access_token", "session_key", "sig"}
    value = "".join(
        f"{key}={params[key]}"
        for key in sorted(params)
        if key not in excluded
    ) + str(secret)
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def build_ok_request_params(
    method: str,
    params: dict[str, str] | None = None,
    *,
    application_key: str = "",
    application_secret: str = "",
    access_token: str = "",
    session_secret_key: str = "",
) -> dict[str, str]:
    """Build an authenticated request; token is intentionally excluded from signature."""
    if not application_key or not application_secret or not access_token:
        raise ValueError("OK API credentials are incomplete.")
    values = {
        "application_key": application_key,
        "access_token": access_token,
        "format": "json",
        "method": method,
    }
    if params:
        for key, value in params.items():
            if key in {"application_key", "access_token", "session_key", "sig"}:
                raise ValueError(f"Reserved OK API parameter: {key}")
            values[str(key)] = str(value)
    secret = ok_session_secret(access_token, application_secret, session_secret_key)
    values["sig"] = build_ok_signature(values, secret)
    return values


def _parse_ok_response(response: httpx.Response, method: str):
    if not response.content:
        payload = None
    else:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OKAPIError(f"OK {method} returned a non-JSON response (HTTP {response.status_code}).") from exc
    if response.status_code >= 400:
        ambiguous = method == "mediatopic.post" and (
            response.status_code in {408, 425} or response.status_code >= 500
        )
        raise OKAPIError(
            f"OK {method} returned HTTP {response.status_code}.",
            ambiguous=ambiguous,
        )
    if isinstance(payload, dict) and (payload.get("error_code") or payload.get("error_msg")):
        error_code = str(payload.get("error_code") or "API_ERROR")
        error_msg = str(payload.get("error_msg") or "Request rejected")
        raise OKAPIError(f"OK {method} rejected the request ({error_code}: {error_msg[:160]}).")
    return payload


async def ok_api_call(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, str] | None = None,
):
    app_key, app_secret, token, session_secret, _group = _credentials()
    body = build_ok_request_params(
        method,
        params,
        application_key=app_key,
        application_secret=app_secret,
        access_token=token,
        session_secret_key=session_secret,
    )
    try:
        response = await client.post(API_URL, data=body, follow_redirects=False)
    except httpx.RequestError as exc:
        definitely_before_send = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        raise OKAPIError(
            f"OK {method} network request failed ({type(exc).__name__}).",
            ambiguous=(method == "mediatopic.post" and not definitely_before_send),
        ) from exc
    return _parse_ok_response(response, method)


async def ok_has_permission(client: httpx.AsyncClient, permission: str) -> bool:
    result = await ok_api_call(client, "users.hasAppPermission", {"ext_perm": permission})
    if isinstance(result, dict):
        result = result.get("result", result.get("value", False))
    return result is True or str(result).lower() == "true"


def _upload_url_allowed(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    return bool(
        parsed.scheme == "https"
        and host
        and (host == "ok.ru" or host.endswith(".ok.ru") or host == "odnoklassniki.ru" or host.endswith(".odnoklassniki.ru"))
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def _attachment(
    text: str,
    *,
    photo_tokens: list[str] | None = None,
    video_id: str | None = None,
    link: str | None = None,
) -> str:
    media: list[dict] = []
    if text.strip():
        media.append({"type": "text", "text": text.strip()})
    if photo_tokens:
        media.append({"type": "photo", "list": [{"id": token} for token in photo_tokens]})
    if video_id:
        media.append({"type": "movie", "list": [{"id": str(video_id)}]})
    if link:
        media.append({"type": "link", "url": link})
    if not media:
        raise ValueError("OK group post has no content.")
    return json.dumps({"media": media}, ensure_ascii=False, separators=(",", ":"))


class OdnoklassnikiAdapter:
    """Publish a native group media topic, optionally including a photo or video."""

    platform = "odnoklassniki"
    max_title = 200
    max_body = MAX_TEXT

    def configured(self) -> bool:
        key, secret, token, _session_secret, group = _credentials()
        return bool(key and secret and token and group)

    async def _has_permission(self, client: httpx.AsyncClient, permission: str) -> bool:
        result = await ok_api_call(client, "users.hasAppPermission", {"ext_perm": permission})
        if isinstance(result, dict):
            result = result.get("result", result.get("value", False))
        return result is True or str(result).lower() == "true"

    async def _upload_photo(
        self,
        client: httpx.AsyncClient,
        path_value: str,
        group_id: str,
    ) -> list[str]:
        from tgfarm.s9_growth.instagram import _ensure_jpeg

        path = await asyncio.to_thread(_ensure_jpeg, path_value)
        if not path.is_file():
            raise OKAPIError("OK cover image is missing.")
        if not await self._has_permission(client, "PHOTO_CONTENT"):
            raise OKAPIError("OK application does not have PHOTO_CONTENT permission.")

        payload = await ok_api_call(
            client,
            "photosV2.getUploadUrl",
            {"gid": group_id, "count": "1"},
        )
        upload_url = str(payload.get("upload_url") or "") if isinstance(payload, dict) else ""
        if not _upload_url_allowed(upload_url):
            raise OKAPIError("OK returned an untrusted photo upload URL.")
        try:
            with path.open("rb") as handle:
                uploaded = await client.post(
                    upload_url,
                    files={"pic1": (path.name, handle, "image/jpeg")},
                    timeout=90.0,
                    follow_redirects=False,
                )
            uploaded.raise_for_status()
            result = uploaded.json()
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                raise OKAPIError(f"OK photo upload failed with HTTP {status}.") from exc
            raise OKAPIError(f"OK photo upload failed ({type(exc).__name__}).") from exc
        photos = result.get("photos") if isinstance(result, dict) else None
        if not isinstance(photos, dict):
            raise OKAPIError("OK did not return photo upload tokens.")
        tokens = [
            str(item.get("token") or "")
            for item in photos.values()
            if isinstance(item, dict) and item.get("token")
        ]
        if not tokens:
            raise OKAPIError("OK photo upload returned no usable token.")
        # Group media topics consume the upload token directly; photosV2.commit
        # is intentionally not called for this upload mode.
        return tokens

    async def _upload_video(
        self,
        client: httpx.AsyncClient,
        path_value: str,
        group_id: str,
        title: str,
        description: str,
    ) -> str:
        path = Path(path_value)
        if not path.is_file():
            raise OKAPIError("OK video file is missing.")
        size = path.stat().st_size
        if size < 16 * 1024 or size > MAX_VIDEO_BYTES:
            raise OKAPIError("OK video must be at least 16 KB and no larger than 1 GB.")
        if not await self._has_permission(client, "VIDEO_CONTENT"):
            raise OKAPIError("OK application does not have VIDEO_CONTENT permission.")

        init = await ok_api_call(
            client,
            "video.getUploadUrl",
            {
                "gid": group_id,
                "file_name": path.name,
                "file_size": str(size),
                "attachment_type": "MOVIE",
                "post_form": "true",
            },
        )
        upload_url = str(init.get("upload_url") or "") if isinstance(init, dict) else ""
        video_id = str(init.get("video_id") or "") if isinstance(init, dict) else ""
        if not video_id or not _upload_url_allowed(upload_url):
            raise OKAPIError("OK returned an invalid video upload session.")
        try:
            with path.open("rb") as handle:
                response = await client.post(
                    upload_url,
                    files={"data": (path.name, handle, "video/mp4")},
                    timeout=180.0,
                    follow_redirects=False,
                )
            response.raise_for_status()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise OKAPIError(
                f"OK video upload failed{f' with HTTP {status}' if status else ''}."
            ) from exc

        # video.update accepts the upload agreement and makes the video public.
        await ok_api_call(
            client,
            "video.update",
            {
                "vid": video_id,
                "title": truncate(title, 1000),
                "description": truncate(description, 5000),
                "privacy": "PUBLIC",
            },
        )
        return video_id

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        _key, _secret, _token, _session_secret, group_id = _credentials()

        title = truncate(str(payload.title or "").strip(), self.max_title)
        body = truncate(
            "\n\n".join(
                part for part in (title, str(payload.text or "").strip())
                if part
            ),
            self.max_body,
        )
        link = str(payload.link or "").strip()
        if not body and not link and not payload.image_path and not payload.video_path:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="OK group posts need text, a link, a photo or a video.",
            )
        if link and not (link.startswith("https://") or link.startswith("http://")):
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="OK source links must use http or https.",
            )

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                if not await self._has_permission(client, "GROUP_CONTENT"):
                    raise OKAPIError("OK application does not have GROUP_CONTENT permission for this group.")
                photo_tokens = []
                video_id = None
                if payload.video_path:
                    video_id = await self._upload_video(
                        client,
                        payload.video_path,
                        group_id,
                        title or "Видео",
                        str(payload.text or ""),
                    )
                elif payload.image_path:
                    photo_tokens = await self._upload_photo(client, payload.image_path, group_id)

                attachment = _attachment(
                    body,
                    photo_tokens=photo_tokens,
                    video_id=video_id,
                    link=link or None,
                )
                result = await ok_api_call(
                    client,
                    "mediatopic.post",
                    {
                        "gid": group_id,
                        "type": "GROUP_THEME",
                        "onBehalfOfGroup": "true",
                        "attachment": attachment,
                    },
                )
        except OKAPIError as exc:
            message = str(exc)
            if exc.ambiguous:
                status = "unknown"
            elif "permission" in message.lower() or "invalid" in message.lower():
                status = "permanent"
            else:
                status = "failed"
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=redact_sensitive_text(message, limit=260),
            )
        except Exception as exc:
            log.warning("OK publish failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error=f"OK publication outcome is unclear ({type(exc).__name__}); check the group before retrying.",
            )

        topic_id = str(result or "").strip('"') if result is not None else ""
        if isinstance(result, dict):
            topic_id = str(result.get("id") or result.get("topic_id") or "")
        if not topic_id:
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="OK returned no media topic ID; check the group before retrying.",
            )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=topic_id,
            url=f"https://ok.ru/group/{group_id}/topic/{topic_id}",
        )

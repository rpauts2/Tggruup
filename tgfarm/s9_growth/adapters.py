"""S9 — crossposting.

One Telegram post becomes one native-format post per external platform. Native
formatting matters: a Telegram post pasted verbatim into VK reads as spam and
gets shadowed. Each adapter therefore rewrites length, hashtag style and link
placement for its own surface rather than forwarding a single blob.

Every adapter:
  * self-disables when its credentials are absent (returns `skipped`)
  * runs as `dry_run` unless TGFARM_MODE=live
  * logs the outcome to `crosspost_logs` so retries are idempotent
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tgfarm.core.errors import AdapterNotConfigured
from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate

log = get_logger(__name__)


class Adapter(Protocol):
    platform: str
    max_title: int
    max_body: int

    def configured(self) -> bool: ...

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult: ...


# ------------------------------------------------------------------- helpers
def _first_source_url(sources: list[dict]) -> str | None:
    return sources[0].get("url") if sources else None


def _clip(payload: CrosspostPayload, title_limit: int, body_limit: int) -> CrosspostPayload:
    """Re-clip a payload to a platform's limits, keeping the CTA last so it
    survives truncation."""
    cta = payload.meta.get("cta")
    body = payload.text
    if cta:
        body = f"{body}\n\n{cta}".strip()
    payload.text = truncate(body, body_limit)
    payload.title = truncate(payload.title, title_limit)
    return payload


# ---------------------------------------------------------------------- MAX
class MaxAdapter:
    """MAX (бывший VK Messenger / новая суперприложенческая платформа).

    MAX's open API surface changes faster than the other three; this adapter
    targets the channel-post endpoint and is designed to degrade to `skipped`
    rather than fail a batch when the schema shifts.
    """

    platform = "max"
    max_title = 128
    max_body = 4000

    def _credentials(self) -> tuple[str, str]:
        settings = get_settings()
        return settings.max_access_token, settings.max_channel_id

    def configured(self) -> bool:
        return all(self._credentials())

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        token, channel = self._credentials()
        if not (token and channel):
            return CrosspostResult(platform=self.platform, status="skipped")
        payload = _clip(payload, self.max_title, self.max_body)
        import httpx

        url = f"https://botapi.max.ru/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": channel,
                        "text": f"{payload.title}\n\n{payload.text}",
                        "link_preview_options": {"is_disabled": False},
                    },
                )
                resp.raise_for_status()
            message_id = str(resp.json().get("message_id", ""))
            return CrosspostResult(platform=self.platform, status="posted", remote_id=message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("max crosspost failed: %s", exc)
            return CrosspostResult(platform=self.platform, status="failed", error=str(exc)[:300])


# ----------------------------------------------------------------------- VK
class VkAdapter:
    platform = "vk"
    max_title = 128
    max_body = 4000

    def _credentials(self) -> tuple[str, str]:
        settings = get_settings()
        return settings.vk_access_token, settings.vk_group_id

    def configured(self) -> bool:
        return all(self._credentials())

    async def _upload_wall_photo(self, image_path: str) -> str | None:
        """Upload a local image and return the `photo{owner}_{id}` attachment.

        Three official calls: an upload server, the multipart upload itself,
        and the save that makes the photo addressable. Saving does not publish
        anything, so a failed upload never leaves a half-made post behind.
        """
        import httpx

        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return None
        token, group = self._credentials()
        group_id = str(group or "").lstrip("-")
        async with httpx.AsyncClient(timeout=30.0) as client:
            server = (
                await client.get(
                    "https://api.vk.com/method/photos.getWallUploadServer",
                    params={"access_token": token, "group_id": group_id, "v": "5.199"},
                )
            ).json().get("response") or {}
            upload_url = server.get("upload_url")
            if not upload_url:
                log.warning("vk photo upload: no upload_url")
                return None
            with path.open("rb") as handle:
                uploaded = (
                    await client.post(upload_url, files={"photo": (path.name, handle)})
                ).json()
            saved = (
                await client.post(
                    "https://api.vk.com/method/photos.saveWallPhoto",
                    data={
                        "access_token": token,
                        "group_id": group_id,
                        "photo": uploaded.get("photo", ""),
                        "server": uploaded.get("server", ""),
                        "hash": uploaded.get("hash", ""),
                        "v": "5.199",
                    },
                )
            ).json()
        items = saved.get("response") or []
        if not items:
            log.warning("vk photo upload: save returned nothing: %s", str(saved)[:160])
            return None
        first = items[0]
        return f"photo{first.get('owner_id')}_{first.get('id')}"

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        token, group = self._credentials()
        if not (token and group):
            return CrosspostResult(platform=self.platform, status="skipped")
        payload = _clip(payload, self.max_title, self.max_body)
        import httpx

        attachment = None
        image_path = (payload.meta or {}).get("image_path")
        if image_path:
            try:
                attachment = await self._upload_wall_photo(str(image_path))
            except Exception as exc:  # noqa: BLE001 - a cover is optional
                log.warning("vk photo upload failed: %s", exc)
        try:
            data = {
                "access_token": token,
                "owner_id": group,
                "message": f"{payload.title}\n\n{payload.text}",
                "publish_date": 0,
                "from_group": 1,
                "v": "5.199",
            }
            if attachment:
                data["attachments"] = attachment
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.vk.com/method/wall.post",
                    data=data,
                )
                resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                return CrosspostResult(
                    platform=self.platform, status="failed", error=str(body["error"])[:300]
                )
            post_id = str(body.get("response", {}).get("post_id", ""))
            return CrosspostResult(
                platform=self.platform,
                status="posted",
                remote_id=post_id,
                url=f"https://vk.com/wall{group}_{post_id}" if post_id else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("vk crosspost failed: %s", exc)
            return CrosspostResult(platform=self.platform, status="failed", error=str(exc)[:300])


# ------------------------------------------------------------------------- X
class XAdapter:
    platform = "x"
    max_title = 100
    max_body = 260  # X counts a URL as 23 chars regardless of real length

    def _credentials(self) -> tuple[str, str]:
        settings = get_settings()
        return settings.x_bearer_token, settings.x_user_id

    def configured(self) -> bool:
        return all(self._credentials())

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        token, _user = self._credentials()
        if not (token and _user):
            return CrosspostResult(platform=self.platform, status="skipped")
        payload = _clip(payload, self.max_title, self.max_body)
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.twitter.com/2/tweets",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"text": f"{payload.title}\n\n{payload.text}".strip()},
                )
                resp.raise_for_status()
            tweet_id = str(resp.json().get("data", {}).get("id", ""))
            return CrosspostResult(
                platform=self.platform,
                status="posted",
                remote_id=tweet_id,
                url=f"https://x.com/i/status/{tweet_id}" if tweet_id else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("x crosspost failed: %s", exc)
            return CrosspostResult(platform=self.platform, status="failed", error=str(exc)[:300])


ADAPTERS: dict[str, Adapter] = {
    "max": MaxAdapter(),
    "vk": VkAdapter(),
    "x": XAdapter(),
}


def primary_image_path(post_id: int) -> str | None:
    """Find an existing farm-generated primary image suitable for a VK wall post."""
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        paths = session.execute(
            select(MediaRow.path)
            .where(
                MediaRow.post_id == post_id,
                MediaRow.is_primary.is_(True),
                MediaRow.kind.notin_(("video", "article_image")),
            )
            .order_by(MediaRow.id.desc())
        ).scalars().all()

    for raw_path in paths:
        if raw_path and Path(raw_path).is_file():
            return str(Path(raw_path))
    return None


def build_payload(post, *, image_path: str | None = None) -> CrosspostPayload:
    """Translate a published post into a per-platform payload."""
    body = post.body.strip()
    link = _first_source_url(post.sources or [])
    if link:
        body = f"{body}\n\nПодробнее: {link}"
    tags = " ".join(f"#{h}" for h in (post.hashtags or [])[:5])
    meta = {"cta": post.cta, "post_public_id": post.public_id}
    if image_path:
        meta["image_path"] = image_path
    return CrosspostPayload(
        platform="",
        title=post.hook.strip(),
        text=f"{body}\n\n{tags}".strip() if tags else body,
        link=link,
        meta=meta,
    )


def already_done(post_public_id: str, platform: str) -> bool:
    from sqlalchemy import select

    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        row = session.execute(
            select(CrosspostLog).where(
                CrosspostLog.post_public_id == post_public_id,
                CrosspostLog.platform == platform,
                CrosspostLog.status == "posted",
            )
        ).scalar_one_or_none()
        return row is not None


def log_result(post_public_id: str, result: CrosspostResult, attempt: int = 1) -> None:
    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=post_public_id,
                platform=result.platform,
                status=result.status,
                remote_id=result.remote_id,
                url=result.url,
                error=result.error,
                attempt=attempt,
            )
        )


__all__ = ["ADAPTERS", "Adapter", "AdapterNotConfigured", "CrosspostPayload", "CrosspostResult"]
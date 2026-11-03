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

import asyncio
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import ceil
from pathlib import Path
from typing import Protocol

from tgfarm.core.errors import AdapterNotConfigured
from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate
from tgfarm.s9_growth.instagram import InstagramAdapter
from tgfarm.s9_growth.facebook import FacebookPageAdapter
from tgfarm.s9_growth.pinterest import PinterestAdapter
from tgfarm.s9_growth.odnoklassniki import OdnoklassnikiAdapter
from tgfarm.s9_growth.tumblr import TumblrAdapter

log = get_logger(__name__)


class Adapter(Protocol):
    platform: str
    max_title: int
    max_body: int

    def configured(self) -> bool: ...

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult: ...


# ------------------------------------------------------------------- helpers
def ambiguous_crosspost_error(exc: BaseException) -> bool:
    """Whether a platform may have accepted a crosspost before the error surfaced."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        if status_code >= 500 or status_code in (408, 425):
            return True
        if 400 <= status_code < 500:
            return False

    kind = type(exc).__name__.lower()
    message = f"{kind}: {exc}".lower()
    definitely_before_send = (
        "connecttimeout",
        "connecterror",
        "gaierror",
        "getaddrinfo",
        "name or service not known",
        "connection refused",
        "network is unreachable",
    )
    if any(marker in message for marker in definitely_before_send):
        return False

    ambiguous = (
        "readtimeout",
        "writetimeout",
        "readerror",
        "writeerror",
        "request timeout",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "server disconnected",
        "remotedisconnected",
        "remoteprotocolerror",
        "incomplete read",
        "unexpected eof",
        "broken pipe",
    )
    if any(marker in message for marker in ambiguous):
        return True
    # Unknown failures after calling an adapter are not evidence that the
    # remote side effect failed. Prefer reconciliation over creating duplicates.
    return True


MAX_AUTOMATIC_CROSSPOST_ATTEMPTS = 5
CROSSPOST_RETRY_DELAYS_MIN = (2, 10, 30, 120)


def crosspost_retry_delay_min(attempt: int) -> int | None:
    """Cooldown after a definite failure, indexed by the failed attempt number."""
    try:
        number = int(attempt)
    except (TypeError, ValueError):
        return None
    if number < 1 or number >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS:
        return None
    return CROSSPOST_RETRY_DELAYS_MIN[number - 1]


def crosspost_retry_after_seconds(
    exc: BaseException, *, now: datetime | None = None, max_seconds: int = 86_400
) -> int | None:
    """Read provider Retry-After for explicit rate-limit/service-unavailable replies."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code not in (429, 503):
        return None
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None

    value = str(raw).strip()
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        seconds = ceil((retry_at.astimezone(UTC) - current.astimezone(UTC)).total_seconds())

    return min(max(0, seconds), max(0, int(max_seconds)))


def crosspost_retry_not_before(row) -> datetime | None:
    """Latest of the configured exponential delay and provider Retry-After."""
    deadlines: list[datetime] = []
    created = getattr(row, "created_at", None)
    if created is not None:
        created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
        delay_min = crosspost_retry_delay_min(getattr(row, "attempt", 1))
        if delay_min is not None:
            deadlines.append(created + timedelta(minutes=delay_min))

    retry_at = getattr(row, "retry_after_at", None)
    if retry_at is not None:
        retry_at = retry_at.replace(tzinfo=UTC) if retry_at.tzinfo is None else retry_at.astimezone(UTC)
        deadlines.append(retry_at)
    return max(deadlines) if deadlines else None


def crosspost_error_status(exc: BaseException) -> str:
    """Classify a failed API request without retrying an ambiguous side effect."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        # A rate-limit rejection is safe to retry after the cooldown.
        if status_code == 429:
            return "failed"
        if status_code in (408, 425) or status_code >= 500:
            return "unknown"
        if 400 <= status_code < 500:
            return "permanent"
    return "unknown" if ambiguous_crosspost_error(exc) else "failed"


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
        settings = get_settings()
        token, channel = self._credentials()
        return bool(token and channel and not getattr(settings, "max_channel_blocked", False))

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        token, channel = self._credentials()
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        payload = _clip(payload, self.max_title, self.max_body)
        # The API's 4,000-character limit applies to the complete message,
        # including the heading separator, not to the body alone.
        from tgfarm.max_api import MaxTemporaryError, select_max_chat
        from tgfarm.core.text import truncate

        try:
            await select_max_chat(token, str(channel))
        except MaxTemporaryError as exc:
            log.warning("max preflight temporarily unavailable: %s", redact_sensitive_text(exc, limit=180))
            return CrosspostResult(
                platform=self.platform,
                status="failed",
                error="MAX permission preflight is temporarily unavailable. The message was not sent; it can be retried.",
                retry_after_seconds=60,
            )
        except Exception as exc:  # noqa: BLE001 - never send without current write permission
            log.warning("max preflight permission failed: %s", redact_sensitive_text(exc, limit=180))
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="MAX no longer confirms the bot's write permission for the selected destination. Re-select the chat after restoring permissions.",
            )

        import httpx

        url = "https://platform-api2.max.ru/messages"
        message_text = truncate(f"{payload.title}\n\n{payload.text}", 4000)
        try:
            async with httpx.AsyncClient(
                timeout=25.0,
                follow_redirects=False,
                headers={"Authorization": token, "Accept": "application/json"},
            ) as client:
                resp = await client.post(
                    url,
                    params={"chat_id": str(channel), "disable_link_preview": "false"},
                    json={"text": message_text},
                )
                resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                return CrosspostResult(
                    platform=self.platform,
                    status="unknown",
                    error="MAX returned an unrecognized success response; verify the chat before retrying.",
                )
            if body.get("success") is False:
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="MAX explicitly rejected the message; review the bot permissions and message.",
                )
            message = body.get("message") if isinstance(body.get("message"), dict) else {}
            message_body = message.get("body") if isinstance(message.get("body"), dict) else {}
            message_id = str(
                message_body.get("mid")
                or message.get("mid")
                or message.get("message_id")
                or body.get("message_id")
                or ""
            )
            if not message_id:
                return CrosspostResult(
                    platform=self.platform,
                    status="unknown",
                    error=(
                        "MAX did not return a message ID. The result may be ambiguous; "
                        "check the destination before authorizing a retry."
                    ),
                )
            return CrosspostResult(
                platform=self.platform,
                status="posted",
                remote_id=message_id,
                url=str(message.get("url") or "") or None,
            )
        except Exception as exc:  # noqa: BLE001
            status = crosspost_error_status(exc)
            log.warning("max crosspost %s: %s", status, redact_sensitive_text(exc, limit=180))
            return CrosspostResult(platform=self.platform, status=status, error=redact_sensitive_text(exc, limit=300),
                retry_after_seconds=crosspost_retry_after_seconds(exc))


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
        try:
            owner_id = -abs(int(str(group).strip()))
        except (TypeError, ValueError):
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="VK community ID must be numeric; enter it without a leading minus sign.",
            )
        if owner_id == 0:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="VK community ID must be a positive numeric identifier.",
            )
        import httpx

        attachment = None
        image_path = (payload.meta or {}).get("image_path")
        if image_path:
            try:
                attachment = await self._upload_wall_photo(str(image_path))
            except Exception as exc:  # noqa: BLE001 - a cover is optional
                log.warning("vk photo upload failed: %s", redact_sensitive_text(exc, limit=180))
        try:
            data = {
                "access_token": token,
                "owner_id": owner_id,
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
                error_body = body["error"]
                try:
                    error_code = int(error_body.get("error_code", 0)) if isinstance(error_body, dict) else 0
                except (TypeError, ValueError):
                    error_code = 0
                if error_code in {6, 9}:
                    error_status = "failed"
                elif error_code == 10:
                    error_status = "unknown"
                else:
                    error_status = "permanent"
                return CrosspostResult(
                    platform=self.platform,
                    status=error_status,
                    error=str(error_body)[:300],
                )
            post_id = str(body.get("response", {}).get("post_id", ""))
            return CrosspostResult(
                platform=self.platform,
                status="posted",
                remote_id=post_id,
                url=f"https://vk.com/wall{owner_id}_{post_id}" if post_id else None,
            )
        except Exception as exc:  # noqa: BLE001
            status = crosspost_error_status(exc)
            log.warning("vk crosspost %s: %s", status, redact_sensitive_text(exc, limit=180))
            return CrosspostResult(platform=self.platform, status=status, error=redact_sensitive_text(exc, limit=300),
                retry_after_seconds=crosspost_retry_after_seconds(exc))


# ------------------------------------------------------------------------- X
class XAdapter:
    platform = "x"
    max_title = 100
    max_body = 260  # X counts a URL as 23 chars regardless of real length

    def _credentials(self) -> tuple[str, str]:
        settings = get_settings()
        return str(settings.x_access_token or "").strip(), str(settings.x_user_id or "").strip()

    def configured(self) -> bool:
        settings = get_settings()
        return bool((settings.x_access_token or settings.x_refresh_token) and settings.x_user_id)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        _token, user_id = self._credentials()
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        payload = _clip(payload, self.max_title, self.max_body)
        import httpx

        try:
            from tgfarm.x_oauth import XOAuthTemporaryError, get_valid_x_access_token

            token = await get_valid_x_access_token()
        except Exception as exc:  # noqa: BLE001 - refresh is separate from posting
            status = "failed" if isinstance(exc, XOAuthTemporaryError) else "permanent"
            log.warning("x OAuth refresh %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status=status,
                error="X OAuth token refresh failed. Reconnect the account if the token was revoked.",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.x.com/2/tweets",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"text": f"{payload.title}\n\n{payload.text}".strip()},
                )
                resp.raise_for_status()
            tweet_id = str(resp.json().get("data", {}).get("id", ""))
            if not tweet_id:
                return CrosspostResult(
                    platform=self.platform, status="unknown",
                    error="X returned a response without a verifiable post ID.",
                )
            return CrosspostResult(
                platform=self.platform, status="posted",
                remote_id=tweet_id,
                url=f"https://x.com/i/status/{tweet_id}",
            )
        except Exception as exc:  # noqa: BLE001 - no blind retry after an ambiguous write
            status = crosspost_error_status(exc)
            log.warning("x crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status=status,
                error=f"X request failed ({type(exc).__name__}).",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )



# ------------------------------------------------------------------ Mastodon
class MastodonAdapter:
    """Publish a native text status to the configured Mastodon instance."""

    platform = "mastodon"
    max_title = 500
    max_body = 500

    def _credentials(self) -> tuple[str, str]:
        from tgfarm.mastodon_oauth import normalize_instance_url

        settings = get_settings()
        try:
            instance = normalize_instance_url(settings.mastodon_instance)
        except (TypeError, ValueError):
            instance = ""
        return instance, str(settings.mastodon_access_token or "").strip()

    def configured(self) -> bool:
        instance, token = self._credentials()
        return bool(instance and token)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        instance, token = self._credentials()
        if not (instance and token):
            return CrosspostResult(platform=self.platform, status="skipped")
        import hashlib
        import httpx

        body = f"{str(payload.title or '').strip()}\n\n{str(payload.text or '').strip()}".strip()
        body = truncate(body, self.max_body).strip()
        if not body:
            return CrosspostResult(
                platform=self.platform, status="permanent",
                error="Mastodon status text is empty.",
            )
        idempotency_key = hashlib.sha256(f"{instance}\0{body}".encode("utf-8")).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{instance}/api/v1/statuses",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": idempotency_key,
                    },
                    data={"status": body},
                )
                response.raise_for_status()
                result = response.json()
            remote_id = str(result.get("id") or "")
            raw_url = str(result.get("url") or "")
            url = raw_url if raw_url.startswith("https://") else None
            if not remote_id and not url:
                return CrosspostResult(
                    platform=self.platform, status="unknown",
                    error="Mastodon accepted a response without a verifiable status ID.",
                )
            return CrosspostResult(
                platform=self.platform, status="posted",
                remote_id=remote_id or None, url=url,
            )
        except Exception as exc:  # noqa: BLE001 - ambiguous writes must not be retried blindly
            status = crosspost_error_status(exc)
            log.warning("mastodon crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status=status,
                error=f"Mastodon request failed ({type(exc).__name__}).",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )


# ------------------------------------------------------------------- Bluesky
class BlueskyAdapter:
    """Publish a short native post through the official AT Protocol API."""

    platform = "bluesky"
    max_title = 120
    max_body = 300
    max_post_length = 300

    def _credentials(self) -> tuple[str, str]:
        settings = get_settings()
        return settings.bluesky_handle.strip(), settings.bluesky_app_password.strip()

    def configured(self) -> bool:
        return all(self._credentials())

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        handle, app_password = self._credentials()
        if not handle or not app_password:
            return CrosspostResult(platform=self.platform, status="skipped")
        import httpx

        text_value = _clip(payload, self.max_title, self.max_body)
        body = f"{text_value.title}\n\n{text_value.text}".strip()
        # Bluesky stores title and body in one text field. Enforce the limit
        # after combining them so a long title cannot silently consume the body.
        body = truncate(body, self.max_post_length)
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                session_response = await client.post(
                    "https://bsky.social/xrpc/com.atproto.server.createSession",
                    json={"identifier": handle, "password": app_password},
                )
                session_response.raise_for_status()
                session = session_response.json()
                access_jwt = str(session.get("accessJwt") or "")
                did = str(session.get("did") or "")
                if not access_jwt or not did:
                    raise RuntimeError("Bluesky session response was incomplete.")
                repo = str(session.get("did") or did)
                record = {
                    "$type": "app.bsky.feed.post",
                    "text": body,
                    "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
                response = await client.post(
                    "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                    headers={"Authorization": f"Bearer {access_jwt}"},
                    json={"repo": repo, "collection": "app.bsky.feed.post", "record": record},
                )
                response.raise_for_status()
                result = response.json()
                uri = str(result.get("uri") or "")
                rkey = uri.rsplit("/", 1)[-1] if uri else ""
                url = f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else None
                return CrosspostResult(
                    platform=self.platform, status="posted",
                    remote_id=uri or rkey, url=url,
                )
        except Exception as exc:  # noqa: BLE001 - no blind retries after ambiguous write errors
            status = crosspost_error_status(exc)
            log.warning("bluesky crosspost %s: %s", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status=status, error=f"Bluesky request failed ({type(exc).__name__}).",
                retry_after_seconds=crosspost_retry_after_seconds(exc)
            )


# --------------------------------------------------------------------- Reddit
class RedditAuthError(RuntimeError):
    """OAuth credentials were rejected before a post could be submitted."""


class RedditAdapter:
    """Publish native self/link posts through Reddit's OAuth Data API.

    A permanent refresh token is required; Reddit usernames/passwords are never
    stored or sent by this adapter. The default target is one configured
    subreddit, with an optional per-payload override for controlled workflows.
    """

    platform = "reddit"
    max_title = 300
    max_body = 40_000
    token_url = "https://www.reddit.com/api/v1/access_token"
    submit_url = "https://oauth.reddit.com/api/submit"

    def __init__(self) -> None:
        import asyncio

        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _credentials(self) -> tuple[str, str, str, str, str]:
        settings = get_settings()
        return (
            settings.reddit_client_id.strip(),
            settings.reddit_client_secret.strip(),
            settings.reddit_refresh_token.strip(),
            settings.reddit_subreddit.strip(),
            settings.reddit_user_agent.strip(),
        )

    def data_api_approved(self) -> bool:
        """Whether the owner has explicitly recorded Reddit's API approval."""
        return bool(getattr(get_settings(), "reddit_api_approved", False))

    def configured(self) -> bool:
        client_id, client_secret, refresh_token, _subreddit, user_agent = self._credentials()
        # Reddit requires explicit API access approval. Credentials alone are not
        # evidence that this app/use case is allowed to call the Data API.
        return bool(
            self.data_api_approved()
            and client_id
            and client_secret
            and refresh_token
            and user_agent
            and "/u/YOUR_REDDIT_USERNAME" not in user_agent
        )

    async def _access_token(self) -> str:
        import time

        import httpx

        client_id, client_secret, refresh_token, _subreddit, user_agent = self._credentials()
        if not (client_id and client_secret and refresh_token and user_agent):
            raise RedditAuthError("Reddit OAuth credentials are incomplete.")

        now = time.monotonic()
        if self._token and self._token_expires_at > now + 30:
            return self._token

        async with self._token_lock:
            now = time.monotonic()
            if self._token and self._token_expires_at > now + 30:
                return self._token

            async with httpx.AsyncClient(
                timeout=20.0,
                headers={"User-Agent": user_agent},
                auth=(client_id, client_secret),
            ) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                )
                response.raise_for_status()
                body = response.json()

            token = str(body.get("access_token") or "")
            if body.get("error") or not token:
                # This request only refreshes credentials; no submission has
                # happened, so classify it as a permanent setup/auth issue.
                raise RedditAuthError("Reddit OAuth rejected the refresh token.")
            try:
                lifetime = max(60, int(body.get("expires_in") or 3600))
            except (TypeError, ValueError):
                lifetime = 3600
            self._token = token
            self._token_expires_at = time.monotonic() + max(1, lifetime - 60)
            return self._token

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        import re

        import httpx

        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")

        _client_id, _client_secret, _refresh_token, default_subreddit, user_agent = self._credentials()
        meta = payload.meta or {}
        subreddit = str(meta.get("subreddit") or default_subreddit).strip()
        if subreddit.lower().startswith("r/"):
            subreddit = subreddit[2:]
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Invalid Reddit subreddit name; use the name without r/.",
            )

        title = str(payload.title or "").strip()
        if not title:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Reddit posts require a non-empty title.",
            )
        payload = _clip(payload, self.max_title, self.max_body)
        title = payload.title.strip()
        kind = str(meta.get("reddit_kind") or "self").strip().lower()
        if kind not in {"self", "link"}:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Unsupported Reddit post kind; choose 'self' or 'link'.",
            )

        form: dict[str, str] = {
            "api_type": "json",
            "sr": subreddit,
            "kind": kind,
            "title": title,
            "resubmit": "false",
            "sendreplies": "true",
            "nsfw": "false",
            "spoiler": "false",
        }
        if kind == "link":
            from urllib.parse import urlparse

            link = str(payload.link or "").strip()
            parsed = urlparse(link)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="Reddit link posts require a valid http(s) URL.",
                )
            form["url"] = link
        else:
            body = payload.text.strip()
            if not body:
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="Reddit text posts require a body.",
                )
            form["text"] = body

        try:
            token = await self._access_token()
        except Exception as exc:  # noqa: BLE001 - no post request has been made yet
            status = "permanent" if isinstance(exc, RedditAuthError) else crosspost_error_status(exc)
            # OAuth refresh has no publishing side effect. A timeout here is
            # retryable, unlike an ambiguous timeout during the submit request.
            if status == "unknown":
                status = "failed"
            log.warning("reddit OAuth refresh %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=(
                    "Reddit OAuth setup was rejected."
                    if isinstance(exc, RedditAuthError)
                    else redact_sensitive_text(exc, limit=300)
                ),
            )

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": user_agent,
                    "Authorization": f"Bearer {token}",
                },
            ) as client:
                response = await client.post(self.submit_url, data=form)
                response.raise_for_status()
                response_body = response.json() or {}
        except Exception as exc:  # noqa: BLE001 - no automatic duplicate on ambiguity
            status = crosspost_error_status(exc)
            log.warning("reddit crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=redact_sensitive_text(exc, limit=300),
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )

        result_body = response_body.get("json") if isinstance(response_body, dict) else None
        if not isinstance(result_body, dict):
            result_body = response_body if isinstance(response_body, dict) else {}
        errors = result_body.get("errors") or []
        if errors:
            first = errors[0]
            if isinstance(first, (list, tuple)):
                error_code = str(first[0] or "REDDIT_ERROR")
                detail = str(first[1] or "Reddit rejected the submission.")
            else:
                error_code = "REDDIT_ERROR"
                detail = str(first)
            transient_codes = {"RATELIMIT", "TOO_MANY_REQUESTS", "TEMPORARY_ERROR"}
            status = "failed" if error_code.upper() in transient_codes else "permanent"
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=f"{error_code}: {detail}"[:300],
            )

        data = result_body.get("data") or {}
        things = data.get("things") or []
        submitted = things[0].get("data") if things and isinstance(things[0], dict) else {}
        submitted = submitted if isinstance(submitted, dict) else {}
        remote_id = str(submitted.get("id") or submitted.get("name") or data.get("id") or "") or None
        permalink = submitted.get("permalink") or data.get("url")
        if isinstance(permalink, str) and permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"
        url = (
            permalink
            if isinstance(permalink, str) and permalink.startswith(("https://", "http://"))
            else None
        )

        if not remote_id and not url:
            # A success-shaped but unidentifiable response is not proof that
            # nothing was posted. S9 will mark it unknown and block auto-retry.
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Reddit returned no post identifier; verify the subreddit before retrying.",
            )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=remote_id,
            url=url,
        )


# ------------------------------------------------------------------- Threads
class ThreadsAdapter:
    """Publish a native text post via the Threads two-step container API."""

    platform = "threads"
    max_title = 500
    max_body = 500
    graph_url = "https://graph.threads.net/v1.0"

    def _credentials(self) -> tuple[str, str, str]:
        settings = get_settings()
        return (
            str(settings.threads_user_id or "").strip(),
            str(settings.threads_access_token or "").strip(),
            str(settings.threads_username or "").strip(),
        )

    def configured(self) -> bool:
        user_id, token, _username = self._credentials()
        return bool(user_id and token)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        user_id, saved_token, _username = self._credentials()
        if not user_id or not saved_token:
            return CrosspostResult(platform=self.platform, status="skipped")
        import httpx
        from tgfarm.threads_oauth import get_valid_threads_access_token

        try:
            token = await get_valid_threads_access_token()
        except Exception as exc:  # noqa: BLE001 - no publish request has been made yet
            log.warning("threads token refresh failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status="permanent",
                error="Threads OAuth expired or could not be refreshed; reconnect the account.",
            )

        body = f"{str(payload.title or '').strip()}\n\n{str(payload.text or '').strip()}".strip()[:self.max_body]
        if not body:
            return CrosspostResult(
                platform=self.platform, status="permanent",
                error="Threads text posts require a non-empty body.",
            )
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
                container_response = await client.post(
                    f"{self.graph_url}/{user_id}/threads",
                    data={"media_type": "TEXT", "text": body, "access_token": token},
                )
                container_response.raise_for_status()
                container_payload = container_response.json()
                creation_id = str(container_payload.get("id") or "")
                if not creation_id:
                    raise RuntimeError("Threads did not return a container identifier.")

                publish_response = await client.post(
                    f"{self.graph_url}/{user_id}/threads_publish",
                    data={"creation_id": creation_id, "access_token": token},
                )
                publish_response.raise_for_status()
                published_payload = publish_response.json()
                remote_id = str(published_payload.get("id") or "")
                if not remote_id:
                    # A success-shaped response without an ID cannot safely be retried.
                    return CrosspostResult(
                        platform=self.platform, status="unknown",
                        error="Threads accepted publish request but returned no post ID; reconcile before retrying.",
                    )

                permalink = None
                try:
                    permalink_response = await client.get(
                        f"{self.graph_url}/{remote_id}",
                        params={"fields": "id,permalink", "access_token": token},
                    )
                    if permalink_response.status_code < 400:
                        permalink = permalink_response.json().get("permalink")
                except Exception:  # noqa: BLE001 - lookup is best effort after confirmed publish
                    permalink = None
                url = (
                    str(permalink) if isinstance(permalink, str) and permalink.startswith("https://")
                    else None
                )
                return CrosspostResult(
                    platform=self.platform, status="posted", remote_id=remote_id, url=url,
                )
        except Exception as exc:  # noqa: BLE001 - no blind retry after a possible publish
            status = crosspost_error_status(exc)
            log.warning("threads crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status=status,
                error=f"Threads request failed ({type(exc).__name__}).",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )


# ------------------------------------------------------------------ LinkedIn
class LinkedInAdapter:
    """Publish native text posts as the connected LinkedIn member."""

    platform = "linkedin"
    max_title = 3000
    max_body = 3000
    api_url = "https://api.linkedin.com/rest/posts"
    api_version = "202610"

    def _credentials(self) -> tuple[str, str, str]:
        settings = get_settings()
        return (
            str(settings.linkedin_member_id or "").strip(),
            str(settings.linkedin_access_token or "").strip(),
            str(settings.linkedin_member_name or "").strip(),
        )

    def configured(self) -> bool:
        member_id, token, _name = self._credentials()
        return bool(member_id and token)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        member_id, saved_token, _name = self._credentials()
        if not member_id or not saved_token:
            return CrosspostResult(platform=self.platform, status="skipped")

        from tgfarm.linkedin_oauth import get_valid_linkedin_access_token

        try:
            token = await get_valid_linkedin_access_token()
        except Exception as exc:  # noqa: BLE001 - no post request has been made
            log.warning("linkedin token refresh failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="LinkedIn OAuth expired or could not be refreshed; reconnect the account.",
            )

        title = str(payload.title or "").strip()
        text = str(payload.text or "").strip()
        body = truncate("\n\n".join(part for part in (title, text) if part), self.max_body)
        if not body:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="LinkedIn text posts require a non-empty body.",
            )

        author = f"urn:li:person:{member_id}"
        post = {
            "author": author,
            "commentary": body,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": self.api_version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=25.0,
                follow_redirects=False,
                headers=headers,
            ) as client:
                response = await client.post(self.api_url, json=post)
                response.raise_for_status()
                remote_id = str(response.headers.get("x-restli-id") or "").strip()
                if not remote_id:
                    return CrosspostResult(
                        platform=self.platform,
                        status="unknown",
                        error="LinkedIn accepted the request without a post ID; reconcile before retrying.",
                    )
                # LinkedIn post IDs are returned as URNs, e.g. urn:li:share:... .
                url = f"https://www.linkedin.com/feed/update/{remote_id}/"
                return CrosspostResult(
                    platform=self.platform,
                    status="posted",
                    remote_id=remote_id,
                    url=url,
                )
        except Exception as exc:  # noqa: BLE001 - never blind retry after an ambiguous side effect
            status = crosspost_error_status(exc)
            log.warning("linkedin crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=f"LinkedIn request failed ({type(exc).__name__}).",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )


# ------------------------------------------------------------------- YouTube
class YouTubeAdapter:
    """Resumable upload of an original MP4 to the OAuth-connected YouTube channel."""

    platform = "youtube"
    max_title = 100
    max_body = 5000
    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"

    def _credentials(self) -> tuple[str, str, str]:
        settings = get_settings()
        return (
            str(settings.youtube_refresh_token or "").strip(),
            str(settings.youtube_client_id or "").strip(),
            str(settings.youtube_client_secret or "").strip(),
        )

    def configured(self) -> bool:
        settings = get_settings()
        refresh_token, client_id, client_secret = self._credentials()
        return bool(
            refresh_token
            and client_id
            and client_secret
            and str(settings.youtube_channel_id or "").strip()
        )

    @staticmethod
    def _tags(title: str, text: str) -> list[str]:
        import re

        seen: set[str] = set()
        tags: list[str] = []
        for raw in re.findall(r"(?<!\w)#([\w\d_]{1,50})", f"{title}\n{text}", flags=re.UNICODE):
            tag = raw.strip()
            key = tag.casefold()
            if not tag or key in seen:
                continue
            # YouTube limits the total serialized tag value to about 500 characters.
            candidate = tags + [tag]
            if len(",".join(candidate)) > 480:
                break
            seen.add(key)
            tags.append(tag)
            if len(tags) >= 15:
                break
        return tags

    @staticmethod
    def _safe_error(response, operation: str) -> CrosspostResult:
        status = crosspost_error_status(
            type("HTTPResponseError", (Exception,), {
                "response": response,
            })(f"HTTP {response.status_code}")
        )
        if status == "unknown":
            error = f"YouTube {operation} returned HTTP {response.status_code}; verify the channel before retrying."
        elif response.status_code == 429:
            error = "YouTube rate limit reached; retry will follow the provider cooldown."
        elif response.status_code in (401, 403):
            error = (
                "YouTube rejected the request. Check OAuth scopes, channel permissions, "
                "API enablement, or upload quota."
            )
        else:
            error = f"YouTube {operation} failed with HTTP {response.status_code}."
        return CrosspostResult(
            platform="youtube",
            status=status,
            error=error,
            retry_after_seconds=crosspost_retry_after_seconds(
                type("HTTPResponseError", (Exception,), {
                    "response": response,
                })(f"HTTP {response.status_code}")
            ),
        )

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        import httpx

        from tgfarm.youtube_oauth import get_valid_youtube_access_token

        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")

        video_path = str(
            payload.video_path or (payload.meta or {}).get("video_path") or ""
        ).strip()
        path = Path(video_path) if video_path else None
        if path is None or not path.is_file():
            render_error = str((payload.meta or {}).get("video_render_error") or "").strip()
            detail = (
                f"Automatic short rendering failed: {render_error}"
                if render_error
                else "No rendered video asset is attached to this post. Render a short before enabling YouTube delivery."
            )
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=redact_sensitive_text(detail, limit=300),
            )
        try:
            byte_count = path.stat().st_size
        except OSError:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="The rendered video file is no longer available.",
            )
        max_bytes = max(1, int(get_settings().video_max_mb)) * 1024 * 1024
        if byte_count <= 0:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="The rendered video file is empty.",
            )
        if byte_count > max_bytes:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=f"Video is larger than the configured VIDEO_MAX_MB limit ({max_bytes // (1024 * 1024)} MB).",
            )

        try:
            token = await get_valid_youtube_access_token()
        except Exception as exc:  # noqa: BLE001 - no upload session was created yet
            message = str(exc).lower()
            transient = any(word in message for word in ("network", "connect", "timeout", "не удалось подключиться"))
            return CrosspostResult(
                platform=self.platform,
                status="failed" if transient else "permanent",
                error=(
                    "YouTube OAuth refresh failed; check network and reconnect the channel if needed."
                ),
            )

        settings = get_settings()
        title = truncate(str(payload.title or "").strip(), self.max_title)
        if not title:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="YouTube videos require a non-empty title.",
            )
        description = truncate(str(payload.text or "").strip(), self.max_body)
        if payload.link and payload.link not in description:
            description = truncate(f"{description}\n\nПодробнее: {payload.link}".strip(), self.max_body)
        snippet = {
            "title": title,
            "description": description,
            "categoryId": str(settings.youtube_category_id or "25"),
            "defaultLanguage": "ru",
            "tags": self._tags(title, description),
        }
        privacy = str(settings.youtube_privacy_status or "private").lower()
        if privacy not in {"private", "unlisted", "public"}:
            privacy = "private"
        metadata = {
            "snippet": snippet,
            "status": {
                "privacyStatus": privacy,
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(byte_count),
            "X-Upload-Content-Type": "video/mp4",
        }

        try:
            async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
                init_response = await client.post(
                    self.upload_url,
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    headers=headers,
                    json=metadata,
                )
                if init_response.status_code >= 400:
                    return self._safe_error(init_response, "upload initialization")
                session_uri = str(init_response.headers.get("Location") or "").strip()
                from urllib.parse import urlsplit

                parsed_session_uri = urlsplit(session_uri)
                upload_host = (parsed_session_uri.hostname or "").lower()
                allowed_upload_host = (
                    upload_host == "www.googleapis.com"
                    or upload_host.endswith(".googleapis.com")
                    or upload_host == "upload.youtube.com"
                    or upload_host.endswith(".youtube.com")
                )
                try:
                    upload_port = parsed_session_uri.port
                except ValueError:
                    upload_port = -1
                if (
                    parsed_session_uri.scheme != "https"
                    or not allowed_upload_host
                    or parsed_session_uri.username
                    or parsed_session_uri.password
                    or parsed_session_uri.fragment
                    or upload_port not in (None, 443)
                ):
                    return CrosspostResult(
                        platform=self.platform,
                        status="failed",
                        error="YouTube returned an invalid resumable-upload destination; the video was not sent.",
                    )
                # The renderer keeps short-form videos small. One resumable PUT
                # sends the entire local MP4; ambiguous failures are never retried blindly.
                import asyncio

                video_bytes = await asyncio.to_thread(path.read_bytes)
                upload_response = await client.put(
                    session_uri,
                    content=video_bytes,
                    headers={
                        "Content-Length": str(len(video_bytes)),
                        "Content-Type": "video/mp4",
                    },
                )
                if upload_response.status_code >= 400:
                    return self._safe_error(upload_response, "media upload")
                try:
                    body = upload_response.json()
                except ValueError:
                    body = {}
        except httpx.RequestError as exc:
            status = crosspost_error_status(exc)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=(
                    "YouTube upload connection failed before confirmation; verify Studio before retrying."
                    if status == "unknown"
                    else "YouTube could not be reached; the upload can be retried."
                ),
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )
        except Exception as exc:  # noqa: BLE001 - preserve ambiguity after an upload request
            status = crosspost_error_status(exc)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=f"YouTube upload failed ({type(exc).__name__}); verify Studio before retrying."
                if status == "unknown"
                else "YouTube upload failed; review diagnostics before retrying.",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )

        video_id = str(body.get("id") or "") if isinstance(body, dict) else ""
        if not video_id:
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="YouTube returned no video ID after upload; reconcile in YouTube Studio before retrying.",
            )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
        )


class TikTokAdapter:
    """Auth/state adapter only; background crossposting is deliberately disabled.

    Direct Post is exposed through a separate endpoint that first queries current
    creator privacy options and requires explicit consent for each specific video.
    """

    platform = "tiktok"

    def configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.tiktok_client_key
            and settings.tiktok_client_secret
            and settings.tiktok_refresh_token
            and settings.tiktok_open_id
        )

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        return CrosspostResult(
            platform=self.platform,
            status="skipped",
            error="TikTok requires explicit per-video confirmation in the dashboard.",
        )



# --------------------------------------------------------------------- Reddit
class RedditAuthError(RuntimeError):
    """OAuth credentials were rejected before a post could be submitted."""


class RedditAdapter:
    """Publish native self/link posts through Reddit's OAuth Data API.

    A permanent refresh token is required; Reddit usernames/passwords are never
    stored or sent by this adapter. The default target is one configured
    subreddit, with an optional per-payload override for controlled workflows.
    """

    platform = "reddit"
    max_title = 300
    max_body = 40_000
    token_url = "https://www.reddit.com/api/v1/access_token"
    submit_url = "https://oauth.reddit.com/api/submit"

    def __init__(self) -> None:
        import asyncio

        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _credentials(self) -> tuple[str, str, str, str, str]:
        settings = get_settings()
        return (
            settings.reddit_client_id.strip(),
            settings.reddit_client_secret.strip(),
            settings.reddit_refresh_token.strip(),
            settings.reddit_subreddit.strip(),
            settings.reddit_user_agent.strip(),
        )

    def data_api_approved(self) -> bool:
        """Whether the owner has explicitly recorded Reddit's API approval."""
        return bool(getattr(get_settings(), "reddit_api_approved", False))

    def configured(self) -> bool:
        client_id, client_secret, refresh_token, _subreddit, user_agent = self._credentials()
        # The S9 scout chooses a subreddit, but API access still requires
        # Reddit's explicit approval for this app and use case.
        return bool(
            self.data_api_approved()
            and client_id
            and client_secret
            and refresh_token
            and user_agent
            and "/u/YOUR_REDDIT_USERNAME" not in user_agent
        )

    async def _access_token(self) -> str:
        import time

        import httpx

        client_id, client_secret, refresh_token, _subreddit, user_agent = self._credentials()
        if not (client_id and client_secret and refresh_token and user_agent):
            raise RedditAuthError("Reddit OAuth credentials are incomplete.")

        now = time.monotonic()
        if self._token and self._token_expires_at > now + 30:
            return self._token

        async with self._token_lock:
            now = time.monotonic()
            if self._token and self._token_expires_at > now + 30:
                return self._token

            async with httpx.AsyncClient(
                timeout=20.0,
                headers={"User-Agent": user_agent},
                auth=(client_id, client_secret),
            ) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                )
                response.raise_for_status()
                body = response.json()

            token = str(body.get("access_token") or "")
            if body.get("error") or not token:
                # This request only refreshes credentials; no submission has
                # happened, so classify it as a permanent setup/auth issue.
                raise RedditAuthError("Reddit OAuth rejected the refresh token.")
            try:
                lifetime = max(60, int(body.get("expires_in") or 3600))
            except (TypeError, ValueError):
                lifetime = 3600
            self._token = token
            self._token_expires_at = time.monotonic() + max(1, lifetime - 60)
            return self._token

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        import re

        import httpx

        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")

        _client_id, _client_secret, _refresh_token, default_subreddit, user_agent = self._credentials()
        meta = payload.meta or {}
        subreddit = str(meta.get("subreddit") or default_subreddit).strip()
        if subreddit.lower().startswith("r/"):
            subreddit = subreddit[2:]
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Invalid Reddit subreddit name; use the name without r/.",
            )

        title = str(payload.title or "").strip()
        if not title:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Reddit posts require a non-empty title.",
            )
        payload = _clip(payload, self.max_title, self.max_body)
        title = payload.title.strip()
        kind = str(meta.get("reddit_kind") or "self").strip().lower()
        if kind not in {"self", "link"}:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Unsupported Reddit post kind; choose 'self' or 'link'.",
            )

        form: dict[str, str] = {
            "api_type": "json",
            "sr": subreddit,
            "kind": kind,
            "title": title,
            "resubmit": "false",
            "sendreplies": "true",
            "nsfw": "false",
            "spoiler": "false",
        }
        if kind == "link":
            from urllib.parse import urlparse

            link = str(payload.link or "").strip()
            parsed = urlparse(link)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="Reddit link posts require a valid http(s) URL.",
                )
            form["url"] = link
        else:
            body = payload.text.strip()
            if not body:
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="Reddit text posts require a body.",
                )
            form["text"] = body

        try:
            token = await self._access_token()
        except Exception as exc:  # noqa: BLE001 - no post request has been made yet
            status = "permanent" if isinstance(exc, RedditAuthError) else crosspost_error_status(exc)
            # OAuth refresh has no publishing side effect. A timeout here is
            # retryable, unlike an ambiguous timeout during the submit request.
            if status == "unknown":
                status = "failed"
            log.warning("reddit OAuth refresh %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=(
                    "Reddit OAuth setup was rejected."
                    if isinstance(exc, RedditAuthError)
                    else str(exc)[:300]
                ),
            )

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": user_agent,
                    "Authorization": f"Bearer {token}",
                },
            ) as client:
                response = await client.post(self.submit_url, data=form)
                response.raise_for_status()
                response_body = response.json() or {}
        except Exception as exc:  # noqa: BLE001 - no automatic duplicate on ambiguity
            status = crosspost_error_status(exc)
            log.warning("reddit crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=str(exc)[:300],
            )

        result_body = response_body.get("json") if isinstance(response_body, dict) else None
        if not isinstance(result_body, dict):
            result_body = response_body if isinstance(response_body, dict) else {}
        errors = result_body.get("errors") or []
        if errors:
            first = errors[0]
            if isinstance(first, (list, tuple)):
                error_code = str(first[0] or "REDDIT_ERROR")
                detail = str(first[1] or "Reddit rejected the submission.")
            else:
                error_code = "REDDIT_ERROR"
                detail = str(first)
            transient_codes = {"RATELIMIT", "TOO_MANY_REQUESTS", "TEMPORARY_ERROR"}
            status = "failed" if error_code.upper() in transient_codes else "permanent"
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=f"{error_code}: {detail}"[:300],
            )

        data = result_body.get("data") or {}
        things = data.get("things") or []
        submitted = things[0].get("data") if things and isinstance(things[0], dict) else {}
        submitted = submitted if isinstance(submitted, dict) else {}
        remote_id = str(submitted.get("id") or submitted.get("name") or data.get("id") or "") or None
        permalink = submitted.get("permalink") or data.get("url")
        if isinstance(permalink, str) and permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"
        url = (
            permalink
            if isinstance(permalink, str) and permalink.startswith(("https://", "http://"))
            else None
        )

        if not remote_id and not url:
            # A success-shaped but unidentifiable response is not proof that
            # nothing was posted. S9 will mark it unknown and block auto-retry.
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Reddit returned no post identifier; verify the subreddit before retrying.",
            )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=remote_id,
            url=url,
        )


# ------------------------------------------------------------------- Threads
class ThreadsAdapter:
    """Publish a native text post via the Threads two-step container API."""

    platform = "threads"
    max_title = 500
    max_body = 500
    graph_url = "https://graph.threads.net/v1.0"

    def _credentials(self) -> tuple[str, str, str]:
        settings = get_settings()
        return (
            str(settings.threads_user_id or "").strip(),
            str(settings.threads_access_token or "").strip(),
            str(settings.threads_username or "").strip(),
        )

    def configured(self) -> bool:
        user_id, token, _username = self._credentials()
        return bool(user_id and token)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        user_id, saved_token, _username = self._credentials()
        if not user_id or not saved_token:
            return CrosspostResult(platform=self.platform, status="skipped")
        import httpx
        from tgfarm.threads_oauth import get_valid_threads_access_token

        try:
            token = await get_valid_threads_access_token()
        except Exception as exc:  # noqa: BLE001 - no publish request has been made yet
            log.warning("threads token refresh failed (%s)", type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status="permanent",
                error="Threads OAuth expired or could not be refreshed; reconnect the account.",
            )

        body = f"{str(payload.title or '').strip()}\n\n{str(payload.text or '').strip()}".strip()[:self.max_body]
        if not body:
            return CrosspostResult(
                platform=self.platform, status="permanent",
                error="Threads text posts require a non-empty body.",
            )
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
                container_response = await client.post(
                    f"{self.graph_url}/{user_id}/threads",
                    data={"media_type": "TEXT", "text": body, "access_token": token},
                )
                container_response.raise_for_status()
                container_payload = container_response.json()
                creation_id = str(container_payload.get("id") or "")
                if not creation_id:
                    raise RuntimeError("Threads did not return a container identifier.")

                publish_response = await client.post(
                    f"{self.graph_url}/{user_id}/threads_publish",
                    data={"creation_id": creation_id, "access_token": token},
                )
                publish_response.raise_for_status()
                published_payload = publish_response.json()
                remote_id = str(published_payload.get("id") or "")
                if not remote_id:
                    # A success-shaped response without an ID cannot safely be retried.
                    return CrosspostResult(
                        platform=self.platform, status="unknown",
                        error="Threads accepted publish request but returned no post ID; reconcile before retrying.",
                    )

                permalink = None
                try:
                    permalink_response = await client.get(
                        f"{self.graph_url}/{remote_id}",
                        params={"fields": "id,permalink", "access_token": token},
                    )
                    if permalink_response.status_code < 400:
                        permalink = permalink_response.json().get("permalink")
                except Exception:  # noqa: BLE001 - lookup is best effort after confirmed publish
                    permalink = None
                url = (
                    str(permalink) if isinstance(permalink, str) and permalink.startswith("https://")
                    else None
                )
                return CrosspostResult(
                    platform=self.platform, status="posted", remote_id=remote_id, url=url,
                )
        except Exception as exc:  # noqa: BLE001 - no blind retry after a possible publish
            status = crosspost_error_status(exc)
            log.warning("threads crosspost %s (%s)", status, type(exc).__name__)
            return CrosspostResult(
                platform=self.platform, status=status,
                error=f"Threads request failed ({type(exc).__name__}).",
            )



# -------------------------------------------------------------------- Discord
class DiscordAdapter:
    """Publish a safe text message through an owner-managed Discord webhook."""

    platform = "discord"
    max_title = 256
    max_body = 2000

    @staticmethod
    def _webhook_url() -> str:
        from urllib.parse import urlsplit

        settings = get_settings()
        raw = str(getattr(settings, "discord_webhook_url", "") or "").strip()
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "discord.com"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"/api/webhooks/\d{17,22}/[A-Za-z0-9._-]{16,}/?", parsed.path)
        ):
            return ""
        return raw.rstrip("/")

    def configured(self) -> bool:
        return bool(self._webhook_url())

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        url = self._webhook_url()
        if not url:
            return CrosspostResult(platform=self.platform, status="skipped")
        content = "\n\n".join(
            part for part in (str(payload.title or "").strip(), str(payload.text or "").strip())
            if part
        )
        content = truncate(content, self.max_body)
        if not content:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Discord messages cannot be empty.",
            )
        params = {"wait": "true"}
        thread_id = str((payload.meta or {}).get("thread_id") or "").strip()
        if thread_id:
            if not re.fullmatch(r"\d{17,22}", thread_id):
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="Discord thread ID must be a numeric snowflake.",
                )
            params["thread_id"] = thread_id
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
                response = await client.post(
                    url,
                    params=params,
                    json={
                        "content": content,
                        "allowed_mentions": {"parse": []},
                    },
                )
                response.raise_for_status()
                result = response.json() if response.content else {}
        except Exception as exc:  # noqa: BLE001 - no blind retry after webhook side effect
            status = crosspost_error_status(exc)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=(
                    f"Discord webhook returned HTTP {getattr(getattr(exc, 'response', None), 'status_code', 'error')}."
                ),
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )

        message_id = str(result.get("id") or "") if isinstance(result, dict) else ""
        if not message_id:
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Discord did not return a message ID; check the channel before retrying.",
            )
        channel_id = str(result.get("channel_id") or "")
        guild_id = str(result.get("guild_id") or "")
        message_url = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            if guild_id and channel_id else None
        )
        return CrosspostResult(
            platform=self.platform,
            status="posted",
            remote_id=message_id,
            url=message_url,
        )


class DouyinAdapter:
    """Connection-only adapter; create_video requires a separate UI action."""
    platform = "douyin"

    def configured(self) -> bool:
        settings = get_settings()
        return bool(settings.douyin_client_key and settings.douyin_client_secret and settings.douyin_refresh_token and settings.douyin_open_id)

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        return CrosspostResult(platform=self.platform, status="skipped", error="Douyin video creation requires explicit preview and owner confirmation.")




class PikabuAdapter:
    """Schedule a native community post through the owner's verified browser session."""
    platform = "pikabu"
    max_title = 140
    max_body = 100_000

    def configured(self) -> bool:
        try:
            from tgfarm.browser_auth import browser_session_status
            status = browser_session_status("pikabu")
            return bool(status.get("verified") and status.get("profile_saved"))
        except Exception:
            return False

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        post_public_id = str((payload.meta or {}).get("post_public_id") or "").strip()
        if not post_public_id:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Pikabu delivery requires the saved TgFarm post identifier.",
            )
        try:
            from tgfarm.community_drafts import CommunityDraftError, publish_pikabu_scheduled
            outcome = await publish_pikabu_scheduled(
                post_public_id, title=str(payload.title or ""), body=str(payload.text or "")
            )
        except CommunityDraftError as exc:
            return CrosspostResult(
                platform=self.platform, status="skipped",
                error=redact_sensitive_text(exc, limit=280),
                retry_after_seconds=6 * 60 * 60,
            )
        status = str(outcome.get("status") or "")
        if status == "scheduled":
            return CrosspostResult(
                platform=self.platform, status="scheduled",
                remote_id=post_public_id, url=outcome.get("url"),
            )
        if status == "unknown":
            return CrosspostResult(
                platform=self.platform, status="unknown",
                remote_id=post_public_id, url=outcome.get("url"),
                error=redact_sensitive_text(outcome.get("detail") or "Pikabu submit outcome is unknown.", limit=280),
            )
        return CrosspostResult(
            platform=self.platform,
            status="skipped" if status == "skipped" else "failed",
            error=redact_sensitive_text(outcome.get("detail") or "Pikabu has no eligible automatic destination.", limit=280),
            retry_after_seconds=6 * 60 * 60,
        )



class MediumAdapter:
    """Publish Markdown articles with a pre-existing Medium legacy API token."""

    platform = "medium"
    api_url = "https://api.medium.com/v1"
    max_title = 100
    max_body = 100_000

    def _token(self) -> str:
        return str(getattr(get_settings(), "medium_access_token", "") or "").strip()

    def configured(self) -> bool:
        return bool(self._token())

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        token = self._token()
        if not token:
            return CrosspostResult(platform=self.platform, status="skipped")

        title = str(payload.title or '').strip()
        body = str(payload.text or '').strip()
        if not title or len(title) > self.max_title:
            return CrosspostResult(platform=self.platform, status='permanent', error='Medium titles must contain 1–100 characters.')
        if not body or len(body) > self.max_body:
            return CrosspostResult(platform=self.platform, status='permanent', error='Medium article body is empty or too long.')

        tags = []
        seen = set()
        for raw in payload.hashtags or []:
            tag = re.sub(r'\s+', ' ', str(raw or '').strip().lstrip('#')).strip()
            key = tag.casefold()
            if not tag or key in seen or len(tag) > 25:
                continue
            seen.add(key)
            tags.append(tag)
            if len(tags) == 3:
                break

        import httpx
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Charset": "utf-8",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=False, headers=headers) as client:
            try:
                me_response = await client.get(f"{self.api_url}/me")
                me_response.raise_for_status()
                me_data = me_response.json().get("data") or {}
                author_id = str(me_data.get("id") or "").strip()
                if not re.fullmatch(r'[A-Za-z0-9_-]{6,128}', author_id):
                    return CrosspostResult(platform=self.platform, status='permanent', error='Medium /me returned no valid author ID.')
            except Exception as exc:  # noqa: BLE001 - profile lookup has no publish side effect
                response = getattr(exc, 'response', None)
                code = getattr(response, 'status_code', 0)
                status = 'permanent' if code in {400, 401, 403, 404} else 'failed'
                return CrosspostResult(
                    platform=self.platform, status=status,
                    error=f'Medium author preflight failed ({type(exc).__name__}).',
                    retry_after_seconds=crosspost_retry_after_seconds(exc),
                )

            request_body = {
                'title': title,
                'contentFormat': 'markdown',
                'content': f'# {title}\n\n{body}',
                'tags': tags,
                'publishStatus': 'public',
                'notifyFollowers': False,
            }
            try:
                response = await client.post(f"{self.api_url}/users/{author_id}/posts", json=request_body)
                response.raise_for_status()
                article = response.json().get("data") or {}
            except Exception as exc:  # noqa: BLE001 - the publish side effect may be ambiguous
                status = crosspost_error_status(exc)
                return CrosspostResult(
                    platform=self.platform, status=status,
                    error=f'Medium publish request failed ({type(exc).__name__}).',
                    retry_after_seconds=crosspost_retry_after_seconds(exc),
                )

        remote_id = str(article.get("id") or "").strip()
        article_url = str(article.get("url") or "").strip()
        if not remote_id:
            return CrosspostResult(platform=self.platform, status='unknown', error='Medium returned no article ID; reconcile before retrying.')
        return CrosspostResult(
            platform=self.platform, status='posted', remote_id=remote_id,
            url=article_url if article_url.startswith('https://') else None,
        )



class TelegraphAdapter:
    """Publish long-form articles with Telegraph's official createPage API."""

    platform = "telegraph"
    max_title = 256
    # 20k characters leaves room below the 64 KiB UTF-8 JSON limit, even for Cyrillic.
    max_body = 20_000

    def _token(self) -> str:
        return str(get_settings().telegraph_access_token or "").strip()

    def configured(self) -> bool:
        return bool(self._token())

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        import json

        token = self._token()
        if not token:
            return CrosspostResult(platform=self.platform, status="skipped")

        from tgfarm.s9_growth.telegraph import API_BASE, USER_AGENT, build_telegraph_content

        try:
            content = build_telegraph_content(str(payload.title or ""), str(payload.text or ""))
        except ValueError as exc:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=redact_sensitive_text(exc, limit=280),
            )

        import httpx

        form = {
            "access_token": token,
            "title": str(payload.title or "").strip(),
            "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
            "return_content": "false",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
                response = await client.post(f"{API_BASE}/createPage", data=form)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return CrosspostResult(
                platform=self.platform,
                status="permanent" if 400 <= code < 500 and code != 429 else "failed",
                error=f"Telegraph API returned HTTP {code}.",
                retry_after_seconds=60 if code == 429 or code >= 500 else None,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Telegraph response was not confirmed after createPage.",
            )
        except (ValueError, TypeError):
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Telegraph returned an unreadable createPage response.",
            )

        if not isinstance(data, dict) or data.get("ok") is not True:
            error = str((data or {}).get("error") or "TELEGRAPH_API_ERROR")[:100]
            retryable = error.startswith("FLOOD_WAIT") or error == "TOO_MANY_REQUESTS"
            return CrosspostResult(
                platform=self.platform,
                status="failed" if retryable else "permanent",
                error=f"Telegraph rejected createPage ({error}).",
                retry_after_seconds=60 if retryable else None,
            )
        page = data.get("result") or {}
        path = str(page.get("path") or "").strip()
        url = str(page.get("url") or "").strip()
        if not path or not re.fullmatch(r"[A-Za-z0-9_-]+", path):
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Telegraph accepted createPage but did not return a valid page path.",
            )
        if not url:
            url = f"https://telegra.ph/{path}"
        if not url.startswith("https://telegra.ph/"):
            return CrosspostResult(
                platform=self.platform,
                status="unknown",
                error="Telegraph returned an unexpected page URL; reconcile before retrying.",
            )
        return CrosspostResult(platform=self.platform, status="posted", remote_id=path, url=url)



ADAPTERS: dict[str, Adapter] = {
    "telegraph": TelegraphAdapter(),
    "medium": MediumAdapter(),
    "pikabu": PikabuAdapter(),
    "douyin": DouyinAdapter(),
    "max": MaxAdapter(),
    "vk": VkAdapter(),
    "x": XAdapter(),
    "reddit": RedditAdapter(),
    "bluesky": BlueskyAdapter(),
    "mastodon": MastodonAdapter(),
    "threads": ThreadsAdapter(),
    "linkedin": LinkedInAdapter(),
    "youtube": YouTubeAdapter(),
    "tiktok": TikTokAdapter(),
    "instagram": InstagramAdapter(),
    "discord": DiscordAdapter(),
    "pinterest": PinterestAdapter(),
    "odnoklassniki": OdnoklassnikiAdapter(),
    "facebook": FacebookPageAdapter(),
    "tumblr": TumblrAdapter(),
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


def primary_video_path(post_id: int) -> str | None:
    """Find the newest existing vertical video attached to a post."""
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        paths = session.execute(
            select(MediaRow.path)
            .where(
                MediaRow.post_id == post_id,
                MediaRow.kind == "video",
            )
            .order_by(MediaRow.id.desc())
        ).scalars().all()

    for raw_path in paths:
        if raw_path and Path(raw_path).is_file():
            return str(Path(raw_path))
    return None


def build_payload(
    post,
    *,
    image_path: str | None = None,
    video_path: str | None = None,
) -> CrosspostPayload:
    """Translate a published post into a per-platform payload."""
    body = post.body.strip()
    link = _first_source_url(post.sources or [])
    if link:
        body = f"{body}\n\nПодробнее: {link}"
    tags = " ".join(f"#{h}" for h in (post.hashtags or [])[:5])
    meta = {"cta": post.cta, "post_public_id": post.public_id}
    if image_path:
        meta["image_path"] = image_path
    if video_path:
        meta["video_path"] = video_path
    return CrosspostPayload(
        platform="",
        title=post.hook.strip(),
        text=f"{body}\n\n{tags}".strip() if tags else body,
        link=link,
        image_path=image_path,
        video_path=video_path,
        meta=meta,
    )


def latest_crosspost(post_public_id: str, platform: str):
    """Most recent state for a post/platform pair."""
    from sqlalchemy import select

    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        return session.execute(
            select(CrosspostLog)
            .where(
                CrosspostLog.post_public_id == post_public_id,
                CrosspostLog.platform == platform,
            )
            .order_by(CrosspostLog.id.desc())
            .limit(1)
        ).scalar_one_or_none()


def already_done(post_public_id: str, platform: str) -> bool:
    """Whether another automatic attempt must wait for operator action or cooldown."""
    row = latest_crosspost(post_public_id, platform)
    if row is None:
        return False
    if row.status in {"posted", "scheduled", "unknown", "publishing", "permanent"}:
        return True
    if row.status == "failed":
        if crosspost_retry_delay_min(row.attempt) is None:
            return True
        retry_at = crosspost_retry_not_before(row)
        return retry_at is not None and datetime.now(UTC) < retry_at
    if row.status == "dry_run":
        # Do not journal the same dry-run forever, but allow a later switch to live.
        return not get_settings().live
    if row.status == "skipped":
        # Missing credentials wait until configuration changes. A readable but
        # unsuitable destination may carry an explicit editorial cooldown.
        adapter = ADAPTERS.get(platform)
        if adapter is None or not adapter.configured():
            return True
        retry_at = getattr(row, "retry_after_at", None)
        if retry_at is None:
            return False
        retry_at = (
            retry_at.replace(tzinfo=UTC)
            if retry_at.tzinfo is None
            else retry_at.astimezone(UTC)
        )
        return datetime.now(UTC) < retry_at
    return False


def begin_crosspost(post_public_id: str, platform: str) -> int | None:
    """Atomically claim a crosspost before network I/O across all farm processes."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError, OperationalError

    from tgfarm.db.models import CrosspostClaim, CrosspostLog
    from tgfarm.db.session import session_scope

    try:
        with session_scope() as session:
            row = session.execute(
                select(CrosspostLog)
                .where(
                    CrosspostLog.post_public_id == post_public_id,
                    CrosspostLog.platform == platform,
                )
                .order_by(CrosspostLog.id.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if row and row.status in {"posted", "scheduled", "unknown", "publishing", "permanent"}:
                return None
            if row and row.status == "failed":
                if crosspost_retry_delay_min(row.attempt) is None:
                    return None
                retry_at = crosspost_retry_not_before(row)
                if retry_at is not None and datetime.now(UTC) < retry_at:
                    return None
            if row and row.status == "skipped":
                adapter = ADAPTERS.get(platform)
                if adapter is None or not adapter.configured():
                    return None
                retry_at = row.retry_after_at
                if retry_at is not None:
                    retry_at = (
                        retry_at.replace(tzinfo=UTC)
                        if retry_at.tzinfo is None
                        else retry_at.astimezone(UTC)
                    )
                    if datetime.now(UTC) < retry_at:
                        return None
            attempt = (row.attempt + 1) if row else 1
            # Unique constraint is the actual cross-process mutex. The log row
            # and claim are committed together, so a crash cannot leave a
            # publishing journal entry without its durable claim or vice versa.
            session.add(CrosspostClaim(post_public_id=post_public_id, platform=platform))
            session.flush()
            session.add(
                CrosspostLog(
                    post_public_id=post_public_id,
                    platform=platform,
                    status="publishing",
                    attempt=attempt,
                )
            )
            session.flush()
            return attempt
    except IntegrityError:
        # Another process already claimed this material/platform pair.
        return None
    except OperationalError:
        # SQLite can report BUSY while another process wins the unique claim.
        # Only treat it as a competing worker if that claim is now visible.
        with session_scope() as session:
            claimed = session.execute(
                select(CrosspostClaim.id)
                .where(
                    CrosspostClaim.post_public_id == post_public_id,
                    CrosspostClaim.platform == platform,
                )
                .limit(1)
            ).scalar_one_or_none()
        if claimed is not None:
            return None
        raise


def log_result(post_public_id: str, result: CrosspostResult, attempt: int = 1) -> None:
    from sqlalchemy import delete

    from tgfarm.db.models import CrosspostClaim, CrosspostLog
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=post_public_id,
                platform=result.platform,
                status=result.status,
                remote_id=result.remote_id,
                url=result.url,
                error=redact_sensitive_text(result.error, limit=300) if result.error else None,
                attempt=attempt,
                retry_after_at=(
                    datetime.now(UTC) + timedelta(seconds=min(86_400, max(0, result.retry_after_seconds)))
                    if result.status in {"failed", "skipped"} and result.retry_after_seconds is not None
                    else None
                ),
            )
        )
        # An unknown outcome remains locked until an operator reconciles it.
        # Definite outcomes release the claim atomically with the journal event.
        if result.status != "unknown":
            session.execute(
                delete(CrosspostClaim).where(
                    CrosspostClaim.post_public_id == post_public_id,
                    CrosspostClaim.platform == result.platform,
                )
            )


__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterNotConfigured",
    "CrosspostPayload",
    "CrosspostResult",
    "MAX_AUTOMATIC_CROSSPOST_ATTEMPTS",
    "RedditAdapter",
    "PikabuAdapter",
    "crosspost_error_status",
    "crosspost_retry_delay_min",
]

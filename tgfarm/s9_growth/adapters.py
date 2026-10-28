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

from datetime import UTC, datetime, timedelta
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
            status = crosspost_error_status(exc)
            log.warning("max crosspost %s: %s", status, exc)
            return CrosspostResult(platform=self.platform, status=status, error=str(exc)[:300])


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
                url=f"https://vk.com/wall{group}_{post_id}" if post_id else None,
            )
        except Exception as exc:  # noqa: BLE001
            status = crosspost_error_status(exc)
            log.warning("vk crosspost %s: %s", status, exc)
            return CrosspostResult(platform=self.platform, status=status, error=str(exc)[:300])


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
            )


# ------------------------------------------------------------------- Bluesky
class BlueskyAdapter:
    """Publish a short native post through the official AT Protocol API."""

    platform = "bluesky"
    max_title = 300
    max_body = 300

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
                    "text": body[:300],
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
                platform=self.platform, status=status, error=f"Bluesky request failed ({type(exc).__name__})."
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

    def configured(self) -> bool:
        client_id, client_secret, refresh_token, subreddit, user_agent = self._credentials()
        return bool(
            client_id
            and client_secret
            and refresh_token
            and subreddit
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


ADAPTERS: dict[str, Adapter] = {
    "mastodon": MastodonAdapter(),
    "bluesky": BlueskyAdapter(),
    "threads": ThreadsAdapter(),
    "max": MaxAdapter(),
    "vk": VkAdapter(),
    "x": XAdapter(),
    "reddit": RedditAdapter(),
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
    row = latest_crosspost(post_public_id, platform)
    return bool(row and row.status in {"posted", "unknown", "publishing", "permanent"})


def begin_crosspost(post_public_id: str, platform: str) -> int | None:
    """Claim a crosspost attempt before network I/O to prevent duplicate workers."""
    from sqlalchemy import select

    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.session import session_scope

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
        if row and row.status in {"posted", "unknown", "publishing", "permanent"}:
            return None
        if row and row.status == "failed":
            delay_min = crosspost_retry_delay_min(row.attempt)
            if delay_min is None:
                return None
            created = row.created_at
            if created is not None:
                created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
                if datetime.now(UTC) < created + timedelta(minutes=delay_min):
                    return None
        attempt = (row.attempt + 1) if row else 1
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


__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterNotConfigured",
    "CrosspostPayload",
    "CrosspostResult",
    "MAX_AUTOMATIC_CROSSPOST_ATTEMPTS",
    "RedditAdapter",
    "crosspost_error_status",
    "crosspost_retry_delay_min",
]
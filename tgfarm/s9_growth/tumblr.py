"""Tumblr NPF text-post adapter."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

import httpx

from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate
from tgfarm.tumblr_oauth import API_ROOT, USER_AGENT, get_valid_tumblr_access_token

MAX_BLOCK_CHARS = 4096
MAX_POST_CHARS = 20_000


def _valid_http_url(value: str) -> str:
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return value


def _split_block(text: str) -> list[str]:
    chunks: list[str] = []
    remaining = str(text or "").strip()
    while remaining:
        if len(remaining) <= MAX_BLOCK_CHARS:
            chunks.append(remaining)
            break
        cut = remaining.rfind(" ", 0, MAX_BLOCK_CHARS + 1)
        if cut < MAX_BLOCK_CHARS // 2:
            cut = MAX_BLOCK_CHARS
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return chunks


def build_npf_content(title: str, body: str) -> list[dict[str, str]]:
    """Build bounded NPF text blocks; no unsupported HTML is passed through."""
    raw_parts = [str(title or "").strip()]
    raw_parts.extend(part.strip() for part in re.split(r"\n\s*\n", str(body or "").strip()) if part.strip())
    blocks: list[dict[str, str]] = []
    total = 0
    for part in raw_parts:
        if not part:
            continue
        remaining = truncate(part, MAX_POST_CHARS - total)
        if not remaining:
            break
        for chunk in _split_block(remaining):
            if total + len(chunk) > MAX_POST_CHARS:
                break
            blocks.append({"type": "text", "text": chunk})
            total += len(chunk)
        if total >= MAX_POST_CHARS or len(blocks) >= 100:
            break
    return blocks


def _tags(payload: CrosspostPayload) -> list[str]:
    candidates = list(payload.hashtags or [])
    if not candidates:
        candidates = re.findall(r"(?<!\w)#([\w\d_]{1,50})", f"{payload.title}\n{payload.text}", flags=re.UNICODE)
    tags: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        tag = re.sub(r"[^\w]", "", str(value or "").strip().lstrip("#"), flags=re.UNICODE)[:30]
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        tags.append(tag)
        if len(tags) >= 30:
            break
    return tags


class TumblrAdapter:
    """Publish native NPF text posts to one OAuth-authorized Tumblr blog."""

    platform = "tumblr"
    max_title = 200
    max_body = MAX_POST_CHARS

    def configured(self) -> bool:
        settings = get_settings()
        return bool(
            str(getattr(settings, "tumblr_client_id", "") or "").strip()
            and str(getattr(settings, "tumblr_client_secret", "") or "").strip()
            and str(getattr(settings, "tumblr_blog_identifier", "") or "").strip()
            and (
                str(getattr(settings, "tumblr_access_token", "") or "").strip()
                or str(getattr(settings, "tumblr_refresh_token", "") or "").strip()
            )
        )

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status="skipped")
        from tgfarm.s9_growth.adapters import crosspost_error_status, crosspost_retry_after_seconds

        settings = get_settings()
        blog = str(settings.tumblr_blog_identifier or "").strip()
        title = truncate(str(payload.title or "").strip(), self.max_title)
        body = str(payload.text or "").strip()
        source_url = _valid_http_url(payload.link)
        if source_url:
            body = re.sub(rf"\n*\s*Подробнее:\s*{re.escape(source_url)}\s*$", "", body, flags=re.IGNORECASE).strip()
        lines = body.splitlines()
        if lines and all(re.fullmatch(r"#[0-9a-zа-яё_]+", item.strip(), re.I) for item in lines[-1].split()):
            body = "\n".join(lines[:-1]).strip()
        body = truncate(body, self.max_body)
        content = build_npf_content(title, body)
        if not content:
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error="Tumblr NPF requires a non-empty title or text body.",
            )
        try:
            token = await get_valid_tumblr_access_token()
        except Exception as exc:  # noqa: BLE001 - no publication request has been sent yet
            return CrosspostResult(
                platform=self.platform,
                status="permanent",
                error=redact_sensitive_text(exc, limit=300) or "Tumblr OAuth could not be refreshed.",
            )
        try:
            async with httpx.AsyncClient(
                timeout=35.0,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            ) as client:
                response = await client.post(
                    f"{API_ROOT}/blog/{quote(blog, safe='')}/posts",
                    json={
                        "content": content,
                        "state": "published",
                        "tags": ", ".join(_tags(payload)),
                        **({"source_url": source_url} if source_url else {}),
                    },
                )
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError:
                    result = {}
            meta = result.get("meta") or {}
            if isinstance(meta, dict) and int(meta.get("status") or 201) >= 400:
                return CrosspostResult(
                    platform=self.platform,
                    status="permanent",
                    error="Tumblr отклонил NPF-публикацию; проверь права записи и формат поста.",
                )
            data = result.get("response") or {}
            post_id = str(data.get("id_string") or data.get("id") or "").strip()
            if not post_id:
                return CrosspostResult(
                    platform=self.platform,
                    status="unknown",
                    error="Tumblr принял запрос, но не вернул ID публикации. Проверь блог перед повтором.",
                )
            blog_url = _valid_http_url(str(settings.tumblr_blog_url or ""))
            permalink = f"{blog_url.rstrip('/')}/post/{post_id}" if blog_url else None
            return CrosspostResult(
                platform=self.platform,
                status="posted",
                remote_id=post_id,
                url=permalink,
            )
        except Exception as exc:  # noqa: BLE001 - unknown write outcome must not be retried blindly
            status = crosspost_error_status(exc)
            return CrosspostResult(
                platform=self.platform,
                status=status,
                error=redact_sensitive_text(exc, limit=300) or "Tumblr publication failed.",
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )

"""S9 — crossposting orchestration and the growth engine's safe half.

This module owns *only* the parts of growth that are legitimate and safe:
crossposting through official APIs, SEO article generation, and crosspost logs.

Deliberately absent, and enforced by `assert_safe_growth()`: mass-adding users to
groups, unsolicited DMs, automated comment/reply posting from owned accounts,
and fake engagement. Those are spam under the Telegram User Agreement and carry
no realistic upside — see RUNBOOK for the measured comparison.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, ensure_utc, to_thread
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.schemas import CrosspostResult
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.s9_growth.adapters import (
    ADAPTERS,
    already_done,
    begin_crosspost,
    build_payload,
    latest_crosspost,
    log_result,
    primary_image_path,
    primary_video_path,
)

log = get_logger(__name__)

# Growth primitives the farm will never execute. Kept as code, not just prose,
# so any attempt to wire one in fails review.
FORBIDDEN_GROWTH_PRIMITIVES = frozenset(
    {
        "mass_invite",
        "scrape_members",
        "unsolicited_dm",
        "auto_comment",
        "auto_react_from_owned_accounts",
        "buy_subscribers",
        "fake_reactions",
        "userbot_posting",
    }
)


class UnsafeGrowthPrimitive(RuntimeError):
    """Raised when something tries to register a forbidden growth primitive."""


def assert_safe_growth(primitive: str) -> None:
    if primitive in FORBIDDEN_GROWTH_PRIMITIVES:
        raise UnsafeGrowthPrimitive(
            f"{primitive!r} is spam under the Telegram User Agreement and is not implemented"
        )


# ---------------------------------------------------------------- crossposting
def _crosspost_sampled(public_id: str, sample_rate: float, rng: random.Random | None = None) -> bool:
    """Stable per-post sampling for production, injectable RNG for deterministic tests.

    Random re-sampling on every scheduler tick eventually selects almost every
    post, even with a configured sample rate below 1. A hash bucket makes the
    decision stable across restarts and cycles while still allowing sample_rate
    to expand or shrink the eligible cohort.
    """
    try:
        rate = float(sample_rate)
    except (TypeError, ValueError):
        rate = 0.0
    if not math.isfinite(rate):
        rate = 0.0
    rate = min(1.0, max(0.0, rate))
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    if rng is not None:
        return rng.random() < rate
    digest = hashlib.sha256(public_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], byteorder="big") / (2**64)
    return bucket < rate


def _pending_crossposts(
    sample_rate: float,
    window_minutes: int = 720,
    rng: random.Random | None = None,
    platforms: list[str] | None = None,
) -> list[tuple[int, str]]:
    """Return stable sampled fresh posts plus durable retries of older deliveries.

    Fresh items are sampled once by public ID. Once a post has entered the
    crosspost journal, retryable states remain discoverable after a long outage
    instead of falling out of the recent-publication window forever.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
    target_platforms = list(platforms if platforms is not None else ADAPTERS.keys())
    retryable_public_ids = None
    if target_platforms:
        from tgfarm.db.models import CrosspostLog

        latest_crosspost_ids = (
            select(func.max(CrosspostLog.id).label("latest_id"))
            .where(CrosspostLog.platform.in_(target_platforms))
            .group_by(CrosspostLog.post_public_id, CrosspostLog.platform)
            .subquery()
        )
        retryable_public_ids = (
            select(CrosspostLog.post_public_id)
            .join(latest_crosspost_ids, CrosspostLog.id == latest_crosspost_ids.c.latest_id)
            .where(CrosspostLog.status.in_(("failed", "retry_authorized", "skipped", "dry_run")))
        )

    with session_scope() as session:
        where_retry = (
            PostRow.public_id.in_(retryable_public_ids)
            if retryable_public_ids is not None
            else False
        )
        rows = session.execute(
            select(PostRow)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(
                PostRow.status == "published",
                or_(PostRow.published_at >= cutoff, where_retry),
                or_(ChannelRow.platform.is_(None), ChannelRow.platform == "telegram"),
                ChannelRow.enabled.is_(True),
            )
            .order_by(PostRow.published_at.asc(), PostRow.id.asc())
        ).scalars().all()

        candidates: list[tuple[int, str]] = []
        for row in rows:
            if row.public_id is None or row.published_at is None:
                continue
            published_at = ensure_utc(row.published_at)
            # A persisted retry/skip/dry-run is already inside the sampled cohort;
            # do not silently drop it if sample_rate changes or the farm was down.
            if published_at < cutoff:
                candidates.append((row.id, row.public_id))
            elif _crosspost_sampled(row.public_id, sample_rate, rng):
                candidates.append((row.id, row.public_id))
        return candidates


def _load_post(post_pk: int) -> PostRow | None:
    with session_scope() as session:
        return session.get(PostRow, post_pk)


async def crosspost_post(post_pk: int, platforms: list[str]) -> list[CrosspostResult]:
    from tgfarm.platforms import is_platform_enabled

    # Defense in depth: neither a manual retry nor a future caller may bypass
    # the global switch from the UI.
    platforms = [
        name for name in platforms
        if name in ADAPTERS and is_platform_enabled(name)
    ]
    if not platforms:
        return []

    settings = get_settings()
    post = await to_thread(_load_post, post_pk)
    if post is None:
        return []

    image_path = (
        await to_thread(primary_image_path, post_pk)
        if "vk" in platforms
        else None
    )
    video_targets = {"youtube", "tiktok", "vk_clips"}
    video_path = (
        await to_thread(primary_video_path, post_pk)
        if any(name in video_targets for name in platforms)
        else None
    )
    results: list[CrosspostResult] = []
    for name in platforms:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            log.warning("unknown crosspost platform %s", name)
            continue
        if await to_thread(already_done, post.public_id, name):
            continue
        if not adapter.configured():
            # Missing credentials are a durable state, not a new failure event
            # on every scheduler tick. Keep one skipped row until the adapter
            # becomes configured; then the normal claim path can resume.
            previous = await to_thread(latest_crosspost, post.public_id, name)
            results.append(CrosspostResult(platform=name, status="skipped"))
            if previous is None or previous.status != "skipped":
                await to_thread(log_result, post.public_id, results[-1])
            continue

        attempt = await to_thread(begin_crosspost, post.public_id, name)
        if attempt is None:
            continue
        payload = build_payload(
            post,
            image_path=image_path if name == "vk" else None,
            video_path=video_path if name in video_targets else None,
        )
        payload.platform = name
        if settings.live:
            try:
                result = await adapter.publish(payload)
            except Exception as exc:  # noqa: BLE001 - leave a durable state after adapter faults
                from tgfarm.s9_growth.adapters import ambiguous_crosspost_error, crosspost_retry_after_seconds

                result = CrosspostResult(
                    platform=name,
                    status="unknown" if ambiguous_crosspost_error(exc) else "failed",
                    error=redact_sensitive_text(exc, limit=300),
                    retry_after_seconds=crosspost_retry_after_seconds(exc),
                )
        else:
            result = CrosspostResult(
                platform=name,
                status="dry_run",
                remote_id=uuid.uuid4().hex[:8],
            )
            log.info("[dry-run] crosspost %s -> %s", post.public_id, name)
        await to_thread(log_result, post.public_id, result, attempt)
        results.append(result)
    return results


async def _run_crossposts_once(limit: int = 20) -> dict[str, int]:
    config = get_config()
    get_settings()
    cfg = config.schedule.crosspost
    if not cfg.get("enabled", True):
        return {"posted": 0, "skipped": 0}

    from tgfarm.platforms import enabled_crosspost_platforms

    platforms = enabled_crosspost_platforms()
    sample_rate = float(cfg.get("sample_rate", 0.8))
    delay_min = int(cfg.get("delay_min", 25))

    with StageTimer("s9_crosspost") as timer:
        candidates = await to_thread(_pending_crossposts, sample_rate, 720, None, platforms)
        # only cross-post posts old enough to have settled in the feed
        cutoff = datetime.now(UTC) - timedelta(minutes=delay_min)
        due: list[tuple[int, list[str]]] = []
        target_limit = max(1, int(limit))
        for post_pk, public_id in candidates:
            post = await to_thread(_load_post, post_pk)
            published_at = ensure_utc(post.published_at) if post else None
            if not post or not published_at or published_at > cutoff:
                continue
            # Don't consume batch slots on posts that have already reached every
            # enabled target. Otherwise old successful rows can starve new posts.
            pending_platforms = [
                name for name in platforms
                if not await to_thread(already_done, public_id, name)
            ]
            if not pending_platforms:
                continue
            due.append((post_pk, pending_platforms))
            if len(due) >= target_limit:
                break

        posted = skipped = failed = permanent_failure = unknown = 0
        for post_pk, pending_platforms in due:
            for result in await crosspost_post(post_pk, pending_platforms):
                if result.status == "posted":
                    posted += 1
                elif result.status == "skipped":
                    skipped += 1
                elif result.status == "failed":
                    failed += 1
                elif result.status == "permanent":
                    permanent_failure += 1
                elif result.status == "unknown":
                    unknown += 1

        timer.items_in = len(candidates)
        timer.items_out = posted
    log.info(
        "s9: crosspost posted=%d skipped=%d failed=%d permanent_failure=%d unknown=%d",
        posted,
        skipped,
        failed,
        permanent_failure,
        unknown,
    )
    return {
        "posted": posted,
        "skipped": skipped,
        "failed": failed,
        "permanent_failure": permanent_failure,
        "unknown": unknown,
        "considered": len(due),
    }


# Serialize the background crosspost tick against the full farm cycle.
_active_crosspost_task: asyncio.Task | None = None


async def run_crossposts(limit: int = 20) -> dict[str, int]:
    """Run one crosspost batch at a time, even when the full cycle and retry tick overlap."""
    global _active_crosspost_task

    current = asyncio.current_task()
    active = _active_crosspost_task
    if active is not None and active is not current and not active.done():
        log.info("s9: crosspost batch skipped because another batch is still running")
        return {
            "posted": 0,
            "skipped": 0,
            "failed": 0,
            "permanent_failure": 0,
            "unknown": 0,
            "considered": 0,
            "skipped_concurrent": 1,
        }

    if current is not None:
        _active_crosspost_task = current
    try:
        return await _run_crossposts_once(limit)
    finally:
        if current is not None and _active_crosspost_task is current:
            _active_crosspost_task = None


# --------------------------------------------------------------- SEO articles
SEO_SYSTEM = """Ты — SEO-редактор. Из телеграм-поста напиши статью для vc.ru / Дзен.
Правила: 1200–2000 слов, подзаголовки, ноль воды, ноль выдумки — только то, что
есть в исходном материале. В конце — естественный переход к Telegram-каналу.
Ответ — ТОЛЬКО JSON: {"title": "...", "slug": "...", "body": "...", "excerpt": "..."}"""

SEO_OFFLINE = {
    "title": "",
    "slug": "",
    "body": "",
    "excerpt": "",
}


async def seo_article(post_pk: int) -> dict | None:
    """Expand one post into a long-form article for external search traffic."""
    from tgfarm.llm.client import get_llm

    post = await to_thread(_load_post, post_pk)
    if post is None:
        return None

    links = "\n".join(f"- {s.get('title')} :: {s.get('url')}" for s in (post.sources or [])[:3])
    prompt = (
        f"ПОСТ:\n{post.hook}\n{post.body}\n\nИСТОЧНИКИ:\n{links}\n\n"
        "Напиши статью. Обязательно упомяни канал в конце одним абзацем."
    )
    payload, response = await get_llm().complete_json(
        SEO_SYSTEM, prompt, max_tokens=4000, offline_fallback=SEO_OFFLINE
    )
    if response.provider == "offline" or not payload.get("body"):
        return None
    return payload


async def run(limit: int = 20) -> dict[str, int]:
    return await run_crossposts(limit=limit)


async def gather_all() -> asyncio.Task:  # pragma: no cover - convenience
    return asyncio.create_task(run_crossposts())


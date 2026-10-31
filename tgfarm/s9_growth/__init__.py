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
import uuid

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.schemas import CrosspostResult
from tgfarm.core.settings import get_settings
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
def _crosspost_sampled(
    public_id: str,
    sample_rate: float,
    rng=None,
) -> bool:
    """Legacy stable sampler retained for compatibility and regression tests.

    The live S9 scheduler no longer calls this lottery: current publishing uses
    the deterministic post/platform editorial planner below. Keeping the pure
    helper avoids breaking old integrations and lets us verify its historical
    semantics independently.
    """
    import hashlib
    import math

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
    digest = hashlib.sha256(str(public_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], byteorder="big") / (2**64)
    return bucket < rate


def _pending_crossposts(
    sample_rate: float,
    window_minutes: int = 720,
    rng=None,
    platforms: list[str] | None = None,
) -> list[tuple[int, str]]:
    """Legacy candidate reader retained for compatibility; unused by live S9.

    Only Telegram-origin posts are eligible, and older retryable delivery
    records remain discoverable after a long outage.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, or_, select

    from tgfarm.core.run import ensure_utc
    from tgfarm.db.models import Channel as ChannelRow
    from tgfarm.db.models import CrosspostLog, Post as PostRow

    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
    target_platforms = list(platforms if platforms is not None else ADAPTERS.keys())
    retryable_public_ids = None
    if target_platforms:
        latest_ids = (
            select(func.max(CrosspostLog.id).label("latest_id"))
            .where(CrosspostLog.platform.in_(target_platforms))
            .group_by(CrosspostLog.post_public_id, CrosspostLog.platform)
            .subquery()
        )
        retryable_public_ids = (
            select(CrosspostLog.post_public_id)
            .join(latest_ids, CrosspostLog.id == latest_ids.c.latest_id)
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
        if any(name in {"vk", "instagram", "pinterest"} for name in platforms)
        else None
    )
    video_targets = {"youtube", "tiktok", "vk_clips", "instagram", "pinterest"}
    video_render_error = None
    video_requested = any(name in video_targets for name in platforms)
    video_path = (
        await to_thread(primary_video_path, post_pk)
        if video_requested
        else None
    )
    # Once the operator enables a live video publisher, produce an original
    # text-led short on demand. Dry-run never spends time rendering or invoking
    # FFmpeg. If rendering fails, each video target receives a clear permanent
    # result rather than silently publishing text as if it were a video.
    if video_requested and settings.live and not video_path:
        # Don't generate large files for a platform whose OAuth/credentials
        # haven't been configured yet. The target will remain a skipped delivery
        # until its connection is ready; rendering starts only when upload could run.
        video_adapter_ready = any(
            ADAPTERS.get(name) is not None and ADAPTERS[name].configured()
            for name in platforms
            if name in video_targets
        )
        if video_adapter_ready:
            try:
                from tgfarm.s11_video.renderer import render_post_short

                artifact = await to_thread(render_post_short, post.public_id)
                video_path = artifact.path
            except Exception as exc:  # noqa: BLE001 - fail the media stage transparently
                video_render_error = redact_sensitive_text(
                    f"{type(exc).__name__}: {exc}", limit=240
                ) or "video rendering failed"
                log.error("video render failed for post %s (%s)", post.public_id, type(exc).__name__)
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

        reddit_submission = None
        if name == "reddit" and settings.live:
            # Pick exactly one destination after reading its current community
            # rules and adapting the post. No fixed subreddit is required.
            from tgfarm.s9_growth.reddit_scout import (
                RedditScoutError,
                build_automatic_submission,
            )

            try:
                reddit_submission = await build_automatic_submission(post.public_id)
            except RedditScoutError as exc:
                reddit_submission = {
                    "ok": False,
                    "reason": f"Reddit community selection failed: {str(exc)[:220]}",
                }
            if not reddit_submission.get("ok"):
                previous = await to_thread(latest_crosspost, post.public_id, name)
                skipped = CrosspostResult(
                    platform=name,
                    status="skipped",
                    error=redact_sensitive_text(
                        reddit_submission.get("reason") or "No suitable Reddit community was found.",
                        limit=300,
                    ),
                    retry_after_seconds=6 * 60 * 60,
                )
                results.append(skipped)
                if previous is None or previous.status != "skipped":
                    await to_thread(log_result, post.public_id, skipped)
                continue

        attempt = await to_thread(begin_crosspost, post.public_id, name)
        if attempt is None:
            continue
        payload = build_payload(
            post,
            image_path=image_path if name in {"vk", "instagram", "pinterest"} else None,
            video_path=video_path if name in video_targets else None,
        )
        from tgfarm.distribution import adapt_payload_for_platform

        payload = adapt_payload_for_platform(payload, name)
        payload.platform = name
        if name == "reddit" and reddit_submission:
            payload.title = reddit_submission["title"]
            payload.text = reddit_submission["body"]
            payload.meta = {
                **(payload.meta or {}),
                "subreddit": reddit_submission["subreddit"],
                "reddit_kind": reddit_submission.get("reddit_kind", "self"),
            }
        if name in video_targets and video_render_error:
            payload.meta = {
                **(payload.meta or {}),
                "video_render_error": video_render_error,
            }
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
    """Execute the deterministic editorial distribution plan.

    One best-fit post per automatic destination is selected per tick. The plan
    already accounts for each platform's 24-hour budget, minimum gap, enabled
    switch, configured credentials, age, topic fit and near-duplicate history.
    """
    settings = get_settings()
    empty = {
        "posted": 0,
        "skipped": 0,
        "failed": 0,
        "permanent_failure": 0,
        "unknown": 0,
        "considered": 0,
    }
    if settings.mode == "off":
        return empty

    from tgfarm.distribution import build_distribution_plan

    with StageTimer("s9_crosspost") as timer:
        plan = await to_thread(
            build_distribution_plan,
            max_selected_per_platform=1,
        )
        # Group destinations by source post so one DB load/heartbeat/claim
        # handles multiple independent destination claims for the same post.
        by_post: dict[int, list[tuple[str, float]]] = {}
        for item in plan.get("platforms", []):
            for chosen in item.get("selected", []):
                by_post.setdefault(int(chosen["post_id"]), []).append(
                    (str(item["platform"]), float(chosen.get("score") or 0))
                )

        due = sorted(
            by_post.items(),
            key=lambda entry: (-max(score for _name, score in entry[1]), entry[0]),
        )
        max_targets = max(0, int(limit))
        posted = skipped = failed = permanent_failure = unknown = considered = 0
        sent_targets = 0
        for post_pk, targets in due:
            if sent_targets >= max_targets:
                break
            # The UI toggle is checked again inside crosspost_post immediately
            # before the adapter call, protecting against a switch-off race.
            available = [
                name for name, _score in sorted(targets, key=lambda value: -value[1])
                if name in ADAPTERS
            ][: max_targets - sent_targets]
            if not available:
                continue
            considered += len(available)
            outcomes = await crosspost_post(post_pk, available)
            sent_targets += len(outcomes)
            for result in outcomes:
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

        timer.items_in = sum(
            int(item.get("eligible_count") or 0)
            for item in plan.get("platforms", [])
        )
        timer.items_out = posted

    log.info(
        "s9 editorial distribution: selected=%d posted=%d skipped=%d failed=%d permanent=%d unknown=%d",
        considered,
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
        "considered": considered,
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


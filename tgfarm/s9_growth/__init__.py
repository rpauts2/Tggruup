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
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, ensure_utc, to_thread
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
    log_result,
    primary_image_path,
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
def _pending_crossposts(
    sample_rate: float,
    window_minutes: int = 720,
    rng: random.Random | None = None,
) -> list[tuple[int, str]]:
    """Recently published Telegram posts that have not gone out to a platform.

    Dzen is a primary surface with its own generated posts, so copying Telegram
    posts there would duplicate the same story under a second byline.
    """
    rng = rng or random.Random()
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
    with session_scope() as session:
        rows = session.execute(
            select(PostRow)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(
                PostRow.status == "published",
                PostRow.published_at >= cutoff,
                or_(ChannelRow.platform.is_(None), ChannelRow.platform == "telegram"),
            )
        ).scalars().all()
        return [
            (row.id, row.public_id)
            for row in rows
            if row.public_id is not None and rng.random() < sample_rate
        ]


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
    results: list[CrosspostResult] = []
    for name in platforms:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            log.warning("unknown crosspost platform %s", name)
            continue
        if await to_thread(already_done, post.public_id, name):
            continue
        if not adapter.configured():
            results.append(CrosspostResult(platform=name, status="skipped"))
            await to_thread(log_result, post.public_id, results[-1])
            continue

        attempt = await to_thread(begin_crosspost, post.public_id, name)
        if attempt is None:
            continue
        payload = build_payload(post, image_path=image_path if name == "vk" else None)
        payload.platform = name
        if settings.live:
            try:
                result = await adapter.publish(payload)
            except Exception as exc:  # noqa: BLE001 - leave a durable state after adapter faults
                from tgfarm.s9_growth.adapters import ambiguous_crosspost_error

                result = CrosspostResult(
                    platform=name,
                    status="unknown" if ambiguous_crosspost_error(exc) else "failed",
                    error=str(exc)[:300],
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


async def run_crossposts(limit: int = 20) -> dict[str, int]:
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
        candidates = await to_thread(_pending_crossposts, sample_rate)
        # only cross-post posts old enough to have settled in the feed
        cutoff = datetime.now(UTC) - timedelta(minutes=delay_min)
        due = []
        for post_pk, _public_id in candidates[:limit]:
            post = await to_thread(_load_post, post_pk)
            published_at = ensure_utc(post.published_at) if post else None
            if post and published_at and published_at <= cutoff:
                due.append(post_pk)

        posted = skipped = failed = permanent_failure = unknown = 0
        for post_pk in due:
            for result in await crosspost_post(post_pk, platforms):
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


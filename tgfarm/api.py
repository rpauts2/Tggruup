"""FastAPI control plane + dashboard.

Serves the web UI at `/` and a read-mostly JSON API under `/api`. Blocking DB work
goes through `io()` so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from tgfarm import pipeline
from tgfarm.config import get_config
from tgfarm.core import apiauth
from tgfarm.core.apiauth import ensure_token, is_authorised, is_public_path
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.core.security import redact_sensitive_text
from tgfarm.db.bootstrap import ensure_bootstrapped
from tgfarm.db.models import (
    Channel,
    ChannelStat,
    Cluster,
    CrosspostLog,
    Media,
    OperatorLog,
    PipelineRun,
    Post,
    PostStat,
    RedditMetricSnapshot,
    RawItem,
    Source,
)
from tgfarm.db.session import session_scope

log = get_logger(__name__)

UI_DIR = Path(__file__).parent / "ui"
T = TypeVar("T")


async def io(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking DB/IO callable off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


class PublishRequest(BaseModel):
    post_public_ids: list[str]


class RejectRequest(BaseModel):
    post_public_ids: list[str]
    reason: str = "manual"


class PublishReconcileRequest(BaseModel):
    action: Literal["confirm_published", "retry_after_check"]
    confirm_not_published: bool = False


class CrosspostReconcileRequest(BaseModel):
    action: Literal["confirm_posted", "retry_after_check"]
    confirm_not_published: bool = False


class PlatformToggleRequest(BaseModel):
    enabled: bool


class PlatformPlanRequest(BaseModel):
    planned: bool


class PlatformCredentialsRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class MastodonOAuthStartRequest(BaseModel):
    instance: str = Field(min_length=1, max_length=300)

class RedditScoutRequest(BaseModel):
    query: str = Field(min_length=2, max_length=120)
    post_public_id: str | None = Field(default=None, max_length=120)
    title: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=5000)
    limit: int = Field(default=6, ge=1, le=8)


class RedditTailorRequest(BaseModel):
    post_public_id: str = Field(min_length=3, max_length=120)
    subreddit: str = Field(min_length=2, max_length=21)


class RedditPublishRequest(BaseModel):
    post_public_id: str = Field(min_length=3, max_length=120)
    subreddit: str = Field(min_length=2, max_length=21)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=40000)
    confirm: bool = False


class RedditMetricsRefreshRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=100)


class PolishRequest(BaseModel):
    """One-direction rewrite of an existing draft from the panel."""

    mode: str = "shorter"  # shorter | funnier | stricter


class DraftUpdate(BaseModel):
    """A manual edit before publishing. Anything omitted is left alone."""

    hook: str | None = None
    body: str | None = None
    cta: str | None = None
    hashtags: list[str] | None = None
    angle: str | None = None


# --------------------------------------------------------------- data helpers
def _warm_index() -> int:
    from tgfarm.db.vector import sync_from_db

    with session_scope() as session:
        return sync_from_db(session, RawItem)


def _capabilities() -> dict:
    from tgfarm.s11_video import guard_summary

    settings = get_settings()
    return {
        "llm_providers": settings.provider_chain,
        "zen_token": bool(settings.opencode_zen_token),
        "zen_model": settings.opencode_zen_model,
        "embedder": settings.embedding_provider,
        "images": settings.images_configured,
        "video_enabled": settings.video_enabled,
        "video": guard_summary(),
    }


def _status_payload() -> dict:
    """Everything the dashboard's KPI row needs, in one pass."""
    base = pipeline.status()
    with session_scope() as session:
        by_status = dict(
            session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
        )
        total_drafts = sum(by_status.values()) or 1
        ready = session.execute(
            select(func.count()).select_from(Cluster).where(Cluster.status == "ready")
        ).scalar_one()
        covers = session.execute(
            select(func.count()).select_from(Media).where(Media.kind != "video")
        ).scalar_one()
        videos = session.execute(
            select(func.count()).select_from(Media).where(Media.kind == "video")
        ).scalar_one()
        crossposts = session.execute(
            select(func.count()).select_from(CrosspostLog).where(CrosspostLog.status == "posted")
        ).scalar_one()
        avg_er = session.execute(
            select(func.avg(ChannelStat.err)).select_from(ChannelStat)
        ).scalar_one()

    base["kpis"] = {
        "sources": base.get("sources", 0),
        "sources_total": len(get_config().sources),
        "raw_items": base.get("raw_items", 0),
        "clusters": base.get("clusters", 0),
        "ready_clusters": int(ready),
        "posts": base.get("posts", 0),
        "gated": int(by_status.get("gated", 0)),
        "scheduled": int(by_status.get("scheduled", 0)),
        "published": int(by_status.get("published", 0)),
        "rejected": int(by_status.get("rejected", 0)),
        "reject_rate": round(by_status.get("rejected", 0) / total_drafts, 3),
        "avg_er": round(float(avg_er or 0.0), 4),
        "covers": int(covers),
        "videos": int(videos),
        "crossposts": int(crossposts),
    }
    base["capabilities"] = _capabilities()
    base["alerts"] = pipeline.collect_alerts()
    return base


def _trends(limit: int) -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            select(Cluster)
            .where(Cluster.status.in_(("forming", "ready")))
            .order_by(Cluster.heat.desc())
            .limit(limit)
        ).scalars().all()
        out = []
        for row in rows:
            items = session.execute(
                select(RawItem).where(RawItem.cluster_id == row.id)
            ).scalars().all()
            out.append(
                {
                    "id": row.public_id,
                    "label": row.label,
                    "niche": row.niche,
                    "heat": row.heat,
                    "status": row.status,
                    "items": len(items),
                    "outlets": sorted({i.source_id for i in items}),
                }
            )
        return out


def _sources() -> list[dict]:
    from tgfarm.s1_sources import effective_interval

    with session_scope() as session:
        rows = session.execute(select(Source)).scalars().all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "kind": r.kind,
                "enabled": r.enabled,
                "weight": r.weight,
                "last_fetched_at": r.last_fetched_at,
                "last_status": r.last_status,
                "failures": r.failures,
                "last_yield": r.last_yield,
                "idle_streak": r.idle_streak,
                "cooldown_until": r.cooldown_until,
                "cooldown_active": bool(
                    r.cooldown_until
                    and (
                        r.cooldown_until.replace(tzinfo=UTC)
                        if r.cooldown_until.tzinfo is None
                        else r.cooldown_until.astimezone(UTC)
                    ) > datetime.now(UTC)
                ),
                "effective_interval_s": effective_interval(r),
                "last_error": (r.last_error or "")[:200] or None,
            }
            for r in rows
        ]


def _post_view(session, row: Post, *, detail: bool) -> dict:
    """Shared projection for the list and the detail endpoints."""
    channel = session.get(Channel, row.channel_id)
    media = session.execute(
        select(Media)
        .where(Media.post_id == row.id, Media.is_primary.is_(True))
        .order_by(Media.created_at.asc())
    ).scalars().all()
    stat = session.execute(
        select(PostStat)
        .where(PostStat.post_id == row.id)
        .order_by(PostStat.collected_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    er = 0.0
    if stat and stat.views:
        er = (stat.reactions + stat.comments + stat.forwards) / stat.views
    ctr = (
        round(min(1.0, stat.opens / stat.impressions), 4)
        if stat and stat.impressions
        else 0.0
    )
    # prefer a media row whose file is actually on disk
    alive = [m for m in media if Path(m.path).exists()]
    cover = next((m for m in alive if m.kind != "video"), None) or next(
        (m for m in media if m.kind != "video"), None
    )
    video = next((m for m in alive if m.kind == "video"), None) or next(
        (m for m in media if m.kind == "video"), None
    )

    platform = ((channel.platform if channel else None) or "telegram").lower()
    payload = {
        "id": row.public_id,
        "channel": row.channel_id,
        "channel_title": channel.title if channel else row.channel_id,
        "platform": platform,
        "angle": row.angle,
        "post_format": row.post_format,
        "status": row.status,
        "chars": row.char_len,
        "quality": row.quality,
        "excerpt": row.body[:220],
        "scheduled_at": row.scheduled_at,
        "published_at": row.published_at,
        "created_at": row.created_at,
        "hook": row.hook[:200],
        "reject_reason": row.reject_reason,
        "cover": {"id": cover.sha} if cover and cover.sha else None,
        "video": {"id": video.sha} if video and video.sha else None,
        "er": round(er, 4),
        "ctr": ctr,
        "impressions": stat.impressions if stat else 0,
        "opens": stat.opens if stat else 0,
        "views": stat.views if stat else 0,
        "tg_message_id": row.tg_message_id,
        "edited_at": row.edited_at,
    }
    if detail:
        external = session.execute(
            select(CrosspostLog)
            .where(
                CrosspostLog.post_public_id == row.public_id,
                CrosspostLog.platform == platform,
                CrosspostLog.status.in_(("posted", "dry_run")),
            )
            .order_by(CrosspostLog.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        payload |= {
            "text": f"{row.hook}\n\n{row.body}",
            "body": row.body,
            "cta": row.cta,
            "sources": row.sources,
            "claims": row.claims,
            "hashtags": row.hashtags,
            "format_reason": row.format_reason,
            "external_url": external.url if external else None,
            "remote_id": external.remote_id if external else None,
            "external_platform": external.platform if external else None,
            "media": [
                {"kind": m.kind, "path": m.path, "sha": m.sha, "width": m.width, "height": m.height}
                for m in media
            ],
        }
    return payload


def _posts(
    status_filter: str | None,
    channel_id: str | None,
    limit: int,
    hook_style: str | None = None,
    variant: str | None = None,
) -> list[dict]:
    with session_scope() as session:
        stmt = select(Post)
        if status_filter:
            stmt = stmt.where(Post.status == status_filter)
        if channel_id:
            stmt = stmt.where(Post.channel_id == channel_id)
        if hook_style:
            stmt = stmt.where(Post.hook_style == hook_style)
        if variant:
            stmt = stmt.where(Post.variant == variant)
        rows = session.execute(stmt.order_by(Post.created_at.desc()).limit(limit)).scalars().all()
        return [_post_view(session, r, detail=False) for r in rows]


def _post_detail(public_id: str) -> dict:
    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "post not found")
        return _post_view(session, row, detail=True)


def _post_trace(public_id: str) -> dict:
    """The full life of one post as an ordered event list.

    "Why did it not go out?" is the most common operator question; answering it
    from logs means reading three tables by hand. This assembles the answer.
    """
    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.models import PostStat

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "post not found")

        events: list[dict] = []

        def add(at, kind: str, detail: str) -> None:
            events.append(
                {
                    "at": at.isoformat(timespec="seconds") if at else None,
                    "kind": kind,
                    "detail": detail,
                }
            )

        quality = row.quality or {}
        add(
            row.created_at,
            "draft",
            f"формат={row.post_format} angle={row.angle} hook_style={row.hook_style or '—'} "
            f"variant={row.variant} provider={row.generation_provider}",
        )
        if quality:
            add(
                row.created_at,
                "quality",
                f"gate={quality.get('gate') or 'ok'} reason={str(quality.get('reason'))[:160]} "
                f"issues={(quality.get('issues') or [])[:3]}",
            )
        if row.reject_reason:
            add(row.edited_at or row.created_at, "reject", row.reject_reason[:200])
        if row.scheduled_at:
            add(row.scheduled_at, "scheduled", f"попыток: {row.attempts}")
        if row.publish_error:
            add(row.edited_at or row.created_at, "publish_error", row.publish_error[:200])
        for entry in session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.post_public_id == public_id)
            .order_by(CrosspostLog.id)
        ).scalars().all():
            add(
                entry.created_at,
                f"crosspost:{entry.platform}",
                f"{entry.status} {entry.remote_id or ''} {(entry.error or '')}".strip()[:200],
            )
        if row.published_at:
            add(row.published_at, "published", f"remote_message_id={row.tg_message_id}")
        media = session.execute(
            select(MediaRow).where(MediaRow.post_id == row.id)
        ).scalars().all()
        stat = session.execute(
            select(PostStat)
            .where(PostStat.post_id == row.id)
            .order_by(PostStat.collected_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    events.sort(key=lambda item: item["at"] or "")
    return {
        "post": public_id,
        "status": row.status,
        "events": events,
        "media": len(media),
        "stats": {
            "views": stat.views if stat else 0,
            "impressions": stat.impressions if stat else 0,
            "opens": stat.opens if stat else 0,
        },
    }


POLISH_HINTS = {
    "shorter": "Сократи текст примерно на треть, сохранив все факты, источники и смысл.",
    "funnier": "Добавь одну сухую ироничную деталь в середину текста; факты и заголовок не трогай.",
    "stricter": "Убери разговорные обороты и оценочные слова, сделай тон нейтральным и точным.",
}


def _operator(action: str, target: str, detail: str = "") -> None:
    """Audit trail: a human pressed something, and that is worth remembering."""
    try:
        with session_scope() as session:
            session.add(
                OperatorLog(action=action, target=target[:64], detail=(detail or "")[:500] or None)
            )
    except Exception as exc:  # noqa: BLE001 - audit must not break the action
        log.warning("operator log failed: %s", redact_sensitive_text(exc, limit=180))


def _polish_post(public_id: str, mode: str) -> dict:
    """One LLM pass over an existing draft, then the gates run again."""
    from tgfarm.core.schemas import QualityReport
    from tgfarm.s5_quality import evaluate, load_draft, rewrite

    loaded = load_draft(public_id)
    if loaded is None:
        raise HTTPException(404, "post not found or channel unknown")
    draft, channel_id = loaded
    persona = get_config().persona(channel_id)
    if persona is None:
        raise HTTPException(404, "channel unknown")

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "post not found")
        if row.status not in EDITABLE_STATUSES or row.tg_message_id is not None:
            raise HTTPException(409, "пост уже отправлен — правка недоступна")
        was_scheduled = row.status == "scheduled"

    hint = POLISH_HINTS.get(mode, POLISH_HINTS["shorter"])
    report = QualityReport(passed=False, gate="polish", reason=hint)
    better = asyncio.run(
        rewrite(draft, persona, report, generation_provider=draft.generation_provider)
    )
    if better is None:
        return {"ok": False, "message": "модель недоступна — попробуйте позже"}
    better.hook_style = draft.hook_style
    better.variant = draft.variant
    result = asyncio.run(
        evaluate(better, persona, generation_provider=better.generation_provider)
    )
    payload = result.model_dump()

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one()
        row.hook = better.hook
        row.body = better.body
        row.cta = better.cta
        row.hashtags = better.hashtags
        row.quality = {
            "scores": result.scores,
            "reason": result.reason,
            "gate": result.gate,
            "issues": result.issues,
        }
        row.edited_at = datetime.now(UTC)
        if result.passed:
            row.status = "scheduled" if was_scheduled else "gated"
            row.reject_reason = None
        else:
            row.status = "rejected"
            row.reject_reason = f"{result.gate}: {result.reason}"

    _operator("polish", public_id, mode)
    return {
        "ok": True,
        "passed": result.passed,
        "hook": better.hook,
        "body": better.body,
        "quality": payload,
        "status": row.status,
    }


EDITABLE_STATUSES = ("draft", "gated", "rejected", "scheduled")


def _update_draft(post_public_id: str, update: DraftUpdate) -> dict:
    """Apply a manual edit and re-run the gates on the result.

    Editing is allowed for anything that has not actually been sent. A post that
    already has a Telegram message id is out of reach until it is unpublished —
    changing text in Telegram after the fact is not something the Bot API offers.
    """
    from tgfarm.s5_quality import evaluate, load_draft

    loaded = load_draft(post_public_id)
    if loaded is None:
        raise HTTPException(404, "post not found or channel unknown")
    draft, channel_id = loaded

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == post_public_id)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "post not found")
        if row.status not in EDITABLE_STATUSES or row.tg_message_id is not None:
            raise HTTPException(
                409,
                f"пост в статусе «{row.status}» уже отправлен — "
                "сначала отзовите публикацию",
            )
        was_scheduled = row.status == "scheduled"

    if update.hook is not None:
        draft.hook = update.hook.strip()
    if update.body is not None:
        draft.body = update.body.strip()
    if update.cta is not None:
        draft.cta = update.cta.strip() or None
    if update.hashtags is not None:
        draft.hashtags = update.hashtags
    if update.angle is not None:
        draft.angle = update.angle

    persona = get_config().persona(channel_id)
    report = asyncio.run(evaluate(draft, persona)) if persona else None
    payload = report.model_dump() if report is not None else {}
    passed = bool(report and report.passed)

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == post_public_id)
        ).scalar_one()
        row.hook = draft.hook
        row.body = draft.body
        row.cta = draft.cta
        row.hashtags = draft.hashtags
        row.angle = draft.angle
        row.quality = payload
        row.edited_at = datetime.now(UTC)
        # A hand edit can only invalidate the verdict it was given, so the post
        # goes back through the gates rather than keeping a score that described
        # different text. A scheduled post keeps its slot when the edit still
        # passes — losing the slot would be a surprising side effect of fixing a typo.
        if passed:
            row.status = "scheduled" if was_scheduled else "gated"
            row.reject_reason = None
        else:
            row.status = "rejected"
            row.reject_reason = report.gate if report else "edited"

    _operator(
        "edit",
        post_public_id,
        f"passed={passed} gate={report.gate if report else None}",
    )
    return {
        "ok": True,
        "status": row.status,
        "quality": payload,
        "passed": passed,
        "gate": report.gate if report else None,
        "chars": len(draft.hook) + len(draft.body),
    }


def _preview_post(post_public_id: str) -> dict:
    """Approximate the target platform's rendering for the editor."""
    from tgfarm.s5_quality import PLATFORM_HARD_LIMITS, load_draft

    loaded = load_draft(post_public_id)
    if loaded is None:
        raise HTTPException(404, "post not found or channel unknown")
    draft, channel_id = loaded

    config = get_config()
    persona = config.persona(channel_id)
    platform = persona.platform if persona else "telegram"
    text_parts: list[str] = []
    # Dzen uses the hook as the article title, so the body preview must not
    # duplicate it the way a Telegram message does.
    if draft.hook and platform != "dzen":
        text_parts.append(draft.hook.strip())
    if draft.body:
        text_parts.append(draft.body.strip())
    if draft.cta:
        text_parts.append(draft.cta.strip())
    if draft.hashtags:
        text_parts.append(" ".join(f"#{tag.lstrip('#')}" for tag in draft.hashtags))
    # Sources are stored as dicts, not strings — rendering them with `str()`
    # would paste a Python repr into the post.
    sources = (draft.sources or [])[: (persona.max_sources if persona else 3)]
    if sources:
        lines = ["Источники:"]
        for source in sources:
            if isinstance(source, dict):
                title = str(source.get("title", "")).strip()
                url = str(source.get("url", "")).strip()
            else:
                title, url = str(source).strip(), ""
            if title:
                lines.append(f"— {title}")
            if url:
                lines.append(f"  {url}")
        text_parts.append("\n".join(lines))

    text = "\n\n".join(p for p in text_parts if p)

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == post_public_id)
        ).scalar_one_or_none()
        cover = None
        if row is not None:
            media = session.execute(
                select(Media)
                .where(Media.post_id == row.id)
                .order_by(Media.is_primary.desc(), Media.id)
            ).scalars().first()
            if media is not None:
                cover = f"/api/media/{media.sha}"

    limit = PLATFORM_HARD_LIMITS.get(platform, 4096)
    return {
        "text": text,
        "chars": len(text),
        "limit": limit,
        "over_limit": len(text) > limit,
        "cover": cover,
        "channel_id": channel_id,
        "channel_title": persona.title if persona else channel_id,
        "platform": platform,
        "disclaimer": persona.disclaimer if persona else None,
        "preview": _telegram_html(text),
    }


def _telegram_html(text: str) -> str:
    """Render the subset of Markdown Telegram accepts, for the preview pane.

    Underscores are italic only at word boundaries: `gnews_business_ru` is an
    identifier, and italicising its middle makes the preview lie about the text.
    """
    import html as html_lib
    import re

    out = html_lib.escape(text)
    out = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w`])_([^_\n]+)_(?![\w])", r"<i>\1</i>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', out)
    return out


def _telegram_state() -> dict:
    from tgfarm import tg_sync, tguser

    authorized = tguser.is_authorized()
    return {
        "configured": tguser.is_configured(),
        "authorized": authorized,
        "read_only": get_settings().telegram_user_readonly,
        "session_present": tguser.session_path().exists(),
        "bot_token_set": bool(get_settings().telegram_bot_token),
        "channels": tg_sync.channel_reality(),
        "note": (
            "Сессия аккаунта только читает. Публикация идёт через бота — "
            "бан бота стоит дешевле, чем бан аккаунта."
        ) if authorized else "Выполните `tgfarm telegram login`",
    }


def _telegram_chats(limit: int) -> dict:
    import asyncio as _asyncio

    from tgfarm import tguser

    if not tguser.is_authorized():
        raise HTTPException(409, "нет авторизованной сессии — выполните `tgfarm telegram login`")
    chats = _asyncio.run(tguser.list_chats(limit=limit))
    return {"chats": [c.as_dict() for c in chats]}


def _telegram_top(limit: int, min_participants: int) -> dict:
    from tgfarm import tguser
    from tgfarm.s1_sources.telegram_top import discover_top_chats

    if not tguser.is_authorized():
        raise HTTPException(409, "нет авторизованной сессии — выполните `tgfarm telegram login`")
    return {"chats": discover_top_chats(limit, min_participants)}


def _telegram_sync() -> dict:
    import asyncio as _asyncio

    from tgfarm import tg_sync

    if not _tguser_ready():
        raise HTTPException(409, "нет авторизованной сессии")
    return _asyncio.run(tg_sync.sync_channels())


def _telegram_stats() -> dict:
    import asyncio as _asyncio

    from tgfarm import tg_sync

    if not _tguser_ready():
        raise HTTPException(409, "нет авторизованной сессии")
    return _asyncio.run(tg_sync.collect_post_stats())


def _tguser_ready() -> bool:
    from tgfarm import tguser

    return tguser.is_configured() and tguser.is_authorized()


def _media_path(media_id: str) -> Path:
    with session_scope() as session:
        row = session.execute(select(Media).where(Media.sha == media_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "media not found")
        path = Path(row.path)
    if not path.exists():
        raise HTTPException(410, "media file missing on disk")
    return path


def _runs(limit: int) -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "run_id": r.run_id,
                "stage": r.stage,
                "status": r.status,
                "in": r.items_in,
                "out": r.items_out,
                "ms": r.duration_ms,
                "error": (r.error or "")[:200] or None,
                "at": r.created_at,
            }
            for r in rows
        ]


def _crossposts(limit: int) -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog).order_by(CrosspostLog.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "post": r.post_public_id,
                "platform": r.platform,
                "status": r.status,
                "remote_id": r.remote_id,
                "url": redact_sensitive_text(r.url, limit=500) if r.url else None,
                "error": redact_sensitive_text(r.error, limit=200) if r.error else None,
                "attempt": r.attempt,
                "retry_after_at": getattr(r, "retry_after_at", None),
                "at": r.created_at,
            }
            for r in rows
        ]


def _crosspost_health() -> dict:
    """Summarize the latest state of every material/platform delivery pair."""
    from tgfarm.db.models import CrosspostClaim
    from tgfarm.s9_growth.adapters import MAX_AUTOMATIC_CROSSPOST_ATTEMPTS

    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=30)
    latest_ids = (
        select(func.max(CrosspostLog.id).label("latest_id"))
        .group_by(CrosspostLog.post_public_id, CrosspostLog.platform)
        .subquery()
    )
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog)
            .join(latest_ids, CrosspostLog.id == latest_ids.c.latest_id)
            .order_by(CrosspostLog.platform.asc(), CrosspostLog.created_at.desc())
        ).scalars().all()
        claims = session.execute(select(CrosspostClaim)).scalars().all()

    by_platform: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_platform.setdefault(
            row.platform,
            {
                "platform": row.platform,
                "targets": 0,
                "posted": 0,
                "failed": 0,
                "permanent": 0,
                "unknown": 0,
                "publishing": 0,
                "stuck": 0,
                "retry_authorized": 0,
                "skipped": 0,
                "dry_run": 0,
                "last_success_at": None,
                "oldest_attention_at": None,
                "state": "not_tested",
            },
        )
        item["targets"] += 1
        status = str(row.status or "unknown")
        if status in item:
            item[status] += 1

        stamp = row.created_at
        if stamp is not None:
            stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        if status == "posted":
            previous = item["last_success_at"]
            if previous is None or (stamp and stamp.isoformat() > previous):
                item["last_success_at"] = stamp.isoformat() if stamp else None

        is_stuck = status == "publishing" and stamp is not None and stamp <= stale_before
        if is_stuck:
            item["stuck"] += 1
        attention = (
            status in {"unknown", "permanent"}
            or (status == "failed" and int(row.attempt or 1) >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS)
            or is_stuck
        )
        if attention and stamp is not None:
            previous_attention = item["oldest_attention_at"]
            if previous_attention is None or stamp.isoformat() < previous_attention:
                item["oldest_attention_at"] = stamp.isoformat()

    for item in by_platform.values():
        exhausted = item["failed"] and any(
            row.platform == item["platform"]
            and row.status == "failed"
            and int(row.attempt or 1) >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS
            for row in rows
        )
        if item["unknown"] or item["permanent"] or exhausted or item["stuck"]:
            item["state"] = "attention"
        elif item["publishing"] or item["retry_authorized"] or item["failed"]:
            item["state"] = "working"
        elif item["posted"]:
            item["state"] = "healthy"
        elif item["skipped"]:
            item["state"] = "setup"
        elif item["dry_run"]:
            item["state"] = "dry_run"
        else:
            item["state"] = "not_tested"

    totals = {
        "targets": len(rows),
        "platforms": len(by_platform),
        "posted": sum(item["posted"] for item in by_platform.values()),
        "failed": sum(item["failed"] for item in by_platform.values()),
        "permanent": sum(item["permanent"] for item in by_platform.values()),
        "unknown": sum(item["unknown"] for item in by_platform.values()),
        "publishing": sum(item["publishing"] for item in by_platform.values()),
        "stuck": sum(item["stuck"] for item in by_platform.values()),
        "retry_authorized": sum(item["retry_authorized"] for item in by_platform.values()),
        "skipped": sum(item["skipped"] for item in by_platform.values()),
        "dry_run": sum(item["dry_run"] for item in by_platform.values()),
        "claims": len(claims),
        "stale_claims": sum(
            1 for claim in claims
            if claim.claimed_at is not None
            and (
                claim.claimed_at.replace(tzinfo=UTC)
                if claim.claimed_at.tzinfo is None
                else claim.claimed_at.astimezone(UTC)
            ) <= stale_before
        ),
        "needs_attention": sum(
            item["unknown"] + item["permanent"] + item["stuck"]
            + sum(
                1 for row in rows
                if row.platform == item["platform"]
                and row.status == "failed"
                and int(row.attempt or 1) >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS
            )
            for item in by_platform.values()
        ),
    }
    overall = (
        "attention" if totals["needs_attention"] or totals["stale_claims"]
        else "working" if totals["publishing"] or totals["failed"] or totals["retry_authorized"]
        else "healthy" if totals["posted"]
        else "not_tested"
    )
    return {
        "generated_at": now.isoformat(),
        "state": overall,
        "totals": totals,
        "platforms": sorted(by_platform.values(), key=lambda item: item["platform"]),
    }


def _crossposts_export(limit: int = 300) -> dict:
    """Export delivery diagnostics without raw error text or credentials."""
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog).order_by(CrosspostLog.id.desc()).limit(limit)
        ).scalars().all()
        return {
            "schema_version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "redactions": ["raw error text omitted", "credentials and browser sessions are never exported"],
            "crossposts": [
                {
                    "post": r.post_public_id,
                    "platform": r.platform,
                    "status": r.status,
                    "remote_id": r.remote_id,
                    "url": redact_sensitive_text(r.url, limit=500) if r.url else None,
                    "attempt": r.attempt,
                    "retry_after_at": getattr(r, "retry_after_at", None).isoformat() if getattr(r, "retry_after_at", None) else None,
                    "at": r.created_at.isoformat() if r.created_at else None,
                    "has_error": bool(r.error),
                }
                for r in rows
            ],
        }


def _report(days: int) -> dict:
    from tgfarm import s8_analytics
    from tgfarm.s8_analytics.dzen_web import metrics_status
    from tgfarm.s8_analytics.portfolio import portfolio_report

    return {
        "fleet": s8_analytics.fleet_summary(days),
        "angles": s8_analytics.angle_performance(days),
        "hook_styles": s8_analytics.hook_style_performance(days),
        "holdout": s8_analytics.holdout_report(days),
        "guardrail": s8_analytics.style_diversity(min(days, 7)),
        "platform_health": s8_analytics.platform_health(min(days, 14)),
        "hours": s8_analytics.slot_performance(days),
        "portfolio": portfolio_report(max(30, days)),
        "dzen": metrics_status(),
    }


def _channels() -> list[dict]:
    from tgfarm import s8_analytics

    return s8_analytics.channel_inventory()


def _reject(public_ids: list[str], reason: str) -> int:
    """Reject only drafts/queued posts; never rewrite published or in-flight state."""
    with session_scope() as session:
        result = session.execute(
            update(Post)
            .where(
                Post.public_id.in_(public_ids),
                Post.status.in_(("draft", "gated", "scheduled", "rejected", "failed")),
            )
            .values(status="rejected", reject_reason=reason)
        )
        return result.rowcount or 0


def _reconcile_crosspost_outcome(
    post_public_id: str,
    platform: str,
    action: str,
) -> dict:
    """Resolve a crosspost whose remote outcome could not be confirmed."""
    from tgfarm.db.models import CrosspostClaim
    from tgfarm.s9_growth.adapters import ADAPTERS, MAX_AUTOMATIC_CROSSPOST_ATTEMPTS

    if platform not in ADAPTERS:
        return {"ok": False, "error": "unsupported_platform"}
    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            return {"ok": False, "error": "not_found"}

        latest = session.execute(
            select(CrosspostLog)
            .where(
                CrosspostLog.post_public_id == post_public_id,
                CrosspostLog.platform == platform,
            )
            .order_by(CrosspostLog.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        # Ambiguous outcomes require reconciliation; definite failed/permanent
        # outcomes may be retried only after an operator fixes the cause and
        # explicitly confirms that the destination does not already contain the post.
        retry_exhausted = bool(
            latest and latest.status == "failed"
            and int(latest.attempt or 1) >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS
        )
        reconcilable_status = bool(
            latest and latest.status in {"unknown", "publishing", "permanent"}
        )
        if latest is None or not (reconcilable_status or retry_exhausted):
            return {
                "ok": False,
                "error": "not_reconcilable",
                "status": latest.status if latest else None,
            }
        if latest.status == "publishing":
            created = latest.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created > datetime.now(UTC) - timedelta(minutes=30):
                return {"ok": False, "error": "still_in_flight"}

        if action not in {"confirm_posted", "retry_after_check"}:
            return {"ok": False, "error": "invalid_action"}

        # Reconciliation is the only path that releases an ambiguous/stale claim.
        session.query(CrosspostClaim).filter_by(
            post_public_id=post_public_id, platform=platform
        ).delete(synchronize_session=False)

        attempt = latest.attempt
        if action == "confirm_posted":
            session.add(
                CrosspostLog(
                    post_public_id=post_public_id,
                    platform=platform,
                    status="posted",
                    remote_id=latest.remote_id,
                    url=latest.url,
                    error="confirmed by operator; destination ID may be unavailable",
                    attempt=attempt,
                )
            )
            return {"ok": True, "status": "posted", "post_id": post.id, "detail": "operator confirmed destination post"}

        session.add(
            CrosspostLog(
                post_public_id=post_public_id,
                platform=platform,
                status="retry_authorized",
                error="operator checked destination and authorized retry",
                attempt=attempt,
            )
        )
        return {
            "ok": True,
            "status": "retry_authorized",
            "post_id": post.id,
            "detail": "operator checked destination and authorized retry",
        }


def _unknown_publish_ids(public_ids: list[str]) -> list[str]:
    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id).where(
                Post.public_id.in_(public_ids),
                Post.status == "publish_unknown",
            )
        ).scalars().all()
        return list(rows)


def _retry_failed_publish(public_id: str) -> dict:
    """Manually requeue a terminal, definite publish failure after the cause is fixed."""
    from tgfarm.platforms import is_platform_enabled

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "not_found"}
        if row.status != "failed":
            return {"ok": False, "error": "not_failed", "status": row.status}
        if row.tg_message_id is not None:
            return {"ok": False, "error": "already_published"}
        channel = session.get(Channel, row.channel_id)
        platform = ((channel.platform if channel else None) or "telegram").lower()
        if not is_platform_enabled(platform):
            return {
                "ok": False,
                "error": "platform_disabled",
                "platform": platform,
            }

        row.status = "scheduled"
        row.scheduled_at = datetime.now(UTC)
        row.attempts = 0
        row.publish_error = None
        row.publish_heartbeat_at = None
        return {
            "ok": True,
            "public_id": public_id,
            "platform": platform,
            "scheduled_at": row.scheduled_at,
        }


def _blocked_publish_now_ids(public_ids: list[str]) -> list[dict]:
    """Reject forced publishing if a post is ineligible or its platform is paused."""
    from tgfarm.platforms import is_platform_enabled

    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id, Post.status, Channel.platform)
            .outerjoin(Channel, Post.channel_id == Channel.id)
            .where(Post.public_id.in_(public_ids))
        ).all()
        details = {
            public_id: (status, (platform or "telegram").lower())
            for public_id, status, platform in rows
        }

    blocked: list[dict] = []
    for public_id in public_ids:
        status, platform = details.get(public_id, ("not_found", ""))
        if status not in {"scheduled", "gated"}:
            blocked.append({"id": public_id, "status": status})
        elif not is_platform_enabled(platform):
            blocked.append({
                "id": public_id,
                "status": status,
                "reason": "platform_disabled",
                "platform": platform,
            })
    return blocked


def _reconcile_publish_outcome(public_id: str, action: str) -> dict:
    """Resolve a publish whose remote outcome could not be confirmed.

    Retrying is only enabled after the operator has checked the destination.
    This is intentionally not an automatic retry policy.
    """
    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "not_found"}
        if row.status != "publish_unknown":
            return {"ok": False, "error": "not_unknown", "status": row.status}

        if action == "confirm_published":
            row.status = "published"
            row.published_at = datetime.now(UTC)
            row.publish_error = None
            detail = "operator confirmed post exists on destination"
        elif action == "retry_after_check":
            row.status = "scheduled"
            row.scheduled_at = datetime.now(UTC)
            row.publish_error = "manual retry approved after operator checked destination"
            detail = "operator checked destination and authorised retry"
        else:
            return {"ok": False, "error": "invalid_action"}

        return {"ok": True, "status": row.status, "detail": detail}


def _config_payload() -> dict:
    cfg = get_config()
    return {
        "channels": [p.as_dict() for p in cfg.personas],
        "sources": [s.as_dict() for s in cfg.sources],
        "schedule": {
            "timezone": cfg.schedule.timezone,
            "hours": cfg.schedule.hours(),
            "peak_hours": cfg.schedule.peak_hours(),
            "fleet_posts_per_hour": cfg.schedule.fleet_posts_per_hour,
            "crosspost": cfg.schedule.crosspost,
        },
    }


def create_app(with_scheduler: bool = False) -> FastAPI:
    """Build the admin app.

    `with_scheduler` owns the background pipeline for the lifetime of the app.
    `tgfarm serve` and the desktop build both want it; a process that only
    exposes the panel (tests, a read-only dashboard) does not.
    """
    token = ensure_token()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ensure_bootstrapped()
        try:
            count = await io(_warm_index)
            log.info("vector index warmed with %d items", count)
        except Exception as exc:  # noqa: BLE001 - a cold start is survivable
            log.warning("vector index warmup failed: %s", redact_sensitive_text(exc, limit=180))

        if not with_scheduler:
            yield
            return

        # The farm is autonomous: closing the dashboard must not stop publishing.
        async with pipeline.running():
            yield

    app = FastAPI(
        title="tgfarm",
        description="Автономная ферма каналов — панель управления",
        version="1.2.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------- auth
    settings = get_settings()
    trusted = {
        h.strip().lower()
        for h in ("127.0.0.1", "localhost", "::1", settings.api_host, *(
            settings.allowed_hosts.split(",") if settings.allowed_hosts else []
        ))
        if h.strip()
    }

    @app.middleware("http")
    async def trusted_host_guard(request: Request, call_next):
        """A public page cannot read the token, but DNS rebinding can: an
        attacker domain resolving to 127.0.0.1 lands here with the operator's
        cookies. Only trusted Host values get answers at all."""
        host = (request.url.hostname or "").lower()
        if host and host not in trusted:
            log.warning("403 untrusted host header %s", host)
            return JSONResponse({"error": "untrusted host"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def require_api_token(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not is_public_path(path):
            header = request.headers.get(apiauth.HEADER)
            query = request.query_params.get("token")
            cookie = request.cookies.get(apiauth.COOKIE)
            # Authorize against the original query, then remove its credential
            # before downstream handlers and ASGI access logging can see it.
            if query is not None:
                request.scope["query_string"] = apiauth.strip_token_from_query(
                    request.scope.get("query_string", b"")
                )
            if not is_authorised(header, query, cookie):
                log.info(
                    "401 %s header=%s query=%s cookie=%s",
                    path,
                    bool(header),
                    bool(query),
                    bool(cookie),
                )
                return JSONResponse(
                    {
                        "error": "unauthorized",
                        "message": (
                            "Требуется токен доступа. Откройте панель по адресу, "
                            "который напечатал tgfarm, или задайте TGFARM_API_TOKEN."
                        ),
                    },
                    status_code=401,
                )
        return await call_next(request)

    @app.middleware("http")
    async def redact_mastodon_oauth_callback(request: Request, call_next):
        """Capture OAuth credentials, then strip the query before access logging."""
        callback_scopes = {
            "/api/platforms/mastodon/auth/callback": "tgfarm_mastodon_oauth_params",
            "/api/platforms/x/auth/callback": "tgfarm_x_oauth_params",
            "/api/platforms/threads/auth/callback": "tgfarm_threads_oauth_params",
            "/api/platforms/linkedin/auth/callback": "tgfarm_linkedin_oauth_params",
        }
        callback_scope = callback_scopes.get(request.url.path)
        if callback_scope:
            from urllib.parse import parse_qs

            raw_query = request.scope.get("query_string", b"")
            if isinstance(raw_query, bytes):
                raw_query = raw_query.decode("utf-8", errors="replace")
            # Strip first: even an overlong/malformed callback query must not reach access logs.
            request.scope["query_string"] = b""
            try:
                parsed = parse_qs(raw_query, keep_blank_values=True, max_num_fields=16)
            except ValueError:
                parsed = {}
            request.scope[callback_scope] = {
                key: values[0]
                for key, values in parsed.items()
                if key in {"code", "state", "error"} and values
            }
        response = await call_next(request)
        if request.url.path in {
            "/api/platforms/mastodon/auth/callback",
            "/api/platforms/x/auth/callback",
            "/api/platforms/threads/auth/callback",
            "/api/platforms/linkedin/auth/callback",
        }:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
        return response

    # ------------------------------------------------------------------- UI
    # no-store: the dashboard ships with the app, and a cached stylesheet after
    # an update is a dashboard that lies about its own state
    NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

    @app.get("/")
    async def index():
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        bootstrap = (
            "<script>window.__TGFARM__ = "
            + json.dumps(
                {
                    "token": token,
                    "tokenHeader": apiauth.HEADER,
                    "tokenRequired": get_settings().require_api_token,
                }
            )
            + ";</script>"
        )
        html = html.replace("</head>", bootstrap + "</head>", 1)
        response = HTMLResponse(html, headers=NO_STORE)
        # An <img> cannot carry a custom header, so covers and videos are fetched
        # with a cookie instead. SameSite=Strict means another site cannot make
        # the browser send it, which is what keeps this from reopening the hole
        # the header closed. HttpOnly keeps it out of reach of page scripts.
        response.set_cookie(
            apiauth.COOKIE,
            token,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    @app.get("/ui/app.css")
    async def ui_css():
        return FileResponse(UI_DIR / "app.css", media_type="text/css", headers=NO_STORE)

    @app.get("/ui/app.js")
    async def ui_js():
        return FileResponse(
            UI_DIR / "app.js", media_type="application/javascript", headers=NO_STORE
        )

    # --------------------------------------------------------------- status
    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/status")
    async def status() -> dict:
        return await io(_status_payload)

    @app.get("/api/config")
    async def config() -> dict:
        return await io(_config_payload)

    @app.get("/api/models")
    async def models() -> dict:
        """Live model list for the configured LLM gateway."""
        from tgfarm.llm.client import get_llm

        def _list() -> dict:
            settings = get_settings()
            zen = next(
                (p for p in getattr(get_llm(), "_providers", []) if p.name == "opencode-zen"),
                None,
            )
            if zen is None:
                return {"configured": False, "models": []}
            found = asyncio.run(zen.list_models(getattr(zen, "_token", "")))
            return {
                "configured": True,
                "selected": getattr(zen, "_resolved_model", "") or settings.opencode_zen_model,
                "count": len(found),
                "models": [m["id"] for m in found],
            }

        return await io(_list)

    # --------------------------------------------------------------- content
    @app.get("/api/trends")
    async def trends(limit: int = Query(25, ge=1, le=200)) -> dict:
        return {"trends": await io(_trends, limit)}

    @app.get("/api/sources")
    async def sources() -> dict:
        return {"sources": await io(_sources)}

    @app.get("/api/posts")
    async def posts(
        status_filter: str | None = Query(None, alias="status"),
        channel_id: str | None = None,
        hook_style: str | None = None,
        variant: str | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> dict:
        return {
            "posts": await io(_posts, status_filter, channel_id, limit, hook_style, variant)
        }

    @app.get("/api/posts/{post_public_id}")
    async def post_detail(post_public_id: str) -> dict:
        return await io(_post_detail, post_public_id)

    @app.get("/api/media/{media_id}")
    async def media(media_id: str):
        path = await io(_media_path, media_id)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    # --------------------------------------------------------------- reports
    @app.get("/api/report")
    async def report(days: int = Query(7, ge=1, le=90)) -> dict:
        return await io(_report, days)

    @app.get("/api/portfolio")
    async def portfolio(
        days: int = Query(30, ge=1, le=90),
        min_age_hours: int = Query(24, ge=0, le=336),
    ) -> dict:
        from tgfarm.s8_analytics.portfolio import portfolio_report

        return await io(portfolio_report, days, min_age_hours)

    @app.get("/api/memory")
    async def memory(
        query: str = Query("", max_length=500),
        channel_id: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1000000),
    ) -> dict:
        """Search the entire published archive, including claims and source metadata."""
        from tgfarm.s4_generation.memory import search_archive

        return await io(
            search_archive,
            query,
            channel_id=channel_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/api/memory/reindex")
    async def memory_reindex() -> dict:
        """Rebuild the derived full-text index from all posts stored in the database."""
        from tgfarm.s4_generation.memory import rebuild_archive_index

        return await io(rebuild_archive_index)

    @app.get("/api/memory/topic")
    async def memory_topic(
        query: str = Query(..., min_length=2, max_length=500),
        channel_id: str | None = None,
        days: int = Query(30, ge=1, le=180),
        archive_limit: int = Query(300, ge=1, le=300),
    ) -> dict:
        """Build a topic dossier from historical posts and recently collected sources."""
        from tgfarm.s4_generation.topic_memory import build_topic_dossier

        return await io(
            build_topic_dossier,
            query,
            channel_id=channel_id,
            preferred_channel_id=channel_id,
            days=days,
            archive_limit=archive_limit,
        )

    @app.get("/api/editorial")
    async def editorial(limit: int = Query(25, ge=1, le=100)) -> dict:
        from tgfarm.editorial import queue

        return {"queue": await io(queue, limit)}

    @app.get("/api/audit")
    async def audit(
        days: int = Query(7, ge=1, le=90),
        channel_id: str | None = None,
    ) -> dict:
        from tgfarm.s8_analytics.audit import content_audit

        return await io(content_audit, days, channel_id=channel_id)

    @app.get("/api/channels")
    async def channels() -> dict:
        return {"channels": await io(_channels)}

    @app.get("/api/platforms")
    async def platforms() -> dict:
        from tgfarm.platforms import get_platform_catalog

        return await io(get_platform_catalog)

    @app.put("/api/platforms/{platform_id}")
    async def toggle_platform(platform_id: str, request: PlatformToggleRequest) -> dict:
        from tgfarm.platforms import set_platform_enabled

        try:
            item = await io(set_platform_enabled, platform_id, request.enabled)
        except ValueError as exc:
            status = 404 if "Неизвестная" in str(exc) else 409
            raise HTTPException(status_code=status, detail=redact_sensitive_text(exc, limit=300)) from exc
        _operator(
            "platform-toggle",
            platform_id,
            "enabled" if request.enabled else "disabled",
        )
        return {"ok": True, "platform": item}

    @app.put("/api/platforms/{platform_id}/plan")
    async def plan_platform(platform_id: str, request: PlatformPlanRequest) -> dict:
        from tgfarm.platforms import set_platform_planned

        try:
            item = await io(set_platform_planned, platform_id, request.planned)
        except ValueError as exc:
            status = 404 if "Неизвестная" in str(exc) else 409
            raise HTTPException(status_code=status, detail=redact_sensitive_text(exc, limit=300)) from exc
        _operator(
            "platform-plan",
            platform_id,
            "planned" if request.planned else "removed from plan",
        )
        return {"ok": True, "platform": item}


    @app.put("/api/platforms/{platform_id}/credentials")
    async def save_platform_credentials(
        platform_id: str, request: PlatformCredentialsRequest
    ) -> dict:
        from tgfarm.platforms import save_platform_credentials as save_credentials

        try:
            item = await io(save_credentials, platform_id, request.values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        _operator(
            "platform-auth-save",
            platform_id,
            "saved keys: " + ", ".join(sorted(request.values.keys()))[:200],
        )
        return {"ok": True, "auth": item}

    @app.post("/api/platforms/{platform_id}/auth/check")
    async def check_platform_credentials(platform_id: str) -> dict:
        from tgfarm.platforms import CATALOG
        from tgfarm.platform_auth import check_platform_auth

        if not any(item["id"] == platform_id.lower() for item in CATALOG):
            raise HTTPException(status_code=404, detail="Неизвестная площадка.")
        if not any(item["id"] == platform_id.lower() and item["mode"] == "built_in" for item in CATALOG):
            raise HTTPException(
                status_code=409,
                detail="Площадка ещё не подключена к публикационному адаптеру. Вход на её сайт не означает интеграцию с TgFarm.",
            )
        result = await check_platform_auth(platform_id)
        _operator(
            "platform-auth-check",
            platform_id,
            "authorized" if result["authorized"] else "check failed",
        )
        return result

    @app.post("/api/platforms/mastodon/auth/start")
    async def mastodon_oauth_start(payload: MastodonOAuthStartRequest, request: Request) -> dict:
        from tgfarm.mastodon_oauth import begin_mastodon_oauth, normalize_instance_url
        from tgfarm.platforms import save_platform_credentials as save_credentials

        try:
            instance = normalize_instance_url(payload.instance)
            # Remember the chosen instance before beginning so the OAuth
            # descriptor and future requests refer to the same server.
            await io(save_credentials, "mastodon", {"MASTODON_INSTANCE": instance})
            redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/mastodon/auth/callback"
            return await begin_mastodon_oauth(instance, redirect_uri)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/mastodon/auth/callback")
    async def mastodon_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.mastodon_oauth import cancel_mastodon_oauth, complete_mastodon_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_mastodon_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        page_headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_mastodon_oauth(state_value)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение не завершено</h1><p>Авторизация отменена или ответ сервера неполный. "
                "Вернись в панель и запусти подключение заново.</p><p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        try:
            credentials = await complete_mastodon_oauth(state_value, code)
            await io(
                save_credentials,
                "mastodon",
                {
                    "MASTODON_INSTANCE": credentials["instance"],
                    "MASTODON_CLIENT_ID": credentials["client_id"],
                    "MASTODON_CLIENT_SECRET": credentials["client_secret"],
                    "MASTODON_ACCESS_TOKEN": credentials["access_token"],
                },
            )
            await io(
                record_auth_check,
                "mastodon",
                True,
                account=credentials["account"],
                detail="Mastodon OAuth подтверждён; профиль доступен.",
            )
            _operator("platform-auth-check", "mastodon", "OAuth connected")
            account = html.escape(credentials["account"])
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Mastodon подключён</h1><p>Доступ подтверждён для аккаунта <strong>" + account + "</strong>.</p>"
                "<p>Токен сохранён локально. Публикация остаётся выключенной, пока ты не включишь площадку в TgFarm.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=200, headers=page_headers)
        except (ValueError, RuntimeError) as exc:
            cancel_mastodon_oauth(state_value)
            # OAuth codes, tokens and exception bodies are never written to logs.
            log.warning("mastodon OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Mastodon</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        except Exception as exc:  # noqa: BLE001
            cancel_mastodon_oauth(state_value)
            log.warning("mastodon OAuth callback failed (%s)", type(exc).__name__)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Mastodon</h1><p>Внутренняя ошибка. Проверь настройки и повтори подключение.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=500, headers=page_headers)

    @app.post("/api/platforms/x/auth/start")
    async def x_oauth_start(request: Request) -> dict:
        from tgfarm.x_oauth import begin_x_oauth
        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/x/auth/callback"
        try:
            return await begin_x_oauth(
                settings.x_oauth_client_id,
                settings.x_oauth_client_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/x/auth/callback")
    async def x_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.x_oauth import cancel_x_oauth, complete_x_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_x_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        page_headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_x_oauth(state_value)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение X не завершено</h1><p>Авторизация отменена или ответ сервера неполный. "
                "Вернись в панель и запусти подключение заново.</p><p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        try:
            credentials = await complete_x_oauth(state_value, code)
            await io(
                save_credentials,
                "x",
                {
                    "X_OAUTH_CLIENT_ID": credentials["client_id"],
                    "X_OAUTH_CLIENT_SECRET": credentials["client_secret"],
                    "X_ACCESS_TOKEN": credentials["access_token"],
                    "X_REFRESH_TOKEN": credentials["refresh_token"],
                    "X_TOKEN_EXPIRES_AT": credentials["expires_at"],
                    "X_USER_ID": credentials["user_id"],
                },
            )
            await io(
                record_auth_check,
                "x",
                True,
                account=credentials["account"],
                detail="X OAuth подтверждён; профиль и права пользователя проверены.",
            )
            _operator("platform-auth-check", "x", "OAuth connected")
            account = html.escape(credentials["account"])
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>X подключён</h1><p>Доступ подтверждён для аккаунта <strong>" + account + "</strong>.</p>"
                "<p>OAuth-токены сохранены локально и будут обновляться автоматически. Публикация остаётся выключенной, пока ты не включишь X в панели.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=200, headers=page_headers)
        except (ValueError, RuntimeError) as exc:
            cancel_x_oauth(state_value)
            log.warning("X OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить X</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        except Exception as exc:  # noqa: BLE001
            cancel_x_oauth(state_value)
            log.warning("X OAuth callback failed (%s)", type(exc).__name__)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить X</h1><p>Внутренняя ошибка. Проверь настройки и повтори подключение.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=500, headers=page_headers)

    @app.post("/api/platforms/threads/auth/start")
    async def threads_oauth_start(request: Request) -> dict:
        from tgfarm.threads_oauth import begin_threads_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/threads/auth/callback"
        try:
            return await begin_threads_oauth(
                settings.threads_app_id,
                settings.threads_app_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/threads/auth/callback")
    async def threads_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.threads_oauth import cancel_threads_oauth, complete_threads_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_threads_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        page_headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_threads_oauth(state_value)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение Threads не завершено</h1><p>Авторизация отменена или ответ сервера неполный. "
                "Вернись в панель и запусти подключение заново.</p><p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        try:
            credentials = await complete_threads_oauth(state_value, code)
            await io(
                save_credentials,
                "threads",
                {
                    "THREADS_APP_ID": credentials["client_id"],
                    "THREADS_APP_SECRET": credentials["client_secret"],
                    "THREADS_ACCESS_TOKEN": credentials["access_token"],
                    "THREADS_TOKEN_EXPIRES_AT": credentials["expires_at"],
                    "THREADS_USER_ID": credentials["user_id"],
                    "THREADS_USERNAME": credentials["username"],
                },
            )
            await io(
                record_auth_check,
                "threads",
                True,
                account=credentials["account"],
                detail="Threads OAuth подтверждён; долгоживущий токен и профиль сохранены локально.",
            )
            _operator("platform-auth-check", "threads", "OAuth connected")
            account = html.escape(credentials["account"])
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Threads подключён</h1><p>Доступ подтверждён для аккаунта <strong>" + account + "</strong>.</p>"
                "<p>OAuth-токен сохранён локально и будет обновляться до истечения срока. Публикация остаётся выключенной, пока ты не включишь Threads в панели.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=200, headers=page_headers)
        except (ValueError, RuntimeError) as exc:
            cancel_threads_oauth(state_value)
            log.warning("Threads OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Threads</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        except Exception as exc:  # noqa: BLE001
            cancel_threads_oauth(state_value)
            log.warning("Threads OAuth callback failed (%s)", type(exc).__name__)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Threads</h1><p>Внутренняя ошибка. Проверь настройки и повтори подключение.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=500, headers=page_headers)

    @app.post("/api/platforms/linkedin/auth/start")
    async def linkedin_oauth_start(request: Request) -> dict:
        from tgfarm.linkedin_oauth import begin_linkedin_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/linkedin/auth/callback"
        try:
            return await begin_linkedin_oauth(
                settings.linkedin_client_id,
                settings.linkedin_client_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/linkedin/auth/callback")
    async def linkedin_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.linkedin_oauth import cancel_linkedin_oauth, complete_linkedin_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_linkedin_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        page_headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_linkedin_oauth(state_value)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение LinkedIn не завершено</h1><p>Авторизация отменена или ответ сервера неполный. "
                "Вернись в панель и запусти подключение заново.</p><p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        try:
            credentials = await complete_linkedin_oauth(state_value, code)
            values = {
                "LINKEDIN_CLIENT_ID": credentials["client_id"],
                "LINKEDIN_CLIENT_SECRET": credentials["client_secret"],
                "LINKEDIN_ACCESS_TOKEN": credentials["access_token"],
                "LINKEDIN_TOKEN_EXPIRES_AT": credentials["expires_at"],
                "LINKEDIN_MEMBER_ID": credentials["member_id"],
                "LINKEDIN_MEMBER_NAME": credentials["member_name"],
                "LINKEDIN_REFRESH_TOKEN_EXPIRES_AT": credentials["refresh_token_expires_at"],
            }
            if credentials.get("refresh_token"):
                values["LINKEDIN_REFRESH_TOKEN"] = credentials["refresh_token"]
            await io(save_credentials, "linkedin", values)
            await io(
                record_auth_check,
                "linkedin",
                True,
                account=credentials["account"],
                detail="LinkedIn OAuth подтверждён; профиль OpenID Connect и пользовательский токен сохранены локально.",
            )
            _operator("platform-auth-check", "linkedin", "OAuth connected")
            account = html.escape(credentials["account"])
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>LinkedIn подключён</h1><p>Доступ подтверждён для аккаунта <strong>" + account + "</strong>.</p>"
                "<p>Токен сохранён локально. Если LinkedIn не выдал refresh token, повторное подключение потребуется после истечения access token. "
                "Публикация остаётся выключенной, пока ты не включишь площадку в панели.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=200, headers=page_headers)
        except (ValueError, RuntimeError) as exc:
            cancel_linkedin_oauth(state_value)
            log.warning("LinkedIn OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить LinkedIn</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        except Exception as exc:  # noqa: BLE001
            cancel_linkedin_oauth(state_value)
            log.warning("LinkedIn OAuth callback failed (%s)", type(exc).__name__)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить LinkedIn</h1><p>Внутренняя ошибка. Проверь настройки и повтори попытку.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=500, headers=page_headers)

    @app.get("/api/reddit/metrics")
    async def reddit_metrics() -> dict:
        from tgfarm.s9_growth.reddit_metrics import get_reddit_metrics_report

        return await io(get_reddit_metrics_report)

    @app.post("/api/reddit/metrics/refresh")
    async def reddit_metrics_refresh(request: RedditMetricsRefreshRequest) -> dict:
        from tgfarm.s9_growth.reddit_metrics import RedditMetricsError, refresh_reddit_metrics

        try:
            return await refresh_reddit_metrics(limit=request.limit)
        except RedditMetricsError as exc:
            raise HTTPException(status_code=503, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.post("/api/reddit/scout")
    async def reddit_scout(request: RedditScoutRequest) -> dict:
        from tgfarm.s9_growth.reddit_scout import RedditScoutError, search_communities

        try:
            return await search_communities(
                request.query,
                post_public_id=request.post_public_id or "",
                title=request.title,
                body=request.body,
                limit=request.limit,
            )
        except RedditScoutError as exc:
            raise HTTPException(status_code=503, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.post("/api/reddit/tailor")
    async def reddit_tailor(request: RedditTailorRequest) -> dict:
        from tgfarm.s9_growth.reddit_scout import RedditScoutError, tailor_post

        try:
            return await tailor_post(request.post_public_id, request.subreddit)
        except RedditScoutError as exc:
            raise HTTPException(status_code=503, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.post("/api/reddit/publish")
    async def reddit_publish(request: RedditPublishRequest) -> dict:
        from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
        from tgfarm.platforms import is_platform_enabled
        from tgfarm.s9_growth import adapters as growth_adapters
        from tgfarm.s9_growth.adapters import ambiguous_crosspost_error, crosspost_retry_after_seconds, log_result
        from tgfarm.s9_growth import already_done, begin_crosspost
        from tgfarm.s9_growth.reddit_scout import (
            RedditScoutError,
            _post_record,
            inspect_community,
            validate_submission,
        )

        if not request.confirm:
            raise HTTPException(
                status_code=400,
                detail="Подтверди, что просмотрел черновик и правила сообщества.",
            )
        settings_now = get_settings()
        if not settings_now.live:
            raise HTTPException(
                status_code=409,
                detail="TGFARM_MODE не live. Сначала проверь черновик в dry-режиме.",
            )
        if not is_platform_enabled("reddit"):
            raise HTTPException(
                status_code=409,
                detail="Reddit выключен в центре площадок.",
            )
        adapter = growth_adapters.ADAPTERS.get("reddit")
        if adapter is None or not adapter.configured():
            raise HTTPException(
                status_code=409,
                detail="Reddit OAuth не настроен. Проверь локальный .env.",
            )
        subreddit = request.subreddit.strip()
        if subreddit.lower().startswith("r/"):
            subreddit = subreddit[2:]
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
            raise HTTPException(status_code=422, detail="Некорректное имя subreddit.")

        try:
            post = await io(_post_record, request.post_public_id)
            if post["status"] != "published":
                raise HTTPException(
                    status_code=409,
                    detail="Для Reddit можно использовать только уже опубликованный пост TgFarm.",
                )
            community = await inspect_community(subreddit)
            problems = validate_submission(request.title, request.body, community)
            if problems:
                raise HTTPException(status_code=422, detail={"errors": problems})
        except RedditScoutError as exc:
            raise HTTPException(status_code=503, detail=redact_sensitive_text(exc, limit=300)) from exc

        if await io(already_done, post["public_id"], "reddit"):
            raise HTTPException(
                status_code=409,
                detail="Для этого исходного поста уже есть результат Reddit. Проверь журнал перед повтором.",
            )
        attempt = await io(begin_crosspost, post["public_id"], "reddit")
        if attempt is None:
            raise HTTPException(
                status_code=409,
                detail="Публикация уже запускалась или ожидает сверки. Повтор запрещён до проверки журнала.",
            )

        payload = CrosspostPayload(
            platform="reddit",
            title=request.title.strip(),
            text=request.body.strip(),
            meta={"subreddit": subreddit, "reddit_kind": "self"},
        )
        # The global switch may have changed while the API refreshed community
        # rules and requirements. Recheck after claiming the attempt and just
        # before the side-effecting request.
        if not await io(is_platform_enabled, "reddit"):
            paused = CrosspostResult(
                platform="reddit",
                status="skipped",
                error="Reddit was disabled before submit; no post was sent.",
            )
            await io(log_result, post["public_id"], paused, attempt)
            raise HTTPException(
                status_code=409,
                detail="Reddit выключен до отправки. Никакая публикация не отправлена.",
            )
        try:
            result = await adapter.publish(payload)
        except Exception as exc:  # noqa: BLE001 - ambiguity must block blind retries
            result = CrosspostResult(
                platform="reddit",
                status="unknown" if ambiguous_crosspost_error(exc) else "failed",
                error=redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=300),
                retry_after_seconds=crosspost_retry_after_seconds(exc),
            )
        await io(log_result, post["public_id"], result, attempt)
        _operator(
            "reddit-publish",
            f"{post['public_id']}:r/{subreddit}",
            result.status,
        )
        return {
            "ok": result.status == "posted",
            "result": result.model_dump(),
            "subreddit": subreddit,
            "review_required": True,
        }

    @app.get("/api/runs")
    async def runs(limit: int = Query(30, ge=1, le=200)) -> dict:
        return {"runs": await io(_runs, limit)}

    @app.get("/api/crossposts")
    async def crossposts(limit: int = Query(50, ge=1, le=300)) -> dict:
        return {"crossposts": await io(_crossposts, limit)}

    @app.get("/api/crossposts/health")
    async def crosspost_health() -> dict:
        return await io(_crosspost_health)

    @app.get("/api/crossposts/export")
    async def export_crossposts(limit: int = Query(300, ge=1, le=300)) -> dict:
        return await io(_crossposts_export, limit)

    @app.post("/api/crossposts/{post_public_id}/{platform}/reconcile")
    async def reconcile_crosspost(
        post_public_id: str,
        platform: str,
        request: CrosspostReconcileRequest,
    ) -> dict:
        if request.action == "retry_after_check" and not request.confirm_not_published:
            raise HTTPException(
                400,
                "Для повтора нужно подтвердить, что вы проверили целевую площадку и поста там нет.",
            )
        if request.action == "retry_after_check":
            from tgfarm.platforms import is_platform_enabled
            from tgfarm.s9_growth.adapters import ADAPTERS

            if not is_platform_enabled(platform):
                raise HTTPException(
                    409,
                    "Эта площадка выключена. Сначала включи её в разделе «Площадки», затем разрешай повтор.",
                )
            adapter = ADAPTERS.get(platform)
            if adapter is None or not adapter.configured():
                raise HTTPException(
                    409,
                    "Площадка не настроена. Исправь подключение и проверь авторизацию перед повтором.",
                )
        result = await io(_reconcile_crosspost_outcome, post_public_id, platform, request.action)
        if result.get("error") == "not_found":
            raise HTTPException(404, "post not found")
        if result.get("error") == "still_in_flight":
            raise HTTPException(409, "Публикация ещё может выполняться; повторно проверьте позже.")
        if result.get("error") == "not_reconcilable":
            raise HTTPException(409, detail=f"Кросспост нельзя сверить из текущего состояния (текущий статус: {result.get('status')})")
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "reconcile failed"))
        if request.action == "retry_after_check":
            from tgfarm import s9_growth

            outcomes = await s9_growth.crosspost_post(result["post_id"], [platform])
            result["outcomes"] = [outcome.model_dump() for outcome in outcomes]
        _operator("crosspost-reconcile", f"{post_public_id}:{platform}", result["detail"])
        return result

    # --------------------------------------------------------------- actions
    @app.post("/api/cycle")
    async def run_cycle(collect: bool = True, max_clusters: int = 8) -> dict:
        result = await pipeline._guarded_cycle(
            collect=collect,
            max_clusters=max(1, min(max_clusters, pipeline.MAX_CLUSTERS_PER_CYCLE)),
        )
        if result is None:
            health = pipeline.cycle_health()
            if health.get("state") == "running":
                raise HTTPException(409, "Полный цикл уже выполняется; повторный запуск пропущен.")
            raise HTTPException(503, "Цикл не завершился успешно. Проверьте состояние и журнал фермы.")
        return result.as_dict()

    @app.post("/api/posts/publish")
    async def force_publish(request: PublishRequest) -> dict:
        from tgfarm import s7_publish

        if not request.post_public_ids:
            raise HTTPException(400, "empty post list")
        unknown_ids = await io(_unknown_publish_ids, request.post_public_ids)
        if unknown_ids:
            raise HTTPException(
                409,
                detail={
                    "error": "publish_outcome_unknown",
                    "post_public_ids": unknown_ids,
                    "message": "Проверьте площадку и разрешите повтор через reconcile.",
                },
            )
        blocked_ids = await io(_blocked_publish_now_ids, request.post_public_ids)
        if blocked_ids:
            raise HTTPException(
                409,
                detail={
                    "error": "post_not_publishable_now",
                    "posts": blocked_ids,
                    "message": "Немедленная публикация разрешена только для постов в очереди или прошедших гейты.",
                },
            )
        updated = await io(s7_publish.unpublish, request.post_public_ids)
        published = await s7_publish.publish_due(
            limit=len(request.post_public_ids),
            post_public_ids=request.post_public_ids,
        )
        _operator(
            "publish",
            ",".join(request.post_public_ids)[:64],
            f"rescheduled={updated} published={published.get('published')} failed={published.get('failed')}",
        )
        return {"rescheduled": updated, **published}

    @app.post("/api/posts/{post_public_id}/retry")
    async def retry_failed_publish(post_public_id: str) -> dict:
        result = await io(_retry_failed_publish, post_public_id)
        if not result.get("ok"):
            code = 404 if result.get("error") == "not_found" else 409
            raise HTTPException(code, result.get("error", "retry failed"))
        _operator(
            "publish-retry",
            post_public_id,
            f"manually requeued after definite failure on {result['platform']}",
        )
        return result

    @app.post("/api/posts/{post_public_id}/reconcile")
    async def reconcile_publish(
        post_public_id: str, request: PublishReconcileRequest
    ) -> dict:
        if request.action == "retry_after_check" and not request.confirm_not_published:
            raise HTTPException(
                400,
                "Для повтора нужно подтвердить, что вы проверили площадку и поста там нет.",
            )
        result = await io(_reconcile_publish_outcome, post_public_id, request.action)
        if result.get("error") == "not_found":
            raise HTTPException(404, "post not found")
        if result.get("error") == "not_unknown":
            raise HTTPException(
                409,
                detail=f"Пост не ожидает сверки (текущий статус: {result.get('status')})",
            )
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "reconcile failed"))
        _operator("publish-reconcile", post_public_id, result["detail"])
        return result

    @app.post("/api/posts/reject")
    async def reject(request: RejectRequest) -> dict:
        rejected = await io(_reject, request.post_public_ids, request.reason)
        _operator(
            "reject",
            ",".join(request.post_public_ids)[:64],
            f"{request.reason} (n={rejected})",
        )
        return {"rejected": rejected}

    @app.put("/api/posts/{post_public_id}/draft")
    async def update_draft(post_public_id: str, request: DraftUpdate) -> dict:
        """Edit a post by hand before it goes out.

        Editing invalidates the quality score it had when generated, because that
        score described a different text — leaving it would let a rewritten post
        through gates it no longer passes. Re-running the gates is the honest
        option, and it is what a pre-publish editor should do.
        """
        return await io(_update_draft, post_public_id, request)

    @app.post("/api/posts/{post_public_id}/preview")
    async def preview(post_public_id: str) -> dict:
        """Render the post the way Telegram will show it, before publishing."""
        return await io(_preview_post, post_public_id)

    @app.get("/api/dzen/status")
    async def dzen_status() -> dict:
        """Is the Yandex session behind the Dzen profile still alive?"""
        def _load() -> dict:
            from tgfarm.dzen_web import DzenWebPublisher

            publisher = DzenWebPublisher()
            result = asyncio.run(publisher.cached_status())
            result["profile_ready"] = publisher.profile_ready()
            return result

        return await io(_load)

    @app.post("/api/dzen/login")
    async def dzen_login(timeout: int = Query(420, ge=60, le=1800)) -> dict:
        """Open the Dzen window and keep the Yandex session in the profile.

        The browser is headed on purpose: the operator types the phone number
        and the code himself. The farm waits, then saves the session.
        """
        def _run() -> dict:
            from tgfarm.dzen_web import DzenNotLoggedIn, DzenWebPublisher

            publisher = DzenWebPublisher(headless=False)
            try:
                result = asyncio.run(publisher.login(timeout_s=timeout))
                from tgfarm.platforms import record_auth_check

                record_auth_check(
                    "dzen", True,
                    account=str(result.get("publisher_name") or result.get("account") or ""),
                    detail="Сессия сохранена через браузерный вход.",
                )
                _operator("dzen-login", "dzen", "сессия обновлена из панели")
                return {"ok": True, "authorized": True, **result}
            except DzenNotLoggedIn as exc:
                from tgfarm.platforms import record_auth_check

                record_auth_check("dzen", False, detail=redact_sensitive_text(exc, limit=200))
                return {"ok": False, "authorized": False, "error": redact_sensitive_text(exc, limit=200)}

        return await io(_run)

    @app.get("/api/posts/{post_public_id}/trace")
    async def post_trace(post_public_id: str) -> dict:
        """Ordered life of a post: generation, gates, retries, publishing."""
        return await io(_post_trace, post_public_id)

    @app.post("/api/posts/{post_public_id}/polish")
    async def polish(post_public_id: str, request: PolishRequest) -> dict:
        """Rewrite an existing draft in one direction, then re-run the gates."""
        return await io(_polish_post, post_public_id, request.mode)

    @app.get("/api/audit-log")
    async def audit_log(limit: int = Query(50, ge=1, le=300)) -> dict:
        def _load() -> list[dict]:
            with session_scope() as session:
                rows = session.execute(
                    select(OperatorLog).order_by(OperatorLog.id.desc()).limit(limit)
                ).scalars().all()
                return [
                    {
                        "at": row.created_at,
                        "action": row.action,
                        "target": row.target,
                        "detail": row.detail,
                    }
                    for row in rows
                ]

        return {"entries": await io(_load)}

    @app.get("/api/metrics")
    async def metrics() -> dict:
        """Counters the operator (or a dashboard) can watch over time."""
        from tgfarm.llm.client import get_stats

        def _load() -> dict:
            with session_scope() as session:
                by_status = dict(
                    session.execute(
                        select(Post.status, func.count()).group_by(Post.status)
                    ).all()
                )
                stuck = session.execute(
                    select(func.count()).select_from(Post).where(Post.status == "publishing")
                ).scalar_one()
                slowest = session.execute(
                    select(PipelineRun.stage, PipelineRun.duration_ms)
                    .order_by(PipelineRun.duration_ms.desc())
                    .limit(5)
                ).all()
            return {
                "posts_by_status": {str(k): int(v) for k, v in by_status.items()},
                "publishing": int(stuck),
                "slowest_stages": [
                    {"stage": stage, "ms": int(ms)} for stage, ms in slowest
                ],
                "llm": get_stats(),
                "alerts": pipeline.collect_alerts(),
            }

        return await io(_load)

    # --------------------------------------------------------------- telegram
    @app.get("/api/telegram")
    async def telegram_state() -> dict:
        """How much of the account integration is actually available."""
        return await io(_telegram_state)

    @app.get("/api/telegram/chats")
    async def telegram_chats(limit: int = Query(100, ge=1, le=500)) -> dict:
        return await io(_telegram_chats, limit)

    @app.get("/api/telegram/top")
    async def telegram_top(
        limit: int = Query(10, ge=1, le=50),
        min_participants: int = Query(1000, ge=0),
    ) -> dict:
        return await io(_telegram_top, limit, min_participants)

    @app.post("/api/telegram/sync")
    async def telegram_sync() -> dict:
        return await io(_telegram_sync)

    @app.post("/api/telegram/stats")
    async def telegram_stats() -> dict:
        return await io(_telegram_stats)

    return app


app = create_app()
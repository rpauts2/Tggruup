"""FastAPI control plane + dashboard.

Serves the web UI at `/` and a read-mostly JSON API under `/api`. Blocking DB work
goes through `io()` so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from tgfarm import pipeline
from tgfarm.config import get_config
from tgfarm.core import apiauth
from tgfarm.core.apiauth import ensure_token, is_authorised, is_public_path
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
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
        log.warning("operator log failed: %s", exc)


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
                "url": r.url,
                "error": (r.error or "")[:200] or None,
                "at": r.created_at,
            }
            for r in rows
        ]


def _report(days: int) -> dict:
    from tgfarm import s8_analytics
    from tgfarm.s8_analytics.dzen_web import metrics_status

    return {
        "fleet": s8_analytics.fleet_summary(days),
        "angles": s8_analytics.angle_performance(days),
        "hook_styles": s8_analytics.hook_style_performance(days),
        "holdout": s8_analytics.holdout_report(days),
        "guardrail": s8_analytics.style_diversity(min(days, 7)),
        "platform_health": s8_analytics.platform_health(min(days, 14)),
        "hours": s8_analytics.slot_performance(days),
        "dzen": metrics_status(),
    }


def _channels() -> list[dict]:
    from tgfarm import s8_analytics

    return s8_analytics.channel_inventory()


def _reject(public_ids: list[str], reason: str) -> int:
    with session_scope() as session:
        rows = session.execute(
            select(Post).where(Post.public_id.in_(public_ids))
        ).scalars().all()
        for row in rows:
            row.status = "rejected"
            row.reject_reason = reason
        return len(rows)


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
            log.warning("vector index warmup failed: %s", exc)

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

    @app.get("/api/runs")
    async def runs(limit: int = Query(30, ge=1, le=200)) -> dict:
        return {"runs": await io(_runs, limit)}

    @app.get("/api/crossposts")
    async def crossposts(limit: int = Query(50, ge=1, le=300)) -> dict:
        return {"crossposts": await io(_crossposts, limit)}

    # --------------------------------------------------------------- actions
    @app.post("/api/cycle")
    async def run_cycle(collect: bool = True, max_clusters: int = 8) -> dict:
        result = await pipeline.run_cycle(collect=collect, max_clusters=max_clusters)
        return result.as_dict()

    @app.post("/api/posts/publish")
    async def force_publish(request: PublishRequest) -> dict:
        from tgfarm import s7_publish

        if not request.post_public_ids:
            raise HTTPException(400, "empty post list")
        updated = await io(s7_publish.unpublish, request.post_public_ids)
        published = await s7_publish.publish_due(limit=len(request.post_public_ids))
        _operator(
            "publish",
            ",".join(request.post_public_ids)[:64],
            f"published={published.get('published')} failed={published.get('failed')}",
        )
        return {"rescheduled": updated, **published}

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
                _operator("dzen-login", "dzen", "сессия обновлена из панели")
                return {"ok": True, **result}
            except DzenNotLoggedIn as exc:
                return {"ok": False, "authorized": False, "error": str(exc)[:200]}

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
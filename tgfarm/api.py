"""FastAPI control plane + dashboard.

Serves the web UI at `/` and a read-mostly JSON API under `/api`. Blocking DB work
goes through `io()` so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from tgfarm import pipeline
from tgfarm.config import get_config
from tgfarm.core import apiauth
from tgfarm.core.apiauth import ensure_token, is_authorised, is_public_path
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.core.security import redact_sensitive_text
from tgfarm.crosspost_journal import (
    JOURNAL_STATUS_DRY_RUN,
    JOURNAL_STATUS_FAILED,
    JOURNAL_STATUS_POSTED,
    JOURNAL_STATUS_PUBLISHING,
    JOURNAL_STATUS_RETRY_AUTHORIZED,
    JournalStatusClass,
    journal_status_class,
    journal_status_of,
)
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


class FailedRequeueRequest(BaseModel):
    """The bulk requeue asks for the same three things the CLI does: how many,
    where, and why. `apply` is False in the shipped default because the panel's
    first call is the preview, and a click that only looked must be the default."""

    apply: bool = False
    limit: int = 50
    platform: str | None = None
    reason: str = ""


class CrosspostReconcileRequest(BaseModel):
    action: Literal["confirm_posted", "retry_after_check"]
    confirm_not_published: bool = False


class PlatformToggleRequest(BaseModel):
    enabled: bool


class RuntimeModeRequest(BaseModel):
    mode: Literal["off", "dry", "live"]
    confirm_live: bool = False


class PlatformPlanRequest(BaseModel):
    planned: bool


class AgentBenchmarkRequest(BaseModel):
    cluster_public_id: str = Field(min_length=3, max_length=40)
    channel_id: str | None = Field(default=None, max_length=64)


class PlatformCredentialsRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)

class TikTokPublishRequest(BaseModel):
    caption: str = Field(min_length=1, max_length=5000)
    privacy_level: str = Field(min_length=1, max_length=40)
    consent: bool = False


class DouyinPublishRequest(BaseModel):
    caption: str = Field(min_length=1, max_length=1000)
    consent: bool = False


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
        # VK wall metrics need a user token: the group token used for posting is
        # rejected by VK with error 27, so the honest state must be visible.
        "vk_metrics": {
            "available": bool(str(getattr(settings, "vk_user_token", "") or "").strip()),
            "required": "VK_USER_TOKEN (user token with the wall scope)",
            "reason": (
                "user token is configured"
                if str(getattr(settings, "vk_user_token", "") or "").strip()
                else "group tokens cannot read wall metrics (VK error 27)"
            ),
        },
    }


def _status_payload() -> dict:
    """Everything the dashboard's KPI row needs, in one pass."""
    base = pipeline.status()
    with session_scope() as session:
        by_status = dict(
            session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
        )
        total_posts = sum(by_status.values())
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
        # Neither rate may invent a zero. An empty `posts` table does not mean
        # "nothing rejected", it means the ratio has no denominator, and
        # `func.avg` over no rows (or over rows whose `err` is NULL) is the
        # measurement that never happened — P34 says that is `None`, and the
        # panel prints «нет отчёта».
        "reject_rate": round(by_status.get("rejected", 0) / total_posts, 3) if total_posts else None,
        "avg_er": round(float(avg_er), 4) if avg_er is not None else None,
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
            # A flagged reprint is evidence of copying, not of a second outlet
            # covering the story, so the panel counts only what survived dedup.
            items = session.execute(
                select(RawItem).where(
                    RawItem.cluster_id == row.id,
                    RawItem.duplicate_of.is_(None),
                )
            ).scalars().all()
            out.append(
                {
                    "id": row.public_id,
                    "label": row.label,
                    "niche": row.niche,
                    "heat": row.heat,
                    "status": row.status,
                    "items": len(items),
                    "trend_phase": row.trend_phase,
                    "acceleration": row.acceleration,
                    "outlets": sorted({i.source_id for i in items}),
                }
            )
        return out


def _skip_entry_present(cluster_public_id: str, skips: list) -> bool:
    """Whether S3's registry really holds a record for this cluster.

    `route_skips` collapses "no record" and "record with nothing skipped" into
    the same empty list, so the panel could not tell a restart/eviction apart
    from a clean plan. The registry map is consulted read-only; if that private
    name ever disappears, the fallback only ever reports *fewer* remembered
    clusters — it cannot invent a record that was never written.
    """
    from tgfarm import s3_router

    registry = getattr(s3_router, "_SKIP_REGISTRY", None)
    if isinstance(registry, Mapping):
        return cluster_public_id in registry
    return bool(skips)


def _router_skip_report(cluster_public_id: str) -> dict | None:
    """Skipped destinations of one cluster plus the memory facts around them.

    Returns None only when the router has no record *and* the cluster is not in
    the database either: a cluster that was simply never routed in this process
    is an honest empty answer, not a missing object.
    """
    from tgfarm import s3_router

    skips = s3_router.route_skips(cluster_public_id)
    remembered = _skip_entry_present(cluster_public_id, skips)
    with session_scope() as session:
        in_db = bool(
            session.execute(
                select(func.count()).select_from(Cluster).where(Cluster.public_id == cluster_public_id)
            ).scalar_one()
        )
    if not remembered and not in_db:
        return None

    if remembered and skips:
        state = "skipped_recorded"
        state_note = "Маршрутизатор записал эти площадки как пропущенные в текущем процессе."
    elif remembered:
        state = "routed_without_skips"
        state_note = "Кластер роутился в этом процессе, и ни одна площадка не была исключена."
    else:
        state = "no_record"
        state_note = (
            "Записи нет: кластер либо ещё не роутился в этом процессе, либо его запись "
            "вытеснило LRU, либо процесс перезапускался. Это НЕ значит, что ничего не пропущено."
        )
    return {
        "cluster_public_id": cluster_public_id,
        "skipped": [
            {
                "cluster_public_id": item.cluster_public_id,
                "channel_id": item.channel_id,
                "platform": item.platform,
                "capability": item.capability,
                "capability_label": item.capability_label,
                "reason": item.reason,
                "note": item.note(),
            }
            for item in skips
        ],
        "count": len(skips),
        "remembered": remembered,
        "cluster_in_db": in_db,
        "state": state,
        "state_note": state_note,
        "storage": "in-process-memory",
        "restart_clears": True,
        "eviction": "lru",
        "registry_limit": s3_router.SKIP_REGISTRY_LIMIT,
    }


_BACKFILL_FACT_KEYS = (
    "rows_scanned",
    "links_found",
    "links_written",
    "candidates_remaining",
    "pages_read",
    "pool_peak",
    "unfingerprinted_rows",
    "undated_rows",
    "stopped_by",
    "budget_exhausted",
    "elapsed_s",
    "window_hours",
    "dry_run",
)


def _reprint_backfill_report() -> dict:
    """The last reprint-backfill pass plus the state the panel must not guess.

    The report lives in the backfill module's process memory only (mirroring
    `_router_skip_report`): it exists once the maintenance step has called
    `note_backfill_report` in this process, and a restart clears it. So
    "never ran here" stays its own state and never collapses into "nothing
    remains"; numbers that were never measured travel as null, never as 0.
    """
    from tgfarm.core.run import ensure_utc
    from tgfarm.s1_sources import backfill_reprints as backfill

    report = backfill.get_last_backfill_report()
    recorded_at = backfill.get_last_backfill_recorded_at()

    if report is None:
        payload = dict.fromkeys(_BACKFILL_FACT_KEYS)
        state = "never_run"
        state_note = (
            "Бэкфилл репринтов ещё не выполнялся в этом процессе (или процесс "
            "перезапускался). Это НЕ значит, что ничего не осталось: остаток здесь "
            "просто не измерен."
        )
        cursor_payload = None
    else:
        payload = report.as_dict()
        if report.stopped_by == backfill.COMPLETE and report.candidates_remaining == 0:
            state = "finished"
            state_note = (
                "Проход дошёл до конца: кандидатов на связывание не осталось. "
                f"Строк без отпечатка {report.unfingerprinted_rows} и без даты "
                f"{report.undated_rows} кандидатами не считаются — этот бэкфилл их "
                "не свяжет никогда."
            )
        elif report.budget_exhausted:
            state = "remainder"
            state_note = (
                f"Проход остановлен по {report.stopped_by}: осталось "
                f"{report.candidates_remaining} кандидатов; следующий срез "
                "продолжит с этого курсора."
            )
        else:
            state = "remainder"
            state_note = (
                f"Проход прошёл всю историю, но кандидатов всё ещё "
                f"{report.candidates_remaining}."
            )
            if report.dry_run:
                state_note += " Пробный проход: связи не записывались."
        cursor = report.next_cursor
        cursor_payload = (
            None
            if cursor is None
            else {"moment": ensure_utc(cursor.moment).isoformat(), "row_id": int(cursor.row_id)}
        )

    payload["next_cursor"] = cursor_payload
    payload["recorded_at"] = None if recorded_at is None else recorded_at.isoformat()
    payload["state"] = state
    payload["state_note"] = state_note
    payload["storage"] = "in-process-memory"
    payload["restart_clears"] = True
    return payload


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
    # Load all attached media: the original vertical MP4 is deliberately
    # non-primary so it never replaces the post's cover image.
    media = session.execute(
        select(Media)
        .where(Media.post_id == row.id)
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
    # Prefer the explicit primary cover, but choose video independently.
    # The renderer intentionally marks MP4 as non-primary so it cannot replace
    # the article cover; filtering to primary rows would hide it from the UI.
    alive = [m for m in media if Path(m.path).exists()]
    cover_rows = [m for m in media if m.kind != "video"]
    alive_covers = [m for m in alive if m.kind != "video"]
    video_rows = [m for m in media if m.kind == "video"]
    alive_videos = [m for m in alive if m.kind == "video"]
    cover = (
        next((m for m in alive_covers if m.is_primary), None)
        or next(iter(alive_covers), None)
        or next((m for m in cover_rows if m.is_primary), None)
        or next(iter(cover_rows), None)
    )
    video = alive_videos[-1] if alive_videos else (video_rows[-1] if video_rows else None)

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
            "crossposts": [
                {
                    "platform": entry.platform,
                    "status": entry.status,
                    "at": entry.created_at,
                    "attempt": entry.attempt,
                    "url": entry.url if str(entry.url or "").startswith("https://") else None,
                    "message": _crosspost_status_message(entry.status),
                }
                for entry in session.execute(
                    select(CrosspostLog)
                    .where(CrosspostLog.post_public_id == row.public_id)
                    .order_by(CrosspostLog.id.desc())
                    .limit(30)
                ).scalars().all()
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


def _instagram_public_media_path(media_id: str) -> Path:
    """Resolve a signed Instagram media id to an asset on a published post."""
    from tgfarm.db.models import Media as MediaRow

    try:
        numeric_id = int(media_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(404, "media not found") from exc
    with session_scope() as session:
        row = session.execute(
            select(MediaRow)
            .join(Post, Post.id == MediaRow.post_id)
            .where(MediaRow.id == numeric_id, Post.status == "published")
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "media not found")
        path = Path(row.path)
    if not path.is_file():
        raise HTTPException(410, "media file missing on disk")
    return path


#: The journal's counter columns are `INTEGER NOT NULL`, so a stage that never
#: measured its work cannot say so in the column. The writer
#: (`tgfarm.pipeline._record`) stores a placeholder 0 there and puts the truth
#: beside it: `meta["counters"]` is `measured` / `partial` / `not_reported` and
#: `meta["unmeasured"]` names the columns whose 0 is only a placeholder.
#: A reader that hands the columns straight back re-invents a fabricated zero,
#: and the operator can no longer tell "the stage handled nothing" from "nobody
#: counted" — which is the only distinction the journal was fixed to make.
#: Every reader of these two columns therefore goes through `_stage_counters`.
COUNTER_STATES = ("measured", "partial", "not_reported")
#: Rows written before the counters contract existed. Their zeros are
#: unverifiable, which is a third thing and not either of the two claims it is
#: usually collapsed into: naming it keeps the reader from promoting "we cannot
#: tell" to "measured nothing" (a false zero) or to "never measured" (a false
#: accusation against a stage that did report).
LEGACY_COUNTER_STATE = "legacy"
COUNTER_COLUMNS = ("items_in", "items_out")


def _raw_count(row: PipelineRun, column: str) -> int | None:
    """A counter column as an int, or None when the stored value is unusable."""
    try:
        return int(getattr(row, column))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _stage_counters(row: PipelineRun) -> tuple[int | None, int | None, str]:
    """Read one journal row's counters as `(items_in, items_out, counters_state)`.

    `None` means "nobody measured this"; `0` means "measured: nothing happened".
    Collapsing the first into the second is the false report this function
    exists to prevent, so the pair travels together with a state that names it.

    Decoding rules, most-specific first:

    * `meta["unmeasured"]` wins over `meta["counters"]` when both are present.
      The list names the columns whose 0 is a placeholder; the marker only
      counts how many are, so the list is the more precise statement.
    * `counters == "not_reported"` marks both columns; `"partial"` without the
      list marks the placeholder columns that hold a 0 — one of them is real but
      the row does not say which, and guessing the wrong one would print a
      count the stage never produced.
    * A row whose marker is missing, or is not one of the three states, is
      treated as having no marker at all: it predates the contract or its meta
      is unreadable, and either way its zeros are unverifiable. A non-zero
      column can only have come from a stage that actually counted (every
      writer so far defaults to 0), while a 0 could be either a real nothing or
      the old placeholder, so an unmarked 0 reads as unknown.

    The state is finally recomputed from the values being returned, so a
    response can never claim `measured` while handing back an unknown.
    """
    meta = row.meta if isinstance(row.meta, dict) else {}
    listed = {str(name) for name in (meta.get("unmeasured") or [])} & set(COUNTER_COLUMNS)
    declared = str(meta.get("counters") or "")
    contract = declared in COUNTER_STATES or bool(listed)

    unknown = set(listed)
    if not contract:
        unknown = {column for column in COUNTER_COLUMNS if _raw_count(row, column) in (None, 0)}
    elif declared == "not_reported":
        unknown = set(COUNTER_COLUMNS)
    elif declared == "partial" and not listed:
        zeros = {column for column in COUNTER_COLUMNS if _raw_count(row, column) in (None, 0)}
        unknown = set(COUNTER_COLUMNS) if len(zeros) == 2 else zeros

    values = tuple(
        None if column in unknown else _raw_count(row, column) for column in COUNTER_COLUMNS
    )
    # An unusable column value is unknown even when the row did not list it, so
    # the state is derived from what is actually returned rather than trusted.
    if contract:
        state = {0: "not_reported", 1: "partial", 2: "measured"}[sum(v is not None for v in values)]
    else:
        state = LEGACY_COUNTER_STATE
    return values[0], values[1], state


def _runs(limit: int) -> list[dict]:
    """The run journal as the API serves it: counters are nullable, state travels.

    `in` / `out` are `int | None` on purpose even though the columns are NOT
    NULL — the payload is where the placeholder stops being a number. Anything
    that totals or charts these fields must treat `None` as absent, not as zero:
    a sum over `out` that read unknown as 0 would silently understate a stage
    that did work, which is the same bug one layer up. Report such an aggregate
    as `(sum, measured_runs)`, or drop it; never as a bare total.

    Aggregates over `ms` are unaffected: `duration_ms` is always measured, so
    `/api/metrics`'s `slowest_stages` stays a real number.
    """
    with session_scope() as session:
        rows = session.execute(
            select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
        ).scalars().all()
        result = []
        for r in rows:
            items_in, items_out, counters = _stage_counters(r)
            result.append(
                {
                    "run_id": r.run_id,
                    "stage": r.stage,
                    "status": r.status,
                    # `None` is "never measured", `0` is "measured: nothing".
                    # Render the first as "—" and the second as "0".
                    "in": items_in,
                    "out": items_out,
                    "counters_state": counters,
                    "ms": r.duration_ms,
                    "error": (r.error or "")[:200] or None,
                    "at": r.created_at,
                }
            )
        return result


def _crosspost_status_message(status: str) -> str:
    """Safe operator hint: never leak the raw exception/provider response."""
    return {
        "posted": "Публикация подтверждена площадкой.",
        "scheduled": "Площадка приняла публикацию в свою очередь обработки или отложенного размещения.",
        "dry_run": "Это был тестовый прогон; публикация в сеть не отправлялась.",
        "skipped": "Отправка пропущена: проверь переключатель и настройки площадки.",
        "failed": "Временная ошибка запроса. Перед повтором проверь, не появился ли пост.",
        "permanent": "Площадка отклонила запрос. Проверь разрешения, формат и настройки.",
        "permanent_failure": "Площадка отклонила запрос. Проверь разрешения, формат и настройки.",
        "unknown": "Результат неоднозначен. Сначала проверь аккаунт площадки вручную.",
        "publishing": "Отправка началась, но не подтверждена. Проверь результат перед повтором.",
        "retry_authorized": "Повтор разрешён оператором после ручной сверки.",
    }.get(str(status or "").lower(), "Статус доставки сохранён. Проверь журнал площадки.")


def _crosspost_incident(post_public_id: str, platform: str) -> dict:
    """Create a minimal incident export without raw provider errors, tokens or post text."""
    from tgfarm.s9_growth.adapters import ADAPTERS

    platform = str(platform or "").strip().lower()
    if platform not in ADAPTERS:
        raise HTTPException(404, "unknown platform")
    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            raise HTTPException(404, "post not found")
        rows = session.execute(
            select(CrosspostLog)
            .where(
                CrosspostLog.post_public_id == post_public_id,
                CrosspostLog.platform == platform,
            )
            .order_by(CrosspostLog.id.desc())
            .limit(25)
        ).scalars().all()
        history = [
            {
                "status": str(row.status or "unknown"),
                "at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
                "attempt": int(row.attempt or 1),
                "url": row.url[:500] if isinstance(row.url, str) and row.url.startswith("https://") else None,
                "message": _crosspost_status_message(row.status),
            }
            for row in rows
        ]
        return {
            "schema": "tgfarm.delivery-incident.v1",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "post_id": post_public_id,
            "post_status": str(post.status or "unknown"),
            "platform": platform,
            "latest_status": history[0]["status"] if history else "not_attempted",
            "latest_message": history[0]["message"] if history else "В журнале нет попыток отправки для этой площадки.",
            "attempt_count": len(history),
            "history": history,
            "privacy": {
                "includes_post_text": False,
                "includes_raw_provider_errors": False,
                "includes_credentials_or_tokens": False,
                "includes_account_profile": False,
            },
        }


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


#: How long a `publishing` journal row still counts as "in flight" before both
#: operator-facing readers agree it is stuck: the fleet/sources counter raises
#: attention at this age, and the reconcile gate stops answering
#: `still_in_flight` past it. One number for one question — a row must not read
#: as stuck to one panel and as still running to the other.
CROSSPOST_PUBLISHING_STALE_AFTER = timedelta(minutes=30)

#: The journal statuses whose newest row means "the operator has to look at this
#: destination": an outcome nobody can confirm (`unknown`) or a definite refusal
#: of this exact content (`permanent`).
#:
#: Deliberately NOT `JOURNAL_SUPPRESSED_STATUSES`. The three sets in
#: `tgfarm/crosspost_journal.py` answer the planner's question — «may this pair
#: be delivered again?» — while this reader answers «does this need a human?».
#: `posted`, `scheduled` and a fresh `publishing` are all suppressed for re-offer
#: and perfectly ordinary to look at, so importing that set here would raise an
#: alarm on every healthy destination. Only the vocabulary's UNKNOWN *class* is
#: consulted below, and only in the fail-closed direction; the timing conditions
#: (an attempt ceiling, a staleness bound) stay this reader's own.
#:
#: `unknown` and `permanent` are literals because the shared module declares
#: single-status constants for six of its nine tokens and these are not among
#: them; both are members of `JOURNAL_SUPPRESSED_STATUSES`, and
#: `tests/test_journal_readers_shared.py` pins this membership against any change
#: of that vocabulary.
JOURNAL_OPERATOR_ATTENTION_STATUSES = frozenset({"unknown", "permanent"})

#: The newest-row statuses the operator may reconcile at all: the three this code
#: cannot resolve by itself — the request may have been accepted (`unknown`), a
#: claim died before the answer (`publishing`, further gated by
#: `CROSSPOST_PUBLISHING_STALE_AFTER`), or the destination definitely refused
#: (`permanent`). Explicit rather than derived from `JOURNAL_SUPPRESSED_STATUSES`
#: because this gate *authorises a re-send*: `posted`/`scheduled` are suppressed
#: for re-offer precisely because the destination is believed to hold the post,
#: and a token the vocabulary grows tomorrow must become reconcilable through a
#: decision in this line, never by omission. `retry_exhausted` is the only other
#: way in, and it is an attempt count, not a status.
JOURNAL_RECONCILABLE_STATUSES = frozenset({"unknown", JOURNAL_STATUS_PUBLISHING, "permanent"})


def _crosspost_health() -> dict:
    """Summarize the latest state of every material/platform delivery pair."""
    from tgfarm.db.models import CrosspostClaim
    from tgfarm.s9_growth.adapters import MAX_AUTOMATIC_CROSSPOST_ATTEMPTS

    now = datetime.now(UTC)
    stale_before = now - CROSSPOST_PUBLISHING_STALE_AFTER
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
    unexplained_tokens: dict[str, int] = {}
    exhausted_rows: dict[str, int] = {}
    for row in rows:
        item = by_platform.setdefault(
            row.platform,
            {
                "platform": row.platform,
                "targets": 0,
                "posted": 0,
                "scheduled": 0,
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
        # One normalisation for every reader of this column, taken from the
        # shared vocabulary: the raw token is what the writer stored, and a
        # row stored as `"Posted "` or `"PUBLISHING"` used to be counted under
        # a key nobody wrote and to miss the staleness bound below.
        status = journal_status_of(row)
        # The one place this reader does consult the classification, and only
        # in the fail-closed direction: a token the vocabulary does not declare
        # has no counter key here, so before this line such a row was counted
        # as a target, put in no bucket, and reported as `state: not_tested`
        # with `needs_attention` unchanged — a publication nobody can explain
        # was the one kind the operator could not see. It is counted in
        # `unknown` (the bucket that already means "we cannot explain this row")
        # and its raw token is named in a warning below, so the report loses
        # nothing and the pair cannot hide.
        if journal_status_class(status) is JournalStatusClass.UNKNOWN:
            unexplained_tokens[status] = unexplained_tokens.get(status, 0) + 1
            status = "unknown"
        if status in item:
            item[status] += 1

        stamp = row.created_at
        if stamp is not None:
            stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        if status == JOURNAL_STATUS_POSTED:
            previous = item["last_success_at"]
            if previous is None or (stamp and stamp.isoformat() > previous):
                item["last_success_at"] = stamp.isoformat() if stamp else None

        is_stuck = (
            status == JOURNAL_STATUS_PUBLISHING
            and stamp is not None
            and stamp <= stale_before
        )
        if is_stuck:
            item["stuck"] += 1
        # A `failed` row needs a human only once the automatic retry budget is
        # gone; the ceiling is the adapter's own constant, not a number typed
        # here, and the count is kept for the platform state and the
        # `needs_attention` total so both read one definition.
        is_exhausted_failure = (
            status == JOURNAL_STATUS_FAILED
            and int(row.attempt or 1) >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS
        )
        if is_exhausted_failure:
            exhausted_rows[row.platform] = exhausted_rows.get(row.platform, 0) + 1
        attention = (
            status in JOURNAL_OPERATOR_ATTENTION_STATUSES
            or is_exhausted_failure
            or is_stuck
        )
        if attention and stamp is not None:
            previous_attention = item["oldest_attention_at"]
            if previous_attention is None or stamp.isoformat() < previous_attention:
                item["oldest_attention_at"] = stamp.isoformat()

    for token, count in sorted(unexplained_tokens.items()):
        log.warning(
            "crosspost health: %d newest journal row(s) carry status %r, which the shared "
            "vocabulary in tgfarm/crosspost_journal.py does not declare; counted as 'unknown' "
            "and reported as needing attention, because the row may already be an accepted "
            "publication",
            count,
            token,
        )

    for item in by_platform.values():
        # The ceiling test was made once per row above, so the platform state and
        # the `needs_attention` total below cannot drift apart: a counted row is
        # by construction also counted in `item["failed"]`, which is why the
        # boolean needs no bucket conjunct of its own.
        exhausted = bool(exhausted_rows.get(item["platform"]))
        if item["unknown"] or item["permanent"] or exhausted or item["stuck"]:
            item["state"] = "attention"
        elif item["publishing"] or item["retry_authorized"] or item["failed"]:
            item["state"] = "working"
        elif item["scheduled"]:
            item["state"] = "scheduled"
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
        "scheduled": sum(item["scheduled"] for item in by_platform.values()),
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
            + exhausted_rows.get(item["platform"], 0)
            for item in by_platform.values()
        ),
    }
    overall = (
        "attention" if totals["needs_attention"] or totals["stale_claims"]
        else "working" if totals["publishing"] or totals["failed"] or totals["retry_authorized"]
        else "scheduled" if totals["scheduled"]
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


#: States in which a post still waits for something. `failed` belongs here because
#: the farm does not retry it on its own — it waits for an operator, and how long
#: it has been waiting is the whole question. `publish_unknown` waits harder: the
#: heartbeat watchdog and the dead-driver branch both leave the post with no
#: booking time and no publication stamp, so only a human can say whether the
#: platform ever received it (live 2026-11-05: 51 such rows, the oldest 6 days).
#: `tgfarm/s2_trends/explain.py` already calls both states in-flight; a card that
#: counted one and not the other would be a second story to keep in sync.
BACKLOG_WAITING = ("draft", "gated", "scheduled", "publishing", "publish_unknown", "failed")
#: Age bucket edges in hours. Fixed, not percentile-based, so «leży 3 дня» means
#: the same thing on every platform and between two refreshes.
BACKLOG_AGE_EDGES_H = (24.0, 72.0, 168.0)
BACKLOG_FLOW_HOURS = 24
BACKLOG_COMPARE_DAYS = 7
#: Which current statuses describe the fate of a post created in the window. A
#: rejection overwrites the status, so «создано и отклонено» is only readable as a
#: cohort outcome of `created_at`, never as an event clock of its own.
BACKLOG_COHORT = {"rejected": "rejected", "gated": "gated", "draft": "draft"}


def _utc(value: datetime | None) -> datetime | None:
    """One clock for the stamps SQLite hands back as naive UTC text."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age_hours(value: datetime | None, now: datetime) -> float | None:
    """Hours between a stamp and `now`, or None when there is no stamp to read."""
    stamp = _utc(value)
    if stamp is None:
        return None
    return round(max(0.0, (now - stamp).total_seconds() / 3600.0), 2)


def _backlog(*, now: datetime | None = None) -> dict:
    """Flow and stock of the publication queue per platform.

    Until now the only way to answer «что происходит с очередью» was SQL, and the
    question was usually asked of the wrong column: the `scheduled` bucket counts
    posts that are booked for the future, so a post scheduled and published the
    same day never shows up in it and a full day of work reads as nothing moving.
    The flow therefore rides on three clocks, each read over the same window —
    `created_at` (intake, split into what those very posts became), `scheduled_at`
    (bookings) and `published_at` (output) — and the stock is reported next to it
    with ages, so VK's forward buffer and Дзен's overdue-and-retried pile cannot
    look the same.

    Nothing is invented: an age, a rate or a blocking reason that could not be
    computed is `None`, which the panel prints as «нет отчёта» (P34). The blocking
    reason is the very ledger `/api/status` shows — `pipeline.last_publish_decision`
    reading `meta.decision` of the newest publish tick — because a second place
    that decides why nothing went out is a second story to keep in sync.
    """
    current = _utc(now) or datetime.now(UTC)
    flow_since = current - timedelta(hours=BACKLOG_FLOW_HOURS)
    week_since = current - timedelta(days=BACKLOG_COMPARE_DAYS)
    ages_since = [current - timedelta(hours=edge) for edge in BACKLOG_AGE_EDGES_H]
    # Posts inherit their platform from the channel row. A channel that is no
    # longer in the table is reported as `unknown`, never guessed as telegram.
    platform_col = func.lower(func.coalesce(Channel.platform, "unknown"))
    error: str | None = None
    platforms: dict[str, dict[str, Any]] = {}

    def entry(name: str) -> dict[str, Any]:
        return platforms.setdefault(
            name,
            {
                "platform": name,
                "channels": 0,
                "channels_enabled": 0,
                "flow": {"created": 0, "rejected": 0, "gated": 0, "draft": 0,
                         "booked": 0, "published": 0},
                "week": {"created": 0, "rejected": 0, "booked": 0, "published": 0},
                "rates": {"created_per_day": 0.0, "published_per_day": 0.0, "lead": None},
                "stock": [],
                "waiting": 0,
                "oldest_age_hours": None,
            },
        )

    try:
        with session_scope() as session:
            inventory = session.execute(
                select(
                    platform_col,
                    func.count(),
                    func.sum(case((Channel.enabled, 1), else_=0)),
                ).group_by(platform_col)
            ).all()
            intake = session.execute(
                select(
                    platform_col,
                    Post.status,
                    func.count(),
                    func.sum(case((Post.created_at >= flow_since, 1), else_=0)),
                )
                .select_from(Post)
                .outerjoin(Channel, Post.channel_id == Channel.id)
                .where(Post.created_at >= week_since)
                .group_by(platform_col, Post.status)
            ).all()
            clocks = session.execute(
                select(
                    platform_col,
                    # A booking counts as flow only once its time has arrived: an
                    # open buffer booked for next week is stock, not a day's work,
                    # and reading it as bookings "made today" is exactly how the
                    # queue's 269 VK posts looked like four idle days.
                    func.sum(case(
                        (and_(Post.scheduled_at >= flow_since, Post.scheduled_at < current), 1),
                        else_=0,
                    )),
                    func.sum(case(
                        (and_(Post.scheduled_at >= week_since, Post.scheduled_at < current), 1),
                        else_=0,
                    )),
                    func.sum(case((Post.published_at >= flow_since, 1), else_=0)),
                    func.sum(case((Post.published_at >= week_since, 1), else_=0)),
                )
                .select_from(Post)
                .outerjoin(Channel, Post.channel_id == Channel.id)
                .where(or_(
                    and_(Post.scheduled_at >= week_since, Post.scheduled_at < current),
                    Post.published_at >= week_since,
                ))
                .group_by(platform_col)
            ).all()
            stock = session.execute(
                select(
                    platform_col,
                    Post.status,
                    func.count(),
                    *[func.sum(case((Post.created_at >= since, 1), else_=0))
                      for since in ages_since],
                    func.min(Post.created_at),
                    func.sum(case((Post.scheduled_at < current, 1), else_=0)),
                    func.sum(case((Post.scheduled_at.is_(None), 1), else_=0)),
                    func.coalesce(func.sum(Post.attempts), 0),
                    func.sum(case((Post.attempts > 0, 1), else_=0)),
                )
                .select_from(Post)
                .outerjoin(Channel, Post.channel_id == Channel.id)
                .where(Post.status.in_(BACKLOG_WAITING))
                .group_by(platform_col, Post.status)
            ).all()
    except SQLAlchemyError as exc:
        error = redact_sensitive_text(exc, limit=200)
        log.warning("api: backlog read failed: %s", error)
        inventory = intake = clocks = stock = []

    for name, channels, enabled in inventory:
        item = entry(str(name or "unknown"))
        item["channels"] = int(channels or 0)
        item["channels_enabled"] = int(enabled or 0)

    for name, status, week_count, flow_count in intake:
        item = entry(str(name or "unknown"))
        item["flow"]["created"] += int(flow_count or 0)
        item["week"]["created"] += int(week_count or 0)
        cohort = BACKLOG_COHORT.get(str(status or ""))
        if cohort:
            item["flow"][cohort] += int(flow_count or 0)
            if cohort == "rejected":
                item["week"]["rejected"] += int(week_count or 0)

    for name, booked_flow, booked_week, published_flow, published_week in clocks:
        item = entry(str(name or "unknown"))
        item["flow"]["booked"] += int(booked_flow or 0)
        item["week"]["booked"] += int(booked_week or 0)
        item["flow"]["published"] += int(published_flow or 0)
        item["week"]["published"] += int(published_week or 0)

    edges = len(BACKLOG_AGE_EDGES_H)
    for row in stock:
        name, status, measured = str(row[0] or "unknown"), str(row[1] or "unknown"), int(row[2] or 0)
        buckets = [int(value or 0) for value in row[3:3 + edges]]
        oldest, overdue, unbooked, attempts, retrying = row[3 + edges:8 + edges]
        item = entry(name)
        # The thresholds nest, so each band is the difference between two
        # cumulative counts instead of a fifth query per edge.
        item["stock"].append({
            "status": status,
            "count": measured,
            "ages": {
                "lt_24h": buckets[0],
                "lt_72h": buckets[1] - buckets[0],
                "lt_168h": buckets[2] - buckets[1],
                "ge_168h": measured - buckets[2],
            },
            "oldest_age_hours": _age_hours(oldest, current),
            # Only a booking time can be late, and only when every row in the
            # bucket has one; a bucket holding unbooked posts reports no count
            # rather than a count that quietly omits them.
            "overdue": int(overdue or 0) if status == "scheduled" and not int(unbooked or 0)
            else None,
            "unbooked": int(unbooked or 0),
            "retries": int(attempts or 0),
            "retrying": int(retrying or 0),
        })

    for item in platforms.values():
        item["stock"].sort(key=lambda row: (-row["count"], row["status"]))
        item["waiting"] = sum(row["count"] for row in item["stock"])
        ages = [row["oldest_age_hours"] for row in item["stock"]
                if row["oldest_age_hours"] is not None]
        item["oldest_age_hours"] = max(ages) if ages else None
        created_per_day = round(item["week"]["created"] / BACKLOG_COMPARE_DAYS, 2)
        published_per_day = round(item["week"]["published"] / BACKLOG_COMPARE_DAYS, 2)
        # A lead against zero published posts is not infinity and not zero, it is
        # an unanswerable question, so it stays None and the panel says so.
        item["rates"] = {
            "created_per_day": created_per_day,
            "published_per_day": published_per_day,
            "lead": round(created_per_day / published_per_day, 1) if published_per_day else None,
        }

    scheduled_rows = [row for item in platforms.values() for row in item["stock"]
                      if row["status"] == "scheduled"]
    totals = {
        "created": sum(item["flow"]["created"] for item in platforms.values()),
        "rejected": sum(item["flow"]["rejected"] for item in platforms.values()),
        "booked": sum(item["flow"]["booked"] for item in platforms.values()),
        "published": sum(item["flow"]["published"] for item in platforms.values()),
        "waiting": sum(item["waiting"] for item in platforms.values()),
        # The sum stays None when any scheduled bucket could not be read, so a
        # half-known queue is never printed as a smaller known one.
        "overdue": None if any(row["overdue"] is None for row in scheduled_rows)
        else sum(row["overdue"] or 0 for row in scheduled_rows),
        "lead": None,
    }
    week_created = sum(item["week"]["created"] for item in platforms.values())
    week_published = sum(item["week"]["published"] for item in platforms.values())
    totals["lead"] = round(week_created / week_published, 1) if week_published else None
    return {
        "generated_at": current.isoformat(),
        "flow_hours": BACKLOG_FLOW_HOURS,
        "compare_days": BACKLOG_COMPARE_DAYS,
        "publication": pipeline.last_publish_decision(),
        "totals": totals,
        "platforms": sorted(platforms.values(), key=lambda item: item["platform"]),
        "error": error,
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


def _journal_identity(
    session,
    post_public_id: str,
    platform: str,
) -> tuple[str | None, str | None]:
    """The best ``remote_id`` and ``url`` the journal already holds for one pair.

    The journal is append-only and the newest row is usually an in-flight one
    that learned nothing — ``publishing`` and ``unknown`` are written before the
    platform answers — so reading an identifier off the row in hand and storing it
    on the confirmation blanked out an id the journal did have, and every reader
    that takes the newest ``posted`` row (the post detail's ``external_url`` and
    ``remote_id``) went back to showing nothing. The same rule the Dzen matcher
    uses (``s8_analytics.dzen_web._journal_id_index``): read across every status
    except ``dry_run``, whose placeholder was never sent anywhere. The id itself is
    kept verbatim rather than prefix-filtered, because a Medium slug may legitimately
    start with ``dry``, and the status already says whether the post existed.
    """
    remote_id: str | None = None
    url: str | None = None
    for row in session.execute(
        select(CrosspostLog)
        .where(
            CrosspostLog.post_public_id == post_public_id,
            CrosspostLog.platform == platform,
            CrosspostLog.status != JOURNAL_STATUS_DRY_RUN,
        )
        .order_by(CrosspostLog.id.desc())
    ).scalars():
        if remote_id is None and str(row.remote_id or "").strip():
            remote_id = str(row.remote_id).strip()
        if url is None and str(row.url or "").strip():
            url = str(row.url).strip()
        if remote_id and url:
            break
    return remote_id, url


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
        latest_status = journal_status_of(latest) if latest is not None else ""
        retry_exhausted = bool(
            latest is not None
            and latest_status == JOURNAL_STATUS_FAILED
            and int(latest.attempt or 1) >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS
        )
        reconcilable_status = latest_status in JOURNAL_RECONCILABLE_STATUSES
        if latest is None or not (reconcilable_status or retry_exhausted):
            # Fail closed, and loudly: the raw token is echoed and the endpoint
            # turns it into a 409 that names it, so an unrecognised status is
            # refused *and* shown rather than silently sent.
            return {
                "ok": False,
                "error": "not_reconcilable",
                "status": latest.status if latest else None,
            }
        if latest_status == JOURNAL_STATUS_PUBLISHING:
            created = latest.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created > datetime.now(UTC) - CROSSPOST_PUBLISHING_STALE_AFTER:
                return {"ok": False, "error": "still_in_flight"}

        if action not in {"confirm_posted", "retry_after_check"}:
            return {"ok": False, "error": "invalid_action"}

        # Reconciliation is the only path that releases an ambiguous/stale claim.
        session.query(CrosspostClaim).filter_by(
            post_public_id=post_public_id, platform=platform
        ).delete(synchronize_session=False)

        attempt = latest.attempt
        if action == "confirm_posted":
            # Completing the confirmation from the journal rather than from the
            # row answered from is what keeps a stored non-NULL from being
            # replaced by a NULL: this new row is the newest `posted` one, so it
            # is the row the post detail and the matchers read next.
            known_remote_id, known_url = _journal_identity(session, post_public_id, platform)
            session.add(
                CrosspostLog(
                    post_public_id=post_public_id,
                    platform=platform,
                    status=JOURNAL_STATUS_POSTED,
                    remote_id=known_remote_id,
                    url=known_url,
                    error="confirmed by operator; destination ID may be unavailable",
                    attempt=attempt,
                )
            )
            return {
                "ok": True,
                "status": JOURNAL_STATUS_POSTED,
                "post_id": post.id,
                "detail": "operator confirmed destination post",
            }

        session.add(
            CrosspostLog(
                post_public_id=post_public_id,
                platform=platform,
                status=JOURNAL_STATUS_RETRY_AUTHORIZED,
                error="operator checked destination and authorized retry",
                attempt=attempt,
            )
        )
        return {
            "ok": True,
            "status": JOURNAL_STATUS_RETRY_AUTHORIZED,
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
    """Manually requeue a terminal, definite publish failure after the cause is fixed.

    The guard is `maintenance.requeue_eligibility` — the same one the bulk sweep
    uses — so the single-post click and the bounded pass can never disagree about
    what is safe to requeue. A second, looser requeue path is the failure mode this
    is written against.
    """
    from tgfarm.maintenance import REQUEUE_REFUSALS, requeue_eligibility
    from tgfarm.platforms import is_platform_enabled

    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "not_found"}
        channel = session.get(Channel, row.channel_id)
        platform = ((channel.platform if channel else None) or "telegram").lower()
        journaled = (
            session.execute(
                select(CrosspostLog.id).where(
                    CrosspostLog.post_public_id == public_id,
                    # Lowered on the SQL side, exactly as the bulk sweep does it:
                    # `platform` here is already lowercased, and the docstring of
                    # the shared guard promises these two paths cannot disagree.
                    func.lower(CrosspostLog.platform) == platform,
                    CrosspostLog.status == "posted",
                ).limit(1)
            ).scalar_one_or_none()
            is not None
        )
        ok, refusal = requeue_eligibility(
            status=row.status,
            publish_error=row.publish_error,
            tg_message_id=row.tg_message_id,
            published_at=row.published_at,
            journaled_posted=journaled,
            platform=platform,
            enabled=is_platform_enabled(platform),
        )
        if not ok:
            payload: dict[str, Any] = {
                "ok": False,
                "error": refusal,
                "reason": REQUEUE_REFUSALS.get(refusal, refusal),
                "platform": platform,
            }
            if refusal == "not_failed":
                payload["status"] = row.status
            return payload

        previous = row.publish_error
        previous_attempts = int(row.attempts or 0)
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
            # The reset above erases the reason from the row, so the audit trail is
            # the only place left that says what this post failed with. Redacted and
            # bounded: `publish_error` is untrusted platform text.
            "previous_error": redact_sensitive_text(previous, limit=200),
            "previous_attempts": previous_attempts,
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


def _freshness_summary_of(detail: dict) -> dict:
    """Count an already-built freshness report; read no database at all.

    ``/api/metrics/freshness`` used to call ``metrics_freshness()`` and
    ``freshness_summary()`` per request, and ``freshness_summary`` builds the whole
    detail again inside itself: four sessions where two answer the same question.
    Only the counting is repeated here, over the detail that was already read, and
    the two vocabularies are the ones ``freshness.py`` exports rather than a third
    list of literals — so what cannot drift is the meaning of a status, and what
    can is only the arithmetic over rows that carry it.
    ``tests/test_metrics_freshness_panel.py`` pins this derivation field by field
    against ``freshness.freshness_summary``, which is the guard that makes keeping
    it honest. The clean home for this function is ``freshness.py`` as
    ``summarize_report(detail)``; that file is owned by another agent right now, so
    the follow-up is filed in the report instead of done here.

    Absent keys stay ``None``: a collector row the server never filled in has no
    row count, and defaulting it to ``0`` would state a measurement that did not
    happen (P34) in the one panel an operator uses to decide what to fix.
    """
    from tgfarm.s8_analytics.freshness import COLLECTOR_STATUSES, STATUSES

    platforms = detail.get("platforms") or {}
    collectors = detail.get("collectors") or []
    destinations = detail.get("secondary_destinations") or []
    outside = detail.get("tables_without_collector") or []

    platform_counts = {status: 0 for status in STATUSES}
    for row in platforms.values():
        row_status = row.get("status")
        if row_status in platform_counts:
            platform_counts[row_status] += 1

    freshest: dict | None = None
    for row in platforms.values():
        stamp = row.get("last_snapshot_at")
        age = row.get("age_hours")
        if stamp is None or age is None:
            continue
        if freshest is None or age < freshest["age_hours"]:
            freshest = {
                "platform": row.get("id"),
                "name": row.get("name"),
                # The freshest row's own status, not a variable left over from the
                # counting loop above: that leak used to stamp the newest snapshot
                # with whatever status the last platform in the dict happened to
                # have, which on the live database read «нет данных» for a row that
                # was plainly `ok`.
                "status": row.get("status"),
                "collected_at": stamp,
                "age_hours": age,
            }

    def _silent_entry_of(collector: dict) -> dict:
        # One builder for both scopes, because the module counts primary readers
        # and secondary destinations into the same `silent_collectors` list; two
        # copies of this shape is how the two halves start meaning different
        # things in one list.
        return {
            "id": collector.get("id"),
            "kind": collector.get("kind"),
            "platform": collector.get("platform"),
            "job_id": collector.get("job_id"),
            "snapshot_table": collector.get("snapshot_table"),
            "data_status": collector.get("data_status"),
            "rows": collector.get("rows"),
            "last_collected_at": collector.get("last_collected_at"),
            "age_hours": collector.get("age_hours"),
            # The bound each row was judged by, carried rather than re-derived:
            # one threshold for the whole block is the defect the server just
            # closed, and a panel that prints its own constant would reopen it.
            "stale_after_hours": collector.get("stale_after_hours"),
            "stale_bound_source": collector.get("stale_bound_source"),
            "stale_bound_derivation": collector.get("stale_bound_derivation"),
            "runs_recorded": collector.get("runs_recorded"),
            "claim": collector.get("reason"),
        }

    def _count(rows: list[dict], into: dict) -> None:
        for collector in rows:
            data_status = collector.get("data_status")
            if data_status in into:
                into[data_status] += 1

    collector_counts = {status: 0 for status in COLLECTOR_STATUSES}
    destination_counts = {status: 0 for status in COLLECTOR_STATUSES}
    silent: list[dict] = []
    _count(collectors, collector_counts)
    _count(destinations, destination_counts)
    for collector in [*collectors, *destinations]:
        if collector.get("scheduled_but_silent"):
            silent.append(_silent_entry_of(collector))

    return {
        "generated_at": detail.get("generated_at"),
        # Not one `stale_after_hours` for the whole report: the platform rows are
        # judged by `platform_stale_after_hours`, every collector and destination
        # row by the bound its own registered cadence derives, and `stale_bound`
        # states the rule plus those per-row numbers.
        "platform_stale_after_hours": detail.get("platform_stale_after_hours"),
        "stale_bound": detail.get("stale_bound"),
        "platforms_total": len(platforms),
        "ok": platform_counts["ok"],
        "stale": platform_counts["stale"],
        "never": platform_counts["never"],
        "blocked": platform_counts["blocked"],
        "snapshots_total": detail.get("snapshots_total"),
        "freshest": freshest,
        "collectors": collectors,
        "collectors_total": len(collectors),
        "collectors_ok": collector_counts["ok"],
        "collectors_stale": collector_counts["stale"],
        "collectors_no_report": collector_counts["no_report"],
        "collectors_missing_table": collector_counts["missing_table"],
        "collectors_unreadable": collector_counts["unreadable"],
        # Counted as its own scope, because `channel_stats` is not a collector:
        # it is a second table the same registered tick writes, and folding it
        # into the collector counts would let one job be counted twice.
        "secondary_destinations": destinations,
        "secondary_destinations_total": len(destinations),
        "secondary_destinations_ok": destination_counts["ok"],
        "secondary_destinations_stale": destination_counts["stale"],
        "secondary_destinations_no_report": destination_counts["no_report"],
        "secondary_destinations_missing_table": destination_counts["missing_table"],
        "secondary_destinations_unreadable": destination_counts["unreadable"],
        "silent_collectors": silent,
        "tables_without_collector": outside,
        "collectors_error": detail.get("collectors_error"),
        "error": detail.get("error"),
    }


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


def _is_trusted_host_request(
    host: str,
    trusted_hosts: set[str],
    public_webhook_host: str | None,
    *,
    method: str,
    path: str,
) -> bool:
    """Expose only MAX's secret-protected callback on the public webhook host."""
    value = str(host or "").strip().lower()
    if not value or value in trusted_hosts:
        return True
    return bool(
        public_webhook_host
        and value == public_webhook_host.strip().lower()
        and str(method or "").upper() == "POST"
        and path == "/api/platforms/max/webhook"
    )


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
    from urllib.parse import urlsplit

    @app.middleware("http")
    async def trusted_host_guard(request: Request, call_next):
        """Allow the public host only for MAX's secret-protected webhook POST.

        The operator dashboard embeds its API token, so trusting the whole
        public domain would expose the control plane to anyone who can reach
        the webhook hostname. All other paths stay on the local/allowlisted host set.
        """
        host = (request.url.hostname or "").lower()
        # The owner may configure TGFARM_PUBLIC_BASE_URL from the MAX
        # card without restarting the app. Read the live settings cache here.
        live_settings = get_settings()
        public_host = (
            urlsplit(str(getattr(live_settings, "public_base_url", "") or "")).hostname or ""
        ).lower()
        if not _is_trusted_host_request(
            host,
            trusted,
            public_host,
            method=request.method,
            path=request.url.path,
        ):
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
            if not is_authorised(header, cookie):
                log.info(
                    "401 %s header=%s query_token_rejected=%s cookie=%s",
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
            "/api/platforms/youtube/auth/callback": "tgfarm_youtube_oauth_params",
            "/api/platforms/tiktok/auth/callback": "tgfarm_tiktok_oauth_params",
            "/api/platforms/douyin/auth/callback": "tgfarm_douyin_oauth_params",
            "/api/platforms/instagram/auth/callback": "tgfarm_instagram_oauth_params",
            "/api/platforms/pinterest/auth/callback": "tgfarm_pinterest_oauth_params",
            "/api/platforms/facebook/auth/callback": "tgfarm_facebook_oauth_params",
            "/api/platforms/reddit/auth/callback": "tgfarm_reddit_oauth_params",
            "/api/platforms/tumblr/auth/callback": "tgfarm_tumblr_oauth_params",
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
            "/api/platforms/youtube/auth/callback",
            "/api/platforms/tiktok/auth/callback",
            "/api/platforms/douyin/auth/callback",
            "/api/platforms/instagram/auth/callback",
            "/api/platforms/pinterest/auth/callback",
            "/api/platforms/facebook/auth/callback",
            "/api/platforms/reddit/auth/callback",
            "/api/platforms/tumblr/auth/callback",
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

    @app.get("/api/trends/{cluster_public_id}/explain")
    async def trend_explain(cluster_public_id: str) -> dict:
        """Why this cluster is hot: numbers, not adjectives."""
        from tgfarm.s2_trends.explain import explain_cluster

        result = await io(explain_cluster, cluster_public_id)
        if not result or result.get("ok") is False:
            raise HTTPException(404, "Кластер не найден")
        return result

    @app.get("/api/router/skips/{cluster_public_id}")
    async def router_skips(cluster_public_id: str) -> dict:
        """Which destinations S3 dropped from this cluster's plan, and why.

        The report lives in the router's process memory only, so the answer
        carries that fact: an empty list is never presented as "nothing was
        skipped" unless the registry actually holds a record for the cluster.
        """
        cid = str(cluster_public_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", cid):
            raise HTTPException(status_code=400, detail="Некорректный идентификатор кластера.")
        report = await io(_router_skip_report, cid)
        if report is None:
            raise HTTPException(404, "Нет записи маршрутизации для этого кластера")
        return report

    @app.get("/api/sources/reprint-backfill")
    async def reprint_backfill_status() -> dict:
        """Last reprint-backfill pass: what it linked, what remains, why it stopped.

        The answer is the backfill module's process memory, said out loud: a
        never-run process reports nulls and `never_run`, never a zero remainder
        the sweep did not measure.
        """
        return await io(_reprint_backfill_report)

    @app.get("/api/sources/health")
    async def sources_health() -> dict:
        """Per-source fleet state: cooldown, failures and why."""
        from tgfarm.s1_sources.health import source_health, source_health_summary

        detail = await io(source_health)
        summary = await io(source_health_summary)
        return {"summary": summary, "sources": detail.get("sources") or []}

    @app.post("/api/sources/{source_id}/cooldown/reset")
    async def reset_source_cooldown_api(source_id: str) -> dict:
        """Let the operator retry a source in cooldown without editing the DB."""
        from tgfarm.s1_sources.health import reset_source_cooldown

        source_id = str(source_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", source_id):
            raise HTTPException(status_code=400, detail="Некорректный идентификатор источника.")
        reset = await io(reset_source_cooldown, source_id, reason="operator reset from panel")
        if not reset:
            raise HTTPException(status_code=404, detail="Источник не найден.")
        _operator("source-cooldown-reset", source_id, "operator cleared the source cooldown")
        return {"ok": True, "source_id": source_id}

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

    @app.get("/public/instagram-media/{media_id}")
    async def instagram_public_media(
        media_id: str,
        expires: int = Query(...),
        sig: str = Query(..., min_length=64, max_length=64),
    ):
        """Serve only signed, expiring assets attached to already-published posts."""
        from tgfarm.instagram_oauth import verify_signed_instagram_media_url

        if not verify_signed_instagram_media_url(media_id, expires, sig):
            raise HTTPException(403, "invalid or expired media signature")
        path = await io(_instagram_public_media_path, media_id)
        return FileResponse(
            path,
            headers={
                "Cache-Control": "public, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )

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

    @app.get("/api/distribution/plan")
    async def distribution_plan() -> dict:
        """Show the current editorial allocation, daily budgets and selected posts."""
        from tgfarm.distribution import build_distribution_plan

        return await io(build_distribution_plan)

    @app.get("/api/editorial/plan")
    async def editorial_plan() -> dict:
        """Active global editorial plan; an ephemeral preview before first refresh."""

        from tgfarm.editorial_plan import build_editorial_plan, get_active_plan

        def _plan_or_preview() -> dict:
            active = get_active_plan()
            if active is not None:
                return active
            return build_editorial_plan(persist=False)

        return await io(_plan_or_preview)

    @app.post("/api/editorial/plan/refresh")
    async def editorial_plan_refresh() -> dict:
        """Rebuild and persist the plan; the previous snapshot stays as history."""

        from tgfarm.editorial_plan import build_editorial_plan

        return await io(build_editorial_plan)

    @app.get("/api/editorial/plan/history")
    async def editorial_plan_history(limit: int = Query(10, ge=1, le=50)) -> dict:
        """Recent plan snapshots with per-revision summaries and diffs."""

        from tgfarm.editorial_plan import plan_history

        return {"plans": await io(plan_history, limit)}

    @app.put("/api/runtime/mode")
    async def set_runtime_mode(request: RuntimeModeRequest) -> dict:
        """Global kill switch; LIVE always requires a separate explicit confirmation."""
        from tgfarm.distribution import build_distribution_plan, set_runtime_mode as persist_runtime_mode

        if request.mode == "live" and not request.confirm_live:
            raise HTTPException(
                status_code=400,
                detail="Для включения LIVE нужно явно подтвердить запуск реальных публикаций.",
            )
        mode = await io(persist_runtime_mode, request.mode)
        _operator("runtime-mode", "farm", mode)
        plan = await io(build_distribution_plan)
        return {"ok": True, "mode": mode, "plan": plan}

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

    @app.post("/api/platforms/{platform_id}/browser-login")
    async def platform_browser_login(
        platform_id: str, timeout: int = Query(420, ge=60, le=1800)
    ) -> dict:
        """Open a local persistent browser profile for an owner-controlled sign-in."""
        from tgfarm.platforms import CATALOG
        from tgfarm.browser_auth import PLATFORM_LOGIN_URLS, login_in_browser

        platform_id = platform_id.lower()
        if not any(item["id"] == platform_id for item in CATALOG):
            raise HTTPException(status_code=404, detail="Неизвестная площадка.")
        if platform_id in {"dzen", "vk"}:
            raise HTTPException(
                status_code=409,
                detail="Для этой площадки есть отдельный сценарий подключения.",
            )
        if platform_id not in PLATFORM_LOGIN_URLS:
            raise HTTPException(status_code=409, detail="Для этой площадки ещё нет браузерного сценария.")
        try:
            result = await io(lambda: asyncio.run(login_in_browser(platform_id, timeout)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=240)) from exc
        except Exception as exc:  # noqa: BLE001 - browser errors are safe operator diagnostics
            log.warning("browser sign-in failed for %s (%s)", platform_id, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Не удалось открыть профиль браузера. Проверь установку Chrome/Chromium и журнал диагностики.",
            ) from exc
        _operator("platform-browser-login", platform_id, result["state"])
        return result

    @app.post("/api/platform-drafts/{platform_id}/{post_public_id}/targets")
    async def recommend_platform_targets(platform_id: str, post_public_id: str) -> dict:
        """Rank real communities/topics and show their published rules before draft preparation."""
        from tgfarm.community_drafts import CommunityDraftError
        from tgfarm.community_targets import recommend_community_target

        try:
            result = await recommend_community_target(platform_id.lower(), post_public_id)
        except CommunityDraftError as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=500)) from exc
        except Exception as exc:  # noqa: BLE001 - public directory changes are recoverable
            log.warning("community target recommendation failed for %s (%s)", platform_id, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Не удалось получить актуальные сообщества/темы. Попробуй позже; цель не выбрана.",
            ) from exc
        _operator(
            "community-target-recommended",
            f"{platform_id}:{post_public_id}",
            str((result.get("selected") or {}).get("name") or "no suitable public target"),
        )
        return result

    @app.get("/api/community-policy/history")
    async def community_policy_history(
        platform: str | None = Query(None, max_length=32),
        target_id: str | None = Query(None, max_length=200),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        """Append-only history of community rules checks and gate outcomes."""
        from tgfarm.community_policy import policy_history

        rows = await io(policy_history, platform=platform, target_id=target_id, limit=limit)
        return {"items": rows, "count": len(rows)}

    @app.post("/api/platform-drafts/{platform_id}/{post_public_id}/editorial")
    async def platform_editorial_preview(platform_id: str, post_public_id: str) -> dict:
        """Create a native editorial preview for any catalogue destination; never publishes."""
        from tgfarm.platform_editorial import (
            adapt_payload_with_ai,
            build_editorial_brief,
            choose_native_format,
            get_editorial_profile,
        )
        from tgfarm.core.schemas import CrosspostPayload
        from tgfarm.distribution import adapt_payload_for_platform
        from tgfarm.platforms import CATALOG

        platform_id = str(platform_id or "").strip().lower()
        if not any(item["id"] == platform_id for item in CATALOG):
            raise HTTPException(status_code=404, detail="Неизвестная площадка.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", str(post_public_id or "")):
            raise HTTPException(status_code=400, detail="Некорректный идентификатор поста.")
        post = await io(_post_detail, post_public_id)
        if post.get("status") != "published":
            raise HTTPException(status_code=409, detail="Нативную версию можно собирать только из опубликованного и проверенного материала.")

        if platform_id == "reddit":
            from tgfarm.s9_growth.reddit_scout import build_automatic_submission

            result = await build_automatic_submission(post_public_id)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "status": "needs_target",
                    "platform": platform_id,
                    "detail": result.get("reason") or "Не найден подходящий subreddit.",
                    "profile": get_editorial_profile(platform_id),
                    "review_required": True,
                    "publish_action": "none",
                }
            brief = build_editorial_brief(platform_id, media_kinds=(), topic=post.get("hook", ""), source_angle=post.get("angle", ""))
            return {
                "ok": True,
                "status": "preview_ready",
                "platform": platform_id,
                "profile": get_editorial_profile(platform_id),
                "brief": {**brief, "format": "self-post", "format_eligible": True},
                "target": result.get("community"),
                "draft": {"title": result.get("title", ""), "body": result.get("body", "")},
                "adaptation": {"status": "ai_native", "reason": result.get("reason", "")},
                "review_required": True,
                "publish_action": "none",
            }

        media_items = [item for item in (post.get("media") or []) if isinstance(item, dict)]
        video = next((item for item in reversed(media_items) if item.get("kind") == "video" and item.get("path") and Path(str(item["path"])).is_file()), None)
        image = next((item for item in media_items if item.get("kind") != "video" and item.get("path") and Path(str(item["path"])).is_file()), None)
        image_path = str(image["path"]) if image else None
        video_path = str(video["path"]) if video else None
        media_kinds = {"image"} if image_path else set()
        if video_path:
            media_kinds.add("video")

        target_context: dict[str, Any] | None = None
        target: dict[str, Any] | None = None
        # CommunityPolicy travels with the selected target: the same verdict the
        # panel shows is handed to the brief and the native adaptation, so the
        # editor cannot "respect" rules nobody verified.
        community_policy: dict[str, Any] | None = None
        if platform_id in {"vc_ru", "pikabu"}:
            from tgfarm.community_targets import recommend_community_target

            plan = await recommend_community_target(platform_id, post_public_id)
            target = plan.get("selected")
            if not target:
                return {
                    "ok": False,
                    "status": "needs_target",
                    "platform": platform_id,
                    "detail": plan.get("detail") or "Подходящая тема/сообщество с подтверждёнными правилами не найдено.",
                    "profile": get_editorial_profile(platform_id),
                    "candidates": plan.get("candidates") or [],
                    "general_notes": plan.get("general_notes") or [],
                    "rules_source": plan.get("rules_source"),
                    "review_required": True,
                    "publish_action": "none",
                }
            community_policy = (plan.get("policy_verdict") or {}).get("policy")
            target_context = {
                "name": target.get("name"),
                "url": target.get("url"),
                "description": target.get("description"),
                "rules": target.get("rules"),
                "risks": target.get("risks"),
                "blockers": target.get("blockers"),
                "target_type": target.get("target_type"),
            }

        sources = [
            item for item in (post.get("sources") or [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        ]
        source_urls = list(dict.fromkeys(str(item["url"]) for item in sources))[:8]
        body = str(post.get("body") or "").strip()
        if post.get("cta") and str(post["cta"]).strip() not in body:
            body = f"{body}\n\n{str(post['cta']).strip()}".strip()
        hashtags = [str(item).strip().lstrip("#") for item in (post.get("hashtags") or []) if str(item).strip()]
        body = "\n\n".join(part for part in (body, "Источники: " + ", ".join(source_urls) if source_urls else "") if part)
        payload = CrosspostPayload(
            platform=platform_id,
            title=str(post.get("hook") or ""),
            text=body,
            link=source_urls[0] if source_urls else None,
            image_path=image_path,
            video_path=video_path,
            hashtags=hashtags,
            meta={"post_public_id": post_public_id, "preview_only": True},
        )
        payload = adapt_payload_for_platform(payload, platform_id)
        brief = build_editorial_brief(
            platform_id,
            media_kinds=media_kinds,
            topic=str(post.get("hook") or ""),
            source_angle=str(post.get("angle") or ""),
            content_chars=len(body),
            community_policy=community_policy,
        )
        if not brief["format_eligible"]:
            return {
                "ok": False,
                "status": "blocked_missing_media",
                "platform": platform_id,
                "detail": brief["format_reason"],
                "profile": get_editorial_profile(platform_id),
                "brief": brief,
                "review_required": True,
                "publish_action": "none",
            }
        adaptation = await adapt_payload_with_ai(
            payload,
            allow_ai=True,
            preview_only=True,
            target_context=target_context,
            community_policy=community_policy,
        )
        payload = adapt_payload_for_platform(payload, platform_id)
        return {
            "ok": True,
            "status": "preview_ready",
            "platform": platform_id,
            "profile": get_editorial_profile(platform_id),
            "brief": brief,
            "target": target,
            "community_policy": brief.get("community_policy"),
            "adaptation_community_policy": adaptation.get("community_policy"),
            "draft": {"title": payload.title, "body": payload.text, "hashtags": payload.hashtags},
            "adaptation": adaptation,
            "review_required": True,
            "publish_action": "none",
        }

    @app.post("/api/platform-drafts/{platform_id}/{post_public_id}/prepare")
    async def prepare_platform_draft(
        platform_id: str,
        post_public_id: str,
        target_url: str | None = Query(None, max_length=500),
        target_name: str | None = Query(None, max_length=120),
    ) -> dict:
        """Fill a destination-specific draft in the saved browser profile; never publish."""
        from tgfarm.community_drafts import CommunityDraftError, prepare_community_draft

        platform_id = platform_id.lower()
        if platform_id not in {"vc_ru", "pikabu"}:
            raise HTTPException(
                status_code=404,
                detail="Подготовка черновиков доступна только для vc.ru и Пикабу.",
            )
        try:
            if target_url:
                # Re-read the live catalogue/rules server-side immediately
                # before opening the editor. A stale UI recommendation or a
                # hand-crafted API request must not bypass this check.
                from tgfarm.community_targets import recommend_community_target

                current_plan = await recommend_community_target(
                    platform_id,
                    post_public_id,
                )
                candidates = current_plan.get("candidates") or []
                target_key = str(target_url).strip().rstrip("/")
                current_target = next(
                    (
                        item for item in candidates
                        if str(item.get("url") or "").strip().rstrip("/") == target_key
                    ),
                    None,
                )
                if not current_target or not current_target.get("recommended"):
                    raise CommunityDraftError(
                        "Выбранная тема/сообщество больше не проходит текущую проверку правил. "
                        "Подбери цель заново; черновик не открывался."
                    )
                current_name = str(
                    current_target.get("name") or current_target.get("title") or ""
                ).strip()
                if target_name and current_name and target_name.strip() != current_name:
                    raise CommunityDraftError(
                        "Название выбранной цели изменилось. Обнови рекомендацию перед подготовкой."
                    )
                target_url = str(current_target.get("url") or target_url)
                target_name = current_name or target_name

            result = await io(
                lambda: asyncio.run(
                    prepare_community_draft(
                        platform_id,
                        post_public_id,
                        target_url=target_url,
                        target_name=target_name,
                    )
                )
            )
        except CommunityDraftError as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=500)) from exc
        except Exception as exc:  # noqa: BLE001 - editor and browser versions can change
            log.warning("community draft preparation failed for %s (%s)", platform_id, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Не удалось подготовить черновик. Проверь авторизацию, редактор и журнал диагностики.",
            ) from exc
        _operator(
            "community-draft-prepared",
            f"{platform_id}:{post_public_id}",
            "draft prepared; publish action was not triggered",
        )
        return result

    @app.get("/api/platform-drafts/{platform_id}/{post_public_id}/package")
    async def manual_platform_package(platform_id: str, post_public_id: str) -> dict:
        """Build a copyable, destination-specific package. Never publishes."""
        from tgfarm.manual_drafts import build_manual_package

        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", str(post_public_id or "")):
            raise HTTPException(status_code=400, detail="Некорректный идентификатор поста.")
        try:
            post = await io(_post_detail, post_public_id)
            return await io(build_manual_package, platform_id, post)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=400)) from exc

    @app.post("/api/platforms/vk/auth/vkhost")
    async def vkhost_auth(timeout: int = Query(420, ge=60, le=1800)) -> dict:
        """Capture only the VK OAuth token returned after owner consent in VKHost."""
        from tgfarm.browser_auth import capture_vkhost_token
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        try:
            credentials = await io(lambda: asyncio.run(capture_vkhost_token(timeout)))
            await io(save_credentials, "vk", {"VK_ACCESS_TOKEN": credentials["access_token"]})
            result = await io(
                record_auth_check,
                "vk",
                True,
                account=credentials["account"],
                detail="VK OAuth токен проверен через users.get и сохранён локально. Проверь ID сообщества и права на публикацию.",
            )
            _operator("platform-auth-check", "vk", "VKHost OAuth connected")
            return {
                "ok": True,
                "authorized": True,
                "account": credentials["account"],
                "user_id": credentials["user_id"],
                "expires_in": credentials["expires_in"],
                "auth": result,
                "detail": "Токен получен, проверен через VK API и сохранён локально. Сам токен в интерфейс не возвращается.",
            }
        except TimeoutError as exc:
            raise HTTPException(status_code=408, detail=redact_sensitive_text(exc, limit=240)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=240)) from exc
        except Exception as exc:  # noqa: BLE001 - do not expose tokens/provider response
            log.warning("VKHost OAuth failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Не удалось завершить VKHost OAuth. Проверь вход, разрешения и доступность VK API.",
            ) from exc

    @app.post("/api/platforms/max/webhook/register")
    async def max_webhook_register() -> dict:
        """Register the official MAX webhook for discovering group/channel IDs."""
        import secrets

        from tgfarm.max_api import build_max_webhook_url, register_max_webhook
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        current = get_settings()
        if not current.max_access_token:
            raise HTTPException(status_code=409, detail="Сначала сохрани MAX_ACCESS_TOKEN.")
        try:
            webhook_url = build_max_webhook_url(current.public_base_url)
            secret = str(current.max_webhook_secret or "").strip() or secrets.token_urlsafe(32)
            # Persist the receiver secret BEFORE creating the subscription, because
            # MAX may deliver a bot_added event as soon as the subscription is live.
            await io(
                save_credentials,
                "max",
                {
                    "MAX_WEBHOOK_SECRET": secret,
                    "MAX_WEBHOOK_URL": webhook_url,
                },
            )
            result = await register_max_webhook(current.max_access_token, webhook_url, secret)
            if result.get("bot_id"):
                await io(save_credentials, "max", {"MAX_BOT_ID": str(result["bot_id"])})
            await io(
                record_auth_check,
                "max",
                None,
                account=str(result.get("bot_name") or "MAX bot"),
                detail="MAX webhook подписан. Добавь бота в нужный канал/чат, затем выбери его в TgFarm.",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        _operator("platform-webhook-registered", "max", webhook_url)
        return {
            "ok": True,
            "platform": "max",
            "bot_name": result.get("bot_name") or "MAX bot",
            "webhook_url": webhook_url,
            "events": result.get("events") or [],
            "detail": (
                "Webhook подключён. Теперь добавь бота в свой канал или групповой чат, "
                "выдай право write, затем нажми «Найти мои чаты»."
            ),
        }

    @app.post("/api/platforms/max/webhook")
    async def max_webhook_receiver(request: Request) -> dict:
        """Public MAX callback secured by the per-subscription secret header."""
        from tgfarm.max_api import (
            apply_max_webhook_event,
            parse_max_discovered_chats,
            verify_max_webhook_secret,
        )
        from tgfarm.platforms import save_platform_credentials as save_credentials

        current = get_settings()
        if not verify_max_webhook_secret(
            request.headers.get("X-Max-Bot-Api-Secret"),
            current.max_webhook_secret,
        ):
            raise HTTPException(status_code=401, detail="MAX webhook secret не совпадает.")
        body = await request.body()
        if len(body) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Слишком большой webhook payload.")
        try:
            update = json.loads(body or b"{}")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="MAX webhook прислал некорректный JSON.") from exc
        if not isinstance(update, dict):
            raise HTTPException(status_code=400, detail="MAX webhook payload должен быть объектом.")
        previous = parse_max_discovered_chats(current.max_discovered_chats)
        discovered, changed = apply_max_webhook_event(previous, update)
        if changed:
            await io(
                save_credentials,
                "max",
                {"MAX_DISCOVERED_CHATS": json.dumps(discovered, ensure_ascii=False, separators=(",", ":"))},
            )
        event_type = str(update.get("update_type") or "")
        selected_id = str(getattr(current, "max_channel_id", "") or "").strip()
        event_chat_id = str(update.get("chat_id") or "").strip()
        changed_bot_id = str(update.get("bot_id") or "").strip()
        own_bot_id = str(getattr(current, "max_bot_id", "") or "").strip()
        selected_was_removed = event_type == "bot_removed"
        own_bot_permissions_changed = (
            event_type == "bot_admin_permissions_changed"
            and own_bot_id
            and changed_bot_id == own_bot_id
        )
        if selected_id and event_chat_id == selected_id and (selected_was_removed or own_bot_permissions_changed):
            await io(
                save_credentials,
                "max",
                {"MAX_CHANNEL_BLOCKED": "true"},
            )
            from tgfarm.platforms import record_auth_check

            await io(
                record_auth_check,
                "max",
                False,
                account=str(getattr(current, "max_channel_name", "") or ""),
                detail=(
                    "MAX сообщил об удалении бота или изменении его прав. "
                    "Фоновая публикация заблокирована до повторной проверки write."
                ),
            )
        return {"ok": True}

    @app.get("/api/platforms/max/destinations")
    async def max_destinations() -> dict:
        """Return discovered chat IDs and confirm the configured subscription is still active."""
        from tgfarm.max_api import max_webhook_subscription_active, parse_max_discovered_chats

        settings = get_settings()
        items = parse_max_discovered_chats(settings.max_discovered_chats)
        registered = False
        subscription_detail = "Webhook ещё не подключён."
        if settings.max_access_token and settings.max_webhook_url:
            try:
                registered = await max_webhook_subscription_active(
                    settings.max_access_token,
                    settings.max_webhook_url,
                )
                subscription_detail = (
                    "MAX подтвердил активную подписку и нужные события."
                    if registered
                    else "Подписка не найдена или её список событий неполный. Подключи webhook повторно."
                )
            except (ValueError, RuntimeError) as exc:
                subscription_detail = redact_sensitive_text(exc, limit=240)
        return {
            "platform": "max",
            "destinations": items,
            "webhook_registered": registered,
            "webhook_detail": subscription_detail,
        }

    @app.post("/api/platforms/max/destination")
    async def max_select_destination(payload: dict) -> dict:
        """Select a chat/channel only after MAX confirms the bot has write permission."""
        from tgfarm.max_api import select_max_chat
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        current = get_settings()
        chat_id = str(payload.get("chat_id") or "").strip()
        try:
            selected = await select_max_chat(current.max_access_token, chat_id)
            await io(
                save_credentials,
                "max",
                {
                    "MAX_CHANNEL_ID": str(selected["id"]),
                    "MAX_CHANNEL_NAME": str(selected["name"]),
                    "MAX_CHANNEL_IS_CHANNEL": "true" if selected["is_channel"] else "false",
                    "MAX_CHANNEL_BLOCKED": "false",
                },
            )
            await io(
                record_auth_check,
                "max",
                True,
                account=str(selected["name"]),
                detail="MAX подтвердил членство бота и право write в выбранном чате/канале.",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        _operator("platform-destination-selected", "max", selected["id"])
        return {
            "ok": True,
            "platform": "max",
            "destination": {
                "id": selected["id"],
                "name": selected["name"],
                "kind": "channel" if selected["is_channel"] else "chat",
                "role": selected["role"],
            },
        }

    @app.get("/api/platforms/vk/destinations")
    async def vk_destinations() -> dict:
        """List VK communities administered or edited by the connected account."""
        from tgfarm.platform_auth import list_vk_groups

        try:
            groups = await list_vk_groups()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        return {"platform": "vk", "destinations": groups}

    @app.post("/api/platforms/vk/destination")
    async def vk_select_destination(payload: dict) -> dict:
        """Store only a VK community revalidated against the current owner's role."""
        from tgfarm.platform_auth import select_vk_group
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        group_id = str(payload.get("group_id") or "").strip()
        try:
            group = await select_vk_group(group_id)
            await io(save_credentials, "vk", {"VK_GROUP_ID": group["id"]})
            await io(
                record_auth_check,
                "vk",
                True,
                account=group["name"],
                detail=(
                    "VK подтвердил административную/редакторскую роль в сообществе. "
                    "Фактическое право wall.post дополнительно проверяется при отправке."
                ),
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        _operator("platform-destination-selected", "vk", group["id"])
        return {
            "ok": True,
            "platform": "vk",
            "destination": {"id": group["id"], "name": group["name"], "role": group["role"]},
        }

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

    @app.post("/api/platforms/instagram/auth/start")
    async def instagram_oauth_start(request: Request) -> dict:
        from tgfarm.instagram_oauth import begin_instagram_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/instagram/auth/callback"
        try:
            return await begin_instagram_oauth(
                settings.instagram_app_id,
                settings.instagram_app_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/instagram/auth/callback")
    async def instagram_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.instagram_oauth import cancel_instagram_oauth, complete_instagram_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_instagram_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        headers = {
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_instagram_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · Instagram</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Instagram не подключён</h1><p>Авторизация отменена или ответ Meta неполный. "
                "<a href='/'>Вернись в TgFarm и повтори подключение.</a></p></body></html>",
                status_code=400, headers=headers,
            )
        try:
            credentials = await complete_instagram_oauth(state_value, code)
            await io(
                save_credentials, "instagram",
                {
                    "INSTAGRAM_APP_ID": credentials["app_id"],
                    "INSTAGRAM_APP_SECRET": credentials["app_secret"],
                    "INSTAGRAM_ACCESS_TOKEN": credentials["access_token"],
                    "INSTAGRAM_TOKEN_EXPIRES_AT": credentials["expires_at"],
                    "INSTAGRAM_USER_ID": credentials["user_id"],
                    "INSTAGRAM_USERNAME": credentials["username"],
                },
            )
            await io(
                record_auth_check, "instagram", True,
                account=credentials["account"],
                detail="Instagram Login подтвердил профессиональный профиль, долгоживущий токен и разрешения.",
            )
            _operator("platform-auth-check", "instagram", "OAuth connected")
            account = html.escape(credentials["account"])
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Instagram подключён</h1><p>Профиль подтверждён: <strong>" + account + "</strong>.</p>"
                "<p>Долгоживущий токен сохранён локально. <a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=200, headers=headers,
            )
        except (ValueError, RuntimeError) as exc:
            cancel_instagram_oauth(state_value)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Instagram</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=400, headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - never echo OAuth material
            cancel_instagram_oauth(state_value)
            log.warning("Instagram OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Ошибка подключения Instagram</h1><p>Проверь настройки приложения и журнал диагностики.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=500, headers=headers,
            )

    @app.post("/api/platforms/pinterest/auth/start")
    async def pinterest_oauth_start(request: Request) -> dict:
        from tgfarm.pinterest_oauth import begin_pinterest_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/pinterest/auth/callback"
        try:
            return await begin_pinterest_oauth(
                settings.pinterest_app_id,
                settings.pinterest_app_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/pinterest/auth/callback")
    async def pinterest_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.pinterest_oauth import cancel_pinterest_oauth, complete_pinterest_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_pinterest_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_pinterest_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · Pinterest</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Pinterest не подключён</h1><p>Авторизация отменена или ответ Pinterest неполный. "
                "<a href='/'>Вернись в TgFarm и повтори подключение.</a></p></body></html>",
                status_code=400,
                headers=headers,
            )
        try:
            credentials = await complete_pinterest_oauth(state_value, code)
            await io(
                save_credentials,
                "pinterest",
                {
                    "PINTEREST_APP_ID": credentials["app_id"],
                    "PINTEREST_APP_SECRET": credentials["app_secret"],
                    "PINTEREST_ACCESS_TOKEN": credentials["access_token"],
                    "PINTEREST_REFRESH_TOKEN": credentials["refresh_token"],
                    "PINTEREST_TOKEN_EXPIRES_AT": credentials["expires_at"],
                    "PINTEREST_REFRESH_TOKEN_EXPIRES_AT": credentials["refresh_token_expires_at"],
                    "PINTEREST_BOARD_ID": credentials["board_id"],
                },
            )
            await io(
                record_auth_check,
                "pinterest",
                True,
                account=credentials["account"],
                detail=(
                    "Pinterest OAuth подтверждён. Выбери доступную доску в карточке Pinterest."
                    if not credentials["board_id"]
                    else "Pinterest OAuth подтвердил доступ к выбранной доске через API v5."
                ),
            )
            _operator("platform-auth-check", "pinterest", "OAuth connected")
            account = html.escape(credentials["account"])
            if credentials["board_id"]:
                message = "Доска <code>" + html.escape(credentials["board_id"]) + "</code> уже подтверждена."
            else:
                message = "Теперь вернись в TgFarm и нажми «Найти мои доски», чтобы выбрать место публикации."
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Pinterest подключён</h1><p>OAuth подтверждён: <strong>" + account + "</strong>.</p>"
                "<p>" + message + " Токены хранятся локально и обновляются автоматически.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=200,
                headers=headers,
            )
        except (ValueError, RuntimeError) as exc:
            cancel_pinterest_oauth(state_value)
            message = html.escape(redact_sensitive_text(exc, limit=260))
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Pinterest</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=400,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - never echo OAuth material
            cancel_pinterest_oauth(state_value)
            log.warning("Pinterest OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Ошибка подключения Pinterest</h1><p>Проверь настройки приложения и журнал диагностики.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=500,
                headers=headers,
            )

    @app.post("/api/platforms/reddit/auth/start")
    async def reddit_oauth_start(request: Request) -> dict:
        from tgfarm.reddit_oauth import begin_reddit_oauth

        settings = get_settings()
        if not bool(getattr(settings, "reddit_api_approved", False)):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Reddit Data API закрыт. Сначала получи явное одобрение Reddit "
                    "для приложения и сценария использования, затем включи REDDIT_API_APPROVED."
                ),
            )
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/reddit/auth/callback"
        try:
            return await begin_reddit_oauth(
                settings.reddit_client_id,
                settings.reddit_client_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/reddit/auth/callback")
    async def reddit_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.reddit_oauth import cancel_reddit_oauth, complete_reddit_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_reddit_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        headers = {
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_reddit_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · Reddit</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Reddit не подключён</h1><p>Авторизация отменена или ответ Reddit неполный. "
                "<a href='/'>Вернись в TgFarm и повтори подключение.</a></p></body></html>",
                status_code=400, headers=headers,
            )
        if not bool(getattr(get_settings(), "reddit_api_approved", False)):
            cancel_reddit_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · Reddit</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Reddit не подключён</h1><p>Доступ к Reddit Data API не подтверждён. "
                "Подключение остановлено до обращения к данным; получи одобрение Reddit "
                "и включи REDDIT_API_APPROVED.</p><a href='/'>Вернуться в TgFarm</a></body></html>",
                status_code=403, headers=headers,
            )
        try:
            credentials = await complete_reddit_oauth(state_value, code)
            await io(
                save_credentials, "reddit",
                {
                    "REDDIT_CLIENT_ID": credentials["client_id"],
                    "REDDIT_CLIENT_SECRET": credentials["client_secret"],
                    "REDDIT_REFRESH_TOKEN": credentials["refresh_token"],
                    "REDDIT_USER_AGENT": credentials["user_agent"],
                },
            )
            await io(
                record_auth_check, "reddit", True,
                account=credentials["account"],
                detail="Reddit OAuth подтвердил аккаунт; постоянный refresh token сохранён локально. Осталось указать целевой subreddit.",
            )
            _operator("platform-auth-check", "reddit", "OAuth connected")
            account = html.escape(credentials["account"])
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Reddit подключён</h1><p>Профиль подтверждён: <strong>" + account + "</strong>.</p>"
                "<p>Токен сохранён локально. Укажи subreddit по умолчанию в карточке Reddit. "
                "<a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=200, headers=headers,
            )
        except (ValueError, RuntimeError) as exc:
            cancel_reddit_oauth(state_value)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Reddit</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=400, headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - never echo OAuth material
            cancel_reddit_oauth(state_value)
            log.warning("Reddit OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Ошибка подключения Reddit</h1><p>Проверь настройки приложения и журнал диагностики.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=500, headers=headers,
            )

    @app.get("/api/platforms/pinterest/destinations")
    async def pinterest_destinations() -> dict:
        """List boards visible to the authenticated Pinterest user."""
        from tgfarm.pinterest_oauth import list_pinterest_boards

        try:
            items = await list_pinterest_boards()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        return {"platform": "pinterest", "destinations": items}

    @app.post("/api/platforms/pinterest/destination")
    async def pinterest_select_destination(payload: dict) -> dict:
        """Select one board only after the live API verifies it is accessible."""
        from tgfarm.pinterest_oauth import select_pinterest_board
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        board_id = str(payload.get("board_id") or "").strip()
        try:
            board = await select_pinterest_board(board_id)
            await io(save_credentials, "pinterest", {"PINTEREST_BOARD_ID": board["id"]})
            await io(
                record_auth_check,
                "pinterest",
                True,
                account=board["name"],
                detail="Pinterest OAuth и выбранная доска повторно подтверждены через API v5.",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        _operator("platform-destination-selected", "pinterest", board["id"])
        return {"ok": True, "platform": "pinterest", "destination": board}

    @app.post("/api/platforms/youtube/auth/start")
    async def youtube_oauth_start(request: Request) -> dict:
        from tgfarm.youtube_oauth import begin_youtube_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/youtube/auth/callback"
        try:
            return await begin_youtube_oauth(
                settings.youtube_client_id,
                settings.youtube_client_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/youtube/auth/callback")
    async def youtube_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials
        from tgfarm.youtube_oauth import cancel_youtube_oauth, complete_youtube_oauth

        params = request.scope.get("tgfarm_youtube_oauth_params") or {}
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
            cancel_youtube_oauth(state_value)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение YouTube не завершено</h1><p>Авторизация отменена или ответ сервера неполный. "
                "Вернись в панель и запусти подключение заново.</p><p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        try:
            credentials = await complete_youtube_oauth(state_value, code)
            await io(
                save_credentials,
                "youtube",
                {
                    "YOUTUBE_CLIENT_ID": credentials["client_id"],
                    "YOUTUBE_CLIENT_SECRET": credentials["client_secret"],
                    "YOUTUBE_ACCESS_TOKEN": credentials["access_token"],
                    "YOUTUBE_REFRESH_TOKEN": credentials["refresh_token"],
                    "YOUTUBE_TOKEN_EXPIRES_AT": credentials["expires_at"],
                    "YOUTUBE_CHANNEL_ID": credentials["channel_id"],
                    "YOUTUBE_CHANNEL_TITLE": credentials["channel_title"],
                },
            )
            await io(
                record_auth_check,
                "youtube",
                True,
                account=credentials["account"],
                detail="YouTube OAuth подтверждён: канал проверен через YouTube Data API и токены сохранены локально.",
            )
            _operator("platform-auth-check", "youtube", "OAuth connected")
            account = html.escape(credentials["account"])
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>YouTube подключён</h1><p>Доступ подтверждён для канала <strong>" + account + "</strong>.</p>"
                "<p>Токены сохранены локально и обновляются автоматически. Новые видео по умолчанию загружаются как Private. "
                "Включай YouTube в панели только после тестовой загрузки и проверки настроек Google Cloud.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=200, headers=page_headers)
        except (ValueError, RuntimeError) as exc:
            cancel_youtube_oauth(state_value)
            log.warning("YouTube OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить YouTube</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        except Exception as exc:  # noqa: BLE001 - callback must return a safe operator-facing page
            cancel_youtube_oauth(state_value)
            log.warning("YouTube OAuth callback failed (%s)", type(exc).__name__)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Ошибка подключения YouTube</h1><p>Не удалось проверить доступ. Проверь журнал диагностики и настройки OAuth.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=500, headers=page_headers)

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

    @app.get("/api/platforms/facebook/destinations")
    async def facebook_destinations() -> dict:
        """List managed Facebook Pages with an actual publishing task."""
        from tgfarm.facebook_oauth import list_facebook_pages

        try:
            pages = await list_facebook_pages()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        return {"platform": "facebook", "destinations": pages}

    @app.post("/api/platforms/facebook/destination")
    async def facebook_select_destination(payload: dict) -> dict:
        """Select a Page only after rechecking the owner's current CREATE_CONTENT task."""
        from tgfarm.facebook_oauth import select_facebook_page
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        page_id = str(payload.get("page_id") or "").strip()
        try:
            page = await select_facebook_page(page_id)
            await io(
                save_credentials,
                "facebook",
                {
                    "FACEBOOK_PAGE_ID": page["id"],
                    "FACEBOOK_PAGE_NAME": page["name"],
                    "FACEBOOK_PAGE_ACCESS_TOKEN": page["page_access_token"],
                },
            )
            await io(
                record_auth_check,
                "facebook",
                True,
                account=page["name"],
                detail="Facebook Page подтверждена через /me/accounts; задача CREATE_CONTENT проверена повторно.",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=redact_sensitive_text(exc, limit=320)) from exc
        _operator("platform-destination-selected", "facebook", page["id"])
        return {
            "ok": True,
            "platform": "facebook",
            "destination": {"id": page["id"], "name": page["name"]},
        }

    @app.post("/api/platforms/facebook/auth/start")
    async def facebook_oauth_start(request: Request) -> dict:
        from tgfarm.facebook_oauth import begin_facebook_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/facebook/auth/callback"
        try:
            return await begin_facebook_oauth(
                settings.facebook_app_id,
                settings.facebook_app_secret,
                settings.facebook_page_id,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/facebook/auth/callback")
    async def facebook_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.facebook_oauth import cancel_facebook_oauth, complete_facebook_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials

        params = request.scope.get("tgfarm_facebook_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_facebook_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · Facebook</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Facebook Page не подключена</h1><p>Авторизация отменена или ответ Facebook неполный. "
                "<a href='/'>Вернись в TgFarm и повтори подключение.</a></p></body></html>",
                status_code=400,
                headers=headers,
            )
        try:
            credentials = await complete_facebook_oauth(state_value, code)
            common = {
                "FACEBOOK_APP_ID": credentials["app_id"],
                "FACEBOOK_APP_SECRET": credentials["app_secret"],
                "FACEBOOK_USER_ACCESS_TOKEN": credentials["user_access_token"],
                "FACEBOOK_USER_TOKEN_EXPIRES_AT": credentials["user_token_expires_at"],
            }
            if credentials.get("page_id"):
                common.update({
                    "FACEBOOK_PAGE_ID": credentials["page_id"],
                    "FACEBOOK_PAGE_NAME": credentials["page_name"],
                    "FACEBOOK_PAGE_ACCESS_TOKEN": credentials["page_access_token"],
                })
            await io(save_credentials, "facebook", common)
            _operator(
                "platform-auth-check",
                "facebook",
                "OAuth connected; Page selection pending" if not credentials.get("page_id") else "OAuth connected",
            )
            if credentials.get("page_id"):
                await io(
                    record_auth_check,
                    "facebook",
                    True,
                    account=credentials["page_name"],
                    detail="Facebook Page OAuth подтверждён; Page access token получен для страницы с CREATE_CONTENT.",
                )
                message = (
                    "<h1>Facebook Page подключена</h1><p>Доступ подтверждён: <strong>"
                    + html.escape(credentials["page_name"])
                    + "</strong>.</p><p>Page ID <code>"
                    + html.escape(credentials["page_id"])
                    + "</code> сохранён.</p>"
                )
            else:
                page_count = len(credentials.get("available_pages") or [])
                message = (
                    "<h1>Facebook OAuth подтверждён</h1><p>Доступ к управляемым страницам получен.</p>"
                    "<p>Найдено Pages с правом публикации: <strong>"
                    + str(page_count)
                    + "</strong>. Вернись в TgFarm, нажми «Найти мои Pages» и выбери нужную страницу. "
                    "Page token будет сохранён только после повторной проверки прав.</p>"
                )
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключение</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                + message
                + "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=200,
                headers=headers,
            )
        except (ValueError, RuntimeError) as exc:
            cancel_facebook_oauth(state_value)
            message = html.escape(redact_sensitive_text(exc, limit=260))
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Facebook Page</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=400,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - never echo OAuth material
            cancel_facebook_oauth(state_value)
            log.warning("Facebook OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Ошибка подключения Facebook</h1><p>Проверь настройки приложения и журнал диагностики.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=500,
                headers=headers,
            )

    @app.post("/api/platforms/tumblr/auth/start")
    async def tumblr_oauth_start(request: Request) -> dict:
        from tgfarm.tumblr_oauth import begin_tumblr_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/tumblr/auth/callback"
        try:
            return await begin_tumblr_oauth(
                settings.tumblr_client_id,
                settings.tumblr_client_secret,
                redirect_uri,
                preferred_blog=settings.tumblr_blog_identifier,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/tumblr/auth/callback")
    async def tumblr_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials
        from tgfarm.tumblr_oauth import cancel_tumblr_oauth, complete_tumblr_oauth

        params = request.scope.get("tgfarm_tumblr_oauth_params") or {}
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
            cancel_tumblr_oauth(state_value)
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение Tumblr не завершено</h1><p>Авторизация отменена или ответ сервера неполный. "
                "Вернись в панель и запусти подключение заново.</p><p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        try:
            credentials = await complete_tumblr_oauth(state_value, code)
            await io(
                save_credentials,
                "tumblr",
                {
                    "TUMBLR_CLIENT_ID": credentials["client_id"],
                    "TUMBLR_CLIENT_SECRET": credentials["client_secret"],
                    "TUMBLR_ACCESS_TOKEN": credentials["access_token"],
                    "TUMBLR_REFRESH_TOKEN": credentials["refresh_token"],
                    "TUMBLR_TOKEN_EXPIRES_AT": credentials["expires_at"],
                    "TUMBLR_BLOG_IDENTIFIER": credentials["blog_identifier"],
                    "TUMBLR_BLOG_TITLE": credentials["blog_title"],
                    "TUMBLR_BLOG_URL": credentials["blog_url"],
                    "TUMBLR_USERNAME": credentials["username"],
                },
            )
            await io(
                record_auth_check,
                "tumblr",
                True,
                account=credentials["account"],
                detail="Tumblr OAuth подтвердил профиль и выбранный блог; токены сохранены локально.",
            )
            _operator("platform-auth-check", "tumblr", "OAuth connected")
            account = html.escape(credentials["account"])
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · подключено</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Tumblr подключён</h1><p>Доступ подтверждён для <strong>" + account + "</strong>.</p>"
                "<p>OAuth refresh token сохранён локально. Публикация выключена до включения Tumblr в карточке площадки.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=200, headers=page_headers)
        except (ValueError, RuntimeError) as exc:
            cancel_tumblr_oauth(state_value)
            log.warning("Tumblr OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · OAuth</title>"
                "<meta name='referrer' content='no-referrer'><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Tumblr</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>"
            )
            return HTMLResponse(body, status_code=400, headers=page_headers)
        except Exception as exc:  # noqa: BLE001 - never echo OAuth material
            cancel_tumblr_oauth(state_value)
            log.warning("Tumblr OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><title>TgFarm · ошибка</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Ошибка подключения Tumblr</h1><p>Проверь настройки OAuth-приложения и журнал диагностики.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=500,
                headers=page_headers,
            )

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

    @app.post("/api/platforms/tiktok/auth/start")
    async def tiktok_oauth_start(request: Request) -> dict:
        from tgfarm.tiktok_oauth import begin_tiktok_oauth

        settings = get_settings()
        redirect_uri = str(request.base_url).rstrip("/") + "/api/platforms/tiktok/auth/callback"
        try:
            return await begin_tiktok_oauth(
                settings.tiktok_client_key,
                settings.tiktok_client_secret,
                redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/tiktok/auth/callback")
    async def tiktok_oauth_callback(request: Request) -> HTMLResponse:
        import html

        from tgfarm.platforms import record_auth_check, save_platform_credentials
        from tgfarm.tiktok_oauth import cancel_tiktok_oauth, complete_tiktok_oauth

        params = request.scope.get("tgfarm_tiktok_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_tiktok_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение TikTok не завершено</h1><p>Ответ OAuth неполный или авторизация отменена.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=400,
                headers=headers,
            )
        try:
            credentials = await complete_tiktok_oauth(state_value, code)
            await io(save_platform_credentials, "tiktok", {
                "TIKTOK_CLIENT_KEY": credentials["client_key"],
                "TIKTOK_CLIENT_SECRET": credentials["client_secret"],
                "TIKTOK_ACCESS_TOKEN": credentials["access_token"],
                "TIKTOK_REFRESH_TOKEN": credentials["refresh_token"],
                "TIKTOK_TOKEN_EXPIRES_AT": credentials["token_expires_at"],
                "TIKTOK_REFRESH_TOKEN_EXPIRES_AT": credentials["refresh_token_expires_at"],
                "TIKTOK_OPEN_ID": credentials["open_id"],
                "TIKTOK_USERNAME": credentials["username"],
            })
            await io(
                record_auth_check,
                "tiktok",
                True,
                account=credentials["account"],
                detail="TikTok OAuth подтверждён; creator-info будет проверяться перед каждой прямой публикацией.",
            )
            _operator("platform-oauth-complete", "tiktok", "TikTok account OAuth completed")
            account = html.escape(credentials["account"] or "TikTok creator")
            body = (
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>TikTok подключён</h1><p>OAuth подтвердил аккаунт <strong>" + account + "</strong>.</p>"
                "<p>Токены сохранены локально. Публикация запускается только после просмотра видео, настройки подписи и отдельного подтверждения.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>"
            )
            return HTMLResponse(body, headers=headers)
        except (ValueError, RuntimeError) as exc:
            cancel_tiktok_oauth(state_value)
            log.warning("TikTok OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить TikTok</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>",
                status_code=400,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            cancel_tiktok_oauth(state_value)
            log.warning("TikTok OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить TikTok</h1><p>Внутренняя ошибка. Проверь конфигурацию и повтори попытку.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>",
                status_code=500,
                headers=headers,
            )

    @app.post("/api/platforms/douyin/auth/start")
    async def douyin_oauth_start(request: Request) -> dict:
        from tgfarm.douyin_oauth import begin_douyin_oauth
        settings = get_settings()
        origin = str(settings.public_base_url or request.base_url).rstrip("/")
        redirect_uri = origin + "/api/platforms/douyin/auth/callback"
        try:
            return await begin_douyin_oauth(settings.douyin_client_key, settings.douyin_client_secret, redirect_uri)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(exc, limit=300)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300)) from exc

    @app.get("/api/platforms/douyin/auth/callback")
    async def douyin_oauth_callback(request: Request) -> HTMLResponse:
        import html
        from tgfarm.douyin_oauth import cancel_douyin_oauth, complete_douyin_oauth
        from tgfarm.platforms import record_auth_check, save_platform_credentials as save_credentials
        params = request.scope.get("tgfarm_douyin_oauth_params") or {}
        state_value = str(params.get("state") or "")
        code = str(params.get("code") or "")
        provider_error = str(params.get("error") or "")
        headers = {
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        }
        if provider_error or not state_value or not code:
            cancel_douyin_oauth(state_value)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Подключение Douyin не завершено</h1><p>Авторизация отменена или ответ OAuth неполный.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>", status_code=400, headers=headers,
            )
        try:
            credentials = await complete_douyin_oauth(state_value, code)
            await io(save_credentials, "douyin", {
                "DOUYIN_CLIENT_KEY": credentials["client_key"],
                "DOUYIN_CLIENT_SECRET": credentials["client_secret"],
                "DOUYIN_ACCESS_TOKEN": credentials["access_token"],
                "DOUYIN_REFRESH_TOKEN": credentials["refresh_token"],
                "DOUYIN_TOKEN_EXPIRES_AT": credentials["token_expires_at"],
                "DOUYIN_REFRESH_TOKEN_EXPIRES_AT": credentials["refresh_token_expires_at"],
                "DOUYIN_OPEN_ID": credentials["open_id"],
                "DOUYIN_USERNAME": credentials["username"],
            })
            await io(record_auth_check, "douyin", True, account=credentials["account"],
                detail="Douyin OAuth и user_info подтверждены; каждое создание видео отдельно подтверждается владельцем.")
            _operator("platform-oauth-complete", "douyin", "Douyin account OAuth completed")
            account = html.escape(credentials["account"] or "Douyin creator")
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Douyin подключён</h1><p>OAuth подтвердил аккаунт <strong>" + account + "</strong>.</p>"
                "<p>Токены сохранены локально. Создание каждого видео запускается только после просмотра и отдельного подтверждения.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>", headers=headers,
            )
        except (ValueError, RuntimeError) as exc:
            cancel_douyin_oauth(state_value)
            log.warning("Douyin OAuth callback rejected (%s)", type(exc).__name__)
            message = html.escape(redact_sensitive_text(exc, limit=240))
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Douyin</h1><p>" + message + "</p>"
                "<p><a href='/'>Вернуться в TgFarm и повторить подключение</a></p></body></html>", status_code=400, headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            cancel_douyin_oauth(state_value)
            log.warning("Douyin OAuth callback failed (%s)", type(exc).__name__)
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                "<title>TgFarm OAuth</title><body style='font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                "<h1>Не удалось подключить Douyin</h1><p>Внутренняя ошибка. Проверь настройки и повтори попытку.</p>"
                "<p><a href='/'>Вернуться в TgFarm</a></p></body></html>", status_code=500, headers=headers,
            )

    @app.post("/api/posts/{post_public_id}/video/render")
    async def render_post_video(post_public_id: str) -> dict:
        """Render an original vertical MP4 from an already-published post."""
        from tgfarm.s11_video.renderer import render_post_short

        detail = await io(_post_detail, post_public_id)
        if detail.get("status") != "published":
            raise HTTPException(409, "Короткое видео доступно после подтверждённой основной публикации.")
        try:
            artifact = await io(render_post_short, post_public_id)
        except Exception as exc:  # noqa: BLE001 - no fake MP4 or fake success
            log.warning("short video render failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=422,
                detail=redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=300),
            ) from exc
        refreshed = await io(_post_detail, post_public_id)
        _operator("short-video-render", post_public_id, f"{artifact.width}x{artifact.height} {artifact.duration_s:.1f}s")
        return {
            "ok": True,
            "artifact": artifact.as_dict(),
            "video": refreshed.get("video"),
            "download_url": (
                f"/api/media/{refreshed['video']['id']}"
                if refreshed.get("video") and refreshed["video"].get("id")
                else None
            ),
        }

    @app.post("/api/posts/{post_public_id}/tiktok/prepare")
    async def prepare_tiktok_post(post_public_id: str) -> dict:
        """Prepare/preview a video and fetch the current creator privacy options."""
        from tgfarm.platforms import is_platform_enabled
        from tgfarm.s9_growth.adapters import ADAPTERS, primary_video_path
        from tgfarm.tiktok_oauth import get_tiktok_creator_info
        from tgfarm.tiktok_publish import _truncate_utf16

        if not is_platform_enabled("tiktok"):
            raise HTTPException(409, "Сначала включи TikTok в разделе «Площадки».")
        adapter = ADAPTERS.get("tiktok")
        if adapter is None or not adapter.configured():
            raise HTTPException(409, "Сначала подключи TikTok через OAuth и проверь доступ.")
        detail = await io(_post_detail, post_public_id)
        if detail.get("status") != "published":
            raise HTTPException(409, "TikTok Direct Post доступен после подтверждённой основной публикации.")
        with session_scope() as session:
            post_pk = session.execute(
                select(Post.id).where(Post.public_id == post_public_id)
            ).scalar_one_or_none()
        if post_pk is None:
            raise HTTPException(404, "post not found")
        video_path = await io(primary_video_path, post_pk)
        artifact = None
        if not video_path:
            from tgfarm.s11_video.renderer import render_post_short

            try:
                artifact = await io(render_post_short, post_public_id)
                video_path = artifact.path
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=422,
                    detail=redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=300),
                ) from exc
            detail = await io(_post_detail, post_public_id)
        if not video_path or not Path(video_path).is_file():
            raise HTTPException(410, "Видео не найдено после подготовки.")
        try:
            creator = await get_tiktok_creator_info()
        except Exception as exc:  # noqa: BLE001 - provider errors are bounded for the dashboard
            raise HTTPException(
                status_code=502,
                detail=redact_sensitive_text(exc, limit=300) or "Не удалось получить настройки TikTok.",
            ) from exc
        caption = _truncate_utf16(
            "\n\n".join(part for part in (str(detail.get("hook") or ""), str(detail.get("body") or "")) if part),
            2200,
        )
        return {
            "ok": True,
            "post": post_public_id,
            "caption": caption,
            "video": detail.get("video"),
            "download_url": (
                f"/api/media/{detail['video']['id']}"
                if detail.get("video") and detail["video"].get("id")
                else None
            ),
            "artifact": artifact.as_dict() if artifact else None,
            "creator": creator,
            "live": get_settings().live,
            "warning": (
                "Проверь видео, подпись и приватность. До аудита приложения TikTok может ограничить видимость API-публикаций."
            ),
        }

    @app.post("/api/posts/{post_public_id}/tiktok/publish")
    async def publish_tiktok_post(post_public_id: str, request: TikTokPublishRequest) -> dict:
        """Submit one video after explicit consent and durable crosspost claim."""
        from tgfarm.core.schemas import CrosspostResult
        from tgfarm.platforms import is_platform_enabled
        from tgfarm.s9_growth.adapters import ADAPTERS, begin_crosspost, log_result, primary_video_path
        from tgfarm.tiktok_publish import publish_tiktok_video

        if not request.consent:
            raise HTTPException(400, "Подтверди отправку именно этого видео в TikTok.")
        settings = get_settings()
        if not settings.live:
            raise HTTPException(409, "TGFARM_MODE не live. Сначала выполни контролируемый запуск в режиме live.")
        if not is_platform_enabled("tiktok"):
            raise HTTPException(409, "TikTok выключен в разделе «Площадки».")
        adapter = ADAPTERS.get("tiktok")
        if adapter is None or not adapter.configured():
            raise HTTPException(409, "TikTok OAuth не настроен. Подключи аккаунт и проверь доступ.")

        detail = await io(_post_detail, post_public_id)
        if detail.get("status") != "published":
            raise HTTPException(409, "Direct Post доступен только для подтверждённой основной публикации.")
        with session_scope() as session:
            post_pk = session.execute(
                select(Post.id).where(Post.public_id == post_public_id)
            ).scalar_one_or_none()
        if post_pk is None:
            raise HTTPException(404, "post not found")
        video_path = await io(primary_video_path, post_pk)
        if not video_path or not Path(video_path).is_file():
            raise HTTPException(409, "Сначала подготовь видео и дождись готового MP4.")

        attempt = await io(begin_crosspost, post_public_id, "tiktok")
        if attempt is None:
            raise HTTPException(409, "Для этого поста уже есть доставка/блокировка. Проверь историю TikTok перед повтором.")
        try:
            result = await publish_tiktok_video(
                video_path,
                caption=request.caption,
                privacy_level=request.privacy_level,
            )
        except Exception as exc:  # noqa: BLE001 - uncertain outcome must never be retried blindly
            log.warning("TikTok Direct Post failed unexpectedly (%s)", type(exc).__name__)
            result = CrosspostResult(
                platform="tiktok",
                status="unknown",
                error="Результат отправки не подтверждён. Проверь TikTok перед повтором.",
            )

        await io(log_result, post_public_id, result, attempt)
        _operator(
            "tiktok-direct-publish",
            post_public_id,
            f"status={result.status} privacy={request.privacy_level}",
        )
        if result.status == "posted":
            message = "TikTok подтвердил публикацию."
        elif result.status == "unknown":
            message = (
                "Результат не подтверждён. Проверь TikTok вручную и не повторяй отправку, "
                "пока не убедишься, что видео отсутствует."
            )
        else:
            message = "TikTok не подтвердил публикацию. Исправь причину перед повтором."
        return {
            "ok": result.status == "posted",
            "status": result.status,
            "remote_id": result.remote_id,
            "url": result.url,
            "error": result.error,
            "detail": message,
        }

    @app.post("/api/posts/{post_public_id}/douyin/prepare")
    async def prepare_douyin_post(post_public_id: str) -> dict:
        from tgfarm.douyin_oauth import get_douyin_user_info
        from tgfarm.platforms import is_platform_enabled
        from tgfarm.s9_growth.adapters import ADAPTERS, primary_video_path
        if not is_platform_enabled("douyin"):
            raise HTTPException(409, "Сначала включи Douyin в разделе «Площадки».")
        adapter = ADAPTERS.get("douyin")
        if adapter is None or not adapter.configured():
            raise HTTPException(409, "Сначала подключи Douyin через Open Platform OAuth.")
        detail = await io(_post_detail, post_public_id)
        if detail.get("status") != "published":
            raise HTTPException(409, "Подготовка Douyin доступна после подтверждённой основной публикации.")
        with session_scope() as session:
            post_pk = session.execute(select(Post.id).where(Post.public_id == post_public_id)).scalar_one_or_none()
        if post_pk is None:
            raise HTTPException(404, "post not found")
        video_path = await io(primary_video_path, post_pk)
        artifact = None
        if not video_path:
            from tgfarm.s11_video.renderer import render_post_short
            try:
                artifact = await io(render_post_short, post_public_id)
                video_path = artifact.path
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=422, detail=redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=300)) from exc
            detail = await io(_post_detail, post_public_id)
        if not video_path or not Path(video_path).is_file():
            raise HTTPException(410, "Видео не найдено после подготовки.")
        try:
            profile = await get_douyin_user_info()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=redact_sensitive_text(exc, limit=300) or "Не удалось проверить Douyin OAuth.") from exc
        caption = "\n\n".join(part for part in (str(detail.get("hook") or ""), str(detail.get("body") or "")) if part)[:1000]
        return {
            "ok": True, "post": post_public_id, "caption": caption,
            "account": profile.get("nickname") or get_settings().douyin_username or "Douyin creator",
            "video": detail.get("video"),
            "download_url": f"/api/media/{detail['video']['id']}" if detail.get("video") and detail["video"].get("id") else None,
            "artifact": artifact.as_dict() if artifact else None,
            "live": get_settings().live,
            "warning": "После создания видео проходит модерацию и временно может быть видно только владельцу. Проверь оригинальный MP4 и подпись.",
        }

    @app.post("/api/posts/{post_public_id}/douyin/publish")
    async def publish_douyin_post(post_public_id: str, request: DouyinPublishRequest) -> dict:
        from tgfarm.core.schemas import CrosspostResult
        from tgfarm.douyin_publish import publish_douyin_video
        from tgfarm.platforms import is_platform_enabled
        from tgfarm.s9_growth.adapters import ADAPTERS, begin_crosspost, log_result, primary_video_path
        if not request.consent:
            raise HTTPException(400, "Подтверди создание именно этого видео в Douyin.")
        if not get_settings().live:
            raise HTTPException(409, "TGFARM_MODE не live. Сначала проверь подготовку в dry-режиме.")
        if not is_platform_enabled("douyin"):
            raise HTTPException(409, "Douyin выключен в разделе «Площадки».")
        adapter = ADAPTERS.get("douyin")
        if adapter is None or not adapter.configured():
            raise HTTPException(409, "Douyin OAuth не настроен. Подключи аккаунт и проверь доступ.")
        detail = await io(_post_detail, post_public_id)
        if detail.get("status") != "published":
            raise HTTPException(409, "Создание Douyin-видео доступно только после подтверждённой основной публикации.")
        with session_scope() as session:
            post_pk = session.execute(select(Post.id).where(Post.public_id == post_public_id)).scalar_one_or_none()
        if post_pk is None:
            raise HTTPException(404, "post not found")
        video_path = await io(primary_video_path, post_pk)
        if not video_path or not Path(video_path).is_file():
            raise HTTPException(409, "Сначала создай и проверь готовый вертикальный MP4.")
        attempt = await io(begin_crosspost, post_public_id, "douyin")
        if attempt is None:
            raise HTTPException(409, "Для этого поста уже есть доставка или блокировка. Проверь историю Douyin перед повтором.")
        try:
            result = await publish_douyin_video(video_path, caption=request.caption)
        except Exception as exc:  # noqa: BLE001
            log.warning("Douyin video creation failed unexpectedly (%s)", type(exc).__name__)
            result = CrosspostResult(platform="douyin", status="unknown", error="Результат создания видео не подтверждён. Проверь Douyin перед повтором.")
        await io(log_result, post_public_id, result, attempt)
        _operator("douyin-video-create", post_public_id, f"status={result.status}")
        detail = (
            "Douyin подтвердил создание видео. Оно проходит модерацию и временно может быть видно только автору."
            if result.status == "posted" else
            "Результат не подтверждён. Проверь Douyin вручную и не запускай повтор, пока не установишь исход."
            if result.status == "unknown" else
            "Douyin не подтвердил создание видео. Исправь причину перед новой отправкой."
        )
        return {"ok": result.status == "posted", "status": result.status, "remote_id": result.remote_id, "url": result.url, "error": result.error, "detail": detail}

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

    @app.get("/api/telegraph/metrics")
    async def telegraph_metrics() -> dict:
        from tgfarm.s9_growth.telegraph_metrics import get_telegraph_metrics_report

        return await io(get_telegraph_metrics_report)

    @app.post("/api/telegraph/metrics/refresh")
    async def telegraph_metrics_refresh(request: RedditMetricsRefreshRequest) -> dict:
        from tgfarm.s9_growth.telegraph_metrics import (
            TelegraphMetricsError,
            refresh_telegraph_metrics,
        )

        try:
            return await refresh_telegraph_metrics(limit=request.limit)
        except TelegraphMetricsError as exc:
            raise HTTPException(
                status_code=503,
                detail=redact_sensitive_text(exc, limit=300),
            ) from exc

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

    @app.get("/api/review/queue")
    async def review_queue_api(limit: int = Query(25, ge=1, le=200)) -> dict:
        """What needs an operator now: unknown outcomes, terminal failures, held posts."""
        from tgfarm.review import review_queue

        return await io(review_queue, limit=limit)

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

    @app.post("/api/posts/retry-failed")
    async def requeue_failed_posts(request: FailedRequeueRequest) -> dict:
        """Bounded, consented, journalled requeue of terminal publish failures.

        A POST that writes nothing unless `apply` is true: the panel calls it once
        to see what a pass would do (counts, a sample with ages, and every refusal
        with the rule that produced it) and again to do it. The §80 gates — the
        consent flag and a fresh copy in `backup/` — are enforced inside
        `maintenance.requeue_failed_publishes`, not here, so the CLI cannot bypass
        them by importing the function and this route cannot weaken them. A refusal
        comes back as a normal report carrying `refused`, not as an HTTP error,
        because the numbers the operator was reading are still the product.
        """
        from tgfarm.maintenance import requeue_failed_publishes

        report = await io(
            requeue_failed_publishes,
            apply=request.apply,
            limit=request.limit,
            platform=request.platform,
            reason=request.reason,
        )
        _operator(
            "requeue-failed-batch",
            (request.platform or "all"),
            " ".join(
                part
                for part in (
                    f"apply={report['apply']}",
                    f"changed={report['changed']}",
                    f"eligible={report['eligible']}",
                    f"scanned={report['scanned']}",
                    f"refusals={report['refusals']}",
                    f"reason={redact_sensitive_text(request.reason, limit=120)}",
                    f"REFUSED: {report['refused']}" if report.get("refused") else "",
                )
                if part
            )[:500],
        )
        return report

    @app.post("/api/posts/{post_public_id}/retry")
    async def retry_failed_publish(post_public_id: str) -> dict:
        result = await io(_retry_failed_publish, post_public_id)
        if not result.get("ok"):
            code = 404 if result.get("error") == "not_found" else 409
            # The refusal has to say which rule refused it, or the operator reads
            # "not_failed" and has to guess whether that means §80 or a stale click.
            raise HTTPException(code, result.get("reason") or result.get("error", "retry failed"))
        _operator(
            "publish-retry",
            post_public_id,
            f"manually requeued after definite failure on {result['platform']}: budget "
            f"reset from {result.get('previous_attempts')} attempts"
            f" | last error: {result.get('previous_error')}",
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

    @app.get("/api/ai/usage")
    async def ai_usage(days: int = Query(7, ge=1, le=30)) -> dict:
        """Persisted AI call-budget use, including offline fallbacks and stale reservations."""
        from tgfarm.llm.policy import usage_report

        return await io(usage_report, days)

    @app.get("/api/agents/benchmark")
    async def agent_benchmark_report() -> dict:
        """Return safe, local metadata for paired editorial-team experiments."""
        from tgfarm.agents.benchmark import aggregate_benchmarks

        return await io(aggregate_benchmarks)

    @app.post("/api/agents/benchmark")
    async def run_agent_benchmark(request: AgentBenchmarkRequest) -> dict:
        """Run one paired, non-publishing writer/strategist comparison."""
        from tgfarm.agents.benchmark import AgentBenchmarkError, run_paired_benchmark

        try:
            return await run_paired_benchmark(
                request.cluster_public_id,
                channel_id=request.channel_id,
            )
        except AgentBenchmarkError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/metrics/formats")
    async def metrics_formats(days: int = Query(30, ge=1, le=365)) -> dict:
        """Published posts and measured engagement per post format."""
        from tgfarm.s8_analytics.formats import format_performance

        return await io(format_performance, days)

    @app.get("/api/metrics/freshness")
    async def metrics_freshness_api() -> dict:
        """Which platforms actually have measured data, and when it was taken.

        One report per request. This used to call ``metrics_freshness()`` and
        ``freshness_summary()``, and the summary builds the same detail again
        inside itself, so every dashboard refresh read the same tables twice
        (four sessions instead of two). The summary is now counted from the detail
        by ``_freshness_summary_of``, which reads nothing.

        ``collectors``, ``secondary_destinations`` and ``tables_without_collector``
        are additionally exposed once at the top level: ``tgfarm/ui/app.js`` renders
        the per-job answer («задача стоит в расписании, но нового снимка не
        оставляла») beside the platform numbers, and a scheduled job is never
        evidence of fresh data.
        """
        from tgfarm.s8_analytics.freshness import metrics_freshness

        detail = await io(metrics_freshness)
        summary = _freshness_summary_of(detail)
        return {
            "generated_at": detail.get("generated_at"),
            # Two bounds, deliberately not merged: the platform rows are judged by
            # one constant, every collector and destination row by the cadence of
            # its own registered job, and a report-wide `stale_after_hours` here is
            # what let an hourly job look healthy for a day.
            "platform_stale_after_hours": detail.get("platform_stale_after_hours"),
            "stale_bound": detail.get("stale_bound"),
            "snapshots_total": detail.get("snapshots_total"),
            "platforms": detail.get("platforms") or {},
            "collectors": detail.get("collectors") or [],
            "secondary_destinations": detail.get("secondary_destinations") or [],
            "tables_without_collector": detail.get("tables_without_collector") or [],
            "collectors_error": detail.get("collectors_error"),
            "error": detail.get("error"),
            "summary": summary,
        }

    @app.get("/api/metrics/coverage")
    async def metrics_coverage_api() -> dict:
        """Which publications are measured per platform, and why the rest is not."""
        from tgfarm.s8_analytics.coverage import measurement_coverage

        return await io(measurement_coverage)

    @app.get("/api/backlog")
    async def backlog_api() -> dict:
        """Flow and stock of the publication queue per platform, with the reason
        the newest publish tick journalled for what it sent."""
        return await io(_backlog)

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
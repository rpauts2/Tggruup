"""Editorial distribution planner for autonomous cross-posting.

The planner chooses *which* published material is suitable for *which* connected
platform, limits each destination's daily volume, respects a minimum interval,
and avoids near-duplicate deliveries. It never treats a browser session as an
API adapter and it excludes platforms requiring per-post consent.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from tgfarm.core.settings import ROOT, get_settings
from tgfarm.core.text import content_tokens, normalize, token_jaccard
from tgfarm.db.models import Channel, CrosspostLog, Media, Post, PostStat
from tgfarm.db.session import session_scope

# These are conservative starting budgets, not claims about provider hard caps.
# Learning may adjust them by at most one post/day from delivery reliability.
PLATFORM_RULES: dict[str, dict[str, Any]] = {
    "vk": {
        "daily": 3, "max_daily": 4, "min_gap_hours": 3,
        "age_days": 7, "broad": True,
        "topics": {
            "технологии": ("технолог", "ai", "нейросет", "программ", "гаджет", "робот"),
            "наука": ("наук", "космос", "физик", "биолог", "исследован", "учен"),
            "экономика": ("эконом", "бизнес", "рынок", "деньг", "финанс", "компан", "работ"),
            "общество": ("обще", "город", "люд", "образован", "истор", "культур", "жизн"),
        },
    },
    "max": {
        "daily": 2, "max_daily": 3, "min_gap_hours": 4,
        "age_days": 7, "broad": True,
        "topics": {
            "полезные разборы": ("как", "почему", "объясн", "инструкц", "совет", "разбор"),
            "технологии": ("технолог", "ai", "нейросет", "программ", "сервис", "робот"),
            "наука": ("наук", "космос", "исследован", "учен", "открыт"),
            "деньги и бизнес": ("бизнес", "эконом", "рынок", "деньг", "компан", "финанс"),
        },
    },
    "x": {
        "daily": 2, "max_daily": 3, "min_gap_hours": 4,
        "age_days": 4, "broad": False,
        "topics": {
            "технологии и AI": ("технолог", "ai", "нейросет", "модель", "программ", "openai", "робот"),
            "данные и наука": ("данн", "исследован", "наук", "космос", "статист", "эксперимент"),
            "бизнес и экономика": ("бизнес", "эконом", "рынок", "стартап", "финанс", "компан"),
        },
    },
    "reddit": {
        "daily": 1, "max_daily": 1, "min_gap_hours": 24,
        "age_days": 5, "broad": False,
        "topics": {
            "практические разборы": ("как", "инструкц", "опыт", "ошиб", "гайд", "решен", "практик"),
            "технологии": ("технолог", "ai", "нейросет", "программ", "разработ", "open source"),
            "наука": ("наук", "исследован", "эксперимент", "данн", "космос"),
        },
    },
    "bluesky": {
        "daily": 2, "max_daily": 3, "min_gap_hours": 6,
        "age_days": 7, "broad": False,
        "topics": {
            "технологии": ("технолог", "ai", "нейросет", "программ", "open source", "linux"),
            "наука": ("наук", "исследован", "космос", "физик", "данн"),
            "игры и культура": ("игр", "кино", "культур", "дизайн", "книг", "музык"),
        },
    },
    "threads": {
        "daily": 2, "max_daily": 3, "min_gap_hours": 6,
        "age_days": 5, "broad": False,
        "topics": {
            "общество и жизнь": ("люд", "обще", "город", "образован", "жизн", "работ", "семь"),
            "культура": ("культур", "книг", "кино", "дизайн", "истор", "музык"),
            "мнения и объяснения": ("почему", "важно", "мнение", "объясн", "вывод", "вопрос"),
        },
    },
    "mastodon": {
        "daily": 1, "max_daily": 2, "min_gap_hours": 12,
        "age_days": 7, "broad": False,
        "topics": {
            "открытые технологии": ("open source", "linux", "privacy", "приват", "свободн", "код"),
            "наука": ("наук", "исследован", "данн", "космос", "эксперимент"),
            "технологии": ("технолог", "ai", "нейросет", "программ", "интернет"),
        },
    },
    "youtube": {
        "daily": 1, "max_daily": 1, "min_gap_hours": 18,
        "age_days": 7, "broad": True,
        "topics": {
            "объяснения и разборы": ("почему", "как", "объясн", "разбор", "истор", "сравнен"),
            "технологии": ("технолог", "ai", "нейросет", "гаджет", "робот", "программ"),
            "наука": ("наук", "космос", "исследован", "эксперимент", "открыт"),
        },
        "requires_video": True,
    },
}

# These platforms may have a technical adapter but are not eligible for
# unattended background sends. The platform's own consent flow is separate.
MANUAL_ONLY: dict[str, str] = {
    "tiktok": "Каждая публикация TikTok требует отдельного просмотра, выбора приватности и согласия автора.",
    "linkedin": "Публичные правила LinkedIn API не допускают использование API для автоматизации публикаций.",
}
MIN_SCORE = 0.38
MAX_CANDIDATES = 180
RECENT_DUPLICATE_LIMIT = 24


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _topic_hits(text: str, topics: dict[str, tuple[str, ...]]) -> list[str]:
    normalized = normalize(text)
    found: list[str] = []
    for topic, markers in topics.items():
        matched = False
        for marker in markers:
            marker = normalize(marker)
            if len(marker) <= 3:
                matched = bool(re.search(rf"(?<![a-zа-яё0-9]){re.escape(marker)}(?![a-zа-яё0-9])", normalized))
            else:
                matched = marker in normalized
            if matched:
                break
        if matched:
            found.append(topic)
    return found


def _quality_score(post: Any, stat: Any | None) -> float:
    quality = getattr(post, "quality", None) or {}
    scores = quality.get("scores") if isinstance(quality, dict) else {}
    values: list[float] = []
    if isinstance(scores, dict):
        for raw in scores.values():
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if 0.0 <= value <= 1.0:
                values.append(value)
    gate = str(quality.get("gate") or "").lower() if isinstance(quality, dict) else ""
    editorial = sum(values) / len(values) if values else (0.72 if gate in {"pass", "passed", "ok"} else 0.58)
    claim_score = 0.0
    claims = getattr(post, "claims", None) or []
    if claims:
        valid = sum(1 for claim in claims if isinstance(claim, dict) and claim.get("verified"))
        claim_score = valid / len(claims)
    evidence = 0.0
    if getattr(post, "sources", None):
        evidence = min(1.0, len(post.sources) / 2)
    performance = 0.0
    if stat is not None:
        views = max(0, int(getattr(stat, "views", 0) or 0))
        reactions = max(0, int(getattr(stat, "reactions", 0) or 0))
        comments = max(0, int(getattr(stat, "comments", 0) or 0))
        engagement = (reactions + comments) / max(views, 1)
        performance = min(1.0, engagement / 0.08) if views >= 30 else 0.0
    return max(0.0, min(1.0, 0.55 * editorial + 0.20 * claim_score + 0.15 * evidence + 0.10 * performance))


def _angle_fit(platform_id: str, angle: str) -> float:
    angle = str(angle or "").lower()
    preferences = {
        "reddit": {"checklist": 1.0, "analysis": 1.0, "comparison": 0.95, "why_it_matters": 0.9, "news": 0.55},
        "x": {"analysis": 1.0, "news": 0.9, "comparison": 0.8, "why_it_matters": 0.8, "checklist": 0.7},
        "bluesky": {"analysis": 0.95, "why_it_matters": 0.9, "history": 0.8, "news": 0.65},
        "threads": {"why_it_matters": 1.0, "history": 0.9, "comparison": 0.9, "checklist": 0.85},
        "mastodon": {"analysis": 1.0, "checklist": 0.95, "history": 0.8, "news": 0.7},
        "youtube": {"analysis": 0.95, "comparison": 1.0, "history": 0.95, "checklist": 0.9, "news": 0.7},
    }
    return preferences.get(platform_id, {}).get(angle, 0.72)


def score_post_for_platform(
    post: Any,
    platform_id: str,
    *,
    channel: Any | None = None,
    stat: Any | None = None,
    now: datetime | None = None,
    recent_texts: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic editorial fit score with an explanation visible in the UI."""
    current = _utc(now or datetime.now(UTC))
    assert current is not None
    rules = PLATFORM_RULES.get(platform_id)
    if rules is None:
        return {"score": 0.0, "eligible": False, "reason": "Нет правила распределения для этой площадки."}

    published_at = _utc(getattr(post, "published_at", None))
    if published_at is None:
        return {"score": 0.0, "eligible": False, "reason": "Дата исходной публикации неизвестна."}
    age_hours = max(0.0, (current - published_at).total_seconds() / 3600)
    if age_hours > float(rules["age_days"]) * 24:
        return {"score": 0.0, "eligible": False, "reason": "Материал уже устарел для этой площадки."}

    hook = str(getattr(post, "hook", "") or "")
    body = str(getattr(post, "body", "") or "")
    source_text = f"{hook}\n{body}"
    hits = _topic_hits(source_text, rules["topics"])
    if rules.get("broad"):
        topic_score = 0.64 if hits else 0.52
    else:
        topic_score = min(1.0, 0.18 + 0.30 * len(hits))
    if not hits and not rules.get("broad"):
        return {
            "score": 0.0,
            "eligible": False,
            "topic": "не совпадает",
            "reason": "Тема не входит в профиль аудитории этой площадки.",
        }

    source_niche = str(getattr(channel, "niche", "") or "").lower()
    if source_niche and source_niche not in {"general", "all", "misc"}:
        niche_markers = {
            "tech": ("технолог", "ai", "нейросет", "программ", "linux", "робот"),
            "business": ("бизнес", "эконом", "рынок", "финанс", "компан", "стартап"),
            "science": ("наук", "исследован", "физик", "биолог", "космос"),
            "games": ("игр", "гейм", "игров", "steam", "консол"),
            "health": ("здоров", "медицин", "врач", "питани", "сон"),
        }.get(source_niche, ())
        niche_fit = 1.0 if not niche_markers or any(m in normalize(source_text) for m in niche_markers) else 0.58
    else:
        niche_fit = 0.88

    angle = _angle_fit(platform_id, getattr(post, "angle", ""))
    quality = _quality_score(post, stat)
    freshness = max(0.0, 1.0 - age_hours / (float(rules["age_days"]) * 24))
    recent_texts = recent_texts or []
    similarity = max((token_jaccard(source_text, text) for text in recent_texts), default=0.0)
    if similarity >= 0.72:
        return {
            "score": 0.0,
            "eligible": False,
            "topic": hits[0] if hits else "общая тема",
            "similarity": round(similarity, 3),
            "reason": "Похожий материал уже публиковался на этой площадке.",
        }
    novelty = 1.0 - max(0.0, similarity - 0.22) * 0.8

    score = (
        0.38 * topic_score
        + 0.18 * niche_fit
        + 0.16 * angle
        + 0.18 * quality
        + 0.10 * freshness
    ) * novelty
    topic = hits[0] if hits else ("общая тематика" if rules.get("broad") else "—")
    reasons = [
        f"тема: {topic}",
        f"качество: {quality:.0%}",
        f"свежесть: {freshness:.0%}",
    ]
    if similarity > 0.35:
        reasons.append("есть частичное сходство с недавним постом")
    return {
        "score": round(score, 4),
        "eligible": score >= MIN_SCORE,
        "topic": topic,
        "topics": hits,
        "similarity": round(similarity, 3),
        "reason": " · ".join(reasons),
        "quality": round(quality, 3),
        "age_hours": round(age_hours, 1),
    }


def effective_daily_cap(platform_id: str, recent_logs: list[Any]) -> int:
    """Adjust a conservative starting cap by delivery reliability, never by luck."""
    rule = PLATFORM_RULES[platform_id]
    base = int(rule["daily"])
    attempts = [
        row for row in recent_logs
        if row.platform == platform_id and row.status in {"posted", "failed", "permanent"}
    ]
    if len(attempts) < 8:
        return base
    success_rate = sum(1 for row in attempts if row.status == "posted") / len(attempts)
    if success_rate >= 0.95:
        return min(int(rule["max_daily"]), base + 1)
    if success_rate < 0.70:
        return max(1, base - 1)
    return base


def _latest_logs(rows: list[Any]) -> dict[tuple[str, str], Any]:
    latest: dict[tuple[str, str], Any] = {}
    for row in rows:
        key = (str(row.post_public_id), str(row.platform))
        previous = latest.get(key)
        if previous is None or row.id > previous.id:
            latest[key] = row
    return latest


def _build_primary_platform_plan(
    now: datetime,
    settings: Any,
    platform_catalog: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose primary-channel budgets beside the crosspost distribution plan."""
    personas = [
        persona for persona in get_config_personas()
        if persona.enabled and persona.platform in {"telegram", "vk", "dzen"}
    ]
    result: list[dict[str, Any]] = []
    for platform_id in ("telegram", "vk", "dzen"):
        active = [persona for persona in personas if persona.platform == platform_id]
        if not active:
            continue
        if platform_id == "dzen":
            daily_cap = 2
        elif platform_id == "vk":
            daily_cap = 3
        else:
            daily_cap = sum(max(1, int(persona.posts_per_day)) for persona in active)

        midnight = now - timedelta(hours=24)
        with session_scope() as session:
            sent = int(
                session.execute(
                    select(func.count())
                    .select_from(Post)
                    .join(Channel, Channel.id == Post.channel_id)
                    .where(
                        Channel.platform == platform_id,
                        Channel.enabled.is_(True),
                        Post.status.in_(("scheduled", "publishing", "published")),
                        func.coalesce(Post.published_at, Post.scheduled_at, Post.created_at) >= midnight,
                    )
                ).scalar_one()
            )
            queued_rows = session.execute(
                select(Post, Channel)
                .join(Channel, Channel.id == Post.channel_id)
                .where(
                    Channel.platform == platform_id,
                    Channel.enabled.is_(True),
                    Post.status.in_(("gated", "scheduled")),
                )
                .order_by(Post.scheduled_at.asc().nullsfirst(), Post.created_at.asc())
                .limit(5)
            ).all()
            queue_preview = [
                {
                    "post_public_id": post.public_id,
                    "title": str(post.hook or "")[:160],
                    "channel": channel.title,
                    "status": post.status,
                    "scheduled_at": (
                        _utc(post.scheduled_at).isoformat(timespec="minutes")
                        if post.scheduled_at else None
                    ),
                }
                for post, channel in queued_rows
            ]
        item = platform_catalog.get(platform_id, {})
        enabled = bool(item.get("enabled"))
        configured = bool(item.get("configured"))
        if not enabled:
            reason = "Площадка выключена в UI."
        elif not configured:
            reason = "Основной аккаунт или канал не настроен."
        elif settings.mode == "off":
            reason = "Глобальный режим OFF: отправки остановлены."
        elif settings.approval_required:
            reason = "Включено ручное одобрение публикаций."
        else:
            reason = "Автоматическая очередь основной площадки активна."
        topics = list(dict.fromkeys(
            str(persona.niche or "общая тематика") for persona in active
        ))
        result.append({
            "platform": platform_id,
            "name": item.get("name", platform_id),
            "kind": "primary",
            "enabled": enabled,
            "configured": configured,
            "automation": "primary",
            "daily_cap": daily_cap,
            "daily_sent": sent,
            "remaining": max(0, daily_cap - sent),
            "channels": [persona.title for persona in active],
            "topics": topics,
            "queue_preview": queue_preview,
            "reason": reason,
        })
    return result


def build_distribution_plan(
    *,
    now: datetime | None = None,
    max_age_days: int = 14,
    max_selected_per_platform: int = 1,
) -> dict[str, Any]:
    """Build a live view of eligible platform deliveries and their daily budgets."""
    from tgfarm import platforms
    from tgfarm.s9_growth.adapters import ADAPTERS

    current = _utc(now or datetime.now(UTC))
    assert current is not None
    settings = get_settings()
    from tgfarm.config import get_config

    crosspost_config = get_config().schedule.crosspost or {}
    min_age_minutes = max(0, int(crosspost_config.get("delay_min", 25)))
    cutoff = current - timedelta(days=max(1, int(max_age_days)))
    day_cutoff = current - timedelta(hours=24)
    logs_cutoff = current - timedelta(days=30)

    with session_scope() as session:
        candidates = session.execute(
            select(Post, Channel)
            .join(Channel, Channel.id == Post.channel_id)
            .where(
                Post.status == "published",
                Post.published_at.is_not(None),
                Post.published_at >= cutoff,
                Channel.enabled.is_(True),
            )
            .order_by(Post.published_at.desc(), Post.id.desc())
            .limit(MAX_CANDIDATES)
        ).all()
        candidate_ids = [post.id for post, _channel in candidates]
        media_by_post: dict[int, list[Any]] = {}
        stats_by_post: dict[int, Any] = {}
        if candidate_ids:
            for media in session.execute(
                select(Media).where(Media.post_id.in_(candidate_ids))
            ).scalars().all():
                media_by_post.setdefault(media.post_id, []).append(media)
            for stat in session.execute(
                select(PostStat)
                .where(PostStat.post_id.in_(candidate_ids))
                .order_by(PostStat.collected_at.desc())
            ).scalars().all():
                stats_by_post.setdefault(stat.post_id, stat)
        logs = session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.created_at >= logs_cutoff)
            .order_by(CrosspostLog.id.asc())
        ).scalars().all()

    latest = _latest_logs(logs)
    catalog = {item["id"]: item for item in platforms.CATALOG}
    platform_catalog = {
        item["id"]: item
        for item in platforms.get_platform_catalog()["platforms"]
    }
    active_primary = {
        item.platform for item in get_config_personas()
        if item.enabled and item.platform in {"telegram", "vk", "dzen"}
    }
    results: list[dict[str, Any]] = []
    selected_by_platform: dict[str, list[dict[str, Any]]] = {}

    for platform_id, rule in PLATFORM_RULES.items():
        catalogue_entry = catalog.get(platform_id, {})
        enabled = platforms.is_platform_enabled(platform_id)
        adapter = ADAPTERS.get(platform_id)
        if platform_id in MANUAL_ONLY:
            automation = "manual"
            detail = MANUAL_ONLY[platform_id]
        elif adapter is None:
            automation = "unavailable"
            detail = "В каталоге нет рабочего адаптера публикации."
        else:
            automation = "automatic"
            detail = ""
        configured = bool(adapter and adapter.configured())
        if platform_id == "vk" and "vk" in active_primary:
            # The VK primary channel has its own queue. Do not duplicate the same
            # information there through a second distribution path.
            auto_primary = True
            detail = "ВК уже обслуживается собственной основной лентой; кросспостинг сюда отключён во избежание дублей."
        else:
            auto_primary = False

        pair_logs = [row for row in logs if row.platform == platform_id]
        cap = effective_daily_cap(platform_id, pair_logs)
        count_statuses = {"posted", "publishing", "unknown"}
        daily_sent = sum(
            1 for row in latest.values()
            if row.platform == platform_id
            and row.status in count_statuses
            and (_utc(row.created_at) or cutoff) >= day_cutoff
        )
        remaining = max(0, cap - daily_sent)
        last_activity = max(
            (
                _utc(row.created_at)
                for row in latest.values()
                if row.platform == platform_id and row.status in count_statuses and row.created_at
            ),
            default=None,
        )
        next_allowed = (
            last_activity + timedelta(hours=int(rule["min_gap_hours"]))
            if last_activity is not None else current
        )
        allowed_now = current >= next_allowed
        reason = detail
        if not enabled:
            reason = "Площадка выключена в UI."
        elif automation != "automatic":
            reason = detail
        elif not configured:
            reason = "Аккаунт или API-доступ не настроен."
        elif auto_primary:
            reason = detail
        elif settings.mode == "off":
            reason = "Глобальный режим OFF: никакие публикации не отправляются."
        elif settings.approval_required:
            reason = "Включён режим ручного одобрения; очередь не отправляет публикации автоматически."
        elif not remaining:
            reason = f"Дневной лимит исчерпан ({cap} публикаций за последние 24 часа)."
        elif not allowed_now:
            reason = f"Пауза между публикациями до {next_allowed.isoformat(timespec='minutes')}."
        eligible: list[dict[str, Any]] = []
        if enabled and automation == "automatic" and configured and not auto_primary:
            if settings.mode != "off" and not settings.approval_required and remaining and allowed_now:
                recent_texts = [
                    f"{post.hook}\n{post.body}"
                    for post, _channel in candidates
                    if (post.public_id, platform_id) in latest
                    and latest[(post.public_id, platform_id)].status == "posted"
                    and (_utc(latest[(post.public_id, platform_id)].created_at) or cutoff) >= logs_cutoff
                ]
                for post, channel in candidates:
                    if channel.platform == platform_id:
                        continue
                    published_at = _utc(post.published_at)
                    if published_at is None or published_at > current - timedelta(minutes=min_age_minutes):
                        continue
                    latest_log = latest.get((post.public_id, platform_id))
                    if latest_log is not None:
                        if latest_log.status in {"posted", "unknown", "publishing", "permanent"}:
                            continue
                        if latest_log.status == "failed":
                            retry_at = _utc(latest_log.retry_after_at)
                            if int(latest_log.attempt or 1) >= 5 or (retry_at and retry_at > current):
                                continue
                        if latest_log.status == "skipped" and configured:
                            retry_at = _utc(latest_log.retry_after_at)
                            if retry_at and retry_at > current:
                                continue
                        if latest_log.status == "dry_run":
                            # A dry-run is journalled once, but must become
                            # eligible again when the operator switches to LIVE.
                            if not settings.live:
                                continue
                    # Reddit target communities are selected dynamically by the S9 scout.
                    if rules_requires_video(platform_id) and not settings.video_enabled:
                        continue
                    if platform_id == "youtube" and str(settings.youtube_privacy_status).lower() not in {"private", "unlisted", "public"}:
                        continue
                    media_rows = media_by_post.get(post.id, [])
                    if platform_id == "youtube" and not any(m.kind == "video" for m in media_rows):
                        # S9 can render a short on demand for configured YouTube accounts.
                        pass
                    score = score_post_for_platform(
                        post,
                        platform_id,
                        channel=channel,
                        stat=stats_by_post.get(post.id),
                        now=current,
                        recent_texts=recent_texts,
                    )
                    if not score["eligible"]:
                        continue
                    eligible.append({
                        "post_id": post.id,
                        "post_public_id": post.public_id,
                        "title": str(post.hook or "")[:160],
                        "source_platform": str(channel.platform or "telegram"),
                        "score": score["score"],
                        "topic": score.get("topic", "—"),
                        "reason": score["reason"],
                    })
        eligible.sort(key=lambda item: (-item["score"], item["post_public_id"]))
        limit = min(remaining, max(0, int(max_selected_per_platform)))
        selected = eligible[:limit]
        if selected:
            reason = f"Подобраны {len(selected)} подходящих материала(ов); лимит {cap}/24 ч."
        elif not reason:
            reason = "Пока нет свежих материалов, подходящих аудитории этой площадки."
        selected_by_platform[platform_id] = selected
        results.append({
            "platform": platform_id,
            "name": catalogue_entry.get("name", platform_id),
            "enabled": enabled,
            "configured": configured,
            "automation": automation,
            "daily_cap": cap,
            "daily_sent": daily_sent,
            "remaining": remaining,
            "min_gap_hours": int(rule["min_gap_hours"]),
            "next_allowed_at": next_allowed.isoformat(timespec="minutes") if next_allowed > current else None,
            "selected": selected,
            "eligible_count": len(eligible),
            "reason": reason,
            "detail": detail,
            "topics": list(rule["topics"]),
        })

    primary_platforms = _build_primary_platform_plan(
        current,
        settings,
        platform_catalog,
    )
    return {
        "mode": settings.mode,
        "live": settings.live,
        "approval_required": settings.approval_required,
        "selected_total": sum(len(items) for items in selected_by_platform.values()),
        "primary_platforms": primary_platforms,
        "platforms": results,
    }


def adapt_payload_for_platform(payload: Any, platform_id: str) -> Any:
    """Reshape a factual source post to the native length and tone of a network."""
    from tgfarm.core.text import first_sentence, truncate

    platform_id = str(platform_id or "").lower()
    title = str(getattr(payload, "title", "") or "").strip()
    body = str(getattr(payload, "text", "") or "").strip()
    link = str(getattr(payload, "link", "") or "").strip()

    # Crosspost builder appends a standalone hashtag line. Reddit uses community
    # flairs/tags and does not benefit from a generic hashtag tail.
    lines = body.splitlines()
    if lines and all(re.fullmatch(r"#[0-9a-zа-яё_]+", item.strip(), re.I) for item in lines[-1].split()):
        body = "\n".join(lines[:-1]).strip()

    if platform_id == "x":
        title = truncate(title, 72)
        core = first_sentence(body, 112)
        body = core
        if link and link not in body:
            body = f"{body}\n{link}".strip()
        payload.title = title
        payload.text = truncate(body, 160)
    elif platform_id == "bluesky":
        title = truncate(title, 96)
        core = first_sentence(body, 170)
        if link and link not in core:
            core = f"{core}\n{link}".strip()
        payload.title = title
        payload.text = truncate(core, 195)
    elif platform_id == "threads":
        payload.title = truncate(title, 140)
        payload.text = truncate(body, 330)
    elif platform_id == "mastodon":
        payload.title = truncate(title, 160)
        payload.text = truncate(body, 420)
    elif platform_id == "reddit":
        payload.title = truncate(title, 300)
        payload.text = body
    elif platform_id == "max":
        payload.title = truncate(title, 120)
        payload.text = truncate(body, 3600)
    elif platform_id == "vk":
        payload.title = truncate(title, 128)
        payload.text = truncate(body, 3600)
    return payload


def rules_requires_video(platform_id: str) -> bool:
    return bool(PLATFORM_RULES.get(platform_id, {}).get("requires_video"))


def get_config_personas() -> list[Any]:
    from tgfarm.config import get_config

    return get_config().personas


def set_runtime_mode(mode: str) -> str:
    """Persist the global kill switch / dry-run / live mode in the local .env."""
    value = str(mode or "").strip().lower()
    if value not in {"off", "dry", "live"}:
        raise ValueError("Режим должен быть off, dry или live.")
    env_path = ROOT / ".env"
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    output: list[str] = []
    written = False
    for line in existing:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) == "TGFARM_MODE":
            if not written:
                output.append(f"TGFARM_MODE={value}")
                written = True
        else:
            output.append(line)
    if not written:
        if output and output[-1].strip():
            output.append("")
        output.append(f"TGFARM_MODE={value}")
    fd, temp_name = tempfile.mkstemp(prefix=".tgfarm-env-", dir=str(ROOT))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, env_path)
        os.environ["TGFARM_MODE"] = value
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    from tgfarm.core.settings import reset_settings_cache

    reset_settings_cache()
    return value

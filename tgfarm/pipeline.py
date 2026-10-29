"""Pipeline orchestrator.

One cycle is the whole farm:

    S1 collect → S2 trends → S4 generate (for ready clusters)
              → S5 quality → S6 visual → S7 schedule+publish
              → S9 crosspost → S8 analytics

Each stage is isolated: a failure is recorded and the cycle continues, because a
dead source or a rate-limited LLM must never stop the other channels.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select

from tgfarm.config import get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Cluster, CrosspostLog, PipelineRun, Post, RawItem, Source
from tgfarm.db.session import session_scope

log = get_logger(__name__)

MAX_CLUSTERS_PER_CYCLE = 1000

_cycle_started_at: datetime | None = None
_cycle_finished_at: datetime | None = None
_cycle_state = "idle"
_active_cycle_task: asyncio.Task | None = None
_watchdog_recovery_attempts = 0
_watchdog_last_recovery_at: datetime | None = None


def _watchdog_recovery_delay_min(
    attempts: int,
    base_min: int,
    max_min: int,
) -> int:
    """Exponential retry delay, capped so a persistent failure cannot hot-loop."""
    base = max(1, int(base_min))
    cap = max(base, int(max_min))
    exponent = min(max(0, int(attempts) - 1), 10)
    return min(cap, base * (2**exponent))


def cycle_health(now: datetime | None = None) -> dict:
    """Watchdog view of the last cycle.

    `stale` is true only while a cycle is still marked running past the
    threshold: a slow cycle is worth an alert, a finished one is not.
    """
    current = now or datetime.now(UTC)
    started = _cycle_started_at
    finished = _cycle_finished_at
    age = None
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        age = max(0, int((current - started).total_seconds()))
    settings = get_settings()
    stale = (
        _cycle_state == "running"
        and age is not None
        and age >= max(1, settings.cycle_stale_min) * 60
    )
    recovery_next_at = None
    recovery_delay_min = None
    recovery_wait_s = None
    if _watchdog_last_recovery_at is not None:
        recovery_at = _watchdog_last_recovery_at
        if recovery_at.tzinfo is None:
            recovery_at = recovery_at.replace(tzinfo=UTC)
        else:
            recovery_at = recovery_at.astimezone(UTC)
        recovery_delay_min = _watchdog_recovery_delay_min(
            _watchdog_recovery_attempts,
            settings.watchdog_recovery_base_min,
            settings.watchdog_recovery_max_min,
        )
        next_at = recovery_at + timedelta(minutes=recovery_delay_min)
        recovery_next_at = next_at.isoformat(timespec="seconds")
        recovery_wait_s = max(0, int((next_at - current).total_seconds()))

    return {
        "state": _cycle_state,
        "started_at": started.isoformat(timespec="seconds") if started else None,
        "finished_at": finished.isoformat(timespec="seconds") if finished else None,
        "age_s": age,
        "stale": stale,
        "recovery_attempts": _watchdog_recovery_attempts,
        "recovery_delay_min": recovery_delay_min,
        "recovery_next_at": recovery_next_at,
        "recovery_wait_s": recovery_wait_s,
    }


@dataclass
class CycleResult:
    run_id: str
    stages: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "stages": self.stages,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
        }


def _record(run_id: str, stage: str, status: str, items_in: int = 0, items_out: int = 0,
            duration_ms: int = 0, error: str | None = None, meta: dict | None = None) -> None:
    """Append a stage outcome to the run journal.

    Wrapped in its own try/except on purpose: telemetry must never be able to
    break the pipeline. A failed log row is a missing metric, not a failed post.
    """
    try:
        with session_scope() as session:
            session.add(
                PipelineRun(
                    run_id=run_id,
                    stage=stage,
                    status=status,
                    items_in=items_in,
                    items_out=items_out,
                    duration_ms=duration_ms,
                    error=redact_sensitive_text(error, limit=1000) if error else None,
                    meta=meta or {},
                )
            )
    except Exception:  # noqa: BLE001 - see docstring
        log.error("could not record pipeline run %s/%s", run_id, stage)


async def _stage(result: CycleResult, name: str, coro, *, meta: dict | None = None) -> object:
    """Run one stage, record its outcome, and never let it kill the cycle.

    `coro` must be an async callable taking no arguments.
    """
    timer = StageTimer(name)
    try:
        with timer:
            value = await coro()
        payload = value if isinstance(value, dict) else {"value": value}
        result.stages[name] = {"ok": True, "duration_ms": timer.duration_ms, **payload}
        await to_thread(
            _record, result.run_id, name, "ok",
            timer.items_in, timer.items_out, timer.duration_ms, None, meta,
        )
        return value
    except Exception as exc:  # noqa: BLE001 - stage isolation is the point
        message = redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=600)
        log.error("stage %s failed: %s", name, message)
        result.stages[name] = {"ok": False, "error": message}
        result.errors.append(f"{name}: {message}")
        await to_thread(_record, result.run_id, name, "error", 0, 0, timer.duration_ms, message)
        return None


def _publication_liveness_alert(
    last_published: datetime | None,
    oldest_waiting: datetime | None,
    queued_count: int,
    current: datetime,
    threshold_hours: int,
) -> dict | None:
    """Alert when the queue has been quiet too long, including before first success."""
    if queued_count <= 0:
        return None

    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    baseline = last_published or oldest_waiting
    if baseline is None:
        return None
    baseline = baseline.replace(tzinfo=UTC) if baseline.tzinfo is None else baseline.astimezone(UTC)
    quiet_hours = (current - baseline).total_seconds() / 3600
    if quiet_hours < max(1, int(threshold_hours)):
        return None

    if last_published is None:
        return {
            "level": "error",
            "code": "first_publication_missing",
            "message": (
                f"первая публикация ещё не состоялась {quiet_hours:.1f} ч, "
                f"в очереди {queued_count} материал(ов)"
            ),
        }
    return {
        "level": "error",
        "code": "no_publications",
        "message": (
            f"последняя публикация {quiet_hours:.1f} ч назад, "
            f"в очереди {queued_count} материал(ов)"
        ),
    }


def _backup_freshness_alert(
    last_successful_backup_at: datetime | None,
    current: datetime,
    max_age_hours: int,
) -> dict | None:
    """Warn when no verified backup exists or the last one is too old."""
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    threshold_hours = max(1, int(max_age_hours))
    if last_successful_backup_at is None:
        return {
            "level": "warning",
            "code": "backup_missing",
            "message": (
                "Нет ни одной успешной проверенной резервной копии. "
                "TgFarm создаёт резервную копию автоматически раз в сутки; "
                "проверь доступность папки BACKUP_DIR."
            ),
        }

    stamp = (
        last_successful_backup_at.replace(tzinfo=UTC)
        if last_successful_backup_at.tzinfo is None
        else last_successful_backup_at.astimezone(UTC)
    )
    age_hours = max(0.0, (current - stamp).total_seconds() / 3600)
    if age_hours < threshold_hours:
        return None
    return {
        "level": "error",
        "code": "backup_stale",
        "message": (
            f"Последняя успешная резервная копия была {age_hours:.1f} ч назад "
            f"(порог {threshold_hours} ч). Проверь расписание, диск и BACKUP_DIR."
        ),
    }


def _source_freshness_alert(enabled_source_rows, current: datetime, settings=None) -> dict | None:
    """Warn when enabled sources are not being polled within their adaptive cadence."""
    from tgfarm.s1_sources import effective_interval

    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    overdue = 0
    total = len(enabled_source_rows)
    for row in enabled_source_rows:
        last_fetched = getattr(row, "last_fetched_at", None)
        created_at = getattr(row, "created_at", None)
        stamp = last_fetched or created_at
        if stamp is None:
            overdue += 1
            continue
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        # Honor the collector's adaptive backoff for idle feeds and repeated
        # failures; otherwise a deliberately backed-off source looks falsely stale.
        interval_s = effective_interval(row, settings)
        # Allow three effective intervals, but never flag a source for less
        # than two hours of silence. Long-interval sources retain their cadence.
        threshold_s = max(2 * 60 * 60, interval_s * 3)
        if (current - stamp).total_seconds() >= threshold_s:
            overdue += 1

    if total == 0 or overdue == 0:
        return None
    if overdue == total:
        return {
            "level": "error",
            "code": "source_collection_stale",
            "message": (
                f"Все включённые источники ({total}) давно не проверялись по своему расписанию. "
                "Проверь цикл сбора, scheduler и доступность базы."
            ),
        }
    return {
        "level": "warning",
        "code": "source_collection_degraded",
        "message": (
            f"{overdue} из {total} включённых источников проверялись позже ожидаемого срока. "
            "Часть новостей может поступать с задержкой."
        ),
    }


def _source_health_alert(enabled_source_rows) -> dict | None:
    """Raise a bounded alert when enabled sources have entered repeated-failure cooldown."""
    total = len(enabled_source_rows)
    if total == 0:
        return None

    unhealthy = [
        row
        for row in enabled_source_rows
        if int(getattr(row, "failures", 0) or 0) >= 3
        or str(getattr(row, "last_status", "") or "").lower() == "cooldown"
    ]
    if not unhealthy:
        return None

    count = len(unhealthy)
    if count == total:
        return {
            "level": "error",
            "code": "source_fleet_down",
            "message": (
                f"Все {total} включённых источника находятся в состоянии повторных ошибок "
                "или cooldown. Проверь сеть и доступность источников; коллекция новостей может остановиться."
            ),
        }
    return {
        "level": "warning",
        "code": "source_fleet_degraded",
        "message": (
            f"{count} из {total} включённых источников не прошли несколько попыток и находятся "
            "в cooldown/повторных ошибках. Остальные источники продолжают работать."
        ),
    }


def _platform_auth_monitor_freshness_alert(
    last_check_at: datetime | None,
    current: datetime,
    max_age_hours: int,
    *,
    has_configured_platforms: bool,
) -> dict | None:
    """Report when enabled/configured platform auth monitoring has stopped running."""
    if not has_configured_platforms:
        return None
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    if last_check_at is None:
        return {
            "level": "warning",
            "code": "platform_auth_monitor_missing",
            "message": (
                "Для настроенных площадок ещё нет ни одной записи проверки авторизации. "
                "Проверь запуск scheduler и задачу platform-auth-health."
            ),
        }
    stamp = last_check_at.replace(tzinfo=UTC) if last_check_at.tzinfo is None else last_check_at.astimezone(UTC)
    age_hours = max(0.0, (current - stamp).total_seconds() / 3600)
    threshold = max(1, int(max_age_hours))
    if age_hours < threshold:
        return None
    return {
        "level": "warning",
        "code": "platform_auth_monitor_stale",
        "message": (
            f"Проверка авторизации не запускалась {age_hours:.1f} ч "
            f"(порог {threshold} ч). Проверь scheduler и журнал platform_auth."
        ),
    }


def _platform_auth_alert(platform_rows) -> dict | None:
    """Alert only for explicitly failed auth checks on enabled, configured adapters."""
    failed = [
        row
        for row in platform_rows
        if row.get("supported")
        and row.get("enabled")
        and row.get("configured")
        and (row.get("auth") or {}).get("status") == "failed"
    ]
    if not failed:
        return None

    names = [
        str(row.get("name") or row.get("id") or "неизвестная площадка")
        for row in failed
    ]
    shown = names[:6]
    suffix = f" и ещё {len(names) - len(shown)}" if len(names) > len(shown) else ""
    return {
        "level": "error",
        "code": "platform_auth_failed",
        "message": (
            "Проверка доступа не пройдена для площадок: "
            + ", ".join(shown)
            + suffix
            + ". Переподключи аккаунт или обнови токен; публикация на этих площадках может не работать."
        ),
    }


def collect_alerts(now: datetime | None = None) -> list[dict]:
    """Operator-facing problems: cheap SQL, no network.

    The watchdog logs these every tick and /api/status carries the same list,
    so a farm that quietly stopped shipping is visible without reading logs.
    Deduplication is deliberately absent: a problem that persists across
    5-minute watchdog ticks *should* keep being reported.
    """
    settings = get_settings()
    current = now or datetime.now(UTC)
    alerts: list[dict] = []

    def _aware(value):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    with session_scope() as session:
        last_published = _aware(
            session.execute(
                select(Post.published_at)
                .where(Post.status == "published")
                .order_by(Post.published_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        )
        gated = session.execute(
            select(func.count()).select_from(Post).where(Post.status == "gated")
        ).scalar_one()
        scheduled = session.execute(
            select(func.count()).select_from(Post).where(Post.status == "scheduled")
        ).scalar_one()
        oldest_waiting = _aware(
            session.execute(
                select(func.min(func.coalesce(Post.scheduled_at, Post.created_at))).where(
                    Post.status.in_(("gated", "scheduled"))
                )
            ).scalar_one_or_none()
        )
        stuck = session.execute(
            select(func.count()).select_from(Post).where(
                Post.status == "publishing",
                Post.scheduled_at.isnot(None),
                Post.scheduled_at < current - timedelta(minutes=30),
            )
        ).scalar_one()
        publish_unknown = session.execute(
            select(func.count()).select_from(Post).where(Post.status == "publish_unknown")
        ).scalar_one()
        latest_crosspost_ids = (
            select(func.max(CrosspostLog.id).label("latest_id"))
            .group_by(CrosspostLog.post_public_id, CrosspostLog.platform)
            .subquery()
        )
        crosspost_attention = session.execute(
            select(func.count())
            .select_from(CrosspostLog)
            .join(latest_crosspost_ids, CrosspostLog.id == latest_crosspost_ids.c.latest_id)
            .where(
                or_(
                    CrosspostLog.status == "unknown",
                    and_(
                        CrosspostLog.status == "publishing",
                        CrosspostLog.created_at <= current - timedelta(minutes=30),
                    ),
                )
            )
        ).scalar_one()
        from tgfarm.s9_growth.adapters import MAX_AUTOMATIC_CROSSPOST_ATTEMPTS

        crosspost_failures = session.execute(
            select(func.count())
            .select_from(CrosspostLog)
            .join(latest_crosspost_ids, CrosspostLog.id == latest_crosspost_ids.c.latest_id)
            .where(
                or_(
                    CrosspostLog.status == "permanent",
                    and_(
                        CrosspostLog.status == "failed",
                        CrosspostLog.attempt >= MAX_AUTOMATIC_CROSSPOST_ATTEMPTS,
                    ),
                )
            )
        ).scalar_one()
        last_generate = session.execute(
            select(PipelineRun.status, PipelineRun.error)
            .where(PipelineRun.stage == "s4_generate")
            .order_by(PipelineRun.id.desc())
            .limit(1)
        ).first()
        last_backup = session.execute(
            select(PipelineRun.status, PipelineRun.error, PipelineRun.meta)
            .where(PipelineRun.stage == "backup")
            .order_by(PipelineRun.id.desc())
            .limit(1)
        ).first()
        last_successful_backup_at = session.execute(
            select(PipelineRun.created_at)
            .where(PipelineRun.stage == "backup", PipelineRun.status == "ok")
            .order_by(PipelineRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        enabled_source_health = session.execute(
            select(
                Source.failures,
                Source.last_status,
                Source.last_fetched_at,
                Source.min_interval_s,
                Source.idle_streak,
                Source.created_at,
            ).where(Source.enabled.is_(True))
        ).all()
        last_platform_auth_check_at = session.execute(
            select(PipelineRun.created_at)
            .where(PipelineRun.stage == "platform_auth")
            .order_by(PipelineRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        dzen_auth_fails = session.execute(
            select(func.count()).select_from(Post).where(
                Post.publish_error.isnot(None),
                Post.publish_error.contains("Яндекса"),
                Post.created_at >= current - timedelta(hours=24),
            )
        ).scalar_one()
        failing_posts = session.execute(
            select(Post)
            .where(
                Post.attempts >= 5,
                Post.status.in_(("scheduled", "publishing")),
                Post.publish_error.isnot(None),
            )
            .order_by(Post.attempts.desc())
            .limit(3)
        ).scalars().all()
        dzen_captcha_fails = session.execute(
            select(func.count()).select_from(Post).where(
                Post.publish_error.isnot(None),
                Post.publish_error.contains("капча"),
                Post.created_at >= current - timedelta(hours=24),
            )
        ).scalar_one()

    if settings.live:
        liveness = _publication_liveness_alert(
            last_published,
            oldest_waiting,
            int(gated or 0) + int(scheduled or 0),
            current,
            settings.alert_no_publish_hours,
        )
        if liveness:
            alerts.append(liveness)
    if stuck:
        alerts.append(
            {
                "level": "warning",
                "code": "publishing_stuck",
                "message": f"{stuck} пост(ов) висят в publishing дольше 30 минут",
            }
        )
    if publish_unknown:
        alerts.append(
            {
                "level": "error",
                "code": "publish_outcome_unknown",
                "message": (
                    f"{publish_unknown} пост(ов) требуют сверки на площадке; "
                    "автоматический повтор остановлен во избежание дублей"
                ),
            }
        )
    if crosspost_attention:
        alerts.append(
            {
                "level": "error",
                "code": "crosspost_outcome_unknown",
                "message": (
                    f"{crosspost_attention} кросспост(ов) требуют сверки на целевой площадке; "
                    "автоматический повтор остановлен во избежание дублей"
                ),
            }
        )

    if crosspost_failures:
        alerts.append(
            {
                "level": "warning",
                "code": "crosspost_delivery_failed",
                "message": (
                    f"{crosspost_failures} кросспост(ов) требуют исправления подключения "
                    "или ручного повторного разрешения после проверки целевой площадки"
                ),
            }
        )

    health = cycle_health(current)
    if health.get("stale"):
        alerts.append(
            {
                "level": "error",
                "code": "cycle_stale",
                "message": f"цикл висит уже {health['age_s']} с (state={health['state']})",
            }
        )
    elif health["state"] in ("failed", "timeout"):
        alerts.append(
            {
                "level": "error",
                "code": f"cycle_{health['state']}",
                "message": "последний цикл завершился ошибкой",
            }
        )

    if dzen_auth_fails:
        alerts.append(
            {
                "level": "error",
                "code": "dzen_auth",
                "message": "сессия Дзена слетела — нажми «Дзен: войти» в панели",
            }
        )
    if failing_posts:
        first = failing_posts[0]
        alerts.append(
            {
                "level": "warning",
                "code": "publish_failing",
                "message": (
                    f"{len(failing_posts)}+ пост(ов) не публикуются после "
                    f"{first.attempts} попыток: {(first.publish_error or '')[:120]}"
                ),
            }
        )
    if dzen_captcha_fails:
        # escalation: three captcha stalls in a day means the session is sick,
        # not unlucky — the operator should re-login, not keep clicking
        alerts.append(
            {
                "level": "error" if dzen_captcha_fails >= 3 else "warning",
                "code": "dzen_captcha",
                "message": "Дзен показывал капчу — при следующей публикации нажми "
                "«Я не робот» в окне фермы (или реши через «Дзен: войти»)",
            }
        )

    if last_generate is not None and last_generate.status == "error":
        alerts.append(
            {
                "level": "warning",
                "code": "generation_failing",
                "message": f"генерация падает: {(last_generate.error or '')[:160]}",
            }
        )
    if last_backup is not None and last_backup.status == "error":
        backup_meta = last_backup.meta or {}
        if backup_meta.get("local_verified"):
            message = (
                "Локальная резервная копия создана, но зеркало резервных копий "
                "не прошло проверку. Проверь BACKUP_MIRROR_DIR и доступность диска."
            )
        else:
            message = (
                "Последняя резервная копия не прошла проверку: "
                + redact_sensitive_text(last_backup.error or "причина не записана", limit=180)
            )
        alerts.append(
            {
                "level": "error",
                "code": "backup_failed",
                "message": message,
            }
        )
    backup_freshness = _backup_freshness_alert(
        last_successful_backup_at,
        current,
        getattr(settings, "backup_max_age_hours", 36),
    )
    if backup_freshness:
        alerts.append(backup_freshness)
    if getattr(settings, "online", False):
        source_freshness = _source_freshness_alert(enabled_source_health, current, settings)
        if source_freshness:
            alerts.append(source_freshness)
    source_alert = _source_health_alert(enabled_source_health)
    if source_alert:
        alerts.append(source_alert)

    # Auth state is read from the local platform catalog; this path performs
    # no network requests and never includes token values in the alert.
    try:
        from tgfarm.platforms import get_platform_catalog

        platform_rows = get_platform_catalog().get("platforms", [])
        auth_alert = _platform_auth_alert(platform_rows)
        if auth_alert:
            alerts.append(auth_alert)
        configured_auth_monitoring = any(
            row.get("supported")
            and row.get("enabled")
            and row.get("configured")
            and row.get("id") in {
                "telegram", "vk", "max", "x", "reddit", "bluesky",
                "threads", "linkedin", "mastodon",
            }
            for row in platform_rows
        )
        if settings.online:
            freshness_alert = _platform_auth_monitor_freshness_alert(
                last_platform_auth_check_at,
                current,
                getattr(settings, "platform_auth_max_age_hours", 12),
                has_configured_platforms=configured_auth_monitoring,
            )
            if freshness_alert:
                alerts.append(freshness_alert)
    except Exception as exc:  # noqa: BLE001 - health reporting must not break the dashboard
        log.debug("platform auth alert projection failed (%s)", type(exc).__name__)
    return alerts


_alert_last_sent: dict[str, float] = {}


async def _tg_send_text(text: str) -> bool:
    """One message to the operator chat; False when delivery is unconfigured."""
    import httpx

    settings = get_settings()
    if not (settings.telegram_bot_token and settings.alert_chat_id):
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.alert_chat_id, "text": text[:4000]},
        )
    return resp.status_code == 200


async def backup_job() -> dict:
    """Create, validate and atomically publish a SQLite backup.

    A same-directory backup protects against operator mistakes and bad
    migrations, but is not an off-machine disaster-recovery plan. The file is
    never added to retention until integrity and disposable-restore checks pass.
    """
    import os
    import sqlite3
    import uuid
    from pathlib import Path

    from sqlalchemy.engine import make_url

    settings = get_settings()

    mirror_config = str(getattr(settings, "backup_mirror_dir", "") or "").strip()

    run_id = f"backup-{uuid.uuid4().hex[:8]}"

    def record_report(report: dict) -> dict:
        verified = bool(report.get("verified"))
        local_verified = bool(report.get("local_verified", verified))
        mirror = report.get("mirror") or {}
        reason = None if verified else str(
            report.get("reason") or mirror.get("reason") or "backup verification failed"
        )
        _record(
            run_id,
            "backup",
            "ok" if verified else "error",
            items_out=1 if local_verified else 0,
            error=reason,
            meta={
                "local_verified": local_verified,
                "mirror_configured": bool(mirror_config),
                "mirror_verified": mirror.get("verified"),
                "restore_tested": bool(report.get("restore_tested")),
            },
        )
        return report

    db_url = make_url(str(get_engine_url()))
    if db_url.get_backend_name() != "sqlite":
        return record_report({
            "backup": None,
            "verified": False,
            "reason": "non-sqlite database; configure an external database backup",
        })

    db_path = db_url.database
    if not db_path or db_path == ":memory:":
        return record_report({
            "backup": None,
            "verified": False,
            "reason": "SQLite database has no persistent file to back up",
        })

    source_path = Path(db_path).expanduser()
    if not source_path.is_absolute():
        source_path = source_path.resolve()
    if not source_path.is_file():
        return record_report({
            "backup": None,
            "verified": False,
            "reason": f"SQLite database file not found: {source_path}",
        })

    dest_dir = (
        Path(settings.backup_dir).expanduser()
        if settings.backup_dir
        else Path(__file__).resolve().parent.parent / "backup"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    keep = max(1, int(settings.keep_backups))
    # Microseconds prevent two manual backups in the same minute from
    # silently overwriting one another. A unique temporary file also makes a
    # crashed/failed backup invisible to the retention/pruning logic.
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S-%f")
    dest = dest_dir / f"tgfarm-{stamp}.db"
    temp = dest_dir / f".{dest.name}.{uuid.uuid4().hex}.tmp"

    src = None
    dst = None
    try:
        src = sqlite3.connect(str(source_path), timeout=30)
        dst = sqlite3.connect(str(temp), timeout=30)
        src.backup(dst)
        dst.commit()
        check = dst.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError(f"backup integrity_check failed: {check!r}")
        dst.close()
        dst = None
        src.close()
        src = None

        # Only a verified, fully closed snapshot receives the public .db name.
        os.replace(temp, dest)

        # Integrity alone proves the file is readable, not that the snapshot can
        # be restored with its schema and data intact. Test the disposable restore
        # before pruning any previous known-good backups or copying the mirror.
        from tgfarm.db.recovery import verify_sqlite_backup_restore

        restore_report = verify_sqlite_backup_restore(dest)
        if not restore_report.get("verified"):
            dest.unlink(missing_ok=True)
            reason = redact_sensitive_text(
                restore_report.get("reason") or "restore verification failed",
                limit=200,
            )
            raise sqlite3.DatabaseError(f"backup restore verification failed: {reason}")

        backups = sorted(dest_dir.glob("tgfarm-*.db"), key=lambda path: path.name)
        for old in backups[: max(0, len(backups) - keep)]:
            old.unlink(missing_ok=True)
        kept = len(list(dest_dir.glob("tgfarm-*.db")))
        size_bytes = dest.stat().st_size
        mirror_report = {"configured": bool(mirror_config), "verified": None, "backup": None}
        if mirror_config:
            from tgfarm.db.backup_mirror import mirror_verified_backup

            try:
                mirror_report = mirror_verified_backup(
                    dest,
                    mirror_config,
                    keep=keep,
                    local_backup_dir=dest_dir,
                )
            except Exception as mirror_exc:  # noqa: BLE001 - local snapshot remains valid
                mirror_report = {
                    "configured": True,
                    "verified": False,
                    "backup": None,
                    "reason": redact_sensitive_text(mirror_exc, limit=240),
                }
            if not mirror_report.get("verified"):
                reason = str(mirror_report.get("reason") or "mirror verification failed")
                log.error(
                    "backup mirror failed; local snapshot verified at %s: %s",
                    dest.name,
                    reason,
                )
                return record_report({
                    "backup": str(dest),
                    "verified": False,
                    "local_verified": True,
                    "integrity_check": "ok",
                    "restore_tested": True,
                    "restore_verification": restore_report,
                    "size_bytes": size_bytes,
                    "kept": kept,
                    "mirror": mirror_report,
                    "reason": "local snapshot verified, but configured backup mirror failed",
                })

        log.info("verified backup written: %s (%s kept; mirror=%s)", dest.name, kept, mirror_report.get("verified"))
        return record_report({
            "backup": str(dest),
            "verified": True,
            "local_verified": True,
            "integrity_check": "ok",
            "restore_tested": True,
            "restore_verification": restore_report,
            "size_bytes": size_bytes,
            "kept": kept,
            "mirror": mirror_report,
        })
    except Exception as exc:
        _record(
            run_id,
            "backup",
            "error",
            error=redact_sensitive_text(exc, limit=240),
            meta={"local_verified": False, "mirror_configured": bool(mirror_config)},
        )
        # Close handles before unlinking the temporary file; Windows refuses
        # to remove an open SQLite file.
        if dst is not None:
            dst.close()
            dst = None
        if src is not None:
            src.close()
            src = None
        temp.unlink(missing_ok=True)
        raise
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def get_engine_url() -> str:
    from tgfarm.db.session import get_engine

    return str(get_engine().url)


async def weekly_report() -> dict:
    """Sunday digest: what shipped and what looked best."""
    settings = get_settings()
    if not (settings.telegram_bot_token and settings.alert_chat_id):
        return {"sent": False, "reason": "ALERT_CHAT_ID / bot token not configured"}

    def _summary() -> dict:
        from tgfarm.s8_analytics import fleet_summary

        cutoff = datetime.now(UTC) - timedelta(days=7)
        with session_scope() as session:
            rows = session.execute(
                select(Post.hook)
                .where(Post.status == "published", Post.published_at >= cutoff)
                .order_by(Post.published_at.desc())
                .limit(3)
            ).scalars().all()
        return {"fleet": fleet_summary(7), "recent": list(rows)}

    data = await to_thread(_summary)
    fleet = data.get("fleet") or {}
    lines = [
        "📊 Недельный отчёт фермы",
        f"Постов: {fleet.get('posts', 0)} · просмотров: {fleet.get('views_total', 0)}"
        f" · ER: {fleet.get('avg_er', 0)}",
    ]
    for hook in data.get("recent") or []:
        lines.append(f"• {hook[:90]}")
    ok = await _tg_send_text("\n".join(lines))
    return {"sent": ok}


async def deliver_alerts() -> int:
    """Push fresh alerts to the operator's Telegram chat.

    Requires ALERT_CHAT_ID and the bot token; without them alerts stay in the
    logs and the status API. A per-code cooldown keeps a persistent problem
    from becoming spam: the watchdog re-emits it every tick, the chat sees it
    once per cooldown window.
    """
    import httpx

    settings = get_settings()
    alerts = await to_thread(collect_alerts)
    if not alerts:
        # Heartbeat: daily "everything is fine", so silence stops being
        # ambiguous. Shares the cooldown map with real alerts.
        heartbeat_hours = max(1, settings.alert_heartbeat_hours) * 3600
        if time.monotonic() - _alert_last_sent.get("heartbeat", 0.0) >= heartbeat_hours:
            alerts = [{
                "level": "info",
                "code": "heartbeat",
                "message": f"ферма жива: режим {settings.mode}, проблем не обнаружено",
            }]
            heartbeat = True
        else:
            return 0
    else:
        heartbeat = False
    if not (settings.telegram_bot_token and settings.alert_chat_id):
        log.debug("alerts pending but delivery not configured: %d", len(alerts))
        return 0

    now = time.monotonic()
    cooldown = max(1, settings.alert_cooldown_min) * 60
    sent = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for alert in alerts:
            code = alert["code"]
            last_sent = _alert_last_sent.get(code)
            if last_sent is not None and now - last_sent < cooldown:
                continue
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.alert_chat_id,
                    "text": f"[tgfarm] {code}: {alert['message']}",
                },
            )
            if resp.status_code == 200:
                _alert_last_sent[code] = now
                sent += 1
                if heartbeat:
                    break
            else:
                log.warning(
                    "alert delivery failed for %s: %s %s",
                    code, resp.status_code, resp.text[:120],
                )
                if heartbeat:
                    break
    if heartbeat and sent:
        log.info("heartbeat delivered")
    return sent


async def _maintenance_stage() -> dict:
    from tgfarm.maintenance import run_maintenance

    return await to_thread(run_maintenance)


def _pending_cluster_ids(limit: int) -> list[str]:
    from tgfarm.core.text import detect_lang
    from tgfarm.db.models import Cluster, RawItem

    with session_scope() as session:
        clusters = session.execute(
            select(Cluster)
            .where(Cluster.status == "ready", Cluster.consumed_at.is_(None))
            .order_by(Cluster.heat.desc())
            .limit(max(1, limit) * 10)
        ).scalars().all()
        ids: list[str] = []
        for cluster in clusters:
            items = session.execute(
                select(RawItem).where(RawItem.cluster_id == cluster.id)
            ).scalars().all()
            if not items:
                continue
            distinct_sources = {item.source_id for item in items}
            if len(distinct_sources) < 2:
                continue
            # Only clusters that can actually produce a Russian post offline.
            if not any(
                detect_lang(f"{item.title or ''} {item.text or ''}") == "ru"
                for item in items
            ):
                continue
            ids.append(cluster.public_id)
            if len(ids) >= limit:
                break
        if not ids:
            # Fallback: original ranking if no Russian clusters are available.
            from tgfarm.editorial import rank_cluster_ids

            return rank_cluster_ids(limit)
        return ids


def _passed_post_ids(limit: int = 1000) -> list[str]:
    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id).where(Post.status == "gated").limit(limit)
        ).scalars().all()
        return list(rows)


async def run_cycle(
    *,
    collect: bool = True,
    max_clusters: int = MAX_CLUSTERS_PER_CYCLE,
) -> CycleResult:
    from tgfarm import (
        s1_sources,
        s2_trends,
        s4_generation,
        s5_quality,
        s6_visual,
        s7_publish,
        s8_analytics,
        s9_growth,
        s12_learning,
    )

    global _cycle_started_at, _cycle_finished_at, _cycle_state

    _cycle_started_at = datetime.now(UTC)
    _cycle_finished_at = None
    _cycle_state = "running"

    result = CycleResult(run_id=uuid.uuid4().hex[:10])
    started = time.perf_counter()

    # ---------------------------------------------------------------- S1 + S2
    if collect:
        await _stage(result, "s1_collect", s1_sources.run)

    await _stage(result, "s2_trends", s2_trends.run)

    # ------------------------------------------------------------ S4 generate
    cluster_ids = await to_thread(_pending_cluster_ids, max_clusters)
    if cluster_ids:
        async def _generate() -> dict:
            created: list[str] = []
            for cluster_id in cluster_ids:
                created.extend(await s4_generation.run(cluster_id))
            return {"clusters": len(cluster_ids), "drafts": len(created)}

        await _stage(result, "s4_generate", _generate)

    # ------------------------------------------------------------ S5 quality
    async def _quality() -> dict:
        return await s5_quality.run()

    quality = await _stage(result, "s5_quality", _quality)

    # --------------------------------------------------------------- S6 visual
    passed_ids: list[str] = []
    if isinstance(quality, dict):
        passed_ids = await to_thread(_passed_post_ids)

    if passed_ids:
        async def _visual() -> dict:
            return await s6_visual.run(passed_ids)

        await _stage(result, "s6_visual", _visual)

    # ------------------------------------------------------------- S7 publish
    await _stage(result, "s7_publish", s7_publish.run)

    # ----------------------------------------------------------- S9 crosspost
    await _stage(result, "s9_crosspost", s9_growth.run_crossposts)

    # ---------------------------------------------------------- S8 analytics
    await _stage(result, "s8_analytics", s8_analytics.run)

    # ---------------------------------------------------------- S12 learning
    await _stage(result, "s12_learning", s12_learning.run)

    # ----------------------------------------------------------- maintenance
    await _stage(result, "maintenance", _maintenance_stage)

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    _cycle_finished_at = datetime.now(UTC)
    _cycle_state = "ok" if not result.errors else "partial"
    await to_thread(_record, result.run_id, "cycle", "ok" if not result.errors else "partial",
                    duration_ms=result.duration_ms, meta={"errors": result.errors})
    log.info(
        "cycle %s finished in %dms errors=%d",
        result.run_id, result.duration_ms, len(result.errors),
    )
    return result


async def _guarded_cycle(
    *,
    collect: bool = True,
    max_clusters: int = MAX_CLUSTERS_PER_CYCLE,
) -> CycleResult | None:
    """Run one full cycle, skipping overlap and bounding its runtime."""
    global _cycle_finished_at, _cycle_state, _active_cycle_task

    current_task = asyncio.current_task()
    if current_task is not None:
        active = _active_cycle_task
        if active is not None and active is not current_task and not active.done():
            log.warning("cycle skipped: another full cycle is still active")
            return None
        _active_cycle_task = current_task

    settings = get_settings()
    timeout = max(60, settings.cycle_timeout_min * 60)
    try:
        try:
            return await asyncio.wait_for(
                run_cycle(collect=collect, max_clusters=max_clusters),
                timeout=timeout,
            )
        except TimeoutError:
            _cycle_state = "timeout"
            _cycle_finished_at = datetime.now(UTC)
            log.error("cycle exceeded %ss and was cancelled", timeout)
            await to_thread(
                _record,
                f"guard-{uuid.uuid4().hex[:8]}",
                "cycle",
                "timeout",
                error=f"cycle exceeded {timeout}s",
            )
            return None
        except Exception as exc:  # noqa: BLE001 - scheduler jobs must not die
            _cycle_state = "failed"
            _cycle_finished_at = datetime.now(UTC)
            log.error("cycle crashed: %s", redact_sensitive_text(exc, limit=300))
            await to_thread(
                _record,
                f"guard-{uuid.uuid4().hex[:8]}",
                "cycle",
                "error",
                error=redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=600),
            )
            return None
    finally:
        if current_task is not None and _active_cycle_task is current_task:
            _active_cycle_task = None


async def _publish_tick() -> dict:
    """Drain the publication queue without re-running expensive content stages.

    The full cycle collects trends, generates drafts, performs quality checks,
    and learns from analytics. That work must not run every five minutes just
    because the scheduler wants to deliver already-prepared posts.
    """
    from tgfarm import s7_publish

    run_id = f"pub-{uuid.uuid4().hex[:10]}"
    result = CycleResult(run_id=run_id)
    started = time.perf_counter()

    await _stage(result, "s7_publish", s7_publish.run)

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    status = "ok" if not result.errors else "partial"
    await to_thread(
        _record,
        result.run_id,
        "publish_tick",
        status,
        duration_ms=result.duration_ms,
        error="; ".join(result.errors) if result.errors else None,
        meta={"errors": result.errors},
    )
    log.info(
        "publish tick %s finished in %dms errors=%d",
        result.run_id,
        result.duration_ms,
        len(result.errors),
    )
    return result.as_dict()


async def _guarded_publish_tick() -> dict:
    """Bound a fast publisher tick independently of the long content cycle."""
    settings = get_settings()
    timeout = max(60, settings.dzen_publish_timeout_s + 60)
    run_id = f"pt-{uuid.uuid4().hex[:8]}"
    try:
        return await asyncio.wait_for(_publish_tick(), timeout=timeout)
    except TimeoutError:
        message = f"publish tick exceeded {timeout}s"
        log.error(message)
        await to_thread(_record, run_id, "publish_tick", "timeout", error=message)
        return {"run_id": run_id, "stages": {}, "errors": [message], "duration_ms": timeout * 1000}
    except Exception as exc:  # noqa: BLE001 - scheduler jobs must not die
        message = f"{type(exc).__name__}: {exc}"
        log.error("publish tick crashed: %s", redact_sensitive_text(exc, limit=300))
        await to_thread(_record, run_id, "publish_tick", "error", error=message)
        return {"run_id": run_id, "stages": {}, "errors": [message], "duration_ms": 0}


async def _guarded_crosspost_tick() -> dict:
    """Drain eligible external deliveries independently of content generation."""
    from tgfarm import s9_growth

    settings = get_settings()
    timeout = max(60, min(900, settings.cycle_timeout_min * 60))
    run_id = f"xpost-{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(s9_growth.run_crossposts(limit=10), timeout=timeout)
        if result.get("skipped_concurrent"):
            log.info("crosspost tick skipped because another batch is still active")
        return result
    except TimeoutError:
        message = f"crosspost tick exceeded {timeout}s"
        log.error(message)
        await to_thread(_record, run_id, "crosspost_tick", "timeout", error=message)
        return {"posted": 0, "failed": 0, "unknown": 0, "errors": [message]}
    except Exception as exc:  # noqa: BLE001 - periodic work must not kill the scheduler
        message = redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=300)
        log.error("crosspost tick failed: %s", message)
        await to_thread(_record, run_id, "crosspost_tick", "error", duration_ms=int((time.perf_counter() - started) * 1000), error=message)
        return {"posted": 0, "failed": 0, "unknown": 0, "errors": [message]}


async def _watchdog() -> None:
    """Keep the farm alive: alert on stale cycles and schedule a catch-up run.

    A hung cycle is already bounded by ``cycle_timeout_min``; once it exits (or
    times out) we kick a fresh publish tick so the queue keeps draining instead
    of waiting for the next scheduled slot. Operator alerts are re-emitted on
    every tick on purpose: silence is the failure mode this exists to prevent.
    """
    global _cycle_state, _cycle_finished_at
    global _watchdog_recovery_attempts, _watchdog_last_recovery_at

    alerts = await to_thread(collect_alerts)
    for alert in alerts:
        emit = log.error if alert["level"] == "error" else log.warning
        emit("alert [%s] %s", alert["code"], alert["message"])

    try:
        delivered = await deliver_alerts()
        if delivered:
            log.info("watchdog: delivered %d alert(s) to the operator", delivered)
    except Exception as exc:  # noqa: BLE001 - delivery must not kill the watchdog
        log.debug("alert delivery failed: %s", redact_sensitive_text(exc, limit=180))

    health = cycle_health()
    if health["stale"]:
        log.error(
            "watchdog: cycle has been running for %ss (state=%s); cancelling stale task",
            health["age_s"],
            health["state"],
        )
        active = _active_cycle_task
        if active is not None and active is not asyncio.current_task() and not active.done():
            active.cancel()
        _cycle_state = "failed"
        _cycle_finished_at = datetime.now(UTC)
        # Do not start a replacement in the same tick. The cancelled task must
        # unwind and release its active-task slot first; the next watchdog tick
        # can safely schedule catch-up if no newer cycle has started.
        return
    elif health["state"] in ("failed", "timeout", "partial"):
        log.warning("watchdog: last cycle ended as %s; considering catch-up", health["state"])
    elif health["state"] == "ok":
        if _watchdog_recovery_attempts or _watchdog_last_recovery_at is not None:
            log.info("watchdog: healthy cycle observed; resetting recovery backoff")
        _watchdog_recovery_attempts = 0
        _watchdog_last_recovery_at = None
        return
    else:
        log.debug("watchdog: cycle %s", health)
        return

    settings = get_settings()
    now = datetime.now(UTC)
    if _watchdog_last_recovery_at is not None:
        delay_min = _watchdog_recovery_delay_min(
            _watchdog_recovery_attempts,
            settings.watchdog_recovery_base_min,
            settings.watchdog_recovery_max_min,
        )
        next_attempt_at = _watchdog_last_recovery_at + timedelta(minutes=delay_min)
        if now < next_attempt_at:
            remaining_min = max(1, int((next_attempt_at - now).total_seconds() // 60) + 1)
            log.warning(
                "watchdog: recovery backoff active; retry window in ~%d min "
                "(attempt=%d, delay=%d min)",
                remaining_min,
                _watchdog_recovery_attempts,
                delay_min,
            )
            return

    try:
        asyncio.create_task(_guarded_cycle(collect=False))
        _watchdog_recovery_attempts += 1
        _watchdog_last_recovery_at = now
        log.warning(
            "watchdog: scheduled recovery cycle attempt %d",
            _watchdog_recovery_attempts,
        )
    except RuntimeError:  # no running event loop
        log.debug("watchdog: no event loop for catch-up task")


async def _top_groups_tick() -> dict | None:
    settings = get_settings()
    if not settings.telegram_top_groups_enabled:
        return None
    from tgfarm.s1_sources.telegram_top import collect_top_groups

    try:
        return await collect_top_groups()
    except Exception as exc:  # noqa: BLE001 - optional source must not kill the scheduler
        log.info("top groups tick failed: %s", redact_sensitive_text(exc, limit=180))
        return None


async def _dzen_warm_tick() -> dict | None:
    """Hourly headless visit to the Dzen editor.

    Keeps the Yandex cookies fresh — a session that is exercised hourly almost
    never meets the phone-number form — and detects an expired session minutes
    after it happens instead of mid-publish.
    """
    try:
        from tgfarm.dzen_web import DzenWebPublisher

        publisher = DzenWebPublisher()
        if not publisher.profile_ready():
            return None
        state = await publisher.auth_check(timeout_s=30)
        if state.get("authorized") is False:
            log.warning("dzen warm tick: сессия мертва — нужен «Дзен: войти» в панели")
        return state
    except Exception as exc:  # noqa: BLE001 - warming must not kill the scheduler
        log.debug("dzen warm tick failed: %s", redact_sensitive_text(exc, limit=180))
        return None


async def _branding_tick() -> dict | None:
    try:
        from tgfarm.s9_growth.branding import run_branding_tick

        return await run_branding_tick()
    except Exception as exc:  # noqa: BLE001 - optional branding must not kill scheduler
        log.info("branding tick failed: %s", redact_sensitive_text(exc, limit=180))
        return None


async def _platform_auth_tick() -> dict:
    """Periodically verify configured credentials with read-only provider requests."""
    settings = get_settings()
    run_id = f"auth-{uuid.uuid4().hex[:8]}"
    if not settings.online:
        return {"checked": 0, "authorized": 0, "failed": 0, "indeterminate": 0, "skipped": "mode=off"}

    try:
        from tgfarm.platform_auth import check_platform_auth
        from tgfarm.platforms import get_platform_catalog

        catalog = await to_thread(get_platform_catalog)
    except Exception as exc:  # noqa: BLE001 - auth monitoring must not kill the scheduler
        error_type = type(exc).__name__
        log.error("platform auth catalog unavailable (%s)", error_type)
        await to_thread(
            _record,
            run_id,
            "platform_auth",
            "error",
            error=f"platform catalog unavailable ({error_type})",
        )
        return {
            "checked": 0,
            "authorized": 0,
            "failed": 0,
            "indeterminate": 0,
            "error": "platform_catalog_unavailable",
        }

    # These adapters have a read-only auth probe. Dzen has its own browser
    # warm-up, while non-integrated catalog entries are intentionally ignored.
    checkable = {
        "telegram", "vk", "max", "x", "reddit", "bluesky",
        "threads", "linkedin", "mastodon",
    }
    candidates = [
        row
        for row in catalog.get("platforms", [])
        if row.get("supported")
        and row.get("enabled")
        and row.get("configured")
        and row.get("id") in checkable
    ]

    authorized_ids: list[str] = []
    failed_ids: list[str] = []
    indeterminate_ids: list[str] = []
    for item in candidates:
        platform_id = str(item["id"])
        try:
            result = await check_platform_auth(platform_id)
            authorized = result.get("authorized")
            if authorized is True:
                authorized_ids.append(platform_id)
            elif authorized is False:
                failed_ids.append(platform_id)
                log.warning("platform auth check failed for %s", platform_id)
            else:
                indeterminate_ids.append(platform_id)
                log.info("platform auth check was inconclusive for %s", platform_id)
        except Exception as exc:  # noqa: BLE001 - one adapter must not block the others
            indeterminate_ids.append(platform_id)
            log.warning(
                "platform auth check raised for %s (%s)",
                platform_id,
                type(exc).__name__,
            )

    state = "error" if failed_ids else "partial" if indeterminate_ids else "ok"
    report = {
        "checked": len(candidates),
        "authorized": len(authorized_ids),
        "failed": len(failed_ids),
        "indeterminate": len(indeterminate_ids),
        "failed_platforms": failed_ids,
        "indeterminate_platforms": indeterminate_ids,
    }
    await to_thread(
        _record,
        run_id,
        "platform_auth",
        state,
        items_in=len(candidates),
        items_out=len(authorized_ids),
        error=("authorization check failed for: " + ", ".join(failed_ids)) if failed_ids else None,
        meta={
            "checked": len(candidates),
            "failed_platforms": failed_ids,
            "indeterminate_platforms": indeterminate_ids,
        },
    )
    return report


async def _startup_backup_check() -> dict | None:
    """Create a verified snapshot on startup when none is fresh or the last attempt failed."""
    settings = get_settings()
    try:
        with session_scope() as session:
            last_successful = session.execute(
                select(PipelineRun.created_at)
                .where(PipelineRun.stage == "backup", PipelineRun.status == "ok")
                .order_by(PipelineRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            last_attempt = session.execute(
                select(PipelineRun.status)
                .where(PipelineRun.stage == "backup")
                .order_by(PipelineRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()

        stale = _backup_freshness_alert(
            last_successful,
            datetime.now(UTC),
            getattr(settings, "backup_max_age_hours", 36),
        )
        if stale is None and last_attempt != "error":
            log.info("startup backup check: a recent verified snapshot exists")
            return None

        reason = stale["code"] if stale else "the last backup attempt failed"
        log.warning("startup backup check: creating a snapshot (%s)", reason)
        report = await backup_job()
        if report.get("verified"):
            log.info("startup backup check: verified snapshot created")
        else:
            log.error(
                "startup backup check: snapshot still needs attention (%s)",
                redact_sensitive_text(report.get("reason") or "verification failed", limit=180),
            )
        return report
    except Exception as exc:  # noqa: BLE001 - recovery must not prevent the scheduler from starting
        log.error(
            "startup backup check failed: %s",
            redact_sensitive_text(f"{type(exc).__name__}: {exc}", limit=200),
        )
        return None


# ------------------------------------------------------------------- scheduler
def _build_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    scheduler.add_job(
        _guarded_cycle,
        IntervalTrigger(minutes=settings.collect_interval_min),
        id="cycle",
        name="farm cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        kwargs={"collect": True},
    )

    scheduler.add_job(
        _guarded_publish_tick,
        IntervalTrigger(minutes=5),
        id="publish-tick",
        name="publish tick",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        _guarded_crosspost_tick,
        IntervalTrigger(minutes=10),
        id="crosspost-tick",
        name="crosspost retry and delivery",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        _watchdog,
        IntervalTrigger(minutes=max(1, settings.watchdog_interval_min)),
        id="watchdog",
        name="cycle watchdog",
        max_instances=1,
        coalesce=True,
    )

    if settings.telegram_top_groups_enabled:
        scheduler.add_job(
            _top_groups_tick,
            IntervalTrigger(minutes=max(30, settings.telegram_top_groups_interval_min)),
            id="telegram-top-groups",
            name="top public groups",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )

    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        backup_job,
        CronTrigger(hour=4, minute=0, timezone=settings.tz),
        id="daily-backup",
        name="daily verified sqlite backup",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        weekly_report,
        CronTrigger(day_of_week="sun", hour=20, minute=30, timezone=settings.tz),
        id="weekly-report",
        name="weekly digest",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _dzen_warm_tick,
        IntervalTrigger(hours=1),
        id="dzen-warm",
        name="dzen session warm-up",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        _branding_tick,
        IntervalTrigger(hours=24),
        id="branding-rotation",
        name="brand cover rotation",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # The weekly/daily cron is not enough for a new install or a machine that
    # was powered off during the scheduled time. Check freshness once at boot.
    scheduler.add_job(
        _startup_backup_check,
        "date",
        run_date=datetime.now(UTC),
        id="startup-backup-check",
        name="ensure a recent backup on startup",
        max_instances=1,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _platform_auth_tick,
        IntervalTrigger(hours=6),
        id="platform-auth-health",
        name="read-only platform auth health",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _platform_auth_tick,
        "date",
        run_date=datetime.now(UTC),
        id="startup-platform-auth-check",
        name="initial read-only platform auth check",
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


_scheduler = None


@asynccontextmanager
async def running():
    """Async context manager for `tgfarm serve` — owns the scheduler lifecycle."""
    global _scheduler
    await to_thread(_ensure_bootstrapped)
    _scheduler = _build_scheduler()
    _scheduler.start()
    log.info(
        "scheduler started: cycle every %d min, publish tick every 5 min",
        get_settings().collect_interval_min,
    )
    try:
        yield _scheduler
    finally:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")


def _ensure_bootstrapped() -> None:
    from tgfarm.db.bootstrap import ensure_bootstrapped

    ensure_bootstrapped()


async def bootstrap() -> None:
    await to_thread(_ensure_bootstrapped)


def status() -> dict:
    settings = get_settings()
    config = get_config()
    with session_scope() as session:
        counts = {
            "raw_items": session.execute(select(func.count()).select_from(RawItem)).scalar_one(),
            "clusters": session.execute(select(func.count()).select_from(Cluster)).scalar_one(),
            "posts": session.execute(select(func.count()).select_from(Post)).scalar_one(),
            "published": session.execute(
                select(func.count()).select_from(Post).where(Post.status == "published")
            ).scalar_one(),
        }

    return {
        "mode": settings.mode,
        "tz": settings.tz,
        "channels": len(config.enabled_personas),
        "configured_channels": len(config.personas),
        "sources": len([s for s in config.sources if s.enabled]),
        "llm_providers": settings.provider_chain,
        "embedder": settings.embedding_provider,
        "can_publish": settings.can_publish(),
        "scheduler_running": _scheduler is not None and _scheduler.running,
        "cycle_health": cycle_health(),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **{k: int(v) for k, v in counts.items()},
    }
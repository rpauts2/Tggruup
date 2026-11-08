"""AI spend policy: reserve before the call, release on offline fallback.

The farm must stay useful without a paid key, and an always-on loop must not
quietly spend a daily budget on posts the deterministic composer already writes
well. Every decision is persisted, including refusals, so `tgfarm audit` can
show what was skipped and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from tgfarm.core.logging import get_logger
from tgfarm.core import settings as core_settings
from tgfarm.db.models import AIUsage
from tgfarm.db.session import session_scope

log = get_logger(__name__)

def get_settings():
    return core_settings.get_settings()

AI_ANGLES = {"analysis", "why_it_matters", "comparison", "history"}
PROVENANCE_STAGES = {"s4_verify", "s5_tone", "s5_relevance", "s5_quality"}


@dataclass(frozen=True)
class AIDecision:
    usage_id: int | None
    allowed: bool
    reason: str


def _start_of_day(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def has_online_provider() -> bool:
    settings = get_settings()
    if not settings.online or settings.ai_policy == "off":
        return False
    return any(name != "offline" for name in settings.provider_chain)


def charged_today(now: datetime | None = None) -> int:
    with session_scope() as session:
        value = session.execute(
            select(func.count())
            .select_from(AIUsage)
            .where(AIUsage.charged.is_(True), AIUsage.created_at >= _start_of_day(now))
        ).scalar_one()
    return int(value or 0)


def should_use_ai(
    stage: str,
    *,
    generation_provider: str = "offline",
    angle: str = "news",
    source_count: int = 1,
    offline_chars: int = 0,
    persona=None,
) -> tuple[bool, str]:
    """Decide whether a stage is worth an online call."""
    settings = get_settings()
    if settings.ai_policy == "off":
        return False, "policy off"
    if not has_online_provider():
        return False, "no online provider"
    if stage in PROVENANCE_STAGES and generation_provider == "offline":
        return False, "offline draft"
    if settings.ai_policy == "always":
        return True, "policy always"
    # ai_policy == "selective" (default)
    if stage == "s4_draft":
        if angle in AI_ANGLES:
            return True, f"angle:{angle}"
        if source_count >= 2:
            return True, "multi-source"
        threshold = max(300, int(getattr(persona, "min_chars", 0) or 0))
        if offline_chars < threshold:
            return True, "offline draft too short"
        return False, "offline draft sufficient"
    # s5_rewrite, s5_relevance, s5_quality
    return True, "stage requires ai"


def reserve(
    stage: str,
    *,
    purpose: str = "",
    provider: str = "",
    model: str = "",
    post_public_id: str = "",
    cluster_public_id: str = "",
) -> AIDecision:
    """Reserve one online call or record why it was refused."""
    if not has_online_provider():
        reason = "policy off" if get_settings().ai_policy == "off" else "no online provider"
        log.warning("AI: not reserving for %s, no online provider", stage)
        return AIDecision(None, False, reason)

    budget = max(0, int(get_settings().ai_daily_budget))
    # The count and insert must be one serialized transaction. A read-then-write
    # in separate transactions lets parallel workers all observe the same free
    # slot and exceed the daily cap.
    with session_scope() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            # SQLite's deferred transactions do not lock out another reader
            # before the count. BEGIN IMMEDIATE obtains the write reservation
            # before we inspect today's usage.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        elif dialect == "postgresql":
            # A transaction-scoped advisory lock serializes reservations across
            # every process/worker without introducing a singleton lock table.
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 731904217})
        else:
            raise RuntimeError(
                f"Atomic AI budget reservations are unsupported for database dialect: {dialect}"
            )

        current_charged = int(
            session.execute(
                select(func.count())
                .select_from(AIUsage)
                .where(
                    AIUsage.charged.is_(True),
                    AIUsage.created_at >= _start_of_day(),
                )
            ).scalar_one()
            or 0
        )

        if current_charged >= budget:
            log.warning("AI: budget exhausted (%d/%d) for stage %s", current_charged, budget, stage)
            row = AIUsage(
                stage=stage,
                purpose=purpose,
                provider=provider,
                model=model,
                charged=False,
                used=False,
                reason=f"budget exhausted ({budget}/day)",
                post_public_id=post_public_id,
                cluster_public_id=cluster_public_id,
            )
            session.add(row)
            session.flush()
            usage_id = row.id
            allowed = False
        else:
            if current_charged >= budget * 0.8:
                log.warning(
                    "AI: approaching budget limit (%d/%d) for stage %s",
                    current_charged,
                    budget,
                    stage,
                )
            row = AIUsage(
                stage=stage,
                purpose=purpose,
                provider=provider,
                model=model,
                charged=True,
                used=False,
                reason="reserved",
                post_public_id=post_public_id,
                cluster_public_id=cluster_public_id,
            )
            session.add(row)
            session.flush()
            usage_id = row.id
            allowed = True

    return AIDecision(
        usage_id,
        allowed,
        "reserved" if allowed else "budget exhausted",
    )


def finalize(
    usage_id: int | None,
    *,
    provider: str,
    model: str = "",
    latency_ms: int = 0,
    used: bool = False,
    billed: bool = False,
) -> None:
    """Settle a reservation. Offline fallbacks release their budget.

    `billed` is the case `used` cannot express: a provider returned a body (so
    the owner paid for the tokens) but the answer was unusable, e.g. prose where
    JSON was required. The reservation stays charged — releasing it lets the
    farm spend past `ai_daily_budget` — while the row is not counted as a used
    draft. A provider that never answered is still fully released.
    """
    if usage_id is None:
        return
    with session_scope() as session:
        row = session.get(AIUsage, usage_id)
        if row is None:
            return
        row.provider = provider or ""
        row.model = model or ""
        row.latency_ms = max(0, int(latency_ms))
        row.used = bool(used and provider != "offline")
        row.charged = row.used or bool(billed)
        if row.used:
            row.reason = "used"
        elif billed:
            row.reason = BILLED_UNUSABLE_REASON
        elif provider == "offline":
            row.reason = "fallback:offline"
        else:
            row.reason = "not_used"


def record_skip(
    stage: str,
    *,
    purpose: str = "",
    reason: str = "",
    provider: str = "",
    model: str = "",
    post_public_id: str = "",
    cluster_public_id: str = "",
) -> None:
    with session_scope() as session:
        session.add(
            AIUsage(
                stage=stage,
                purpose=purpose,
                provider=provider,
                model=model,
                charged=False,
                used=False,
                reason=reason[:500],
                post_public_id=post_public_id,
                cluster_public_id=cluster_public_id,
            )
        )


#: A provider answered and was paid, but the answer could not be used.
BILLED_UNUSABLE_REASON = "billed:unusable"

#: A reservation whose process died is closed, not refunded: the provider may
#: have been called, so the charge stays and only the alarm stops repeating.
STALE_CLOSURE_REASON = "stale reservation closed (unused)"


def close_stale_reservations(minutes: int = 30, now: datetime | None = None) -> int:
    """Close reservations left open by a crashed process; never refund them."""
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    cutoff = current - timedelta(minutes=max(1, int(minutes)))
    with session_scope() as session:
        rows = session.execute(
            select(AIUsage).where(
                AIUsage.charged.is_(True),
                AIUsage.used.is_(False),
                AIUsage.reason == "reserved",
                AIUsage.created_at <= cutoff,
            )
        ).scalars().all()
        for row in rows:
            row.reason = STALE_CLOSURE_REASON
        return len(rows)


def usage_report(days: int = 7, now: datetime | None = None) -> dict:
    """Safe dashboard report of call-budget use, fallbacks, and persistent reservations."""
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    days = min(30, max(1, int(days)))
    start = _start_of_day(current) - timedelta(days=days - 1)
    today_start = _start_of_day(current)
    with session_scope() as session:
        rows = session.execute(
            select(AIUsage).where(AIUsage.created_at >= start)
        ).scalars().all()

    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    rows_today = [row for row in rows if _aware(row.created_at) >= today_start]
    used_rows = [row for row in rows if row.used]
    fallback_rows = [row for row in rows if row.reason == "fallback:offline"]
    closed_reasons = {
        "",
        "fallback:offline",
        "reserved",
        STALE_CLOSURE_REASON,
        BILLED_UNUSABLE_REASON,
    }
    blocked_rows = [
        row for row in rows
        if not row.used and row.reason not in closed_reasons
    ]
    pending_rows = [
        row for row in rows
        if row.charged and not row.used and row.reason == "reserved"
    ]
    stale_pending = [
        row for row in pending_rows
        if (current - _aware(row.created_at)).total_seconds() >= 15 * 60
    ]

    def _group_by(field: str) -> list[dict]:
        groups: dict[str, dict] = {}
        for row in rows:
            key = str(getattr(row, field, "") or "unknown")
            item = groups.setdefault(key, {"name": key, "calls": 0, "charged": 0, "used": 0, "fallbacks": 0})
            item["calls"] += 1
            item["charged"] += int(bool(row.charged))
            item["used"] += int(bool(row.used))
            item["fallbacks"] += int(row.reason == "fallback:offline")
        return sorted(groups.values(), key=lambda item: (-item["charged"], -item["calls"], item["name"]))

    daily: dict[str, dict] = {}
    for row in rows:
        day = _aware(row.created_at).date().isoformat()
        item = daily.setdefault(day, {"date": day, "calls": 0, "charged": 0, "used": 0, "fallbacks": 0, "skipped": 0})
        item["calls"] += 1
        item["charged"] += int(bool(row.charged))
        item["used"] += int(bool(row.used))
        item["fallbacks"] += int(row.reason == "fallback:offline")
        item["skipped"] += int(not row.used and row.reason not in ("", "fallback:offline", "reserved"))

    budget = max(0, int(get_settings().ai_daily_budget))
    charged_today = sum(1 for row in rows_today if row.charged)
    return {
        "period_days": days,
        "since": start.isoformat(timespec="seconds"),
        "budget_unit": "online_call_reservations",
        "daily_budget": budget,
        "today": {
            "calls": len(rows_today),
            "charged": charged_today,
            "used": sum(1 for row in rows_today if row.used),
            "fallbacks": sum(1 for row in rows_today if row.reason == "fallback:offline"),
            "skipped": sum(1 for row in rows_today if not row.used and row.reason not in closed_reasons),
            "pending_reservations": sum(1 for row in rows_today if row.charged and not row.used and row.reason == "reserved"),
            "stale_reservations": sum(
                1 for row in rows_today
                if row.charged and not row.used and row.reason == "reserved"
                and (current - _aware(row.created_at)).total_seconds() >= 15 * 60
            ),
            "remaining": max(0, budget - charged_today),
        },
        "totals": {
            "calls": len(rows),
            "charged": sum(1 for row in rows if row.charged),
            "used": len(used_rows),
            "fallbacks": len(fallback_rows),
            "skipped": len(blocked_rows),
            "pending_reservations": len(pending_rows),
            "stale_reservations": len(stale_pending),
            "average_latency_ms": round(sum(row.latency_ms for row in used_rows) / len(used_rows), 1) if used_rows else 0,
        },
        "by_provider": _group_by("provider"),
        "by_stage": _group_by("stage"),
        "daily": sorted(daily.values(), key=lambda item: item["date"]),
    }


def usage_summary(days: int = 1, now: datetime | None = None) -> dict[str, int | str]:
    current = now or datetime.now(UTC)
    since = _start_of_day(current) - timedelta(days=max(0, days - 1))
    with session_scope() as session:
        rows = session.execute(
            select(AIUsage).where(AIUsage.created_at >= since)
        ).scalars().all()
    used = [r for r in rows if r.used]
    fallbacks = [r for r in rows if r.reason == "fallback:offline"]
    blocked = [
        r for r in rows
        if not r.used and r.reason and r.reason not in ("fallback:offline", BILLED_UNUSABLE_REASON)
    ]
    return {
        "calls": len(rows),
        "charged": sum(1 for r in rows if r.charged),
        "used": len(used),
        "fallbacks": len(fallbacks),
        "skipped": len(blocked),
        "average_latency_ms": round(
            sum(r.latency_ms for r in used) / len(used), 1
        ) if used else 0,
    }

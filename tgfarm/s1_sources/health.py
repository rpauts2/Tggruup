"""Read-only health picture of the S1 source fleet.

The live status line only says *how many* sources are parked or failing. It does
not say which ones, why, or when they come back, so the operator has to open the
database to answer a question the panel already asked. This module turns the
``sources`` table into that answer.

It never fetches, never schedules and never changes collection logic. The only
write it performs is the operator-log line inside :func:`reset_source_cooldown`.

What the schema actually stores (verified against ``tgfarm/db/models.py``)
--------------------------------------------------------------------------
``Source`` has exactly one timestamp column: ``last_fetched_at``. Despite the
name, ``tgfarm/s1_sources._mark_source()`` writes it on *every* attempt, success
or failure, so it means "last attempt". This module therefore reports:

* ``last_attempt_at`` — exact, from ``last_fetched_at``;
* ``last_success_at`` — only when ``last_status == "ok"``, the one case where the
  attempt and the success are the same event. After a failure the previous
  success time has been overwritten and is not stored anywhere, so the field is
  ``None`` with ``last_success_known = False``. It is never estimated.

Other limits, stated so nobody reads more into the numbers than they hold:

* ``failures`` is a *consecutive* counter: ``_mark_source()`` resets it to 0 on
  every success. There is no lifetime error count, no per-error history, and
  ``last_error`` keeps only the most recent message.
* ``enabled`` is a single flag. ``seed_sources()`` folds config-side disable into
  it, so "off in YAML" and "disabled by the registry prune" are indistinguishable
  here.
* Only rows of the ``sources`` table are reported. Every entry point calls
  ``ensure_bootstrapped()`` -> ``seed_sources()``, which keeps that table in sync
  with ``config/sources.yaml``, so the table is the fleet.
* ``due`` is a forecast, not a promise: ``due_sources()`` additionally requires
  the source to still be present and enabled in the YAML config.
* No source-staleness setting exists in ``Settings`` today; the stale horizon
  comes from the ``stale_hours`` argument, else a reserved
  ``source_stale_after_hours`` attribute if one is ever added, else 24 hours.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.db.models import OperatorLog
from tgfarm.db.models import Source as SourceRow
from tgfarm.db.session import session_scope

log = get_logger(__name__)

#: Canonical set of states reported per source.
STATE_OK = "ok"
STATE_DUE = "due"
STATE_COOLDOWN = "cooldown"
STATE_FAILING = "failing"
STATE_DISABLED = "disabled"

STATES: tuple[str, ...] = (STATE_OK, STATE_DUE, STATE_COOLDOWN, STATE_FAILING, STATE_DISABLED)

#: Display order: what needs a human first, what works fine later.
_STATE_RANK = {
    STATE_COOLDOWN: 0,
    STATE_FAILING: 1,
    STATE_DUE: 2,
    STATE_OK: 3,
    STATE_DISABLED: 4,
}

#: Operator-log action written by :func:`reset_source_cooldown`.
RESET_ACTION = "source-cooldown-reset"

#: Fallback when ``settings.source_auto_cooldown_after_failures`` is unreadable.
DEFAULT_FAILURE_THRESHOLD = 5

#: Fallback horizon for "not checked for too long", in hours.
DEFAULT_STALE_AFTER_HOURS = 24.0

_MAX_ERROR_CHARS = 200
_MAX_DETAIL_CHARS = 500


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every stored stamp is UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _hours_between(later: datetime, earlier: datetime | None) -> float | None:
    if earlier is None:
        return None
    return round((later - earlier).total_seconds() / 3600.0, 3)


def _resolve_failure_threshold(value: int | None) -> int:
    """Threshold that marks failures as repeated, mirroring the auto-cooldown limit."""
    if value is not None:
        return max(1, int(value))
    try:
        return max(1, int(get_settings().source_auto_cooldown_after_failures))
    except Exception:  # noqa: BLE001 - a health read must not die on a bad settings file
        return DEFAULT_FAILURE_THRESHOLD


def _resolve_stale_hours(value: float | None) -> float:
    if value is not None:
        return max(0.0, float(value))
    try:
        configured = getattr(get_settings(), "source_stale_after_hours", None)
    except Exception:  # noqa: BLE001 - fall through to the documented default
        configured = None
    if configured is not None:
        try:
            return max(0.0, float(configured))
        except (TypeError, ValueError):
            pass
    return DEFAULT_STALE_AFTER_HOURS


def _interval(row: SourceRow) -> int:
    """Adaptive cadence, imported lazily to keep the package import graph acyclic."""
    from tgfarm.s1_sources import effective_interval

    return int(effective_interval(row))


def _describe_error(row: SourceRow) -> str:
    error = (row.last_error or "").strip()
    return f"; последняя ошибка: {error[:_MAX_ERROR_CHARS]}" if error else ""


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def _classify(
    row: SourceRow,
    now: datetime,
    *,
    failure_threshold: int,
    attempt: datetime | None,
    cooldown: datetime | None,
) -> tuple[str, str]:
    """Return ``(state, reason)`` for one row.

    Precedence is deliberate: an explicit disable answers "why nothing arrives"
    more directly than a cooldown, and an active cooldown answers it more
    directly than the failures that caused it.
    """
    failures = int(row.failures or 0)
    status = (row.last_status or "").strip().lower()
    attempt_age = _hours_between(now, attempt)
    attempt_text = (
        f"последняя попытка {attempt_age:.1f} ч назад"
        if attempt_age is not None
        else "попыток ещё не было"
    )

    if not row.enabled:
        return STATE_DISABLED, (
            "источник отключён в реестре — сборщик его не опрашивает, "
            "пока его не включат снова"
        )

    if cooldown is not None and cooldown > now:
        remaining = _hours_between(cooldown, now) or 0.0
        return STATE_COOLDOWN, (
            f"на паузе ещё {remaining:.1f} ч (до {cooldown.isoformat(timespec='seconds')}); "
            f"провалов подряд: {failures}; {attempt_text}"
            f"{_describe_error(row)}"
        )

    if failures > 0 or status in {"error", "cooldown"}:
        if failures >= failure_threshold:
            streak = f"провалов подряд {failures} — достигнут порог автопаузы {failure_threshold}"
        else:
            streak = f"провалов подряд {failures}, до автопаузы {failure_threshold}"
        if status == "ok" and failures > 0:
            # Hand-edited or half-migrated row: the counter disagrees with the
            # last outcome. Report the contradiction instead of smoothing it.
            streak += " (счётчик не сброшен, хотя последняя проверка успешна)"
        if cooldown is not None:
            streak += f"; пауза истекла {cooldown.isoformat(timespec='seconds')}"
        head = "последняя проверка не удалась" if status != "ok" else "источник числится проблемным"
        return (
            STATE_FAILING,
            f"{head}; {streak}; {attempt_text}{_describe_error(row)}",
        )

    interval = _interval(row)
    if attempt is None:
        return STATE_DUE, (
            "источник ещё ни разу не проверялся — попадёт в ближайший проход S1"
        )
    if (now - attempt).total_seconds() >= interval:
        return STATE_DUE, (
            f"пора проверять: {attempt_text}, эффективный интервал {interval} с"
        )

    return STATE_OK, f"{attempt_text}, эффективный интервал {interval} с, сбоев нет"


def _entry(row: SourceRow, now: datetime, *, failure_threshold: int) -> dict[str, Any]:
    attempt = _as_utc(row.last_fetched_at)
    cooldown = _as_utc(row.cooldown_until)
    created = _as_utc(row.created_at)

    state, reason = _classify(
        row,
        now,
        failure_threshold=failure_threshold,
        attempt=attempt,
        cooldown=cooldown,
    )

    success_known = (row.last_status or "").strip().lower() == "ok"
    remaining = _hours_between(cooldown, now) if cooldown is not None and cooldown > now else None
    # Silence is measured from the last attempt, or from the row's creation when
    # no attempt was ever recorded, so a freshly added source is not called stale.
    silence = _hours_between(now, attempt if attempt is not None else created)

    return {
        "id": row.id,
        "title": (row.name or row.id),
        "kind": row.kind,
        "enabled": bool(row.enabled),
        "state": state,
        "reason": reason,
        "last_attempt_at": _iso(attempt),
        "last_attempt_age_h": _hours_between(now, attempt),
        "last_success_at": _iso(attempt) if success_known else None,
        "last_success_known": success_known,
        "failures": int(row.failures or 0),
        "failure_threshold": failure_threshold,
        "idle_streak": int(row.idle_streak or 0),
        "last_yield": int(row.last_yield or 0),
        "cooldown_until": _iso(cooldown),
        "cooldown_active": remaining is not None,
        "cooldown_remaining_h": remaining,
        "last_error": (row.last_error or "")[:_MAX_ERROR_CHARS] or None,
        "effective_interval_s": _interval(row),
        "silence_hours": silence,
    }


# --------------------------------------------------------------------------- #
# public read API
# --------------------------------------------------------------------------- #
def source_health(
    now: datetime | None = None,
    *,
    failure_threshold: int | None = None,
) -> dict[str, Any]:
    """Per-source state of the whole fleet, most attention-worthy first.

    Shape::

        {"generated_at": iso, "total": int,
         "sources": [{id, title, kind, enabled, state, reason,
                       last_attempt_at, last_attempt_age_h,
                       last_success_at, last_success_known,
                       failures, failure_threshold, idle_streak, last_yield,
                       cooldown_until, cooldown_active, cooldown_remaining_h,
                       last_error, effective_interval_s, silence_hours}, ...]}

    ``state`` is one of ``ok``, ``due``, ``cooldown``, ``failing``, ``disabled``
    (see the module docstring for what each one can and cannot mean). ``reason``
    is operator-facing Russian text naming the cause and the next relevant time.

    Empty fleet (no rows at all) returns ``total=0`` and ``sources=[]`` — the
    zero case is normal for a fresh database, not an error.
    """
    current = _as_utc(now) or datetime.now(UTC)
    threshold = _resolve_failure_threshold(failure_threshold)

    with session_scope() as session:
        rows = session.execute(select(SourceRow)).scalars().all()
        entries = [_entry(row, current, failure_threshold=threshold) for row in rows]

    entries.sort(key=lambda item: (_STATE_RANK.get(item["state"], 99), -item["failures"], item["id"]))
    return {
        "generated_at": _iso(current),
        "total": len(entries),
        "sources": entries,
    }


def source_health_summary(
    now: datetime | None = None,
    *,
    stale_hours: float | None = None,
    failure_threshold: int | None = None,
    top: int = 5,
) -> dict[str, Any]:
    """Counters, staleness and the worst offenders, for the status line.

    ``stale`` counts enabled sources whose last attempt — or, if none was ever
    recorded, whose creation — is at least ``stale_hours`` old. "Never checked"
    is counted separately and also contributes to ``stale``; when both stamps are
    missing the age is unknown and the source is treated as stale, because the
    panel cannot prove it is fresh.

    ``top_problems`` holds at most ``top`` sources in ``cooldown`` or ``failing``
    state, worst streak first. Disabled sources are excluded on purpose: turning
    one off was a decision, not a fault.
    """
    current = _as_utc(now) or datetime.now(UTC)
    threshold = _resolve_stale_hours(stale_hours)
    health = source_health(current, failure_threshold=failure_threshold)

    counts = dict.fromkeys(STATES, 0)
    for entry in health["sources"]:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1

    enabled = [entry for entry in health["sources"] if entry["enabled"]]
    stale = [
        entry
        for entry in enabled
        if entry["silence_hours"] is None or entry["silence_hours"] >= threshold
    ]
    never_checked = [entry for entry in enabled if entry["last_attempt_at"] is None]

    problems = [
        entry for entry in health["sources"] if entry["state"] in {STATE_COOLDOWN, STATE_FAILING}
    ]
    problems.sort(key=lambda item: (_STATE_RANK[item["state"]], -item["failures"], item["id"]))
    top_problems = [
        {
            "id": entry["id"],
            "title": entry["title"],
            "state": entry["state"],
            "failures": entry["failures"],
            "cooldown_until": entry["cooldown_until"],
            "last_attempt_at": entry["last_attempt_at"],
            "last_error": entry["last_error"],
            "reason": entry["reason"],
        }
        for entry in problems[: max(0, int(top))]
    ]

    return {
        "generated_at": health["generated_at"],
        "total": health["total"],
        "enabled": len(enabled),
        "counts": counts,
        "attention": counts[STATE_COOLDOWN] + counts[STATE_FAILING],
        "stale_hours": threshold,
        "stale": len(stale),
        "stale_ids": [entry["id"] for entry in stale],
        "never_checked": len(never_checked),
        "never_checked_ids": [entry["id"] for entry in never_checked],
        "top_problems": top_problems,
    }


def reset_source_cooldown(source_id: str, *, reason: str) -> bool:
    """Clear one source's cooldown and leave an audit trail. No network calls.

    Unparking is not a claim that the feed works: ``failures`` and ``last_error``
    are kept, so the source immediately shows up as ``failing`` in
    :func:`source_health` and is retried by the next S1 pass. ``last_status`` is
    moved from ``"cooldown"`` to ``"error"`` for the same reason — it describes
    the last attempt, which did fail.

    Writes one ``OperatorLog`` row with action ``source-cooldown-reset`` in the
    same transaction as the reset. Returns ``False`` when the source does not
    exist (nothing is written then); an existing source without an active
    cooldown still returns ``True`` and is logged, so the call is idempotent.
    """
    clean_id = (source_id or "").strip()
    if not clean_id:
        return False
    note = (reason or "").strip() or "причина не указана"

    with session_scope() as session:
        row = session.get(SourceRow, clean_id)
        if row is None:
            log.warning("cooldown reset requested for unknown source %r", clean_id)
            return False

        previous = _as_utc(row.cooldown_until)
        was_parked = (row.last_status or "").strip().lower() == "cooldown"
        row.cooldown_until = None
        if was_parked:
            row.last_status = "error"

        detail = f"cooldown снят вручную: {note}"
        if previous is not None:
            detail += f" (был до {previous.isoformat(timespec='seconds')})"
        session.add(
            OperatorLog(
                action=RESET_ACTION,
                target=clean_id[:64],
                detail=detail[:_MAX_DETAIL_CHARS],
            )
        )

    log.info("source %s cooldown cleared manually: %s", clean_id, note)
    return True

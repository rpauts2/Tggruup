"""Persistent, cross-process guard for paid image-provider calls."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import wraps

from sqlalchemy import func, select, text

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.db.models import ImageUsage
from tgfarm.db.session import session_scope

log = get_logger(__name__)

_IMAGE_BUDGET_LOCK_KEY = 731904218


def _acquire_image_budget_lock(session) -> None:
    """Serialize budget checks across threads and separate farm processes."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    elif dialect == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _IMAGE_BUDGET_LOCK_KEY},
        )
    else:
        raise RuntimeError(
            f"Atomic image budget reservations are unsupported for database dialect: {dialect}"
        )


def reserve_image_call(provider: str, model: str) -> tuple[int | None, bool]:
    """Reserve one daily image request. Failed attempts remain charged conservatively."""
    budget = max(0, int(get_settings().image_daily_budget))
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    with session_scope() as session:
        _acquire_image_budget_lock(session)
        used = int(
            session.execute(
                select(func.count())
                .select_from(ImageUsage)
                .where(
                    ImageUsage.charged.is_(True),
                    ImageUsage.created_at >= start,
                )
            ).scalar_one()
            or 0
        )
        allowed = used < budget
        row = ImageUsage(
            provider=(provider or "")[:32],
            model=(model or "")[:128],
            charged=allowed,
            status="reserved" if allowed else "blocked",
            reason="reserved" if allowed else f"budget exhausted ({budget}/day)",
        )
        session.add(row)
        session.flush()
        usage_id = row.id

    if not allowed:
        log.warning("image budget exhausted (%d/%d); using template fallback", used, budget)
        return usage_id, False
    if used + 1 >= max(1, int(budget * 0.8)):
        log.warning("image budget approaching limit (%d/%d)", used + 1, budget)
    return usage_id, True


def finish_image_call(usage_id: int | None, status: str, error_kind: str = "") -> None:
    """Record the outcome without storing prompts, provider response bodies, or secrets."""
    if usage_id is None:
        return
    with session_scope() as session:
        row = session.get(ImageUsage, usage_id)
        if row is None:
            return
        row.status = status[:24]
        row.error_kind = error_kind[:80]
        row.finished_at = datetime.now(UTC)


def budgeted_image_call(method):
    """Decorator that charges every attempted provider request, including failures."""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        provider = type(self).__name__
        model = str(getattr(self, "_model", "") or "")
        usage_id, allowed = reserve_image_call(provider, model)
        if not allowed:
            raise ProviderUnavailable("daily image generation budget exhausted")
        try:
            result = await method(self, *args, **kwargs)
        except Exception as exc:
            finish_image_call(usage_id, "failed", type(exc).__name__)
            raise
        finish_image_call(usage_id, "succeeded")
        return result

    return wrapped

"""Reservations left open by a crashed process must not alarm forever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tgfarm.db.models import AIUsage
from tgfarm.db.session import session_scope
from tgfarm.llm.policy import (
    STALE_CLOSURE_REASON,
    close_stale_reservations,
    usage_report,
)


def _reservation(minutes_ago: float, *, charged: bool = True, reason: str = "reserved") -> int:
    with session_scope() as session:
        row = AIUsage(
            stage="s4_draft",
            purpose="test",
            provider="compatible",
            model="x",
            charged=charged,
            used=False,
            reason=reason,
            created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        )
        session.add(row)
        session.flush()
        return row.id


def _row(usage_id: int) -> AIUsage:
    with session_scope() as session:
        return session.execute(select(AIUsage).where(AIUsage.id == usage_id)).scalar_one()


def test_only_old_open_reservations_are_closed():
    old = _reservation(90)
    fresh = _reservation(2)
    used = _reservation(120, reason="used")

    assert close_stale_reservations(30) >= 1

    assert _row(old).reason == STALE_CLOSURE_REASON
    assert _row(fresh).reason == "reserved"  # too young to judge
    assert _row(used).reason == "used"  # settled calls are untouched


def test_closing_does_not_refund_the_charge():
    usage_id = _reservation(90)
    close_stale_reservations(30)
    row = _row(usage_id)
    # The provider may have been called: the budget stays charged, only the
    # repeating alarm stops.
    assert row.charged is True
    assert row.used is False


def test_closed_reservations_leave_the_pending_report():
    """Two days, not one: the report windows from midnight, not from the clock.

    `usage_report(days=1)` starts its window at `_start_of_day`, so a reservation
    aged 90 minutes sits outside it during the first 90 minutes of every UTC day.
    Read on a rolling window the row is always in view, and the test stops
    depending on the hour it happens to run at — it failed at 00:10 and passed at
    23:51 on identical code.
    """
    _reservation(90)
    before = usage_report(days=2)["totals"]
    assert before["pending_reservations"] >= 1
    close_stale_reservations(30)
    after = usage_report(days=2)["totals"]
    assert after["pending_reservations"] < before["pending_reservations"]
    assert after["stale_reservations"] < before["stale_reservations"]

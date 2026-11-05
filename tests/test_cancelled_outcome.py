"""A cancelled stage and a cancelled cycle have to leave a trace behind.

`asyncio.CancelledError` has been a `BaseException` since Python 3.8, so the
`except Exception` clauses that record every other stage outcome never saw it.
Live evidence (2026-11-05), read through a read-only connection to the real
database: since the last restart the journal held **no `s5_quality` row and no
`cycle` row at all**, while the log said

    stage s5_quality failed after 2698ms:
    03:26:01 ERROR watchdog: cycle has been running for 2701s (state=running);
        cancelling stale task

— the first line ending in nothing, because `str(CancelledError())` is the empty
string. The farm's most common failure was therefore doubly invisible: no row in
the journal, and an unreadable sentence in the log.

Nothing here reaches the network or a publisher: the stages under test are
stand-ins that hang until something cancels them.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.schema import CheckConstraint

from tgfarm import pipeline
from tgfarm.core.run import StageTimer, describe_exception
from tgfarm.db.models import PipelineRun
from tgfarm.db.session import session_scope


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _rows(run_id: str) -> list[PipelineRun]:
    with session_scope() as session:
        return list(
            session.execute(
                select(PipelineRun).where(PipelineRun.run_id == run_id)
            ).scalars().all()
        )


def _drop(*run_ids: str) -> None:
    with session_scope() as session:
        session.execute(delete(PipelineRun).where(PipelineRun.run_id.in_(run_ids)))


def _cancelled_cycles() -> list[PipelineRun]:
    with session_scope() as session:
        return list(
            session.execute(
                select(PipelineRun)
                .where(PipelineRun.stage == "cycle", PipelineRun.status == "cancelled")
                .order_by(PipelineRun.id)
            ).scalars().all()
        )


async def _hang_forever(inside: asyncio.Event | None = None):
    """A stand-in stage that only ever ends because somebody cancelled it."""
    if inside is not None:
        inside.set()
    await asyncio.Event().wait()


# ------------------------------------------------------------------ one stage
@pytest.mark.asyncio
async def test_a_cancelled_stage_is_journalled_instead_of_vanishing():
    run_id = f"cut-{_uid()}"
    result = pipeline.CycleResult(run_id=run_id)
    inside = asyncio.Event()

    async def _measured_then_stuck():
        with StageTimer("s5_quality") as timer:
            # What the stage counted before the cut is all it will ever be able
            # to report, so it has to reach the row.
            timer.items_in = 17
            timer.items_out = 4
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(pipeline._stage(result, "s5_quality", _measured_then_stuck))
    await asyncio.wait_for(inside.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        rows = _rows(run_id)
        assert len(rows) == 1, f"отменённый этап обязан оставить ровно одну строку: {rows}"
        row = rows[0]
        assert row.stage == "s5_quality"
        assert row.status == "cancelled"
        assert (row.items_in, row.items_out) == (17, 4)
        assert row.duration_ms >= 0
        assert "cancelled" in (row.error or "")
        assert row.meta["counters"] == "measured"
        assert "unmeasured" not in row.meta
    finally:
        _drop(run_id)


@pytest.mark.asyncio
async def test_cancellation_is_passed_up_not_swallowed():
    """A stage that recorded its own cancellation must still let the cycle die.

    Isolation means one failed stage does not stop the others; it must not mean
    the farm cannot be stopped.
    """
    run_id = f"cut-{_uid()}"
    result = pipeline.CycleResult(run_id=run_id)

    task = asyncio.create_task(pipeline._stage(result, "s2_trends", _hang_forever))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        assert result.stages["s2_trends"]["ok"] is False
        assert result.stages["s2_trends"]["cancelled"] is True
        assert _rows(run_id)[0].status == "cancelled"
    finally:
        _drop(run_id)


@pytest.mark.asyncio
async def test_a_cancelled_stage_that_measured_nothing_does_not_claim_a_zero():
    """The placeholder zero and the marker saying it is a placeholder stay paired."""
    run_id = f"cut-{_uid()}"
    result = pipeline.CycleResult(run_id=run_id)

    task = asyncio.create_task(pipeline._stage(result, "s8_analytics", _hang_forever))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        row = _rows(run_id)[0]
        assert row.status == "cancelled"
        assert row.meta["counters"] == "not_reported"
        assert row.meta["unmeasured"] == ["items_in", "items_out"]
    finally:
        _drop(run_id)


@pytest.mark.asyncio
async def test_a_second_cancellation_still_lets_the_row_land(monkeypatch):
    """The watchdog can cancel a task that is already unwinding.

    The journal write in the handler is itself an `await`, so a second `cancel()`
    interrupts it before the executor reports back. `shield` is what lets that
    insert finish while the task gets out; without it, the very failure this file
    exists to record would again leave no trace.
    """
    run_id = f"cut-{_uid()}"
    result = pipeline.CycleResult(run_id=run_id)
    inside = asyncio.Event()
    writing = threading.Event()
    real_record = pipeline._record

    def slow_record(*args, **kwargs):
        writing.set()
        time.sleep(0.3)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_record", slow_record)

    async def _stuck():
        await _hang_forever(inside)

    task = asyncio.create_task(pipeline._stage(result, "s6_visual", _stuck))
    await asyncio.wait_for(inside.wait(), timeout=2)
    task.cancel()
    assert await asyncio.to_thread(writing.wait, 2), "запись журнала не началась"
    task.cancel()  # interrupt the in-flight journal write
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.5)  # the shielded insert completes on its own
    try:
        rows = _rows(run_id)
        assert len(rows) == 1, "отмена во время записи не должна стирать саму запись"
        assert rows[0].status == "cancelled"
    finally:
        _drop(run_id)


# ---------------------------------------------------------------- one cycle
@pytest.mark.asyncio
async def test_a_cancelled_cycle_is_journalled_as_cancelled(monkeypatch):
    before = len(_cancelled_cycles())
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_cycle(*, collect=True, max_clusters=1000):
        started.set()
        await release.wait()
        return pipeline.CycleResult(run_id="never-recorded")

    monkeypatch.setattr(pipeline, "_active_cycle_task", None)
    monkeypatch.setattr(pipeline, "_cycle_state", "idle")
    monkeypatch.setattr(pipeline, "_cycle_finished_at", None)
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)

    task = asyncio.create_task(pipeline._guarded_cycle())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    after = _cancelled_cycles()
    assert len(after) == before + 1, "отменённый цикл обязан отличаться от молчания"
    row = after[-1]
    try:
        assert row.run_id.startswith("guard-")
        assert "cancelled" in (row.error or "")
        assert row.meta["counters"] == "not_reported", (
            "у guard-строки нет права на выдуманный ноль в счётчиках"
        )
    finally:
        _drop(row.run_id)
    assert pipeline._cycle_state == "cancelled"
    assert pipeline._active_cycle_task is None, "слот активного цикла должен освободиться"


@pytest.mark.asyncio
async def test_cancelled_and_timeout_are_two_different_rows(monkeypatch):
    """`cycle/timeout` and `cycle/cancelled` must not collapse into each other.

    The first says the cycle outran its own budget; the second says something
    stopped it — the watchdog, a shutdown, an operator. Those are different
    things to do about, and an operator reading the journal afterwards should not
    have to guess which one happened.
    """
    recorded: list[tuple[str, str]] = []

    async def overruns(*, collect=True, max_clusters=1000):
        # The exception `asyncio.wait_for` raises when the budget expires.
        raise TimeoutError("cycle budget spent")

    async def stopped(*, collect=True, max_clusters=1000):
        raise asyncio.CancelledError

    monkeypatch.setattr(pipeline, "_active_cycle_task", None)
    monkeypatch.setattr(pipeline, "_cycle_state", "idle")
    monkeypatch.setattr(pipeline, "_cycle_finished_at", None)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(cycle_timeout_min=1))

    def spy_record(run_id, stage, status, *args, **kwargs):
        recorded.append((stage, status))
        return None

    monkeypatch.setattr(pipeline, "_record", spy_record)

    monkeypatch.setattr(pipeline, "run_cycle", overruns)
    assert await pipeline._guarded_cycle() is None

    monkeypatch.setattr(pipeline, "run_cycle", stopped)
    with pytest.raises(asyncio.CancelledError):
        await pipeline._guarded_cycle()

    assert ("cycle", "timeout") in recorded
    assert ("cycle", "cancelled") in recorded
    assert [stage for stage, _ in recorded] == ["cycle", "cycle"]


# ------------------------------------------------------------------ watchdog
@pytest.mark.asyncio
async def test_watchdog_still_considers_catch_up_after_a_cancelled_cycle(monkeypatch):
    """`cancelled` has to join the states that earn a recovery cycle.

    A state the watchdog does not recognise falls through to a debug line and a
    return, so publication would stop draining right after the failure this
    commit makes visible — a fix that introduced a new silence.
    """
    monkeypatch.setattr(
        pipeline,
        "cycle_health",
        lambda: {"state": "cancelled", "stale": False, "age_s": 12},
    )
    monkeypatch.setattr(pipeline, "collect_alerts", lambda: [])

    async def no_alerts():
        return 0

    monkeypatch.setattr(pipeline, "deliver_alerts", no_alerts)
    monkeypatch.setattr(pipeline, "_watchdog_recovery_attempts", 0)
    monkeypatch.setattr(pipeline, "_watchdog_last_recovery_at", None)

    scheduled = []
    real_create_task = pipeline.asyncio.create_task

    def capture(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(pipeline.asyncio, "create_task", capture)
    try:
        await pipeline._watchdog()
    finally:
        pipeline.asyncio.create_task = real_create_task

    assert len(scheduled) == 1, "отменённый цикл обязан вести к попытке восстановления"


# --------------------------------------------------------------------- the log
@pytest.mark.parametrize(
    "exc",
    [
        asyncio.CancelledError(),
        RuntimeError(),
        ValueError("bad payload"),
        TimeoutError(),
        Exception(),
    ],
)
def test_describe_exception_always_names_the_type(exc):
    """A line about a failure must never end in nothing.

    `str(CancelledError())`, `str(RuntimeError())` and `str(TimeoutError())` are
    all empty; only the class name tells them apart, and the class name is the
    part the live log was missing.
    """
    text = describe_exception(type(exc), exc)
    assert text, "описание сбоя не бывает пустым"
    assert text.startswith(type(exc).__name__)
    if str(exc):
        assert text == f"{type(exc).__name__}: {str(exc).strip()}"
    else:
        assert text == type(exc).__name__


def test_describe_exception_falls_back_to_the_instance_or_a_plain_name():
    assert describe_exception(None, ValueError("x")) == "ValueError: x"
    assert describe_exception(None, None) == "BaseException"


def test_a_stage_that_throws_logs_the_measured_counts_and_the_name(caplog):
    with caplog.at_level("ERROR", logger="tgfarm.core.run"):
        with pytest.raises(ValueError):
            with StageTimer("s5_quality") as timer:
                timer.items_in = 0
                raise ValueError("bad payload")

    lines = [r.getMessage() for r in caplog.records if "failed after" in r.getMessage()]
    assert lines
    # The elapsed milliseconds are a real measurement, so only its ends are pinned.
    assert lines[-1].startswith("stage s5_quality failed after ")
    assert lines[-1].endswith("ms with 0 -> ? measured: ValueError: bad payload")


def test_a_cancelled_stage_logs_a_sentence_an_operator_can_read(caplog):
    """The exact live line from the incident, reconstructed and now readable."""
    with caplog.at_level("ERROR", logger="tgfarm.core.run"):
        with pytest.raises(asyncio.CancelledError):
            with StageTimer("s5_quality"):
                raise asyncio.CancelledError

    lines = [r.getMessage() for r in caplog.records if "failed after" in r.getMessage()]
    assert lines, "таймер обязан остаться в логе"
    assert lines[-1].endswith("CancelledError"), (
        f"строка не должна обрываться на пустом описании: {lines[-1]!r}"
    )
    assert not lines[-1].rstrip().endswith(":"), "никакого голого двоеточия"


# ------------------------------------------------------------------- the shape
def test_cancelled_is_a_status_the_journal_can_already_hold():
    """`status` is a length-16 string with no CHECK constraint, so no migration.

    Reusing `error` instead of adding `cancelled` was the cheap option; the only
    reason the honest one is available is this column's shape. If a CHECK ever
    lands here, this test is where it gets noticed.
    """
    column = PipelineRun.__table__.c.status
    assert column.type.length == 16
    assert len("cancelled") <= column.type.length
    assert not [
        constraint
        for constraint in PipelineRun.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    ]

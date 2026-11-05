"""``PLAN_EXECUTION_ENABLED`` is the only thing that puts the plan on a clock.

Flag off must mean *base*: the same nineteen job ids ``_build_scheduler``
registered at ``094a67f``, no plan rows written, no journal rows written, and
both ticks on the floor before they read the database. Flag on must add exactly
two jobs — the refresh and the apply — because the live database measured
``editorial_plans = 0`` rows and ``editorial_plan_entries = 0`` rows: the plan had
a builder and three HTTP readers and no consumer at all.

The set comparisons are real set diffs, not `in` checks, so a third job that
appears by accident is a failure rather than a surprise. Job-id counts are read
as deltas against a baseline captured in the same test, because
``tests/conftest.py`` gives the whole suite one database and absolute counters
have already broken this repo twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, func, select, update

from tgfarm import pipeline, plan_execution
from tgfarm.core.settings import Settings, reset_settings_cache
from tgfarm.db.models import Cluster, EditorialPlan, EditorialPlanEntry, PipelineRun
from tgfarm.db.session import session_scope

#: The exact ids `_build_scheduler` registered at `094a67f` (16 `add_job` calls,
#: 19 ids: three collector specs and two sync pairs each add a `startup-*` twin).
BASELINE_JOB_IDS = frozenset(
    {
        "branding-rotation",
        "crosspost-tick",
        "cycle",
        "daily-backup",
        "dzen-warm",
        "metrics-and-learning",
        "platform-auth-health",
        "publish-tick",
        "startup-backup-check",
        "startup-metrics-and-learning",
        "startup-platform-auth-check",
        "startup-telegram-channel-sync",
        "startup-telegram-post-metrics",
        "startup-telegraph-metrics",
        "telegram-channel-sync",
        "telegram-post-metrics",
        "telegraph-metrics",
        "watchdog",
        "weekly-report",
    }
)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _flag(monkeypatch, *, enabled: bool) -> None:
    """Flip only this switch, and make `get_settings()` see the flip."""
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if enabled else "false")
    reset_settings_cache()


def _job_ids() -> set[str]:
    scheduler = pipeline._build_scheduler()
    try:
        return {job.id for job in scheduler.get_jobs()}
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def _plan_count() -> int:
    with session_scope() as session:
        return int(session.execute(select(func.count()).select_from(EditorialPlan)).scalar_one())


def _journal(stage: str) -> list[PipelineRun]:
    with session_scope() as session:
        return list(
            session.execute(
                select(PipelineRun).where(PipelineRun.stage == stage).order_by(PipelineRun.id)
            ).scalars().all()
        )


class _Owned:
    """Rows this file creates, named so they are the only rows it deletes."""

    def __init__(self) -> None:
        self.cluster_ids: list[str] = []
        self.plan_public_ids: list[str] = []

    def cluster(self, db) -> str:
        public_id = f"w17plan-topic-{_uid()}"
        self.cluster_ids.append(public_id)
        db.merge(
            Cluster(
                public_id=public_id,
                label=f"Тема w17plan {_uid()}",
                niche="",
                status="ready",
                heat=0.99,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        return public_id

    def forget(self) -> None:
        """Delete only rows this file created; the shared session DB stays as found.

        One transaction per step, because a rollback would otherwise undo the
        steps that came before it and leave the journal full of my ticks: a plan
        the builder superseded keeps `superseded_by_id` pointing at the snapshot
        my tick wrote, so that link is unhooked before the row goes away.
        """
        with session_scope() as session:
            if self.plan_public_ids:
                plan_ids = list(
                    session.execute(
                        select(EditorialPlan.id).where(
                            EditorialPlan.public_id.in_(self.plan_public_ids)
                        )
                    ).scalars().all()
                )
                if plan_ids:
                    session.execute(
                        update(EditorialPlan)
                        .where(EditorialPlan.superseded_by_id.in_(plan_ids))
                        .values(superseded_by_id=None)
                    )
                    session.execute(
                        delete(EditorialPlanEntry).where(
                            EditorialPlanEntry.plan_id.in_(plan_ids)
                        )
                    )
                    session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
        with session_scope() as session:
            if self.cluster_ids:
                session.execute(delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids)))
        with session_scope() as session:
            session.execute(
                delete(PipelineRun).where(
                    PipelineRun.stage.in_((plan_execution.STAGE_BUILD, plan_execution.STAGE_APPLY))
                )
            )


@pytest.fixture()
def owned():
    rows = _Owned()
    yield rows
    rows.forget()


@pytest.fixture()
def _no_active_plan():
    """Neutralise other files' active snapshots for one test, then put them back."""
    with session_scope() as session:
        ids = session.execute(
            select(EditorialPlan.id).where(EditorialPlan.status == "active")
        ).scalars().all()
        for plan_id in ids:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in ids:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


# ------------------------------------------------------------------ the switch
def test_the_switch_is_declared_off_and_named():
    """A defaulted-off knob: `Settings` must not run the plan by itself."""
    field = Settings.model_fields["plan_execution_enabled"]
    assert field.default is False
    assert field.alias == "PLAN_EXECUTION_ENABLED"


def test_flag_off_leaves_the_scheduler_exactly_as_base():
    assert _job_ids() == set(BASELINE_JOB_IDS)


def test_flag_on_adds_exactly_two_jobs(monkeypatch):
    _flag(monkeypatch, enabled=True)
    try:
        assert _job_ids() - set(BASELINE_JOB_IDS) == {
            plan_execution.PLAN_BUILD_JOB_ID,
            plan_execution.PLAN_APPLY_JOB_ID,
        }
    finally:
        reset_settings_cache()


def test_flag_off_registers_neither_of_the_two_jobs(monkeypatch):
    _flag(monkeypatch, enabled=False)
    try:
        ids = _job_ids()
        assert plan_execution.PLAN_BUILD_JOB_ID not in ids
        assert plan_execution.PLAN_APPLY_JOB_ID not in ids
    finally:
        reset_settings_cache()


def test_the_two_jobs_are_registered_the_way_their_neighbours_are(monkeypatch):
    """`max_instances=1`, `coalesce=True`, a grace time, an interval trigger.

    The same shape every other conditional job in `_build_scheduler` has, so a
    future edit cannot quietly give the plan a different life cycle from the
    switch that owns it. `telegram-top-groups` is the neighbour read here only
    to prove the base flag set is unchanged: it stays off in this suite.
    """
    _flag(monkeypatch, enabled=True)
    scheduler = pipeline._build_scheduler()
    try:
        neighbour = scheduler.get_job("telegram-top-groups")
        assert neighbour is None, "the comparison below needs the off-flag shape"
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    try:
        build = pipeline._build_scheduler()
        try:
            recurring = build.get_job(plan_execution.PLAN_BUILD_JOB_ID)
            apply_job = build.get_job(plan_execution.PLAN_APPLY_JOB_ID)
            assert recurring.func is plan_execution.build_plan_tick
            assert apply_job.func is plan_execution.apply_plan_tick
            for job, expected in (
                (recurring, plan_execution.BUILD_INTERVAL_HOURS * 3600),
                (apply_job, plan_execution.APPLY_INTERVAL_MINUTES * 60),
            ):
                assert isinstance(job.trigger, IntervalTrigger)
                assert job.trigger.interval.total_seconds() == expected
                assert job.max_instances == 1
                assert job.coalesce is True
                assert job.misfire_grace_time and job.misfire_grace_time > 0
        finally:
            if build.running:
                build.shutdown(wait=False)
    finally:
        reset_settings_cache()


# ----------------------------------------------------------------- ticks off
@pytest.mark.asyncio
async def test_the_build_tick_does_nothing_while_the_flag_is_off(monkeypatch, owned):
    _flag(monkeypatch, enabled=False)
    try:
        before = _plan_count()
        report = await plan_execution.build_plan_tick()
        assert report == {"skipped": plan_execution.REASON_DISABLED}
        assert _plan_count() == before, "off must not write a plan row"
        assert _journal(plan_execution.STAGE_BUILD) == []
    finally:
        reset_settings_cache()


@pytest.mark.asyncio
async def test_the_apply_tick_does_nothing_while_the_flag_is_off(monkeypatch):
    _flag(monkeypatch, enabled=False)
    try:
        report = await plan_execution.apply_plan_tick()
        assert report == {"skipped": plan_execution.REASON_DISABLED}
        assert _journal(plan_execution.STAGE_APPLY) == []
    finally:
        reset_settings_cache()


# ----------------------------------------------------------------- ticks on
@pytest.mark.asyncio
async def test_the_build_tick_lands_a_plan_in_the_database_with_no_http_call(
    monkeypatch, db, owned
):
    """The gap this job exists to close: a plan that only a panel click built."""
    owned.cluster(db)
    db.commit()
    _flag(monkeypatch, enabled=True)
    try:
        before = _plan_count()
        plan = await plan_execution.build_plan_tick()
        assert _plan_count() == before + 1, "флаг включён — план обязан сохраниться"
        owned.plan_public_ids.append(plan["public_id"])

        with session_scope() as session:
            row = session.execute(
                select(EditorialPlan).where(EditorialPlan.public_id == plan["public_id"])
            ).scalar_one()
            assert row.status == "active"
            entries = session.execute(
                select(EditorialPlanEntry).where(EditorialPlanEntry.plan_id == row.id)
            ).scalars().all()
        assert entries, "план обязан перечислить темы, иначе журнал ничего не проверяет"
        assert {entry.cluster_public_id for entry in entries} == {
            row["cluster_public_id"] for row in plan["entries"]
        }
    finally:
        reset_settings_cache()


@pytest.mark.asyncio
async def test_the_build_tick_journals_the_entries_it_really_wrote(monkeypatch, db, owned):
    owned.cluster(db)
    db.commit()
    _flag(monkeypatch, enabled=True)
    try:
        plan = await plan_execution.build_plan_tick()
        owned.plan_public_ids.append(plan["public_id"])
        rows = _journal(plan_execution.STAGE_BUILD)
        assert len(rows) == 1, "тик обязан оставить след, иначе его не проверить"
        row = rows[-1]
        assert row.status == "ok"
        assert row.items_out == len(plan["entries"])
        # P34: an unmeasured input is not a zero. The builder never counts the
        # queue it read, so the row says which half of the pair is a placeholder.
        assert row.meta["counters"] == "partial"
        assert row.meta["unmeasured"] == ["items_in"]
        assert row.meta["job"] == plan_execution.PLAN_BUILD_JOB_ID
    finally:
        reset_settings_cache()


@pytest.mark.asyncio
async def test_a_run_without_a_plan_is_not_logged_as_a_measured_zero(
    monkeypatch, _no_active_plan
):
    """`applied 0` with no plan is a different fact from `scanned 0, queued 0`."""
    _flag(monkeypatch, enabled=True)
    try:
        report = await plan_execution.apply_plan_tick()
        assert report["no_plan"] is True
        assert report["applied"] == 0
        assert report["reasons"] == {plan_execution.REASON_NO_PLAN: 1}
        rows = _journal(plan_execution.STAGE_APPLY)
        assert len(rows) == 1
        row = rows[-1]
        assert row.status == "skipped"
        assert row.meta["counters"] == "not_reported"
        assert sorted(row.meta["unmeasured"]) == ["items_in", "items_out"]
    finally:
        reset_settings_cache()


def test_the_module_never_writes_a_post_state_by_hand():
    """A column write would be the second scheduler, with its own bugs.

    The state transition is `s7_publish.unpublish`'s — one conditional UPDATE —
    and every other status/slot write in the package stays in s7.
    """
    import inspect

    body = inspect.getsource(plan_execution)
    assert "unpublish(" in body, "передача должна идти в существующий путь планирования"
    for forbidden in ("post.status =", "row.status =", ".scheduled_at ="):
        assert forbidden not in body, f"{forbidden} — это рука, а не существующий путь"

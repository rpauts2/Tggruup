"""``plan-dry-run``: the executor's verdicts, readable without turning it on.

``apply_plan(dry_run=True)`` — ``evaluate_plan`` is the name the CLI uses — walks
the same loop with the same decision functions and withholds the one write. That
promise is worth nothing unless it is pinned from both sides, so this file pins
it twice: a dry run must predict exactly what a real run then does (the
anti-divergence pin, worthless if the two paths are separate implementations),
and it must leave the shared database exactly as it found it — no post moved, no
plan row touched, no journal row. The no-write claim is additionally read off the
driver: every statement the dry run sends to SQLite is captured and must be a
read.

``PLAN_EXECUTION_ENABLED`` gates the writes, not the judgement. The default-off
switch is what makes the verdicts unreadable, so a dry run must not answer
``plan_execution_disabled`` to every entry; it reports the flag instead, and the
flag keeps short-circuiting a real run.

Database hygiene: ``tests/conftest.py`` gives the whole suite one database, so
every channel, cluster, post and plan snapshot below is ``w18a-``-named, counted
as a delta against its own baseline, and deleted again by the fixture teardown.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.engine import Connection

from tgfarm import cli, plan_execution, s7_publish
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, PipelineRun, Post
from tgfarm.db.session import get_engine, session_scope

#: The same test clock the apply file uses: 04:30 UTC is 09:30 in Tashkent and
#: 01:30 in São Paulo, so one tick gives two local answers.
W18A_MORNING_UTC = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)
W18A_LEGAL_WINDOW = [9, 18]
W18A_DUE_SLOT = W18A_MORNING_UTC - timedelta(minutes=1)

#: "no slot passed" must stay distinct from "no slot at all".
_NOT_SET = object()


def _w18a_uid() -> str:
    return uuid.uuid4().hex[:8]


def _w18a_config(active_hours: list[int] | None) -> SimpleNamespace:
    """A cadence of our own, so the hour gate is this file's, not the shipped YAML's."""
    return SimpleNamespace(
        schedule=SimpleNamespace(
            cadence={"mode": "interval", "active_hours": active_hours},
            hours=lambda: list(range(24)),
            hour_probability=lambda hour: 1.0,
        )
    )


class _W18aRows:
    """Every row this file creates, so teardown deletes exactly these and nothing else."""

    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.cluster_ids: list[str] = []
        self.post_ids: list[str] = []
        self.plan_public_ids: list[str] = []

    def forget(self) -> None:
        with session_scope() as session:
            plan_ids = (
                list(
                    session.execute(
                        select(EditorialPlan.id).where(
                            EditorialPlan.public_id.in_(self.plan_public_ids)
                        )
                    ).scalars().all()
                )
                if self.plan_public_ids
                else []
            )
            if plan_ids:
                session.execute(
                    update(EditorialPlan)
                    .where(EditorialPlan.superseded_by_id.in_(plan_ids))
                    .values(superseded_by_id=None)
                )
                session.execute(
                    delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
                )
                session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
        with session_scope() as session:
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
        with session_scope() as session:
            if self.cluster_ids:
                session.execute(delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids)))
        with session_scope() as session:
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))


@pytest.fixture()
def w18a_rows():
    owned = _W18aRows()
    yield owned
    owned.forget()


@pytest.fixture()
def w18a_flag_on(monkeypatch):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture()
def w18a_flag_off(monkeypatch):
    """The default an operator actually has before they decide."""
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "false")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def w18a_only_my_plan_is_active():
    """One reader, one snapshot: this file's own.

    `get_active_plan` orders by `created_at desc` and the suite shares one
    database, so a foreign snapshot could win a same-second tie and make a dry
    run judge a plan it never wrote. Foreign rows are parked for the duration of
    one test and put back afterwards — only their status moves.
    """
    with session_scope() as session:
        foreign_ids = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.status == "active")
            ).scalars().all()
        )
        for plan_id in foreign_ids:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in foreign_ids:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


@pytest.fixture()
def w18a_quiet_budget(monkeypatch):
    """The AI ledger as this file states it, so another file's rows cannot move it."""
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 0)
    yield


@pytest.fixture()
def w18a_switches_on(monkeypatch):
    """The operator's per-platform switches pinned to on."""
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda platform: True)
    yield


@pytest.fixture()
def w18a_no_unpublish(monkeypatch):
    """A dry run that handed anything to the scheduling path is a bug, not a report."""

    def _explode(public_ids):
        raise AssertionError(f"сухой прогон не имеет права ничего передавать: {public_ids}")

    monkeypatch.setattr(s7_publish, "unpublish", _explode)
    yield


# ------------------------------------------------------------------ row builders
def _w18a_channel(owned: _W18aRows, *, platform="telegram", timezone="Europe/Moscow",
                  posts_per_day=5, enabled=True) -> str:
    channel_id = f"w18a-ch-{_w18a_uid()}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform=platform,
                title=channel_id,
                niche="",
                enabled=enabled,
                timezone=timezone,
                posts_per_day=posts_per_day,
            )
        )
    return channel_id


def _w18a_cluster(owned: _W18aRows) -> str:
    public_id = f"w18a-cl-{_w18a_uid()}"
    owned.cluster_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Cluster(
                public_id=public_id,
                label=f"Тема w18a {_w18a_uid()}",
                niche="",
                status="ready",
                heat=0.5,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    return public_id


def _w18a_post(owned: _W18aRows, channel_id: str, cluster_id: str | None = None, *,
               status="gated", provider="rule", scheduled_at=None) -> str:
    public_id = f"w18a-post-{_w18a_uid()}"
    owned.post_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                cluster_public_id=cluster_id,
                hook="hook",
                body="body",
                status=status,
                generation_provider=provider,
                scheduled_at=scheduled_at,
            )
        )
    return public_id


def _w18a_plan(owned: _W18aRows, entries: list[dict]) -> str:
    """One active snapshot with exactly the entries a test needs to describe."""
    public_id = f"w18a-plan-{_w18a_uid()}"
    owned.plan_public_ids.append(public_id)
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=public_id,
            created_at=datetime.now(UTC),
            horizon_days=7,
            status="active",
            summary={"total": len(entries)},
        )
        session.add(plan)
        session.flush()
        for entry in entries:
            session.add(
                EditorialPlanEntry(
                    plan_id=plan.id,
                    cluster_public_id=entry["cluster_public_id"],
                    title="Тема",
                    niche="",
                    action=entry["action"],
                    platform=entry.get("platform"),
                    channel_id=entry.get("channel_id"),
                    format="news",
                    priority=1,
                    confidence=0.5,
                    reasons=["тест"],
                    slot_at=entry.get("slot_at"),
                    payload={},
                )
            )
    return public_id


def _w18a_entry(owned: _W18aRows, channel_id: str, *, action="schedule",
                slot_at: datetime | None | object = _NOT_SET, platform="telegram") -> dict:
    return {
        "cluster_public_id": _w18a_cluster(owned),
        "action": action,
        "channel_id": channel_id,
        "platform": platform,
        "slot_at": W18A_DUE_SLOT if slot_at is _NOT_SET else slot_at,
    }


# ---------------------------------------------------------------- state snapshots
def _w18a_post_state(public_ids: list[str]) -> list[tuple]:
    """Everything this module could be accused of changing on a post row."""
    if not public_ids:
        return []
    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id, Post.status, Post.scheduled_at, Post.published_at, Post.attempts)
            .where(Post.public_id.in_(public_ids))
            .order_by(Post.public_id)
        ).all()
    return [tuple(row) for row in rows]


def _w18a_plan_state() -> list[tuple]:
    """The plan tables as they read, so a rebuild or a supersede cannot hide."""
    with session_scope() as session:
        plans = session.execute(
            select(
                EditorialPlan.id,
                EditorialPlan.public_id,
                EditorialPlan.status,
                EditorialPlan.summary,
                EditorialPlan.superseded_by_id,
            ).order_by(EditorialPlan.id)
        ).all()
        entries = session.execute(
            select(
                EditorialPlanEntry.id,
                EditorialPlanEntry.plan_id,
                EditorialPlanEntry.cluster_public_id,
                EditorialPlanEntry.action,
                EditorialPlanEntry.slot_at,
            ).order_by(EditorialPlanEntry.id)
        ).all()
    return [("plan",) + tuple(row) for row in plans] + [("entry",) + tuple(row) for row in entries]


def _w18a_journal_count() -> int:
    with session_scope() as session:
        return int(session.execute(select(func.count()).select_from(PipelineRun)).scalar_one())


def _w18a_row(report: dict, cluster_id: str) -> dict:
    matches = [e for e in report["entries"] if e["cluster_public_id"] == cluster_id]
    assert len(matches) == 1, f"a plan entry must report exactly once: {cluster_id}"
    return matches[0]


def _w18a_verdicts(report: dict) -> dict[str, tuple[str, str]]:
    return {
        str(row["cluster_public_id"]): (str(row["verdict"]), str(row["reason"]))
        for row in report["entries"]
    }


def _w18a_outcomes(report: dict) -> dict[str, tuple[str, str]]:
    return {
        str(row["cluster_public_id"]): (str(row["outcome"]), str(row["reason"]))
        for row in report["entries"]
    }


#: A dry-run verdict is the outcome it predicts, in prediction words.
W18A_PREDICTS = {
    plan_execution.VERDICT_WOULD_SCHEDULE: "scheduled",
    plan_execution.VERDICT_WOULD_HOLD: "held",
    plan_execution.VERDICT_WOULD_SKIP: "skipped",
}


# ------------------------------------------------------------------ no writes, ever
def test_a_dry_run_reports_its_verdict_and_leaves_everything_else_as_it_found_it(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    """One qualifying entry: the verdict is `would_schedule`, the row is `gated`."""
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    channel_id = _w18a_channel(w18a_rows)
    entry = _w18a_entry(w18a_rows, channel_id)
    post_id = _w18a_post(w18a_rows, channel_id, entry["cluster_public_id"])
    _w18a_plan(w18a_rows, [entry])

    posts_before = _w18a_post_state([post_id])
    plan_before = _w18a_plan_state()
    journal_before = _w18a_journal_count()

    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)

    verdict = _w18a_row(report, entry["cluster_public_id"])
    assert verdict["verdict"] == plan_execution.VERDICT_WOULD_SCHEDULE
    assert verdict["reason"] == ""
    assert report["dry_run"] is True
    assert report["scanned"] == 1
    assert report["would_schedule"] == 1
    assert report["held"] == 0
    # `applied` stays 0 and means 0: nothing was applied. Reading it as a failed
    # run is the confusion the `would_schedule` counter exists to prevent.
    assert report["applied"] == 0
    assert report["verdicts"] == {plan_execution.VERDICT_WOULD_SCHEDULE: 1}

    assert _w18a_post_state([post_id]) == posts_before, "сухой прогон не двигает посты"
    assert posts_before[0][1] == "gated", "и до прогона, и после него пост остаётся в gated"
    assert _w18a_plan_state() == plan_before, "сухой прогон не перестраивает и не вытесняет план"
    assert _w18a_journal_count() == journal_before, "отчёт о намерениях не является тиком"


def test_a_held_entry_is_held_by_a_named_guard_and_stays_gated(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    """The report has to say which guard fired, or `held: 1` is a mood, not a fact."""
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(W18A_LEGAL_WINDOW))
    channel_id = _w18a_channel(w18a_rows, timezone="America/Sao_Paulo")
    entry = _w18a_entry(w18a_rows, channel_id)
    post_id = _w18a_post(w18a_rows, channel_id, entry["cluster_public_id"])
    _w18a_plan(w18a_rows, [entry])

    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)

    verdict = _w18a_row(report, entry["cluster_public_id"])
    assert verdict["verdict"] == plan_execution.VERDICT_WOULD_HOLD
    assert verdict["reason"] == plan_execution.REASON_LOCAL_HOURS
    assert report["reasons"] == {plan_execution.REASON_LOCAL_HOURS: 1}
    assert report["held"] == 1
    assert _w18a_post_state([post_id]) == [(post_id, "gated", None, None, 0)]


def test_the_dry_run_sends_no_write_statement_to_the_database(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    """Said by the driver, not by the docstring: every statement is a read."""
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    channel_id = _w18a_channel(w18a_rows)
    entry = _w18a_entry(w18a_rows, channel_id)
    _w18a_post(w18a_rows, channel_id, entry["cluster_public_id"])
    _w18a_plan(w18a_rows, [entry])

    seen: list[str] = []

    def _watch(_conn: Connection, _cursor, statement, *_args):
        seen.append(str(statement))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _watch)
    try:
        report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)
    finally:
        event.remove(engine, "before_cursor_execute", _watch)

    assert report["would_schedule"] == 1, "запрет записи не должен отменить само решение"
    writes = [
        statement
        for statement in seen
        if re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM|ATTACH)\b",
                     statement, re.IGNORECASE)
    ]
    assert writes == [], writes
    assert any("posts" in statement for statement in seen), seen


# ------------------------------------------------------------- the anti-divergence pin
def test_a_dry_run_predicts_exactly_what_the_real_run_then_does(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, monkeypatch
):
    """One fixture, two runs, one answer.

    The dry run goes first — it writes nothing, so the real run afterwards starts
    from the same database and the same `now`. Every verdict must equal the
    outcome the real run reports for the same entry, including the reason string,
    which is the only way to notice that the two paths drifted apart.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(W18A_LEGAL_WINDOW))

    inside_id = _w18a_channel(w18a_rows, timezone="Asia/Tashkent")
    outside_id = _w18a_channel(w18a_rows, timezone="America/Sao_Paulo")
    capped_id = _w18a_channel(w18a_rows, timezone="Asia/Tashkent", posts_per_day=1)
    disabled_id = _w18a_channel(w18a_rows, timezone="Asia/Tashkent", enabled=False)

    goes_in = _w18a_entry(w18a_rows, inside_id)
    wrong_hour = _w18a_entry(w18a_rows, outside_id)
    over_cap = _w18a_entry(w18a_rows, capped_id)
    on_disabled = _w18a_entry(w18a_rows, disabled_id)
    not_due = _w18a_entry(w18a_rows, inside_id, slot_at=W18A_MORNING_UTC + timedelta(hours=3))
    slotless = _w18a_entry(w18a_rows, inside_id, slot_at=None)
    no_draft = _w18a_entry(w18a_rows, inside_id)
    held_by_plan = _w18a_entry(w18a_rows, inside_id, action="hold", slot_at=None)
    produced = _w18a_entry(w18a_rows, inside_id, action="produce", slot_at=None)

    moved_post = _w18a_post(w18a_rows, inside_id, goes_in["cluster_public_id"])
    hour_post = _w18a_post(w18a_rows, outside_id, wrong_hour["cluster_public_id"])
    cap_post = _w18a_post(w18a_rows, capped_id, over_cap["cluster_public_id"])
    off_post = _w18a_post(w18a_rows, disabled_id, on_disabled["cluster_public_id"])
    due_post = _w18a_post(w18a_rows, inside_id, not_due["cluster_public_id"])
    slotless_post = _w18a_post(w18a_rows, inside_id, slotless["cluster_public_id"])
    hold_post = _w18a_post(w18a_rows, inside_id, held_by_plan["cluster_public_id"])
    produce_post = _w18a_post(w18a_rows, inside_id, produced["cluster_public_id"])
    # The capped channel already has its one local-day post committed.
    _w18a_post(w18a_rows, capped_id, None, status="scheduled", scheduled_at=W18A_MORNING_UTC)

    _w18a_plan(
        w18a_rows,
        [goes_in, wrong_hour, over_cap, on_disabled, not_due, slotless, no_draft,
         held_by_plan, produced],
    )

    dry = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)
    predicted = {
        cluster: (W18A_PREDICTS[verdict], reason)
        for cluster, (verdict, reason) in _w18a_verdicts(dry).items()
    }
    real = plan_execution.apply_plan(now=W18A_MORNING_UTC)
    actual = _w18a_outcomes(real)

    assert dry["scanned"] == 9
    assert predicted == actual, "расхождение сухого прогона с настоящим"
    assert len(actual) == 9, "каждая запись плана отчитана с обеих сторон"
    assert dry["would_schedule"] == real["applied"] == 1
    assert dry["held"] == real["held"] == 5
    assert dry["reasons"] == real["reasons"]

    assert _w18a_post_state([moved_post])[0][1] == "scheduled", "предсказание сбылось"
    still_gated = [hour_post, cap_post, off_post, due_post, slotless_post, hold_post, produce_post]
    assert {row[1] for row in _w18a_post_state(still_gated)} == {"gated"}


# ------------------------------------------------------------------- the flag, honestly
def test_a_dry_run_with_the_flag_off_judges_the_plan_instead_of_refusing_it(
    w18a_rows, w18a_flag_off, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    """The whole point: the switch gates the writes, not the reading.

    `apply_plan` returns `_empty_result(flag_enabled=False)` with
    `plan_execution_disabled` counted once — that is the *write* path refusing.
    A dry run reports the same flag state and still names the guards that would
    have held each entry, so the operator can see the difference between a queue
    that is healthy and a queue that is stalled without first enabling the thing
    they are deciding about.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    channel_id = _w18a_channel(w18a_rows)
    disabled_id = _w18a_channel(w18a_rows, enabled=False)
    good = _w18a_entry(w18a_rows, channel_id)
    blocked = _w18a_entry(w18a_rows, disabled_id)
    good_post = _w18a_post(w18a_rows, channel_id, good["cluster_public_id"])
    blocked_post = _w18a_post(w18a_rows, disabled_id, blocked["cluster_public_id"])
    _w18a_plan(w18a_rows, [good, blocked])

    dry = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)

    assert dry["flag_enabled"] is False, "переключатель в отчёте — тот, что в настройках"
    assert dry["dry_run"] is True
    assert plan_execution.REASON_DISABLED not in dry["reasons"]
    assert _w18a_row(dry, good["cluster_public_id"])["verdict"] == (
        plan_execution.VERDICT_WOULD_SCHEDULE
    )
    assert _w18a_row(dry, blocked["cluster_public_id"])["reason"] == (
        plan_execution.REASON_CHANNEL_DISABLED
    )
    assert dry["reasons"] == {plan_execution.REASON_CHANNEL_DISABLED: 1}

    # Same rows, same clock, flag on: the verdicts must not move with the switch.
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    with_flag_on = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)
    assert _w18a_verdicts(with_flag_on) == _w18a_verdicts(dry)
    assert with_flag_on["flag_enabled"] is True

    # And the write path still refuses while the flag is off — the guard above is
    # not a weakening of it.
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "false")
    reset_settings_cache()
    refused = plan_execution.apply_plan(now=W18A_MORNING_UTC)
    assert refused["flag_enabled"] is False
    assert refused["reasons"] == {plan_execution.REASON_DISABLED: 1}
    assert refused["entries"] == []
    assert "dry_run" not in refused and "would_schedule" not in refused, (
        "обычный отчёт не выдаёт ключи сухого прогона"
    )

    for post_id in (good_post, blocked_post):
        assert _w18a_post_state([post_id])[0][1] == "gated"


def test_a_dry_run_with_no_active_plan_names_that_instead_of_an_empty_queue(
    w18a_flag_off, w18a_no_unpublish
):
    """No plan to judge: `scanned 0` would read as a healthy, empty queue."""
    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)

    assert report["no_plan"] is True
    assert report["plan_id"] is None
    assert report["entries"] == []
    assert report["scanned"] == 0
    assert report["would_schedule"] == 0
    assert report["reasons"] == {plan_execution.REASON_NO_PLAN: 1}


# --------------------------------------------------------------------- what was judged
def test_a_verdict_names_the_channel_post_and_slot_it_was_made_about(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    """The report must be checkable against the rows, so it says which rows it used.

    The channel counted is the gated post's channel: `_find_target` joins on the
    cluster, so a plan `channel_id` is the planner's note, not the measured row.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    channel_id = _w18a_channel(w18a_rows, timezone="Asia/Tashkent")
    entry = _w18a_entry(w18a_rows, channel_id)
    post_id = _w18a_post(w18a_rows, channel_id, entry["cluster_public_id"])
    _w18a_plan(w18a_rows, [entry])

    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)
    judged = _w18a_row(report, entry["cluster_public_id"])["judged"]

    assert judged["target_read"] is True
    assert judged["post_public_id"] == post_id
    assert judged["channel_id"] == channel_id
    assert judged["channel_platform"] == "telegram"
    assert judged["channel_timezone"] == "Asia/Tashkent"
    assert judged["plan_channel_id"] == channel_id
    assert judged["plan_platform"] == "telegram"
    assert judged["plan_format"] == "news"
    # The stored slot round-trips through SQLite without its UTC offset and
    # `_slot_at` re-attaches it when the decision is made, so the report carries
    # the plan's own string rather than a re-formatted one.
    assert judged["plan_slot_at"] == W18A_DUE_SLOT.replace(tzinfo=None).isoformat()
    assert report["judged_at"] == W18A_MORNING_UTC.isoformat()


def test_an_entry_that_reached_no_row_is_reported_as_not_looked_up(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    """An unmeasured thing is not a zero: `hold` never reached a post or a channel."""
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    channel_id = _w18a_channel(w18a_rows)
    entry = _w18a_entry(w18a_rows, channel_id, action="hold", slot_at=None)
    _w18a_plan(w18a_rows, [entry])

    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)
    row = _w18a_row(report, entry["cluster_public_id"])

    assert row["verdict"] == plan_execution.VERDICT_WOULD_SKIP
    assert row["reason"] == plan_execution.REASON_PLAN_HOLD
    assert row["judged"]["target_read"] is False
    assert row["judged"]["post_public_id"] is None
    assert row["judged"]["channel_id"] is None
    assert report["verdicts"] == {plan_execution.VERDICT_WOULD_SKIP: 1}


def test_the_limit_bounds_the_judgement_like_the_job_bounds_the_run(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish, monkeypatch
):
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    channel_id = _w18a_channel(w18a_rows)
    entries = [_w18a_entry(w18a_rows, channel_id) for _ in range(3)]
    for entry in entries:
        _w18a_post(w18a_rows, channel_id, entry["cluster_public_id"])
    _w18a_plan(w18a_rows, entries)

    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC, limit=2)

    assert report["scanned"] == 2
    assert len(report["entries"]) == 2
    assert report["would_schedule"] == 2, "записей проверено две, а не три"
    posts = _w18a_post_state(w18a_rows.post_ids)
    assert [row[1] for row in posts] == ["gated", "gated", "gated"]


# ------------------------------------------------------------------------- the CLI
def test_plan_dry_run_exits_zero_prints_the_reasons_and_says_it_changed_nothing(
    w18a_rows, w18a_flag_off, w18a_quiet_budget, w18a_switches_on, w18a_no_unpublish,
    monkeypatch, capsys
):
    """The operator's command, on the default flag, on the real reader.

    No `now` reaches this path: the CLI judges at the wall clock, so the slot is
    stated relative to it and the hour gate is taken out by the fake cadence.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    due_now = datetime.now(UTC) - timedelta(minutes=1)
    channel_id = _w18a_channel(w18a_rows)
    disabled_id = _w18a_channel(w18a_rows, enabled=False)
    good = _w18a_entry(w18a_rows, channel_id, slot_at=due_now)
    blocked = _w18a_entry(w18a_rows, disabled_id, slot_at=due_now)
    _w18a_post(w18a_rows, channel_id, good["cluster_public_id"])
    _w18a_post(w18a_rows, disabled_id, blocked["cluster_public_id"])
    _w18a_plan(w18a_rows, [good, blocked])

    posts_before = _w18a_post_state(w18a_rows.post_ids)
    journal_before = _w18a_journal_count()
    plan_before = _w18a_plan_state()

    assert cli.main(["plan-dry-run", "--limit", "5"]) == 0

    out = capsys.readouterr().out
    assert plan_execution.VERDICT_WOULD_SCHEDULE in out
    assert plan_execution.REASON_CHANNEL_DISABLED in out
    assert blocked["cluster_public_id"] in out
    assert json.loads(out[: out.index("\nпредпросмотр")])["dry_run"] is True
    assert "ни один пост не изменён" in out
    assert "PLAN_EXECUTION_ENABLED выключен" in out
    assert _w18a_post_state(w18a_rows.post_ids) == posts_before, "команда ничего не изменила"
    assert _w18a_plan_state() == plan_before
    assert _w18a_journal_count() == journal_before


def test_plan_dry_run_reports_a_missing_plan_instead_of_a_finished_one(
    w18a_flag_off, w18a_no_unpublish, capsys
):
    assert cli.main(["plan-dry-run"]) == 0
    out = capsys.readouterr().out
    assert plan_execution.REASON_NO_PLAN in out
    assert "активного плана нет" in out
    assert "ни одна строка не изменена" in out


# ------------------------------------------------------------------------ the wiring
def test_the_parser_wires_the_command_and_its_limit():
    parser = cli.build_parser()
    args = parser.parse_args(["plan-dry-run"])

    assert args.func is cli.cmd_plan_dry_run
    assert args.limit == plan_execution.MAX_ENTRIES_PER_RUN == cli.plan_entry_limit()
    assert parser.parse_args(["plan-dry-run", "--limit", "5"]).limit == 5


def test_the_new_command_is_documented_where_the_cli_documents_itself():
    assert "plan-dry-run" in cli.__doc__


def test_a_dry_run_never_reaches_a_transport(
    w18a_rows, w18a_flag_on, w18a_quiet_budget, w18a_switches_on, monkeypatch
):
    """Judging the queue is not sending anything: every transport is a trap here."""
    hits: list[str] = []

    def trap(name):
        def _explode(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"plan-dry-run дошёл до транспорта {name}")

        return _explode

    monkeypatch.setattr(plan_execution, "get_config", lambda: _w18a_config(None))
    monkeypatch.setattr(s7_publish, "_publish_one", trap("_publish_one"))
    monkeypatch.setattr(s7_publish, "publish_due", trap("publish_due"))
    monkeypatch.setattr(s7_publish, "run", trap("s7.run"))
    monkeypatch.setattr(s7_publish, "unpublish", trap("unpublish"))
    monkeypatch.setattr(s7_publish, "_publish_vk", trap("_publish_vk"))
    monkeypatch.setattr(s7_publish, "_publish_dzen", trap("_publish_dzen"))

    channel_id = _w18a_channel(w18a_rows)
    entry = _w18a_entry(w18a_rows, channel_id)
    post_id = _w18a_post(w18a_rows, channel_id, entry["cluster_public_id"])
    _w18a_plan(w18a_rows, [entry])

    report = plan_execution.evaluate_plan(now=W18A_MORNING_UTC)

    assert hits == []
    assert report["would_schedule"] == 1
    assert _w18a_post_state([post_id])[0][1] == "gated"

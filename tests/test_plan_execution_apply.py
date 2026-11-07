"""``apply_plan``: one plan entry at a time, and every refusal names itself.

The plan decides; this module is the consumer that makes the decision real —
behind five limits the acceptance list names (the channel's own cap counted in
the channel's *local* day, the channel's legal local hours, the platform's
publish capability, the ``DZEN_REQUIRES_LLM`` provenance gate, the daily AI
budget) plus the operator's approval mode. Each one has a test that fails when
the guard is deleted, so a guard cannot rot into a comment.

Nothing here publishes and nothing generates: ``tgfarm.s7_publish.unpublish`` is
the only write the module makes, and it is the scheduling path's own conditional
UPDATE.

Database hygiene: ``tests/conftest.py`` gives the whole suite one database, so
every channel, cluster, post and plan snapshot below is throwaway-named, counted
as a delta against its own baseline, and deleted again by the fixture teardown —
never by a table-wide count.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from tgfarm import plan_execution, s7_publish
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope

#: A moment whose UTC hour (04:30) is inside the test window in Tashkent (09:30)
#: and outside it in São Paulo (01:30): the same tick, two local answers.
MORNING_UTC = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)
LEGAL_WINDOW = [9, 18]


#: A slot that has already arrived for every test clock below: one minute before
#: the tick, so the release is due without being absurdly old.
DUE_SLOT = MORNING_UTC - timedelta(minutes=1)

#: "No slot passed" must stay distinct from "no slot at all" — an entry without a
#: planned slot is one of the reasons this module names.
_NOT_SET = object()

#: The instant W22-C gives `DUE_SLOT - 1min` on a São Paulo channel with the
#: window above: the 90-minute grid walked from the refused minute (01:29 local)
#: lands on 02:59, 04:29, 05:59, 07:29, 08:59 — all closed. The next cell, 10:29
#: local, is legal, but the hour 09:00-09:59 lies between two cells and no grid
#: point lands in it, so the walk now also tests the top of that hour: 09:00
#: local (12:00 UTC) is the first instant the window admits. Written out as a
#: literal because the test must state the number the planner owes, not
#: recompute it.
REBOUND_ILLEGAL_BOOKING = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _fake_config(active_hours: list[int] | None) -> SimpleNamespace:
    """A cadence of our own, so the local-hour tests do not ride the shipped YAML."""
    return SimpleNamespace(
        schedule=SimpleNamespace(
            cadence={"mode": "interval", "active_hours": active_hours},
            hours=lambda: list(range(24)),
            hour_probability=lambda hour: 1.0,
        )
    )


class _Rows:
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
                    delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
                )
                session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.cluster_ids:
                session.execute(
                    delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids))
                )
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))


@pytest.fixture()
def rows():
    owned = _Rows()
    yield owned
    owned.forget()


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def only_my_plan_is_active():
    """One reader, one snapshot: this file's own.

    `get_active_plan` orders by `created_at desc`, and the suite shares one
    database, so a snapshot another file left behind can win a same-second tie
    and make a test read a plan it never wrote. Foreign snapshots are parked for
    the duration of one test and put back afterwards — only their status moves.
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
def quiet_budget(monkeypatch):
    """The AI ledger as this file states it, so another file's rows cannot move it.

    `plan_execution` reads the budget through its owner, so the owner is patched
    here rather than a copy of the arithmetic.
    """
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 0)
    yield


@pytest.fixture()
def primary_switches_on(monkeypatch):
    """The operator's per-platform switches pinned to on.

    Those switches are `s7_publish`'s own gate and they have their own tests;
    here the fact under test is the plan's decision, so a destination must not
    fail for a reason the assertion does not name.
    """
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda platform: True)
    yield


def _channel(owned: _Rows, *, platform="telegram", timezone="Europe/Moscow",
             posts_per_day=5, enabled=True) -> str:
    channel_id = f"w17plan-ch-{_uid()}"
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


def _cluster(owned: _Rows) -> str:
    public_id = f"w17plan-cl-{_uid()}"
    owned.cluster_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Cluster(
                public_id=public_id,
                label=f"Тема w17plan {_uid()}",
                niche="",
                status="ready",
                heat=0.5,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    return public_id


def _post(owned: _Rows, channel_id: str, cluster_id: str | None = None, *,
          status="gated", provider="rule", scheduled_at=None) -> str:
    public_id = f"w17plan-post-{_uid()}"
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


def _plan(owned: _Rows, entries: list[dict], *, created_at: datetime | None = None) -> str:
    """One active snapshot with exactly the entries a test needs to describe."""
    public_id = f"w17plan-{_uid()}"
    owned.plan_public_ids.append(public_id)
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=public_id,
            created_at=created_at or datetime.now(UTC),
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


def _entry(owned: _Rows, channel_id: str, *, action="schedule",
           slot_at: datetime | None | object = _NOT_SET, platform="telegram") -> dict:
    return {
        "cluster_public_id": _cluster(owned),
        "action": action,
        "channel_id": channel_id,
        "platform": platform,
        "slot_at": DUE_SLOT if slot_at is _NOT_SET else slot_at,
    }


def _status(public_id: str) -> tuple[str, datetime | None, int]:
    with session_scope() as session:
        row = session.execute(
            select(Post.status, Post.scheduled_at, Post.attempts).where(
                Post.public_id == public_id
            )
        ).one()
    return str(row[0]), row[1], int(row[2] or 0)


def _aware(moment: datetime | None) -> datetime | None:
    """A stored stamp as an aware UTC instant, or `None`.

    SQLite hands `DateTime(timezone=True)` back naive, so a row written by the
    scheduling path and the plan's own aware `slot_at` are not comparable until
    the naive one is read as what the column means: UTC.
    """
    return None if moment is None else (moment if moment.tzinfo else moment.replace(tzinfo=UTC))


def _outcome(report: dict, cluster_id: str) -> dict:
    matches = [e for e in report["entries"] if e["cluster_public_id"] == cluster_id]
    assert len(matches) == 1, f"запись плана должна отчитаться одна: {cluster_id}"
    return matches[0]


# ------------------------------------------------------------------- the flag
def test_flag_off_leaves_a_gated_draft_gated(monkeypatch, rows):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "false")
    reset_settings_cache()
    try:
        channel_id = _channel(rows)
        cluster_id = _cluster(rows)
        post_id = _post(rows, channel_id, cluster_id)
        _plan(rows, [{"cluster_public_id": cluster_id, "action": "schedule",
                      "channel_id": channel_id, "slot_at": datetime.now(UTC)}])

        report = plan_execution.apply_plan()

        assert report["flag_enabled"] is False
        assert report["applied"] == 0
        assert report["reasons"] == {plan_execution.REASON_DISABLED: 1}
        assert _status(post_id)[0] == "gated", "выключенный флаг не имеет права ничего двигать"
    finally:
        reset_settings_cache()


def test_no_active_plan_says_so_instead_of_a_finished_run(flag_on):
    """Nothing to read: the run names that fact instead of reporting two zeros."""
    report = plan_execution.apply_plan()
    assert report["no_plan"] is True
    assert report["plan_id"] is None
    assert report["applied"] == 0
    assert report["entries"] == []
    # The summary of a run that had nothing to read must not read as completed
    # work: the reason is named, not implied by a pair of zeros.
    assert report["reasons"] == {plan_execution.REASON_NO_PLAN: 1}


# ------------------------------------------------------------- the hand-off path
def test_a_qualifying_draft_becomes_a_queued_post(flag_on, rows, quiet_budget):
    channel_id = _channel(rows)
    entry = _entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    before, scheduled_at_before, attempts_before = _status(post_id)
    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert before == "gated"
    assert report["applied"] == 1
    assert report["plan_id"] in rows.plan_public_ids
    assert _outcome(report, entry["cluster_public_id"])["outcome"] == "scheduled"
    status, scheduled_at, attempts = _status(post_id)
    assert status == "scheduled", "решение плана должно дойти до реальной очереди"
    assert scheduled_at is not None
    assert attempts == attempts_before == 0, "планирование не имеет права тратить попытки"
    assert scheduled_at_before is None


def test_the_slot_is_stamped_by_the_existing_path_with_the_plans_instant(
    flag_on, rows, quiet_budget, monkeypatch
):
    """`unpublish` owns the write; the instant it writes is the plan's `slot_at`.

    Two claims, and W21-B changed only the second one. The first still holds:
    the write is the scheduling path's conditional UPDATE, not a column this
    module touches — proved by the spy seeing the call, and by
    `test_plan_execution_flag.py::test_the_module_never_writes_a_post_state_by_hand`.
    The second is what this package exists for: the released row carries the
    minute the entry reserved, not the tick that happened to reach it. At base
    `unpublish` could only stamp `now`, so the assertion here was that the slot
    differed from the plan; that difference was one tick interval on every entry
    and it is the reason `/api/editorial/plan/booking` had nothing to report but
    `shifted` on the executor's path.
    """
    channel_id = _channel(rows)
    slot = MORNING_UTC - timedelta(minutes=7)
    entry = _entry(rows, channel_id, slot_at=slot)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    seen: list[tuple[list[str], datetime | None]] = []
    real_unpublish = s7_publish.unpublish

    def spy(public_ids, *, at=None):
        seen.append((list(public_ids), at))
        return real_unpublish(public_ids, at=at)

    monkeypatch.setattr(s7_publish, "unpublish", spy)
    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 1
    assert seen == [([post_id], slot)], "отдать пост должен существующий путь планирования"
    _, scheduled_at, _ = _status(post_id)
    assert _aware(scheduled_at).replace(microsecond=0) == slot.replace(microsecond=0), (
        "забронированная минута должна доехать до строки"
    )
    assert _aware(scheduled_at) <= MORNING_UTC, "исполнитель не ставит пост в будущее"


def test_a_future_slot_waits_instead_of_jumping_the_queue(flag_on, rows, quiet_budget):
    channel_id = _channel(rows)
    entry = _entry(rows, channel_id, slot_at=MORNING_UTC + timedelta(hours=3))
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 0
    assert _outcome(report, entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_SLOT_NOT_DUE
    )
    assert _status(post_id)[0] == "gated", "слот ещё не наступил — пост ждёт"


def test_a_schedule_entry_without_a_slot_is_held_not_guessed(flag_on, rows, quiet_budget):
    channel_id = _channel(rows)
    entry = _entry(rows, channel_id, slot_at=None)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert _outcome(report, entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_NO_SLOT
    )
    assert _status(post_id)[0] == "gated"


# ---------------------------------------------------------------- the limits
def test_a_disabled_channel_holds_its_slot(flag_on, rows, quiet_budget):
    channel_id = _channel(rows, enabled=False)
    entry = _entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert _outcome(report, entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_CHANNEL_DISABLED
    )
    assert _status(post_id)[0] == "gated", "выключенный канал не получает слота"


@pytest.mark.parametrize("platform", ["vc_ru", "rutube"])
def test_a_capability_below_publish_never_queues(flag_on, rows, quiet_budget, platform):
    """`draft_only` and `manual_upload` are human destinations: queueing is a lie."""
    from tgfarm.platform_capability import capability_of

    assert capability_of(platform) in ("draft_only", "manual_upload")
    channel_id = _channel(rows, platform=platform)
    entry = _entry(rows, channel_id, platform=platform)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert _outcome(report, entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_CAPABILITY
    )
    assert _status(post_id)[0] == "gated"


def test_the_local_day_cap_counts_that_channels_local_midnight(flag_on, rows, quiet_budget):
    """One post per day, and the day boundary is the channel's, not UTC's.

    The already-queued post sits in the *previous* UTC day and in the *same*
    local day as the tick, which is exactly the case a UTC-day count misses.
    """
    channel_id = _channel(rows, timezone="Asia/Tashkent", posts_per_day=1)
    # 2026-11-04 21:00 UTC == 2026-11-05 02:00 in Tashkent (UTC+5): same local day
    # as MORNING_UTC, different UTC day.
    earlier = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
    _post(rows, channel_id, None, status="scheduled", scheduled_at=earlier)
    entry = _entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=2))
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert _outcome(report, entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_DAILY_CAP
    )
    assert _status(post_id)[0] == "gated", "недельный лимит канала по местному дню исчерпан"


def test_two_entries_cannot_both_spend_one_remaining_slot(flag_on, rows, quiet_budget):
    """The cap is counted against the run, or two topics take one slot."""
    channel_id = _channel(rows, posts_per_day=2)
    _post(rows, channel_id, None, status="scheduled", scheduled_at=MORNING_UTC)
    first = _entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=3))
    second = _entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=2))
    third = _entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=1))
    _post(rows, channel_id, first["cluster_public_id"])
    _post(rows, channel_id, second["cluster_public_id"])
    _post(rows, channel_id, third["cluster_public_id"])
    _plan(rows, [first, second, third])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 1, "один слот дня — одна передача"
    reasons = {
        _outcome(report, entry["cluster_public_id"])["reason"]
        for entry in (second, third)
    }
    assert reasons == {plan_execution.REASON_DAILY_CAP}


def test_the_legal_local_hours_are_read_in_the_channels_zone(
    flag_on, rows, quiet_budget, monkeypatch
):
    """Same tick, two channels, two answers: only a local clock can do that.

    Window [9,18) local. At 04:30 UTC Tashkent is 09:30 (inside, queued) and
    São Paulo is 01:30 (outside, held). A UTC-hour implementation would give
    both channels the same verdict.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _fake_config(LEGAL_WINDOW))
    inside_id = _channel(rows, timezone="Asia/Tashkent")
    outside_id = _channel(rows, timezone="America/Sao_Paulo")
    inside_entry = _entry(rows, inside_id, slot_at=MORNING_UTC - timedelta(minutes=1))
    outside_entry = _entry(rows, outside_id, slot_at=MORNING_UTC - timedelta(minutes=1))
    inside_post = _post(rows, inside_id, inside_entry["cluster_public_id"])
    outside_post = _post(rows, outside_id, outside_entry["cluster_public_id"])
    _plan(rows, [inside_entry, outside_entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert _outcome(report, inside_entry["cluster_public_id"])["outcome"] == "scheduled"
    assert _outcome(report, outside_entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_LOCAL_HOURS
    )
    assert _status(inside_post)[0] == "scheduled"
    assert _status(outside_post)[0] == "gated", "нелегальный местный час не получает поста"


def test_an_illegal_booked_minute_is_held_while_the_tick_is_closed_and_rebound_when_it_opens(
    flag_on, rows, quiet_budget, monkeypatch
):
    """The hours gate judges the instant the row will carry — and since W22-C it
    replaces that instant instead of parking the entry forever.

    One entry, two ticks. At 04:30 UTC São Paulo is 01:30, so the *tick* is in an
    hour this channel may not publish in and the entry is held whatever its
    minute: that half of the old pin is still true, and it is the one W21-B grew,
    because a release makes the post due on the next publish tick and the farm
    would otherwise deliver at a closed hour.

    What W22-C superseded is the second half. The old test asserted that at 14:30
    UTC — 11:30 local, an open tick — the entry whose reserved minute is 04:29 UTC
    (01:29 local) was *still* held, under `REASON_LOCAL_HOURS`, with no slot on
    the row and no end to that state. That pin was wrong twice over: it described
    a refusal that never ends (`plan_slot_at` is written once and
    `s4_generation.apply_plan_booking` only claims rows whose booking is still
    NULL, so no later snapshot could reach the row), and it read the hours gate as
    a verdict instead of as a gate that also owes an answer to "then when". Since
    W22-C the minute the entry would stamp is corrected to the first instant this
    channel may publish in at or after the refused one — the planner's own walk,
    `editorial_plan.next_legal_slot_after`, floored at the refused minute rather
    than at the clock — and the entry is released on the open tick.

    The correction moves `scheduled_at`, the column the queue owns and that
    requeue and retry already move. It does not move what the plan reserved: the
    entry's own `slot_at` in the snapshot is asserted below to be the refused
    minute after the release, which is the same rule registry P100 states for
    `posts.plan_slot_at` and the reason the booking is still auditable. The
    release on the right-hand side is the positive control — a legal booked minute
    still goes through on the same tick, with that minute on the row, and it is
    not counted as a correction.
    """
    monkeypatch.setattr(plan_execution, "get_config", lambda: _fake_config(LEGAL_WINDOW))
    channel_id = _channel(rows, timezone="America/Sao_Paulo")
    illegal = _entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=1))
    illegal_post = _post(rows, channel_id, illegal["cluster_public_id"])
    later = MORNING_UTC + timedelta(hours=10)  # 14:30 UTC = 11:30 in São Paulo
    legal = _entry(rows, channel_id, slot_at=later - timedelta(minutes=1))
    legal_post = _post(rows, channel_id, legal["cluster_public_id"])
    _plan(rows, [illegal, legal])

    # --- the tick is closed: the refusal stands, and nothing is written.
    held = plan_execution.apply_plan(now=MORNING_UTC)
    assert _outcome(held, illegal["cluster_public_id"])["reason"] == (
        plan_execution.REASON_LOCAL_HOURS
    )
    assert _status(illegal_post)[0] == "gated"
    assert _status(illegal_post)[1] is None
    # A closed tick does not even get as far as a correction: `held` is the whole
    # report, so the journal of this pass names no rebound.
    assert held["applied"] == 0, held

    # --- the tick opens: the entry is released at a minute the channel may post.
    moved = plan_execution.apply_plan(now=later)
    entry = _outcome(moved, illegal["cluster_public_id"])
    assert entry["outcome"] == "scheduled", entry
    assert entry["reason"] == plan_execution.REASON_SLOT_REBOUND, (
        f"open tick on a refused minute: {entry}"
    )
    status, slot, _ = _status(illegal_post)
    assert status == "scheduled", status
    assert _aware(slot) == REBOUND_ILLEGAL_BOOKING, (
        f"01:29 local is closed and the walk owes the first open minute after it, "
        f"got {_aware(slot)}"
    )
    assert _aware(slot) != illegal["slot_at"], "the refused minute was stamped anyway"
    assert _aware(slot) > illegal["slot_at"], "a correction went backwards in time"
    assert plan_execution.local_hours_allow(_aware(slot), "America/Sao_Paulo") is True, (
        "the corrected instant is itself outside the window"
    )
    assert moved["reasons"].get(plan_execution.REASON_SLOT_REBOUND) == 1, moved["reasons"]
    assert moved["reasons"].get(plan_execution.REASON_LOCAL_HOURS, 0) == 0, moved["reasons"]

    # The plan is not what moved: the snapshot still reserves the refused minute.
    with session_scope() as session:
        reserved = session.execute(
            select(EditorialPlanEntry.slot_at).where(
                EditorialPlanEntry.cluster_public_id == illegal["cluster_public_id"]
            )
        ).scalar_one()
    assert _aware(reserved) == illegal["slot_at"], (
        f"the correction rewrote the plan's own reserved minute: {_aware(reserved)}"
    )

    # --- positive control: the legal entry is untouched by any of it.
    assert _outcome(moved, legal["cluster_public_id"])["outcome"] == "scheduled"
    assert _outcome(moved, legal["cluster_public_id"])["reason"] == "", (
        "a booked minute the window admits was reported as a correction"
    )
    status, slot, _ = _status(legal_post)
    assert status == "scheduled"
    assert _aware(slot) == legal["slot_at"]


def test_the_provenance_gate_holds_an_offline_draft(
    flag_on, rows, quiet_budget, primary_switches_on, monkeypatch
):
    """Dzen publishes no template text while DZEN_REQUIRES_LLM is on (default)."""
    monkeypatch.setitem(s7_publish.PRIMARY_PLATFORM_DAILY_CAPS, "dzen", 99)
    channel_id = _channel(rows, platform="dzen")
    entry = _entry(rows, channel_id, platform="dzen")
    post_id = _post(rows, channel_id, entry["cluster_public_id"], provider="offline")
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 0
    assert _outcome(report, entry["cluster_public_id"])["reason"] == (
        s7_publish.GATE_DZEN_NEEDS_ONLINE_MODEL
    )
    assert _status(post_id)[0] == "gated", "офлайн-черновик Дзена не становится слотом"


def test_the_same_draft_passes_once_the_provenance_gate_is_off(
    flag_on, rows, quiet_budget, primary_switches_on, monkeypatch
):
    """The hold above is the gate's doing, not an unrelated block."""
    monkeypatch.setitem(s7_publish.PRIMARY_PLATFORM_DAILY_CAPS, "dzen", 99)
    monkeypatch.setenv("DZEN_REQUIRES_LLM", "false")
    reset_settings_cache()
    try:
        channel_id = _channel(rows, platform="dzen")
        entry = _entry(rows, channel_id, platform="dzen")
        post_id = _post(rows, channel_id, entry["cluster_public_id"], provider="offline")
        _plan(rows, [entry])

        report = plan_execution.apply_plan(now=MORNING_UTC)

        assert s7_publish.GATE_DZEN_NEEDS_ONLINE_MODEL not in report["reasons"]
        assert _status(post_id)[0] == "scheduled"
    finally:
        reset_settings_cache()


def test_an_online_dzen_draft_is_not_blocked_by_the_gate(
    flag_on, rows, quiet_budget, primary_switches_on, monkeypatch
):
    monkeypatch.setitem(s7_publish.PRIMARY_PLATFORM_DAILY_CAPS, "dzen", 99)
    channel_id = _channel(rows, platform="dzen")
    entry = _entry(rows, channel_id, platform="dzen")
    post_id = _post(rows, channel_id, entry["cluster_public_id"], provider="openai")
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert _outcome(report, entry["cluster_public_id"])["outcome"] == "scheduled"
    assert _status(post_id)[0] == "scheduled"


def test_the_ai_budget_holds_the_fleet_once_the_day_is_spent(
    flag_on, rows, monkeypatch
):
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 7)
    monkeypatch.setenv("AI_DAILY_BUDGET", "7")
    reset_settings_cache()
    try:
        channel_id = _channel(rows)
        entry = _entry(rows, channel_id)
        post_id = _post(rows, channel_id, entry["cluster_public_id"])
        _plan(rows, [entry])

        report = plan_execution.apply_plan(now=MORNING_UTC)

        assert report["reasons"] == {plan_execution.REASON_BUDGET: 1}
        assert _status(post_id)[0] == "gated", "дневной бюджет ИИ исчерпан — день не обещаем"
    finally:
        reset_settings_cache()


def test_one_call_of_headroom_in_the_same_budget_queues_the_draft(
    flag_on, rows, monkeypatch
):
    """The guard above fires because of the budget, not because of a typo."""
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 7)
    monkeypatch.setenv("AI_DAILY_BUDGET", "8")
    reset_settings_cache()
    try:
        channel_id = _channel(rows)
        entry = _entry(rows, channel_id)
        post_id = _post(rows, channel_id, entry["cluster_public_id"])
        _plan(rows, [entry])

        report = plan_execution.apply_plan(now=MORNING_UTC)

        assert report["applied"] == 1
        assert _status(post_id)[0] == "scheduled"
    finally:
        reset_settings_cache()


def test_approval_mode_parks_the_post_for_a_human(flag_on, rows, quiet_budget, monkeypatch):
    monkeypatch.setenv("TGFARM_APPROVAL_REQUIRED", "true")
    reset_settings_cache()
    try:
        channel_id = _channel(rows)
        entry = _entry(rows, channel_id)
        post_id = _post(rows, channel_id, entry["cluster_public_id"])
        _plan(rows, [entry])

        report = plan_execution.apply_plan(now=MORNING_UTC)

        assert _outcome(report, entry["cluster_public_id"])["reason"] == (
            s7_publish.GATE_APPROVAL_REQUIRED
        )
        assert _status(post_id)[0] == "gated"
    finally:
        reset_settings_cache()


# ----------------------------------------------------------------- other actions
def test_a_produce_entry_never_generates_and_never_calls_a_model(
    flag_on, rows, quiet_budget, monkeypatch
):
    """`produce` is S4's decision, not this module's job: it reports, it does not write.

    Proved twice over — the generation entry points stay untouched, and the AI
    ledger gains no row, since a call to a model would spend the day's budget.
    """
    from tgfarm import s4_generation
    from tgfarm.db.models import AIUsage

    calls: list[str] = []

    def record(name):
        def _call(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"apply_plan не имеет права ничего генерировать: {name}")

        return _call

    monkeypatch.setattr(s4_generation, "run", record("s4_generation.run"))
    monkeypatch.setattr(s4_generation, "generate", record("s4_generation.generate"))

    channel_id = _channel(rows)
    entry = _entry(rows, channel_id, action="produce", slot_at=None)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    with session_scope() as session:
        ledger_before = int(
            session.execute(select(func.count()).select_from(AIUsage)).scalar_one()
        )

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert calls == [], "у produce нет безопасной точки входа в этот модуль"
    assert report["applied"] == 0
    assert _outcome(report, entry["cluster_public_id"]) == {
        "cluster_public_id": entry["cluster_public_id"],
        "action": "produce",
        "outcome": "skipped",
        "reason": plan_execution.REASON_NO_GENERATION,
    }
    assert _status(post_id)[:2] == ("gated", None)
    with session_scope() as session:
        ledger_after = int(
            session.execute(select(func.count()).select_from(AIUsage)).scalar_one()
        )
    assert ledger_after == ledger_before, "планирование не тратит бюджет ИИ"


def test_hold_and_produce_entries_are_never_touched(flag_on, rows, quiet_budget, monkeypatch):
    """`hold` stays a decision on paper; `produce` has no safe entry point here."""
    from tgfarm import s4_generation

    def explode(*args, **kwargs):
        raise AssertionError("apply_plan не имеет права ничего генерировать")

    monkeypatch.setattr(s4_generation, "run", explode)
    monkeypatch.setattr(s4_generation, "generate", explode)

    channel_id = _channel(rows)
    hold_entry = _entry(rows, channel_id, action="hold", slot_at=None)
    produce_entry = _entry(rows, channel_id, action="produce", slot_at=None)
    hold_post = _post(rows, channel_id, hold_entry["cluster_public_id"])
    produce_post = _post(rows, channel_id, produce_entry["cluster_public_id"])
    _plan(rows, [hold_entry, produce_entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 0
    assert _outcome(report, hold_entry["cluster_public_id"]) == {
        "cluster_public_id": hold_entry["cluster_public_id"],
        "action": "hold",
        "outcome": "skipped",
        "reason": plan_execution.REASON_PLAN_HOLD,
    }
    assert _outcome(report, produce_entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_NO_GENERATION
    )
    assert _status(hold_post)[:2] == ("gated", None), "hold не двигает пост вообще"
    assert _status(produce_post)[:2] == ("gated", None)


def test_an_entry_with_no_gated_draft_is_named_not_silently_dropped(
    flag_on, rows, quiet_budget
):
    cluster_id = _cluster(rows)
    _plan(rows, [{"cluster_public_id": cluster_id, "action": "schedule",
                  "channel_id": None, "slot_at": MORNING_UTC}])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 0
    assert _outcome(report, cluster_id)["reason"] == plan_execution.REASON_POST_MISSING


# ------------------------------------------------------------------- idempotency
def test_a_second_run_reports_the_post_the_first_one_queued(
    flag_on, rows, quiet_budget, monkeypatch
):
    channel_id = _channel(rows)
    entry = _entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    calls: list[list[str]] = []
    real_unpublish = s7_publish.unpublish

    def spy(public_ids, *, at=None):
        # W21-B widened the transition with the instant to stamp; the spy forwards
        # it unchanged so the write below is still `unpublish`'s own.
        calls.append(list(public_ids))
        return real_unpublish(public_ids, at=at)

    monkeypatch.setattr(s7_publish, "unpublish", spy)

    first = plan_execution.apply_plan(now=MORNING_UTC)
    _, slot_after_first, attempts_after_first = _status(post_id)
    second = plan_execution.apply_plan(now=MORNING_UTC)
    status_now, slot_now, attempts_now = _status(post_id)

    assert first["applied"] == 1
    assert second["applied"] == 0, "второй прогон не имеет права ничего ставить второй раз"
    assert _outcome(second, entry["cluster_public_id"])["outcome"] == "skipped"
    assert _outcome(second, entry["cluster_public_id"])["reason"] == (
        plan_execution.REASON_ALREADY_SCHEDULED
    )
    assert calls == [[post_id]], "передача должна случиться ровно один раз"
    assert status_now == "scheduled"
    assert slot_now == slot_after_first, "слот не переставляется каждым прогоном"
    assert attempts_now == attempts_after_first == 0


# ------------------------------------------------------------------ publish-free
def test_apply_plan_never_reaches_a_transport(flag_on, rows, quiet_budget, monkeypatch):
    """The module queues; it does not send, crosspost or open a browser."""
    hits: list[str] = []

    def trap(name):
        def _explode(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"apply_plan дошёл до транспорта {name}")

        return _explode

    monkeypatch.setattr(s7_publish, "_publish_one", trap("_publish_one"))
    monkeypatch.setattr(s7_publish, "publish_due", trap("publish_due"))
    monkeypatch.setattr(s7_publish, "run", trap("s7.run"))
    monkeypatch.setattr(s7_publish.TelegramPublisher, "send", trap("TelegramPublisher.send"))
    monkeypatch.setattr(s7_publish, "_publish_vk", trap("_publish_vk"))
    monkeypatch.setattr(s7_publish, "_publish_dzen", trap("_publish_dzen"))

    channel_id = _channel(rows)
    entry = _entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 1, "гейт выше должен ловить передачу, а не отменять её"
    assert hits == []
    status_now, _, _ = _status(post_id)
    assert status_now == "scheduled", "пост в очереди, но не опубликован этим модулем"

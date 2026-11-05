"""W21-A seam: the booking pass a self-running farm actually reaches.

`book_plan_slots` landed in `15afbe1` with its offline contract proven, and the
gap this file closes is that it had exactly one caller in the repository — the
stage `tests/test_autonomy_chain_offline.py` walks. Measured at `a6c9b46`,
`grep -rn book_plan_slots tgfarm/` returns the definition and nothing else: no
scheduler job, no CLI path, no cycle stage. So `python -m tgfarm.cli serve` with
`PLAN_EXECUTION_ENABLED=true` built the plan (`editorial-plan-build`), applied
its verdicts (`editorial-plan-apply`), and left `posts.plan_slot_at` NULL, which
is the column `schedule_ready_posts` has obeyed since `0d8fd6f` — the planner
re-derived a minute the plan had already decided, and the plan's booking died in
its own table.

The caller is `s4_generation.book_plan_tick`, registered as the scheduler's
third plan job. What follows is the claim that job makes, each half falsified:

* off `PLAN_EXECUTION_ENABLED` it answers before opening a session, so base
  behaviour stays byte-identical (and `pipeline` registers no such job at all —
  proven in `tests/test_w21a_booking_registration.py`);
* on the flag it books what the ledger says and journals what it really did,
  with counters that are measurement rather than placeholders (P34);
* a database failure is merged the way the two neighbouring ticks merge it —
  `log.warning`, an `error` journal row, a report naming the exception — so a
  rot in the ledger cannot take the scheduler down with it;
* it calls nothing that publishes, hands a row over, or rebuilds a plan. That
  half is checked twice: once by spies that fail the test if those functions are
  reached at all, once against the rows they would have changed.

Database hygiene: the suite shares one session database (`tests/conftest.py`),
so every channel, post and plan here is `w21ab_`-prefixed, remembered by the
`owned` holder and deleted again in teardown; foreign ACTIVE plans are
superseded for the duration of a test because `get_active_plan()` reads the whole
table, and every counter is compared against the same tick's own report or an
own-baseline delta — never an absolute number of shared rows.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from tgfarm import editorial, plan_execution, s4_generation, s7_publish
from tgfarm.core.run import ensure_utc
from tgfarm.db.models import Channel, EditorialPlan, EditorialPlanEntry, PipelineRun, Post
from tgfarm.db.session import session_scope

#: The instant the plan reserves. Second-precision on purpose: the booked slot is
#: compared exactly, so a pass that shifted the plan's minute by a minute is red.
_BOOKED_SLOT = datetime(2027, 3, 9, 6, 15, tzinfo=UTC)


class _Owned:
    """Rows this file creates, named so they are the only rows it deletes."""

    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.post_public_ids: list[str] = []
        self.plan_public_ids: list[str] = []


@pytest.fixture()
def owned():
    holder = _Owned()
    yield holder
    with session_scope() as session:
        # The tick's own journal rows first: they are identified by the job id in
        # `meta`, which no other writer in the repository puts there, so this
        # removes exactly what these tests wrote and never a chain's row.
        journal_ids = [
            int(row.id)
            for row in session.execute(
                select(PipelineRun.id, PipelineRun.meta).where(
                    PipelineRun.stage == s4_generation.STAGE_BOOKING
                )
            ).all()
            if (row.meta or {}).get("job") == s4_generation.PLAN_BOOKING_JOB_ID
        ]
        if journal_ids:
            session.execute(delete(PipelineRun).where(PipelineRun.id.in_(journal_ids)))
        plan_ids = (
            list(
                session.execute(
                    select(EditorialPlan.id).where(
                        EditorialPlan.public_id.in_(holder.plan_public_ids)
                    )
                ).scalars().all()
            )
            if holder.plan_public_ids
            else []
        )
        if plan_ids:
            session.execute(
                delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
            )
            session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
        if holder.post_public_ids:
            session.execute(delete(Post).where(Post.public_id.in_(holder.post_public_ids)))
        if holder.channel_ids:
            session.execute(delete(Channel).where(Channel.id.in_(holder.channel_ids)))


@pytest.fixture()
def switch(monkeypatch):
    """Set `PLAN_EXECUTION_ENABLED` for one test and put the cache back."""
    from tgfarm.core.settings import reset_settings_cache

    def _set(enabled: bool) -> None:
        monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if enabled else "false")
        reset_settings_cache()

    yield _set
    reset_settings_cache()


@pytest.fixture()
def no_foreign_active_plan():
    """`get_active_plan()` reads the table; a neighbour's plan must not win."""
    with session_scope() as session:
        foreign = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.status == "active")
            ).scalars().all()
        )
        for plan_id in foreign:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in foreign:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _channel(holder: _Owned) -> str:
    channel_id = f"w21ab_ch_{_uid()}"
    holder.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title="Канал W21-A",
                niche="business",
                enabled=True,
                posts_per_day=5,
                timezone="Europe/Moscow",
            )
        )
    return channel_id


def _open_order(holder: _Owned) -> tuple[str, str]:
    """One topic the plan ordered, drafted and never answered: a ledger entry.

    The draft is `gated` with `scheduled_at` and `plan_slot_at` both NULL and a
    `plan_entry_id` set — precisely what `editorial._open_order_conditions`
    selects and what `apply_plan_booking` claims. The entry carries the minute,
    which is the only thing this package is allowed to copy onto the row.
    """
    channel_id = _channel(holder)
    cluster_id = f"w21ab_cl_{_uid()}"
    post_id = f"w21ab_po_{_uid()}"
    holder.post_public_ids.append(post_id)
    with session_scope() as session:
        plan_public_id = f"w21ab_pl_{_uid()}"
        holder.plan_public_ids.append(plan_public_id)
        plan = EditorialPlan(
            public_id=plan_public_id,
            status="active",
            horizon_days=3,
            summary={},
            created_at=datetime.now(UTC),
        )
        session.add(plan)
        session.flush()
        entry = EditorialPlanEntry(
            plan_id=plan.id,
            cluster_public_id=cluster_id,
            channel_id=channel_id,
            action="schedule",
            title="ЦБ сохранил ставку",
            niche="business",
            platform="telegram",
            format="news",
            slot_at=_BOOKED_SLOT,
            priority=5,
            confidence=0.6,
            reasons=["w21a seam"],
        )
        session.add(entry)
        session.flush()
        session.add(
            Post(
                public_id=post_id,
                channel_id=channel_id,
                cluster_public_id=cluster_id,
                hook="ЦБ сохранил ставку",
                body="Регулятор оставил ключевую ставку на уровне 19 процентов.",
                status="gated",
                plan_entry_id=int(entry.id),
                plan_slot_at=None,
                scheduled_at=None,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    return post_id, cluster_id


def _post_row(public_id: str) -> dict:
    with session_scope() as session:
        row = session.execute(
            select(Post.status, Post.scheduled_at, Post.plan_slot_at, Post.published_at).where(
                Post.public_id == public_id
            )
        ).one()
    return {"status": row[0], "scheduled_at": row[1], "plan_slot_at": row[2], "published_at": row[3]}


def _count(model) -> int:
    with session_scope() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _booking_journal() -> list[PipelineRun]:
    """Journal rows written by *this package's* tick, oldest first.

    Filtered on `meta["job"]` rather than on the stage name: `s4_plan_booking` is
    the stage the offline chain also writes, and a test that read those rows
    would be measuring somebody else's pass. The `owned` teardown removes
    exactly these rows.
    """
    with session_scope() as session:
        rows = list(
            session.execute(
                select(PipelineRun)
                .where(PipelineRun.stage == s4_generation.STAGE_BOOKING)
                .order_by(PipelineRun.id)
            ).scalars().all()
        )
    return [
        row for row in rows if (row.meta or {}).get("job") == s4_generation.PLAN_BOOKING_JOB_ID
    ]


def _block_the_publishers(monkeypatch) -> list[str]:
    """Record every reach toward a publisher, a hand-over or a plan rebuild.

    These are the three things the booking job must not do. Recording instead of
    raising keeps the failure readable; the test that installs the spies still
    asserts against the rows those calls would have moved.
    """
    reached: list[str] = []

    def _sync_spy(name: str, result):
        def _fake(*_args, **_kwargs):
            reached.append(name)
            return result

        return _fake

    def _async_spy(name: str):
        async def _fake(*_args, **_kwargs):
            reached.append(name)
            return {}

        return _fake

    monkeypatch.setattr(s7_publish, "publish_due", _sync_spy("publish_due", []))
    monkeypatch.setattr(s7_publish, "unpublish", _sync_spy("unpublish", None))
    monkeypatch.setattr(
        s7_publish, "schedule_ready_posts", _sync_spy("schedule_ready_posts", {"scheduled": 0})
    )
    monkeypatch.setattr(s7_publish, "run", _async_spy("s7_publish.run"))
    monkeypatch.setattr(
        plan_execution, "apply_plan", _sync_spy("apply_plan", {"applied": 0, "entries": []})
    )
    monkeypatch.setattr(plan_execution, "build_plan_job", _sync_spy("build_plan_job", {}))
    monkeypatch.setattr(
        plan_execution, "build_plan_tick", _async_spy("build_plan_tick"), raising=False
    )
    return reached


# ------------------------------------------------------------------ flag off
@pytest.mark.asyncio
async def test_flag_off_tick_answers_before_it_opens_a_session(owned, switch, monkeypatch):
    """Off the switch the tick is a return statement, as its two neighbours are.

    `book_plan_slots` is not reached at all, so neither the ledger read nor the
    one conditional UPDATE it would issue can happen — which is what keeps a
    flag-off farm byte-identical to base rather than merely unwritten.
    """
    post_id, _cluster_id = _open_order(owned)
    switch(False)
    reached = _block_the_publishers(monkeypatch)

    def _no_booking(*_args, **_kwargs):
        reached.append("book_plan_slots")
        return {"considered": 0, "booked": 0, "states": {}, "decisions": []}

    monkeypatch.setattr(s4_generation, "book_plan_slots", _no_booking)
    report = await s4_generation.book_plan_tick()

    assert report == {"skipped": s4_generation.PLAN_BOOKING_DISABLED}, report
    assert reached == [], "flag off must not reach the booking pass or anything behind it"
    assert _booking_journal() == [], "a skipped pass may not leave a journal row"
    row = _post_row(post_id)
    assert row["plan_slot_at"] is None, f"flag off wrote a booked slot: {row}"
    assert row["status"] == "gated"


@pytest.mark.asyncio
async def test_flag_off_tick_needs_no_seeded_rows_to_refuse(switch):
    """The off branch never touches a session, so nothing is set up here at all.

    Asserted as a call with an empty holder: a refusal that had to read the
    ledger first would be a different — and slower, and write-capable — thing.
    """
    switch(False)
    assert await s4_generation.book_plan_tick() == {"skipped": s4_generation.PLAN_BOOKING_DISABLED}


# ------------------------------------------------------------------ flag on
@pytest.mark.asyncio
async def test_flag_on_tick_books_the_open_order_it_finds(owned, switch, no_foreign_active_plan):
    """The whole point of the job: the plan's minute reaches the draft's row."""
    post_id, _cluster_id = _open_order(owned)
    switch(True)
    report = await s4_generation.book_plan_tick()

    assert report["booked"] >= 1, report
    row = _post_row(post_id)
    assert row["plan_slot_at"] is not None, (
        "the tick ran but left the slot NULL — the gap this package closes is still open"
    )
    assert ensure_utc(row["plan_slot_at"]) == _BOOKED_SLOT, (
        f"the booking moved the plan's own minute: {row['plan_slot_at']!r} != {_BOOKED_SLOT!r}"
    )


@pytest.mark.asyncio
async def test_booking_a_slot_is_not_a_handover_and_not_a_publication(
    owned, switch, no_foreign_active_plan, monkeypatch
):
    """`plan_slot_at` only. Status, `scheduled_at` and `published_at` stay as found.

    A tick that also queued or shipped the row would be a second scheduler, and
    this package is not that: the publisher's own gates decide the rest.
    """
    post_id, _cluster_id = _open_order(owned)
    switch(True)
    reached = _block_the_publishers(monkeypatch)
    posts_before = _count(Post)

    report = await s4_generation.book_plan_tick()

    assert report["booked"] >= 1, report
    assert reached == [], f"the booking tick reached a publisher or a plan rebuild: {reached}"
    row = _post_row(post_id)
    assert row["status"] == "gated", f"the tick moved the status: {row}"
    assert row["scheduled_at"] is None, f"the tick handed the row over: {row}"
    assert row["published_at"] is None, f"the tick published the row: {row}"
    assert _count(Post) == posts_before, "a booking pass may not create or delete a post"


@pytest.mark.asyncio
async def test_the_tick_journals_what_the_pass_really_returned(
    owned, switch, no_foreign_active_plan
):
    """One row per pass, under the stage the cycle already uses for this work.

    `items_in`/`items_out` are compared against the same tick's own report — not
    against a shared-database total — because the ledger is global and this file
    cannot know how many open orders a neighbouring suite left in it.
    """
    _open_order(owned)
    switch(True)
    report = await s4_generation.book_plan_tick()

    rows = _booking_journal()
    assert len(rows) == 1, f"one pass, one journal row: {rows}"
    row = rows[0]
    assert row.status == "ok"
    assert (row.items_in, row.items_out) == (report["considered"], report["booked"])
    assert row.error is None
    meta = row.meta or {}
    assert meta["job"] == s4_generation.PLAN_BOOKING_JOB_ID
    assert meta["states"] == report["states"]
    assert meta["counters"] == "measured", "a measured pass must not look like a placeholder"
    assert "unmeasured" not in meta


@pytest.mark.asyncio
async def test_an_empty_ledger_is_a_measured_zero_not_a_skipped_pass(owned, switch, monkeypatch):
    """P34, restated for this job: "looked, found nothing" is a measurement.

    `apply_plan_tick` has a reason to answer `skipped` with unmeasured counters —
    there was no plan to read. Here the ledger read completed and came back
    empty, which is a different fact and has to read as one. The ledger is stubbed
    to empty because neighbouring suites leave real open orders in the shared
    database, and an absolute `considered == 0` would be their number, not this
    test's.
    """
    switch(True)
    monkeypatch.setattr(editorial, "open_order_cluster_ids", lambda _limit=40: [])

    report = await s4_generation.book_plan_tick()
    assert report == {"considered": 0, "booked": 0, "states": {}, "decisions": []}, report

    rows = _booking_journal()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.status == "ok"
    assert (row.items_in, row.items_out) == (0, 0)
    assert (row.meta or {}).get("counters") == "measured"
    assert "unmeasured" not in (row.meta or {})


@pytest.mark.asyncio
async def test_a_failure_is_merged_the_way_the_neighbouring_ticks_merge_it(
    owned, switch, monkeypatch
):
    """A broken ledger must not raise into APScheduler and take the job with it.

    The two existing plan ticks answer a failure with a warning, an `error`
    journal row whose counters say nobody measured, and a report naming the
    exception. This is the same three sentences, so an operator reading the
    journal cannot tell which of the three plan jobs broke — only that one did.
    """
    switch(True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("w21a ledger unreadable")

    monkeypatch.setattr(s4_generation, "book_plan_slots", _boom)
    report = await s4_generation.book_plan_tick()

    assert report == {"error": "RuntimeError: w21a ledger unreadable"}, report
    rows = _booking_journal()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.status == "error"
    assert "RuntimeError" in (row.error or "")
    meta = row.meta or {}
    assert meta["job"] == s4_generation.PLAN_BOOKING_JOB_ID
    assert meta["counters"] == "not_reported"
    assert sorted(meta.get("unmeasured") or []) == ["items_in", "items_out"]


@pytest.mark.asyncio
async def test_the_limit_reaches_the_pass_it_wraps(owned, switch, monkeypatch):
    """`limit` is the caller's, not a default the wrapper forgot to forward.

    Without this a tick that dropped the keyword would still book everything and
    nobody would notice until one pass cost forty plan reads.
    """
    _open_order(owned)
    switch(True)
    seen: list[int] = []

    def _fake_slots(*_args, **kwargs):
        seen.append(int(kwargs.get("limit", -1)))
        return {"considered": 0, "booked": 0, "states": {}, "decisions": []}

    monkeypatch.setattr(s4_generation, "book_plan_slots", _fake_slots)
    await s4_generation.book_plan_tick(limit=7)
    assert seen == [7], f"the tick did not forward its limit: {seen}"


# ------------------------------------------------------------------- the shape
def test_the_tick_is_a_wrapper_that_reaches_no_publisher_or_plan_builder():
    """Names the compiled body actually resolves, not names its prose mentions.

    `co_names` is every global and attribute the function can touch, so a future
    edit that reaches the publisher through a name the spy list has not thought
    of still lands here — and the docstring, which legitimately *talks* about
    `schedule_ready_posts`, cannot make this red by accident.
    """
    reachable = set(s4_generation.book_plan_tick.__code__.co_names) | set(
        s4_generation._journal_booking_tick.__code__.co_names
    )
    for forbidden in (
        "publish_due",
        "schedule_ready_posts",
        "unpublish",
        "s7_publish",
        "apply_plan",
        "build_plan_job",
        "build_plan_tick",
        "build_editorial_plan",
        "persist_draft",
        "generate",
        "run",
        "httpx",
        "requests",
        "send_message",
        "VK_USER_TOKEN",
    ):
        assert forbidden not in reachable, (
            f"the booking tick can reach {forbidden}: its whole job is one column"
        )
    assert "book_plan_slots" in reachable, "the tick is not the wrapper this file tests"


def test_the_journal_stage_is_the_stage_the_work_is_measured_under():
    """One name for one piece of work, whoever runs it.

    The cycle times this pass with `StageTimer(STAGE_BOOKING)`; the tick journals
    under the same constant. Two spellings of one stage would split the
    operator's view of the same ledger into halves that neither says is missing.
    """
    assert s4_generation.STAGE_BOOKING == "s4_plan_booking"
    assert "StageTimer(STAGE_BOOKING)" in inspect.getsource(s4_generation.book_plan_slots)

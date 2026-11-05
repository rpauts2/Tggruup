"""W22-D: the release gate walks the plan booking pass and names its numbers.

Wave 21 registered `book_plan_tick` as the scheduler's third plan job and left
the offline gate with `booked: 0` — true, but unmeasured: the demo never ran the
pass, so its zero said nothing about booking. These tests hold the finished stage
to five things, read back from the store the demo wrote rather than from its own
report where that is possible:

* on the throwaway store `tgfarm demo` owns, the pass really runs, through the
  scheduler's own entry point, and leaves the scheduler's own journal row;
* what it returned is reported — and a pass that booked nothing says why in the
  state names the pass itself chose, so a measured zero never reads as a stall;
* on a store the demo does not own the pass is not run, and the counters of a
  stage that never measured stay absent instead of arriving as invented zeros;
* a pass that could not look, and a pass that raised, are gate failures: those
  two words have to be distinguishable from "the plan had nothing to book";
* the stage sits where the chain needs it — after the draft exists and before the
  scheduler decides — and it never, in any of these shapes, publishes anything.

Shared-database discipline: every test runs on its own throwaway file, as the
other demo tests do.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgfarm import s4_generation  # noqa: E402
from tgfarm.core import settings as settings_mod  # noqa: E402
from tgfarm.db import session as session_mod  # noqa: E402

#: every booking counter the stage trail has to carry; a trail that drops one is
#: caught here rather than in the report an operator reads
BOOKING_KEYS = (
    "booking_mode",
    "booking_status",
    "booking_job",
    "booking_flag",
    "booking_note",
    "booking_considered",
    "booking_booked",
    "booking_states",
)

#: the counters a pass that never measured may not invent a zero for
BOOKING_COUNTER_KEYS = ("booking_considered", "booking_booked", "booking_states")


@pytest.fixture()
def w22d_db(tmp_path):
    """Point the engine at a throwaway file, then hand the session DB back.

    Deliberately leaves ``PLAN_EXECUTION_ENABLED`` off: the writing run has to
    force it itself, and a fixture that set it would hide whether it did.
    """

    def _reset_caches() -> None:
        from tgfarm.llm import client as client_mod
        from tgfarm.llm import embedding as embedding_mod
        from tgfarm.llm import images as images_mod

        settings_mod.reset_settings_cache()
        session_mod.dispose_engine()
        client_mod.reset_llm()
        embedding_mod.reset_embedder()
        images_mod._PROVIDER = None  # noqa: SLF001 - the cache is module state

    previous_url = os.environ.get("DATABASE_URL")
    previous_media = os.environ.get("TGFARM_MEDIA_DIR")
    previous_flag = os.environ.get("PLAN_EXECUTION_ENABLED")
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'w22d.db').as_posix()}"
    os.environ["TGFARM_MEDIA_DIR"] = str(tmp_path / "media")
    os.environ["PLAN_EXECUTION_ENABLED"] = "false"
    _reset_caches()
    try:
        yield tmp_path
    finally:
        for name, value in (
            ("DATABASE_URL", previous_url),
            ("TGFARM_MEDIA_DIR", previous_media),
            ("PLAN_EXECUTION_ENABLED", previous_flag),
        ):
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        _reset_caches()


class _RefusingTransport:
    """A transport that records that it was asked and then refuses to send."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    def configured(self) -> bool:
        return False

    async def __call__(self, *args, **kwargs):
        self._calls.append(self.name)
        raise AssertionError(f"the booking stage tried to use a publish transport: {self.name}")

    async def publish(self, payload):  # s9 crosspost adapters
        self._calls.append(f"{self.name}.publish")
        raise AssertionError(f"the booking stage tried to use a crosspost transport: {self.name}")


@pytest.fixture()
def w22d_transports_locked(monkeypatch):
    """Booking writes one column; it must not become a delivery stage."""
    from tgfarm import s7_publish
    from tgfarm.s9_growth import adapters as s9_adapters

    calls: list[str] = []
    monkeypatch.setattr(s7_publish.PUBLISHER, "send", _RefusingTransport("telegram", calls))
    monkeypatch.setattr(s7_publish, "_publish_vk", _RefusingTransport("vk", calls))
    monkeypatch.setattr(s7_publish, "_publish_dzen", _RefusingTransport("dzen", calls))
    monkeypatch.setattr(
        s9_adapters,
        "ADAPTERS",
        {name: _RefusingTransport(name, calls) for name in s9_adapters.ADAPTERS},
        raising=False,
    )
    return calls


def _demo_db(report: dict) -> Path:
    """The throwaway file the demo just wrote, resolved from its own report."""
    store = report["store"]
    assert store["kind"] == "throwaway", store
    path = Path(store["path"]) / "tgfarm-demo.db"
    assert path.exists(), f"the demo reported a store that is not there: {path}"
    return path


def _rows(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _journal(db: Path, stage: str, columns: str) -> list[dict]:
    return _rows(
        db,
        f"SELECT {columns}, meta FROM pipeline_runs WHERE stage = ? ORDER BY id",
        (stage,),
    )


# ------------------------------------------------------------------- the walked pass
def test_the_gate_walks_the_booking_pass_and_carries_its_numbers(
    w22d_db, w22d_transports_locked
) -> None:
    """`tgfarm demo` runs the pass on the store it owns and reports what it said."""
    from tgfarm import demo

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))
    stages = report["cycle0_stages"]

    missing = [key for key in BOOKING_KEYS if key not in stages]
    assert not missing, f"the stage trail carries no booking: missing {missing}"

    assert stages["booking_mode"] == "walked", stages
    assert stages["booking_status"] == "ran", stages
    assert stages["booking_job"] == s4_generation.PLAN_BOOKING_JOB_ID, stages
    assert stages["booking_flag"] is True, stages
    # the demo corpus gives the plan produce entries, so the pass looks at open
    # orders and reserves none: the honest output is a counted walk, not a silence
    assert stages["booking_considered"] >= 1, stages
    assert stages["booking_booked"] == 0, stages
    assert stages["booking_states"], stages
    assert sum(stages["booking_states"].values()) == stages["booking_considered"], stages
    for state in stages["booking_states"]:
        assert state in s4_generation.PLAN_BOOKING_STATES, f"unknown booking state {state}"

    note = stages["booking_note"]
    assert "measured zero" in note, f"a zero booking is not stated as a measurement: {note}"
    assert "no_schedule_entry" in note, f"the pass's own reason is missing: {note}"

    # and a zero the pass explained is not this cycle's failure
    assert report["verdict"]["ok"] is True, report["verdict"]
    assert "booking" in report["verdict"], report["verdict"]
    assert "booking" not in " ".join(report["verdict"]["failures"]).lower()

    # the walk was over the store's own rows: every order it considered is a post
    # the plan wrote an entry id on, and the pass cannot consider more topics than
    # the store holds. Read after the run, so `<=` is the honest direction: S7 may
    # have taken some of those drafts off the open list while the cycle went on.
    ordered = _rows(
        _demo_db(report),
        "SELECT COUNT(DISTINCT cluster_public_id) AS c FROM posts WHERE plan_entry_id IS NOT NULL",
    )[0]["c"]
    assert 1 <= stages["booking_considered"] <= ordered, (stages, ordered)

    # §13.1 unchanged: walking the pass delivered nothing, anywhere
    assert w22d_transports_locked == [], w22d_transports_locked
    assert report["cycle0_publish"]["published"] == 0, report["cycle0_publish"]


def test_the_walked_pass_leaves_the_schedulers_own_journal_row(w22d_db) -> None:
    """The gate drives `book_plan_tick`, not a copy of it: the journal proves it.

    A stage that re-implemented the pass could print the same numbers while
    writing none of the same rows; the row this test reads is the one only the
    tick's own `_journal_booking_tick` produces.
    """
    from tgfarm import demo

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))
    stages = report["cycle0_stages"]
    rows = _journal(_demo_db(report), s4_generation.STAGE_BOOKING, "status, items_in, items_out")

    assert rows, "the demo booked nothing into the journal: the pass never ran"
    assert len(rows) == 1, f"one cycle walked the pass {len(rows)} times: {rows}"
    row = rows[0]
    assert row["status"] == "ok", row
    assert row["items_in"] == stages["booking_considered"], (row, stages)
    assert row["items_out"] == stages["booking_booked"], (row, stages)
    meta = json.loads(row["meta"] or "{}")
    assert meta["job"] == s4_generation.PLAN_BOOKING_JOB_ID, meta
    assert meta["states"] == stages["booking_states"], meta


def test_the_booking_stage_runs_before_the_scheduler(w22d_db, monkeypatch) -> None:
    """Position matters: the scheduler must be handed the minute in the same cycle.

    A pass that ran after `schedule_ready_posts` could only ever book a slot the
    queue had already re-derived from cadence, which is the gap wave 21 closed.
    """
    from tgfarm import demo, s7_publish

    order: list[str] = []
    real_schedule = s7_publish.schedule_ready_posts
    real_tick = s4_generation.book_plan_tick

    def _spy_schedule(limit, blocked=None):
        order.append("schedule")
        return real_schedule(limit, blocked)

    async def _spy_tick(*, limit: int = s4_generation.MAX_BOOKINGS_PER_RUN):
        order.append("booking")
        return await real_tick(limit=limit)

    monkeypatch.setattr(s7_publish, "schedule_ready_posts", _spy_schedule)
    monkeypatch.setattr(s4_generation, "book_plan_tick", _spy_tick)

    asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))

    assert order[:2] == ["booking", "schedule"], order


def test_the_walked_trail_is_identical_on_two_identical_runs(w22d_db) -> None:
    """The booking counters join the deterministic trail, ids and instants aside."""
    from tgfarm import demo

    trails = [
        json.dumps(
            asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))[
                "cycle0_stages"
            ],
            sort_keys=True,
        )
        for _ in range(2)
    ]

    assert trails[0] == trails[1], "the booking stage made the trail depend on the clock"
    assert json.loads(trails[0])["booking_status"] == "ran"


# ------------------------------------------------------------------ a real booking
def _seed_open_order(*, action: str = "schedule", slot_at: datetime | None) -> tuple[str, str]:
    """One plan entry and the gated draft it ordered, on the store the caller owns.

    Written through the real ORM, so the stage meets the same rows the executor
    would leave behind: `plan_entry_id` is what makes an order open at all.
    """
    from tgfarm.db.bootstrap import ensure_bootstrapped
    from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
    from tgfarm.db.session import session_scope

    ensure_bootstrapped()
    stamp = datetime.now(UTC).strftime("%H%M%S%f")
    channel_id = f"w22d-ch-{stamp}"
    cluster_id = f"w22d-cl-{stamp}"
    post_id = f"w22d-post-{stamp}"
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title=channel_id,
                niche="",
                enabled=True,
                timezone="Europe/Moscow",
                posts_per_day=5,
            )
        )
        session.add(
            Cluster(
                public_id=cluster_id,
                label="Тема w22d",
                niche="",
                status="ready",
                heat=0.5,
                last_seen_at=datetime.now(UTC),
            )
        )
        plan = EditorialPlan(
            public_id=f"w22d-plan-{stamp}",
            created_at=datetime.now(UTC),
            horizon_days=7,
            status="active",
            summary={"total": 1},
        )
        session.add(plan)
        session.flush()
        session.add(
            EditorialPlanEntry(
                plan_id=plan.id,
                cluster_public_id=cluster_id,
                title="Тема",
                niche="",
                action=action,
                platform="telegram",
                channel_id=channel_id,
                format="news",
                priority=1,
                confidence=0.5,
                reasons=["тест W22-D"],
                slot_at=slot_at,
                payload={},
            )
        )
        session.add(
            Post(
                public_id=post_id,
                channel_id=channel_id,
                cluster_public_id=cluster_id,
                hook="Заголовок поста, который план просит забронировать.",
                body="Тело поста; гейт его пропустил, слота ещё нет.",
                status="gated",
                generation_provider="offline-template",
                plan_entry_id=1,
                created_at=datetime.now(UTC),
            )
        )
    return cluster_id, post_id


def _switch(value: bool) -> None:
    os.environ["PLAN_EXECUTION_ENABLED"] = "true" if value else "false"
    settings_mod.reset_settings_cache()


def test_a_minute_the_plan_named_reaches_the_row_through_the_stage(w22d_db) -> None:
    """The stage is not decoration: a `schedule` entry really books through it."""
    from tgfarm import demo

    slot = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    cluster_id, post_id = _seed_open_order(slot_at=slot)
    _switch(True)

    look = asyncio.run(demo._booking_look(writes=True))

    assert look["status"] == "ran", look
    assert look["considered"] == 1 and look["booked"] == 1, look
    booked = [row for row in look["slots"] if row["cluster"] == cluster_id]
    assert booked, f"the pass did not name the order it booked: {look['slots']}"
    assert booked[0]["post"] == post_id, booked
    assert booked[0]["slot_at"] == slot.isoformat(), booked
    assert demo._booking_note(look) == (
        f"1 of 1 open orders booked to the plan's minute ({s4_generation.PLAN_BOOKING_BOOKED}=1)"
    ), demo._booking_note(look)

    rows = _rows(
        w22d_db / "w22d.db",
        "SELECT status, plan_slot_at, scheduled_at, published_at FROM posts WHERE public_id = ?",
        (post_id,),
    )
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "gated", "a booking moved the post out of the gate"
    assert rows[0]["scheduled_at"] is None, "a booking put the post in the queue itself"
    assert rows[0]["published_at"] is None, "a booking published"
    assert rows[0]["plan_slot_at"] is not None, "the booked minute never reached the row"


def test_a_pass_the_switch_would_not_let_look_is_not_a_measured_zero(w22d_db) -> None:
    """"Could not look" and "nothing to book" are two sentences, tested apart.

    Same store, same rows, one switch: the counters must not arrive as zeros the
    pass never measured, and the wording must not be the wording of a measurement.
    """
    from tgfarm import demo

    _seed_open_order(slot_at=datetime.now(UTC))
    _switch(False)

    look = asyncio.run(demo._booking_look(writes=True))
    counters = demo._booking_counters(look)

    assert look["status"] == "skipped", look
    assert look["flag"] is False, look
    for key in BOOKING_COUNTER_KEYS:
        assert counters[key] is None, f"a pass that could not look reports {key}=0: {counters}"
    assert "could not look" in counters["booking_note"], counters["booking_note"]
    assert "measured zero" not in counters["booking_note"], counters["booking_note"]
    # and the row it could not look at is still unbooked
    assert _rows(
        w22d_db / "w22d.db", "SELECT plan_slot_at FROM posts"
    ) == [{"plan_slot_at": None}], "a pass that skipped still wrote a slot"


def test_a_store_the_demo_does_not_own_is_never_booked_in(w22d_db) -> None:
    """`--configured-store` reads; it does not reserve minutes in someone's rows."""
    from tgfarm import demo

    _seed_open_order(slot_at=datetime.now(UTC))
    _switch(True)
    before = _rows(
        w22d_db / "w22d.db", "SELECT plan_slot_at FROM posts WHERE public_id LIKE 'w22d-%'"
    )
    assert before == [{"plan_slot_at": None}], before

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=False))
    stages = report["cycle0_stages"]

    assert stages["booking_mode"] == "read-only", stages
    assert stages["booking_status"] == "not-run", stages
    for key in BOOKING_COUNTER_KEYS:
        assert stages[key] is None, f"a stage that never ran reports {key}=0: {stages}"
    assert "operator" in stages["booking_note"], stages["booking_note"]
    assert _rows(
        w22d_db / "w22d.db", "SELECT plan_slot_at FROM posts WHERE public_id LIKE 'w22d-%'"
    ) == before, "the read-only demo booked a row it does not own"
    assert _journal(w22d_db / "w22d.db", s4_generation.STAGE_BOOKING, "status") == [], (
        "the read-only demo journalled a pass it did not run"
    )
    # the plan stage of the same run is read the same way, and the gate still passes
    assert stages["plan_mode"] == "read-only", stages
    assert report["verdict"]["ok"] is True, report["verdict"]


# ---------------------------------------------------------------------- gate failures
def _stages_with(booking: dict) -> dict:
    """A trail with the plan half neutral and the booking half as the test states it."""
    from tgfarm import demo

    plan_look = {
        "mode": demo.PLAN_EXECUTED,
        "status": demo.PLAN_BUILT,
        "flag": True,
        "public_id": "p",
        "entries": 1,
        "produce": 1,
        "schedule": 0,
        "hold": 0,
        "scanned": 1,
        "booked": 0,
        "held": 0,
        "reasons": {},
        "note": "",
        "build": None,
        "judgement": None,
    }
    return demo._stage_counts(
        seeded=18,
        stored=18,
        fresh=18,
        trends={"duplicates": 0, "clusters": 6, "promoted": 6},
        plan=demo._plan_counters(plan_look),
        booking=demo._booking_counters(booking),
        ready_clusters=6,
        drafts=4,
        quality={"passed": 4, "rejected": 0, "deferred": 0},
        visual={"rendered": 4, "inline": 0},
        scheduled=3,
        schedule_holds={"dzen_needs_online_model": 1},
        publish={"published": 0, "dry_run": 3, "gates": {"farm_not_live": 3}},
        crosspost={},
    )


def _look(**overrides) -> dict:
    look = {
        "mode": "walked",
        "status": "ran",
        "job": s4_generation.PLAN_BOOKING_JOB_ID,
        "flag": True,
        "considered": 4,
        "booked": 0,
        "states": {"no_schedule_entry": 4},
        "slots": [],
        "note": "",
    }
    look.update(overrides)
    return look


def test_a_pass_that_raised_fails_the_gate(w22d_db, monkeypatch) -> None:
    """The stage is allowed to break the run: a silent booking failure is a lie."""
    from tgfarm import demo

    async def _boom(*, limit: int = s4_generation.MAX_BOOKINGS_PER_RUN):
        raise RuntimeError("соединение с базой потеряно")

    monkeypatch.setattr(s4_generation, "book_plan_tick", _boom)
    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))

    stages = report["cycle0_stages"]
    assert stages["booking_status"] == "error", stages
    for key in BOOKING_COUNTER_KEYS:
        assert stages[key] is None, stages
    failures = " ".join(report["verdict"]["failures"])
    assert "the booking pass raised" in failures, failures


def test_the_gate_rules_on_the_three_kinds_of_zero(w22d_db) -> None:
    """One table, three zeroes: measured, blind, and never asked.

    The gate must fail exactly one of them.
    """
    from tgfarm import demo

    walked_zero = _stages_with(_look())
    assert demo._gate_failures(0, walked_zero) == [], walked_zero

    empty_walk = _stages_with(_look(considered=0, states={}))
    assert demo._gate_failures(0, empty_walk) == [], empty_walk
    assert "no open order" in demo._booking_note(_look(considered=0, states={}))

    blind = _stages_with(_look(status="skipped", considered=None, booked=None, states=None))
    failures = demo._gate_failures(0, blind)
    assert len(failures) == 1, failures
    assert "could not look" in failures[0], failures[0]
    assert "booking=skipped" in failures[0], failures[0]
    assert "booking_booked=?" in failures[0], failures[0]

    broken = _stages_with(_look(status="error", considered=None, booked=None, states=None))
    failures = demo._gate_failures(0, broken)
    assert len(failures) == 1, failures
    assert "the booking pass raised" in failures[0], failures[0]

    # the same skipped pass on a store the demo only reads was never promised
    read_only = _stages_with(
        _look(mode="read-only", status="not-run", considered=None, booked=None, states=None)
    )
    assert demo._gate_failures(0, read_only) == [], read_only


def test_the_stage_trail_says_nothing_was_measured_instead_of_printing_zero(
    w22d_db, monkeypatch
) -> None:
    """`NOT MEASURED` is printed where a `0` would be an invented number."""
    from tgfarm import demo

    class _Recorder:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def info(self, message, *args):
            self.lines.append(message % args)

        def warning(self, message, *args):
            self.lines.append(message % args)

    recorder = _Recorder()
    monkeypatch.setattr(demo, "log", recorder)

    stages = _stages_with(_look(status="skipped", considered=None, booked=None, states=None))
    assert stages["booking_booked"] is None, stages
    demo._log_stages(0, stages)

    line = next(entry for entry in recorder.lines if "s4 booking" in entry)
    assert "NOT MEASURED" in line, line
    assert " 0 " not in line, line

    # a pass that ran and measured nothing still prints its measured zero, and
    # prints it as explained rather than as a stage that did no work
    recorder.lines.clear()
    demo._log_stages(0, _stages_with(_look(considered=0, states={})))
    booking_line = next(entry for entry in recorder.lines if "s4 booking" in entry)
    assert " 0  (" in booking_line, booking_line
    assert "NOTHING" not in booking_line, booking_line

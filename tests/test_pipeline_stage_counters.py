"""P34: the run journal has to carry the counters a stage actually measured.

Live evidence (2026-11-05): 2 433 `pipeline_runs` rows for stage `s4_generate`,
every one of them `items_in=0, items_out=0`, while drafts were demonstrably being
written to `posts` in the same minutes. `_stage` wrote the counters of the timer
*it* opened, and every stage fills a private timer inside itself, so the numbers
never travelled. An operator reading the journal could not tell "the stage did no
work" from "nobody measured the work" — which is the one distinction the pipeline
is supposed to expose.

These tests drive real stage functions (`s1_sources.run`, `s5_quality.run`,
`s2_trends.run`, `s6_visual.run`) through the real `_stage` wrapper and read the
row back out of the journal table.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tgfarm.core.run import StageTimer, current_stage_timer, to_thread
from tgfarm.db.models import PipelineRun
from tgfarm.db.session import session_scope


def _run_id() -> str:
    return "p34-" + uuid.uuid4().hex[:10]


def _journal(run_id: str, stage: str) -> PipelineRun:
    with session_scope() as session:
        return session.execute(
            select(PipelineRun).where(PipelineRun.run_id == run_id, PipelineRun.stage == stage)
        ).scalar_one()


@pytest.mark.asyncio
async def test_a_real_stage_s_counters_reach_the_journal_row(monkeypatch):
    """S5 measures 3 drafts in, 2 passed; the row must say exactly that.

    `gate_post` is the only thing stubbed — the stage function, its loop, its
    `StageTimer` and the orchestrator that records the row are all production
    code.
    """
    from tgfarm import pipeline, s5_quality

    outcomes = {"d1": True, "d2": True, "d3": False}

    async def fake_gate(public_id: str):
        return SimpleNamespace(passed=outcomes[public_id])

    monkeypatch.setattr(s5_quality, "gate_post", fake_gate)

    result = pipeline.CycleResult(run_id=_run_id())
    value = await pipeline._stage(result, "s5_quality", lambda: s5_quality.run(["d1", "d2", "d3"]))

    assert value == {"in": 3, "passed": 2, "rejected": 1, "deferred": 0}
    row = _journal(result.run_id, "s5_quality")
    assert row.status == "ok"
    assert row.items_in == 3
    assert row.items_out == 2
    assert row.meta["counters"] == "measured"
    assert "unmeasured" not in row.meta
    sub_stage = row.meta["sub_stages"][0]
    assert sub_stage["stage"] == "s5_quality"
    assert sub_stage["runs"] == 1
    assert (sub_stage["items_in"], sub_stage["items_out"]) == (3, 2)
    assert 0 <= sub_stage["duration_ms"] <= row.duration_ms
    # The cycle result the API serves carries the same numbers as the row.
    assert result.stages["s5_quality"]["items_in"] == 3
    assert result.stages["s5_quality"]["items_out"] == 2


@pytest.mark.asyncio
async def test_a_real_stage_that_measured_zero_is_not_recorded_as_silence():
    """S1 offline: the stage itself says "zero written".

    A measured zero and an unmeasured nothing both land in a NOT NULL integer
    column, so the difference has to survive in the row — otherwise the journal
    cannot distinguish a starving collector from an uninstrumented one.
    """
    from tgfarm import pipeline, s1_sources

    result = pipeline.CycleResult(run_id=_run_id())
    written = await pipeline._stage(result, "s1_collect", s1_sources.run)

    assert written == 0
    row = _journal(result.run_id, "s1_collect")
    assert row.status == "ok"
    assert row.items_out == 0
    assert row.meta["counters"] == "partial"
    assert row.meta["unmeasured"] == ["items_in"]
    assert row.meta["sub_stages"][0]["items_out"] == 0, "a measured zero, not an absent number"


@pytest.mark.asyncio
async def test_a_stage_that_reports_nothing_is_not_credited_with_zeros():
    """S6 renders covers and never opens a timer; the row must not claim it did."""
    from tgfarm import pipeline, s6_visual

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s6_visual", lambda: s6_visual.run([]))

    row = _journal(result.run_id, "s6_visual")
    assert row.status == "ok"
    assert row.meta["counters"] == "not_reported"
    assert row.meta["unmeasured"] == ["items_in", "items_out"]
    assert "sub_stages" not in row.meta
    assert result.stages["s6_visual"]["items_in"] is None
    assert result.stages["s6_visual"]["items_out"] is None


@pytest.mark.asyncio
async def test_the_handoff_survives_the_thread_hop():
    """A timer that opens and closes inside a worker thread still reaches the row.

    `asyncio.to_thread` copies the context into the executor thread, so the nested
    timer finds the enclosing stage object there; mutating that shared object is
    what carries the counters back. Without the copy, this test records 0/0.
    """
    from tgfarm import pipeline

    main_thread = threading.get_ident()
    worker_threads: list[int] = []
    enclosing_seen_in_worker: list[str | None] = []

    def measure_in_a_worker() -> None:
        worker_threads.append(threading.get_ident())
        outer = current_stage_timer()
        enclosing_seen_in_worker.append(outer.stage if outer is not None else None)
        with StageTimer("s9_crosspost") as timer:
            timer.items_in = 7
            timer.items_out = 3

    async def stage_body() -> dict:
        await to_thread(measure_in_a_worker)
        return {"hops": len(worker_threads)}

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s9_crosspost", stage_body)

    assert worker_threads and worker_threads[0] != main_thread, "the timer really ran off-loop"
    assert enclosing_seen_in_worker == ["s9_crosspost"], "the enclosing scope crossed into the thread"
    assert current_stage_timer() is None, "and it did not leak back out"
    row = _journal(result.run_id, "s9_crosspost")
    assert (row.items_in, row.items_out) == (7, 3)
    assert row.meta["counters"] == "measured"
    assert row.meta["sub_stages"][0]["stage"] == "s9_crosspost"


@pytest.mark.asyncio
async def test_a_stage_that_throws_records_what_it_had_measured(monkeypatch):
    """S2 dies in its scoring pass: dedup and clustering already measured themselves.

    The numbers measured before the throw are the reason the error row is worth
    having — dropping them back to 0/0 is what made the failure indistinguishable
    from an empty farm.
    """
    from tgfarm import pipeline, s2_trends

    def _boom() -> int:
        msg = "scoring pass could not read embeddings"
        raise RuntimeError(msg)

    monkeypatch.setattr(s2_trends, "_score_pass", _boom)

    result = pipeline.CycleResult(run_id=_run_id())
    value = await pipeline._stage(result, "s2_trends", s2_trends.run)

    assert value is None
    assert any("s2_trends" in error for error in result.errors)
    row = _journal(result.run_id, "s2_trends")
    assert row.status == "error"
    assert "scoring pass could not read embeddings" in (row.error or "")
    measured = {entry["stage"]: entry for entry in row.meta["sub_stages"]}
    assert set(measured) == {"s2_dedup", "s2_cluster", "s2_score"}
    # The passes that finished keep their (zero) measurements; the one that threw
    # is recorded as measured-nothing, and the stage headline is the last real
    # measurement taken before the throw, not a fabricated 0.
    assert measured["s2_dedup"]["items_out"] == 0
    assert measured["s2_cluster"]["items_out"] == 0
    assert measured["s2_score"]["items_out"] is None
    assert row.items_out == 0
    assert row.meta["counters"] == "partial"
    assert row.meta["unmeasured"] == ["items_in"]
    # The row's duration is the stage's own wall time, which contains the passes.
    assert row.duration_ms >= max(entry["duration_ms"] for entry in measured.values())


@pytest.mark.asyncio
async def test_a_multi_pass_stage_gets_its_final_pass_not_a_sum(monkeypatch):
    """S2's passes count different things; adding them would invent a number.

    Headline `items_out` is the last pass that measured one (scored clusters), and
    every pass keeps its own line in `meta["sub_stages"]`.
    """
    from tgfarm import pipeline, s2_trends

    monkeypatch.setattr(s2_trends, "_dedup_pass", lambda: 4)
    monkeypatch.setattr(s2_trends, "_cluster_pass", lambda: 9)
    monkeypatch.setattr(s2_trends, "assign_niches", lambda: 2)
    monkeypatch.setattr(s2_trends, "_score_pass", lambda: 6)
    monkeypatch.setattr(s2_trends, "promote_ready", lambda: 1)

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s2_trends", s2_trends.run)

    row = _journal(result.run_id, "s2_trends")
    assert row.items_out == 6, "the final pass, not 4+9+6"
    assert row.meta["counters"] == "partial"
    assert row.meta["unmeasured"] == ["items_in"]
    assert [(entry["stage"], entry["items_out"]) for entry in row.meta["sub_stages"]] == [
        ("s2_dedup", 4),
        ("s2_cluster", 9),
        ("s2_score", 6),
    ]


@pytest.mark.asyncio
async def test_repeated_clusters_of_one_stage_add_up():
    """S4 runs once per cluster inside one stage — those children are the same unit."""
    from tgfarm import pipeline

    async def one_cluster(plans: int, drafts: int) -> None:
        with StageTimer("s4_generate") as timer:
            timer.items_in = plans
            timer.items_out = drafts

    async def stage_body() -> dict:
        for plans, drafts in ((3, 2), (4, 4), (2, 0)):
            await one_cluster(plans, drafts)
        return {"clusters": 3, "drafts": 6}

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s4_generate", stage_body)

    row = _journal(result.run_id, "s4_generate")
    assert (row.items_in, row.items_out) == (9, 6)
    assert len(row.meta["sub_stages"]) == 1
    folded = row.meta["sub_stages"][0]
    assert (folded["stage"], folded["runs"], folded["items_in"], folded["items_out"]) == (
        "s4_generate",
        3,
        9,
        6,
    )
    assert folded["duration_ms"] <= row.duration_ms


@pytest.mark.asyncio
async def test_a_foreign_sub_stage_is_not_credited_to_the_stage_it_ran_inside():
    """S3 routes inside S4; its routes/plans counts are not S4's items."""
    from tgfarm import pipeline

    async def stage_body() -> None:
        with StageTimer("s3_route") as routing:
            routing.items_in = 50
            routing.items_out = 12
        with StageTimer("s4_generate") as generating:
            generating.items_in = 12
            generating.items_out = 5

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s4_generate", stage_body)

    row = _journal(result.run_id, "s4_generate")
    assert (row.items_in, row.items_out) == (12, 5)
    assert [entry["stage"] for entry in row.meta["sub_stages"]] == ["s4_generate"]


@pytest.mark.asyncio
async def test_concurrent_stages_do_not_share_counters():
    """Two stages in flight at once must not write into each other's totals."""
    from tgfarm import pipeline

    async def measure(stage: str, items_in: int, items_out: int) -> None:
        await asyncio.sleep(0.01)
        with StageTimer(stage) as timer:
            timer.items_in = items_in
            timer.items_out = items_out

    async def s5_body() -> None:
        await measure("s5_quality", 11, 4)

    async def s8_body() -> None:
        await measure("s8_analytics", 2, 9)

    first = pipeline.CycleResult(run_id=_run_id())
    second = pipeline.CycleResult(run_id=_run_id())
    await asyncio.gather(
        pipeline._stage(first, "s5_quality", s5_body),
        pipeline._stage(second, "s8_analytics", s8_body),
    )

    s5_row = _journal(first.run_id, "s5_quality")
    s8_row = _journal(second.run_id, "s8_analytics")
    assert (s5_row.items_in, s5_row.items_out) == (11, 4)
    assert (s8_row.items_in, s8_row.items_out) == (2, 9)


@pytest.mark.asyncio
async def test_the_journal_duration_is_the_real_stage_duration():
    """The row's `duration_ms` is the stage's wall time, not a sub-timer's slice."""
    from tgfarm import pipeline

    async def stage_body() -> None:
        with StageTimer("s7_publish"):
            pass  # a sub-stage that finishes instantly

        await asyncio.sleep(0.06)

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s7_publish", stage_body)

    row = _journal(result.run_id, "s7_publish")
    assert row.duration_ms >= 50
    assert row.meta["sub_stages"][0]["duration_ms"] < row.duration_ms


@pytest.mark.asyncio
async def test_a_stage_without_timers_still_gets_its_row_and_its_error(monkeypatch):
    """The failure path must not lose the row, and must not invent counters."""
    from tgfarm import pipeline

    async def stage_body() -> None:
        msg = "adapter is not configured"
        raise ValueError(msg)

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s12_learning", stage_body)

    row = _journal(result.run_id, "s12_learning")
    assert row.status == "error"
    assert "adapter is not configured" in (row.error or "")
    assert row.items_in == 0 and row.items_out == 0
    assert row.meta["counters"] == "not_reported"
    assert result.stages["s12_learning"]["items_in"] is None


def test_a_timer_outside_any_stage_scope_reports_nothing():
    """A bare timer must not publish into an unrelated stage's totals."""
    from tgfarm.core import run

    outer = StageTimer("s5_quality")
    with outer:
        with StageTimer("s5_quality") as inner:
            inner.items_out = 3
        assert outer.sub_stages[0]["items_out"] == 3
        assert run._stage_stack.get()[-1] is outer

    orphan = StageTimer("s5_quality")
    with orphan:
        pass
    assert orphan.reported_out is None
    assert run._stage_stack.get() == (), "a closed timer must not leave its scope behind"

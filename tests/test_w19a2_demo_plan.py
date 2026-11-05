"""W19-A2: `tgfarm demo` genuinely exercises the editorial plan, and says so.

A green demo used to prove a pipeline with no editorial judgement in it: it
walked source -> clusters -> drafts -> gate -> covers -> schedule -> publish
decision without ever asking the plan what it made of the ranked queue. These
tests hold the finished increment to four things, and every one of them is read
back from the store the demo wrote rather than from the demo's own words:

* on the throwaway store `tgfarm demo` owns, the plan runs through the
  scheduler's two jobs, and the report carries its numbers — total, produce,
  schedule, hold — plus the booking verdict;
* the plan books nothing on that walk (its entries are `produce`, which is S4's
  job, not the plan's), and that must read as a *measurement of zero*, never as
  a failed stage and never as a silent skip;
* a plan that was consulted over an empty queue is stated as *nothing to plan*,
  with real zeros beside it — not a stage that did not run wearing a zero;
* on a store the demo does not own, the plan is read and never rewritten: no
  snapshot that supersedes the operator's active plan, no journalled build, and
  no counters faked up from a stage that did not run.

Every test runs against its own throwaway database, as the other demo tests do.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgfarm import plan_execution  # noqa: E402
from tgfarm.core import settings as settings_mod  # noqa: E402
from tgfarm.db import session as session_mod  # noqa: E402

#: every plan counter the stage trail has to carry; a trail that drops one is
#: caught here rather than in the report an operator reads
PLAN_KEYS = (
    "plan_mode",
    "plan_status",
    "plan_consulted",
    "plan_flag",
    "plan_entries",
    "plan_produce",
    "plan_schedule",
    "plan_hold",
    "plan_scanned",
    "plan_booked",
    "plan_held",
    "plan_reasons",
    "plan_note",
)

#: the counters a plan the demo only read may not invent a zero for
PLAN_COUNTER_KEYS = (
    "plan_entries",
    "plan_produce",
    "plan_schedule",
    "plan_hold",
    "plan_scanned",
    "plan_booked",
    "plan_held",
)


@pytest.fixture()
def w19a2_db(tmp_path):
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
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'w19a2.db').as_posix()}"
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
        raise AssertionError(f"the demo tried to use a publish transport: {self.name}")

    async def publish(self, payload):  # s9 crosspost adapters
        self._calls.append(f"{self.name}.publish")
        raise AssertionError(f"the demo tried to use a crosspost transport: {self.name}")


@pytest.fixture()
def w19a2_transports_locked(monkeypatch):
    """The plan stage must not become a delivery stage: every transport raises."""
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


def test_w19a2_demo_reports_plan_numbers_and_states_the_booking_honestly(
    w19a2_db, w19a2_transports_locked
) -> None:
    """The demo's verdict carries the plan's numbers, and the zero booking is named.

    ``tgfarm demo`` builds and judges the plan on the store it owns, so the trail
    has to show total/produce/schedule/hold, and the fact that the plan booked
    nothing has to read as a measurement of nothing — not a broken stage, and not
    a stage that quietly ran no numbers at all.
    """
    from tgfarm import demo

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))
    stages = report["cycle0_stages"]

    missing = [key for key in PLAN_KEYS if key not in stages]
    assert not missing, f"the stage trail carries no plan: missing {missing}"

    assert stages["plan_mode"] == "executed", stages
    assert stages["plan_status"] == "built", stages
    assert stages["plan_consulted"] is True, stages
    assert stages["plan_flag"] is True, stages
    assert stages["plan_entries"] >= 1, stages
    assert (
        stages["plan_produce"] + stages["plan_schedule"] + stages["plan_hold"]
        == stages["plan_entries"]
    ), stages
    assert stages["plan_scanned"] == stages["plan_entries"], stages

    # the plan booked nothing on this walk: every entry is a produce booking that
    # belongs to S4, so the honest statement is a measured zero, never a failure
    assert stages["plan_booked"] == 0, stages
    assert isinstance(stages["plan_held"], int), stages
    note = stages["plan_note"]
    assert "booked" in note and "0" in note, note
    assert "not a failure" in note or "not booked" in note or "S4" in note, (
        f"a booked-nothing plan is not explained honestly: {note}"
    )

    # an honest booking is not a gate failure: nothing in the plan made this cycle
    # stop, and the verdict says the plan was reached, in numbers
    assert report["verdict"]["ok"] is True, report["verdict"]
    failures = " ".join(report["verdict"]["failures"])
    assert "editorial plan" not in failures.lower(), failures
    assert "plan" in report["verdict"], report["verdict"]

    # never publish, asserted not assumed: no transport was touched and the store
    # holds no published post
    assert w19a2_transports_locked == [], w19a2_transports_locked
    assert report["cycle0_publish"]["published"] == 0, report["cycle0_publish"]
    db = _demo_db(report)
    published = _rows(db, "SELECT COUNT(*) AS c FROM posts WHERE status = 'published'")[0]["c"]
    assert published == 0, "the demo published something"


def test_w19a2_demo_leaves_a_journalled_plan_in_the_store_it_owns(
    w19a2_db, w19a2_transports_locked
) -> None:
    """The gate's plan evidence is rows in the database, not words in the report."""
    from tgfarm import demo

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))
    stages = report["cycle0_stages"]
    db = _demo_db(report)

    builds = _journal(db, plan_execution.STAGE_BUILD, "status, items_out")
    applies = _journal(db, plan_execution.STAGE_APPLY, "status, items_in, items_out")
    assert builds, "the demo journalled no plan build"
    assert applies, "the demo journalled no plan judgement"
    assert builds[0]["status"] == "ok", builds[0]

    build_meta = json.loads(builds[0]["meta"] or "{}")
    entries = _rows(
        db,
        "SELECT action FROM editorial_plan_entries WHERE plan_id ="
        " (SELECT id FROM editorial_plans WHERE public_id = ?)",
        (str(build_meta.get("plan_id")),),
    )
    assert stages["plan_entries"] == len(entries), (stages["plan_entries"], len(entries))
    assert builds[0]["items_out"] == len(entries), "the build row counted something else"

    judged_meta = json.loads(applies[0]["meta"] or "{}")
    reasons = judged_meta.get("reasons") or {}
    assert stages["plan_reasons"] == reasons, (stages["plan_reasons"], reasons)
    # scanned counts every entry the judgement looked at, booked is the subset it
    # actually handed to the scheduling path, and the rest name themselves
    assert stages["plan_scanned"] == len(entries), stages
    assert stages["plan_booked"] + sum(int(n) for n in reasons.values()) == len(entries), stages

    # a booked slot is a booking, never a delivery
    assert stages["plan_booked"] <= stages["plan_schedule"], stages


def test_w19a2_demo_states_a_plan_with_nothing_to_plan_as_consulted_not_broken(
    w19a2_db, monkeypatch
) -> None:
    """An empty queue judged by the plan is a measurement of nothing.

    Suppressing the corpus leaves the store with no clusters, so the plan builds
    with zero entries. That is the demo asking the plan a real question and
    getting "nothing to plan" back: the counters read as measured zeros and the
    note names the emptiness, never a stage that did not run dressed up as a
    zero, and never a gate failure. Going through the real writing path proves the
    renderer and the gate agree, not only a helper in isolation.
    """
    from tgfarm import demo

    monkeypatch.setattr(demo, "_seed_items", lambda: 0)
    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))
    stages = report["cycle0_stages"]

    assert stages["plan_mode"] == "executed", stages
    assert stages["plan_status"] == "built-empty", stages
    assert stages["plan_consulted"] is True, stages
    assert stages["plan_entries"] == 0, stages
    assert stages["plan_scanned"] == 0, stages
    assert stages["plan_booked"] == 0, stages
    assert "nothing to plan" in stages["plan_note"], stages["plan_note"]

    # nothing to plan is not nothing measured: the counters are real zeros, not
    # the unknown a stage that never ran reports
    for key in PLAN_COUNTER_KEYS:
        assert stages[key] is not None, f"an empty plan reports {key} as unknown: {stages}"

    # and an empty plan is not the reason this cycle stopped: the gate's complaint
    # is the spent corpus, not the editorial plan
    failures = " ".join(report["verdict"]["failures"])
    assert "editorial plan" not in failures.lower(), failures


def test_w19a2_demo_read_only_store_never_persists_a_plan(w19a2_db) -> None:
    """On a store the demo does not own, the plan is read, never rewritten.

    ``isolated=False`` is how ``--configured-store`` runs and how the other demo
    tests call it: superseding that store's active plan and moving its gated
    drafts is more than a rehearsal consents to. With no plan to read the honest
    output is the statement that it holds none — with no counters printed beside
    it, because nothing was measured.
    """
    from tgfarm import demo

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=False))
    stages = report["cycle0_stages"]

    assert stages["plan_mode"] == "read-only", stages
    assert stages["plan_status"] == "read-none", stages
    assert stages["plan_consulted"] is True, stages
    for key in (*PLAN_COUNTER_KEYS, "plan_reasons"):
        assert stages[key] is None, f"a stage that did not run reports {key}=0: {stages}"

    db = w19a2_db / "w19a2.db"
    assert _journal(db, plan_execution.STAGE_BUILD, "status, items_out") == [], (
        "the read-only demo journalled a plan build it did not run"
    )
    assert _rows(db, "SELECT COUNT(*) AS c FROM editorial_plans")[0]["c"] == 0, (
        "the read-only demo persisted a plan on a store it does not own"
    )
    assert report["verdict"]["ok"] is True, report["verdict"]
    assert "plan" in report["verdict"]


def test_w19a2_demo_forces_the_plan_flag_on_its_own_store_and_gives_it_back(
    w19a2_db, w19a2_transports_locked
) -> None:
    """The demo runs the executor whatever the environment says.

    On the store it owns it forces ``PLAN_EXECUTION_ENABLED`` on for the duration
    of the run — a rehearsal that never asks the editor proves a pipeline with no
    editorial decision in it — and hands the flag back exactly as the offline run
    hands back ``mode``, so a demo cannot leave the process switched on.
    """
    from tgfarm import demo
    from tgfarm.core.settings import get_settings

    assert get_settings().plan_execution_enabled is False, "fixture should leave the flag off"

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False, isolated=True))

    # the plan really ran (build journalled, flag read as on inside the run)...
    assert report["cycle0_stages"]["plan_flag"] is True, report["cycle0_stages"]
    assert report["cycle0_stages"]["plan_status"] == "built", report["cycle0_stages"]
    # ...and the switch is the operator's again the moment the run returns
    assert get_settings().plan_execution_enabled is False, (
        "the demo left PLAN_EXECUTION_ENABLED forced on after its run"
    )

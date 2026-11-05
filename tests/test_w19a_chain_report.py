"""W19-A: the chain report must state the editorial plan stage, or say it did not read one.

`tools/autonomy_chain_report.py` is deliberately import-free: it walks a SQLite
file strictly in `mode=ro` and prints what the journal and the plan tables say.
Two rules this file pins:

* the plan's numbers are the ones in the DB — entries per action, scanned and
  booked per apply row, slots that really read `scheduled` — never a number the
  report invents;
* when the chain never ran the plan, the plan line says "не прочитан". A zero is
  a measurement, and printing one for a stage that did not run reports a
  measurement that was never taken.

The fixtures write their own throwaway DB files with raw `sqlite3` (no app
import, no session DB, nothing of the repo's own `tgfarm.db` is opened).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgfarm import plan_execution  # noqa: E402
from tools.autonomy_chain_report import main  # noqa: E402

#: The plan's own journal stage names, as the executor declares them; the tests
#: below read them through these constants so a renamed stage cannot drift out
#: of the report unnoticed.

CHAIN_RUN_ID = "w19achain01"
BASE = datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC)

MINIMAL_SCHEMA = """
CREATE TABLE pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, stage TEXT, status TEXT,
    items_in INTEGER, items_out INTEGER, duration_ms INTEGER,
    error TEXT, meta TEXT, created_at TEXT
);
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT, channel_id TEXT, cluster_public_id TEXT,
    status TEXT, attempts INTEGER DEFAULT 0,
    scheduled_at TEXT, published_at TEXT, reject_reason TEXT
);
CREATE TABLE crosspost_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_public_id TEXT, platform TEXT, status TEXT,
    attempt INTEGER, error TEXT, created_at TEXT
);
CREATE TABLE editorial_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT, created_at TEXT, horizon_days INTEGER, status TEXT,
    superseded_by_id INTEGER, summary TEXT
);
CREATE TABLE editorial_plan_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER, cluster_public_id TEXT, title TEXT, niche TEXT,
    action TEXT, platform TEXT, channel_id TEXT, format TEXT,
    priority INTEGER, confidence REAL, reasons TEXT, slot_at TEXT,
    payload TEXT, created_at TEXT
);
"""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _stage_row(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    stage: str,
    status: str,
    items_in: int | None,
    items_out: int | None,
    meta: dict[str, Any],
    offset_seconds: int,
) -> None:
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, stage, status, items_in, items_out, duration_ms,"
        " error, meta, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            stage,
            status,
            0 if items_in is None else items_in,
            0 if items_out is None else items_out,
            12,
            None,
            json.dumps(meta),
            _iso(BASE + timedelta(seconds=offset_seconds)),
        ),
    )


def _chain_without_plan(conn: sqlite3.Connection) -> None:
    """The journal a chain leaves when no plan stage ran inside it."""
    for offset, stage in enumerate(
        ("s2_trends", "s4_generate", "s5_quality", "s7_publish", "s7_publish_due", "s12_learning"),
        start=1,
    ):
        _stage_row(
            conn,
            run_id=CHAIN_RUN_ID,
            stage=stage,
            status="ok",
            items_in=3,
            items_out=2,
            meta={"counters": "measured"},
            offset_seconds=offset,
        )


@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "chain.db"
    conn = sqlite3.connect(path)
    conn.executescript(MINIMAL_SCHEMA)
    conn.execute(
        "INSERT INTO crosspost_logs (post_public_id, platform, status, attempt, error,"
        " created_at) VALUES (?,?,?,?,?,?)",
        ("post_clu0000001", "vk", "dry_run", 1, None, _iso(BASE)),
    )
    _chain_without_plan(conn)
    conn.commit()
    conn.close()
    return path


def _plan_rows(
    db: Path,
    *,
    plan_public_id: str = "planpub01",
    actions: tuple[str, ...] = ("schedule", "produce"),
    booked_clusters: tuple[str, ...] = ("clu0000001",),
    scanned: int = 2,
    applied: int = 1,
    reasons: dict[str, int] | None = None,
    build_meta: dict[str, Any] | None = None,
    inside_window: bool = True,
) -> None:
    """One build row, one apply row, the persisted plan and the posts under it."""
    conn = sqlite3.connect(db)
    try:
        _write_plan_rows(
            conn,
            plan_public_id=plan_public_id,
            actions=actions,
            booked_clusters=booked_clusters,
            scanned=scanned,
            applied=applied,
            reasons=reasons,
            build_meta=build_meta,
            inside_window=inside_window,
        )
        conn.commit()
    finally:
        conn.close()


def _write_plan_rows(
    conn: sqlite3.Connection,
    *,
    plan_public_id: str,
    actions: tuple[str, ...],
    booked_clusters: tuple[str, ...],
    scanned: int,
    applied: int,
    reasons: dict[str, int] | None,
    build_meta: dict[str, Any] | None,
    inside_window: bool,
) -> None:
    cluster_ids = [f"clu000000{index + 1}" for index in range(len(actions))]
    summary = {
        "total": len(actions),
        "produce": actions.count("produce"),
        "schedule": actions.count("schedule"),
        "hold": actions.count("hold"),
    }
    conn.execute(
        "INSERT INTO editorial_plans (public_id, created_at, horizon_days, status,"
        " superseded_by_id, summary) VALUES (?,?,?,?,?,?)",
        (plan_public_id, _iso(BASE), 7, "active", None, json.dumps(summary)),
    )
    plan_id = conn.execute(
        "SELECT id FROM editorial_plans WHERE public_id = ?", (plan_public_id,)
    ).fetchone()[0]
    for cluster_id, action in zip(cluster_ids, actions, strict=True):
        conn.execute(
            "INSERT INTO editorial_plan_entries (plan_id, cluster_public_id, title, niche,"
            " action, platform, channel_id, format, priority, confidence, reasons, slot_at,"
            " payload, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id,
                cluster_id,
                f"Тема {cluster_id}",
                "tech",
                action,
                "telegram",
                "tg_main",
                "news",
                1,
                0.7,
                json.dumps(["температура 0.8"]),
                _iso(BASE + timedelta(minutes=30)) if action == "schedule" else None,
                json.dumps({}),
                _iso(BASE),
            ),
        )
    for cluster_id in cluster_ids:
        booked = cluster_id in booked_clusters
        conn.execute(
            "INSERT INTO posts (public_id, channel_id, cluster_public_id, status, attempts,"
            " scheduled_at, published_at, reject_reason) VALUES (?,?,?,?,?,?,?,?)",
            (
                f"post_{cluster_id}",
                "tg_main",
                cluster_id,
                "scheduled" if booked else "gated",
                0,
                _iso(BASE) if booked else None,
                None,
                None,
            ),
        )
    offset = 7 if inside_window else 600
    _stage_row(
        conn,
        run_id=f"plan-{plan_public_id[:8]}",
        stage=plan_execution.STAGE_BUILD,
        status="ok",
        items_in=None,
        items_out=len(actions),
        meta=(
            {"job": plan_execution.PLAN_BUILD_JOB_ID, "plan_id": plan_public_id, **summary}
            if build_meta is None
            else build_meta
        ),
        offset_seconds=offset,
    )
    _stage_row(
        conn,
        run_id=f"plan-apply-{plan_public_id[:8]}",
        stage=plan_execution.STAGE_APPLY,
        status="ok",
        items_in=scanned,
        items_out=applied,
        meta={
            "job": plan_execution.PLAN_APPLY_JOB_ID,
            "plan_id": plan_public_id,
            "held": 0 if reasons is None else sum(reasons.values()),
            "reasons": (
                {plan_execution.REASON_SLOT_NOT_DUE: 1} if reasons is None else reasons
            ),
        },
        offset_seconds=offset + 1,
    )


def _report(db: Path, capsys) -> str:
    assert main(["--db", str(db), "--run-id", CHAIN_RUN_ID]) == 0
    return capsys.readouterr().out


def _plan_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if plan_execution.STAGE_BUILD in line
        or plan_execution.STAGE_APPLY in line
        or "план" in line.lower()
        or "plan" in line.lower()
    ]


def test_w19a_report_says_ne_prochitan_when_the_plan_never_ran(db, capsys) -> None:
    """A chain that never built a plan must not be reported as a plan with zeros."""
    out = _report(db, capsys)

    assert "не прочитан" in out, out
    lines = _plan_lines(out)
    assert any(plan_execution.STAGE_BUILD in line for line in lines), out
    for line in lines:
        for counter in ("produce", "schedule", "hold", "booked", "scanned", "entries"):
            assert f"{counter}=0" not in line, (
                f"a stage that did not run is reported as a measured zero: {line!r}"
            )


def test_w19a_report_names_the_executors_own_stage_strings(db, capsys) -> None:
    """The report tracks the executor's constants, not a copy that can drift."""
    assert plan_execution.STAGE_BUILD == "editorial_plan_build"
    assert plan_execution.STAGE_APPLY == "editorial_plan_apply"
    out = _report(db, capsys)
    assert plan_execution.STAGE_BUILD in out
    assert plan_execution.STAGE_APPLY in out


def test_w19a_report_prints_the_plan_stage_measured_numbers(db, capsys) -> None:
    """Every number the plan link has to state, from the rows the DB holds."""
    _plan_rows(db)
    out = _report(db, capsys)

    assert "не прочитан" not in out, out
    lines = "\n".join(_plan_lines(out))
    # the plan's own decisions, counted from editorial_plan_entries
    assert "produce=1" in lines, out
    assert "schedule=1" in lines, out
    assert "hold=0" in lines, out
    # the judgement: scanned, booked, held and the reason codes it recorded
    assert "scanned=2" in lines, out
    assert "booked=1" in lines, out
    assert "slot_not_due=1" in lines, out
    # the plan the journal names is the plan the table holds
    assert "planpub01" in lines, out
    # the slots that really read scheduled, with the denominator they came from
    assert "1/1" in lines, out


def test_w19a_report_reports_a_slot_the_plan_held_as_held_not_as_booked(db, capsys) -> None:
    """Two schedule decisions, one booked: the report must not round that up."""
    _plan_rows(
        db,
        actions=("schedule", "schedule", "produce"),
        booked_clusters=("clu0000001",),
        scanned=3,
        applied=1,
        reasons={
            plan_execution.REASON_SLOT_NOT_DUE: 1,
            plan_execution.REASON_NO_GENERATION: 1,
        },
    )
    out = _report(db, capsys)
    lines = "\n".join(_plan_lines(out))

    assert "schedule=2" in lines, out
    assert "produce=1" in lines, out
    assert "booked=1" in lines, out
    assert "1/2" in lines, out


def test_w19a_report_does_not_claim_a_plan_that_belongs_to_another_run(db, capsys) -> None:
    """Attribution is by the chain's own journal window, not by table presence."""
    _plan_rows(db, inside_window=False)
    out = _report(db, capsys)

    assert "не прочитан" in out, out
    # the rows are still reported as existing somewhere in this DB, so the
    # operator is not told the plan never ran at all
    assert "друг" in out or "иных" in out or "вне" in out, out


def test_w19a_report_keeps_meta_without_counters_unknown(db, capsys) -> None:
    """A build row whose meta carries no decision counts stays unknown."""
    _plan_rows(db, build_meta={"job": plan_execution.PLAN_BUILD_JOB_ID, "plan_id": "planpub01"})
    out = _report(db, capsys)
    lines = "\n".join(_plan_lines(out))

    for counter in ("produce", "schedule", "hold"):
        assert f"{counter}=0" not in lines, (
            f"a plan row without recorded decision counts is printed as a zero: {lines!r}"
        )
    assert "?" in lines, lines

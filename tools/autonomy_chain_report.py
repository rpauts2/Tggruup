"""W17-C: read-only autonomy chain report.

Walks one SQLite DB strictly in `mode=ro` and prints, for every link of the
autonomy chain (source -> candidate -> ranked -> draft -> gate -> media ->
schedule -> publish decision -> metrics -> recommendation):

- the stage journal row (status, timestamps, duration, three-state counters),
- an attempt count (pipeline stages journal once per (run_id, stage); post
  retries live in `posts.attempts`, platform retries in `crosspost_logs.attempt`),
- a coarse error category derived from the redacted error text.

Default DB is the repo `tgfarm.db` (opened read-only; nothing here ever writes
or imports the app, so the live file is safe to point at). Point it at the
harness temp DB instead with `--db` to see a real offline chain run.

Usage:
    python tools/autonomy_chain_report.py [--db PATH] [--run-id ID]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "tgfarm.db"

# Chain stage order as journaled by tgfarm.pipeline for one cycle, mapped to
# the acceptance-chain link it carries evidence for (docs/AUTONOMY_ACCEPTANCE.md).
CHAIN_STAGES: list[tuple[str, str]] = [
    ("s1_sources", "source items collected (unreachable offline; see below)"),
    ("s2_trends", "candidate cluster exists and is ranked (heat)"),
    ("s4_generate", "draft with provenance (sources cite seeded urls)"),
    ("s5_quality", "gate verdict with reason (gated/rejected)"),
    ("s6_visual", "media recorded for passed drafts"),
    ("s7_publish", "schedule slot or hold reason"),
    ("s7_publish_due", "publish decision journalled in an outcome state"),
    ("s9_crosspost", "platform crosspost decision journalled"),
    ("s8_analytics", "metrics attempted (honest skip when offline)"),
    ("s12_learning", "recommendation states its sample size"),
    ("maintenance", "fleet upkeep (needs network; excluded offline)"),
]

# Chain links that cannot be driven offline at base 094a67f. These mirror the
# strict-xfail tests in tests/test_autonomy_chain_offline.py; the harness does
# NOT stub them — it names the missing production capability instead.
UNDRIVEABLE_OFFLINE: list[tuple[str, str]] = [
    (
        "s1_sources (collection)",
        "tgfarm.s1_sources.run short-circuits while settings.online is False, "
        "before collectors.collect_detailed is ever reached; no offline "
        "collection entry point exists.",
    ),
    (
        "s7_publish (real send)",
        "tgfarm.s7_publish._publish_one send branch raises PublishError by "
        "construction outside live mode; tgfarm.platforms.PUBLISHER.send needs "
        "real platform credentials/sessions.",
    ),
    (
        "s8_analytics (telegram + dzen metrics)",
        "tgfarm.tg_sync.collect_post_stats needs a Telethon session via "
        "tgfarm.tguser.fetch_post_stats; tgfarm.s8_analytics.dzen_web."
        "collect_and_record needs an authenticated D Zen browser session.",
    ),
    (
        "s12_learning (from live engagement)",
        "tgfarm.s12_learning.run consumes tgfarm.s8_analytics.recent_metrics, "
        "which requires published AND measured posts; offline chains have none, "
        "so learning can only report an empty-lesson honesty path.",
    ),
]


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


NON_ERROR_STATUSES = {"ok", "posted", "dry_run", "skipped", "scheduled", "not_reported"}


def categorize_error(status: str, error: str | None) -> str:
    """Coarse, redaction-safe error category for one journal row."""
    if error is None:
        return "none" if status in NON_ERROR_STATUSES else f"status:{status}"
    text = error.lower()
    for needle, category in (
        ("publisherror", "publish_error"),
        ("gate_", "gate_decision"),
        ("timeout", "timeout"),
        ("connect", "network"),
        ("resolve", "network"),
        ("auth", "auth"),
        ("401", "auth"),
        ("403", "auth"),
        ("cancelled", "cancelled"),
        ("sqlite", "storage"),
        ("no such table", "storage"),
        ("schema", "storage"),
    ):
        if needle in text:
            return category
    return "exception"


def counters_from_meta(meta_raw: Any) -> tuple[str, str]:
    """Return (counters_state, unmeasured_columns) from a journal row's meta.

    `meta["counters"]` is tgfarm.core.run.counters_state: one of "measured",
    "partial", "not_reported"; `meta["unmeasured"]` names the placeholder columns.
    """
    if isinstance(meta_raw, str):
        try:
            meta = json.loads(meta_raw)
        except ValueError:
            meta = {}
    else:
        meta = meta_raw or {}
    state = str(meta.get("counters", "?"))
    unmeasured = ",".join(str(name) for name in (meta.get("unmeasured") or [])) or "-"
    return state, unmeasured


def resolve_run_id(conn: sqlite3.Connection, requested: str | None) -> str | None:
    if requested:
        return requested
    row = conn.execute(
        "SELECT run_id FROM pipeline_runs ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None


def report_stages(conn: sqlite3.Connection, run_id: str) -> int:
    rows = conn.execute(
        "SELECT stage, status, items_in, items_out, duration_ms, error, meta, created_at "
        "FROM pipeline_runs WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    if not rows:
        print(f"run {run_id}: no pipeline_runs journal rows (nothing was journaled).")
        return 1
    by_stage = {row["stage"]: row for row in rows}
    print(f"=== chain stages for run {run_id} ({len(rows)} journal rows) ===")
    print(f"{'stage':<15} {'status':<10} {'created_at':<20} {'dur_ms':>7} {'in>out':>9} "
          f"{'counters':<12} {'unmeasured':<11} {'err_cat':<14} note")
    for stage, meaning in CHAIN_STAGES:
        row = by_stage.get(stage)
        if row is None:
            print(f"{stage:<15} {'-':<10} {'-':<20} {'-':>7} {'-':>9} {'-':<12} "
                  f"{'-':<11} {'-':<14} absent from this run ({meaning})")
            continue
        counters, unmeasured = counters_from_meta(row["meta"])
        err_cat = categorize_error(row["status"], row["error"])
        created = str(row["created_at"])[:19]
        inout = f"{row['items_in']}>{row['items_out']}"
        print(f"{stage:<15} {row['status']:<10} {created:<20} {row['duration_ms']:>7} "
              f"{inout:>9} {counters:<12} {unmeasured:<11} {err_cat:<14} {meaning}")
    for stage, row in by_stage.items():
        if stage not in {name for name, _ in CHAIN_STAGES}:
            counters, unmeasured = counters_from_meta(row["meta"])
            err_cat = categorize_error(row["status"], row["error"])
            created = str(row["created_at"])[:19]
            inout = f"{row['items_in']}>{row['items_out']}"
            print(f"{stage:<15} {row['status']:<10} {created:<20} {row['duration_ms']:>7} "
                  f"{inout:>9} {counters:<12} {unmeasured:<11} {err_cat:<14} (extra stage row)")
    return 0


def report_publish_decisions(conn: sqlite3.Connection) -> None:
    print("\n=== publish decisions (posts.attempts / crosspost_logs) ===")
    posts = conn.execute(
        "SELECT public_id, status, attempts, scheduled_at, published_at, reject_reason "
        "FROM posts ORDER BY id DESC LIMIT 20"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]
    if total == 0:
        print("no posts in this DB.")
    else:
        by_status = conn.execute(
            "SELECT status, COUNT(*) AS c FROM posts GROUP BY status ORDER BY c DESC"
        ).fetchall()
        summary = ", ".join(f"{r['status']}={r['c']}" for r in by_status)
        print(f"posts: {total} total ({summary}); showing the 20 newest:")
    for p in posts:
        reason = (p["reject_reason"] or "")[:40]
        print(f"post {p['public_id']:<14} status={p['status']:<16} "
              f"attempts={p['attempts'] if p['attempts'] is not None else '?'} "
              f"scheduled_at={p['scheduled_at']} published_at={p['published_at']} {reason}")
    logs = conn.execute(
        "SELECT platform, status, attempt, error, created_at FROM crosspost_logs "
        "ORDER BY platform, id"
    ).fetchall()
    if not logs:
        print("crosspost_logs: empty (no publish decision was journalled).")
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in logs:
        grouped.setdefault((row["platform"], row["status"]), []).append(row)
    for (platform, status), rows in sorted(grouped.items()):
        attempts = ", ".join(str(r["attempt"]) for r in rows)
        cats = sorted({categorize_error(status, r["error"]) for r in rows})
        print(f"crosspost {platform:<8} status={status:<18} rows={len(rows)} "
              f"attempts=[{attempts}] error_category={cats}")


def report_metrics_and_learning(conn: sqlite3.Connection) -> None:
    print("\n=== metrics / recommendation evidence ===")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "post_stats" in tables:
        n = conn.execute("SELECT COUNT(*) AS c FROM post_stats").fetchone()["c"]
        print(f"post_stats rows: {n} "
              "(0 offline is the honest outcome: no platform was ever contacted)")
    else:
        print("post_stats table absent")
    lesson_tables = [t for t in ("learning_memory", "lessons", "learnings") if t in tables]
    if lesson_tables:
        for name in lesson_tables:
            n = conn.execute(f"SELECT COUNT(*) AS c FROM {name}").fetchone()["c"]
            print(f"{name} rows: {n} (each carries sample_size/confidence when present)")
    else:
        print("no lesson table in this schema; offline recommendations must state "
              "sample_size=0 / not_enough_evidence")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="SQLite file to read (opened strictly mode=ro)")
    parser.add_argument("--run-id", default=None,
                        help="pipeline_runs run_id to report (default: newest)")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    conn = connect_read_only(args.db)
    try:
        run_id = resolve_run_id(conn, args.run_id)
        if run_id is None:
            print("pipeline_runs is empty: no chain cycle has been journaled in this DB yet.")
            rc = 1
        else:
            rc = report_stages(conn, run_id)
        report_publish_decisions(conn)
        report_metrics_and_learning(conn)
        print("\n=== chain links NOT drivable offline (unstubbed; strict-xfail in tests) ===")
        for link, why in UNDRIVEABLE_OFFLINE:
            print(f"- {link}: {why}")
        return rc
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

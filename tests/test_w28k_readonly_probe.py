"""W28-K: the read-only probe an operator runs before trusting a recovery.

"Read-only" is asserted here as an observable property, not as a claim in a
docstring: the file's bytes, length and modification time are unchanged, no
``-wal``/``-shm`` appears beside it, and a write attempt through the same handle
the probe uses is refused by SQLite.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from tgfarm.db.backup_snapshot import create_verified_backup
from tgfarm.db.recovery import (
    OVERALL_FAILED,
    OVERALL_OK,
    OVERALL_UNKNOWN,
    open_read_only,
    probe_backup_restorable,
)

TABLES = ("posts", "raw_items", "pipeline_runs")


def _snapshot(tmp_path: Path, name: str = "snap.db", *, rows: int = 5) -> Path:
    source = tmp_path / f"{name}.src.db"
    with sqlite3.connect(source) as conn:
        for table in TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')
            conn.executemany(
                f'INSERT INTO "{table}" (body) VALUES (?)',
                [(f"{table}-{i}",) for i in range(rows)],
            )
    dest = tmp_path / name
    assert create_verified_backup(source, dest)["verified"] is True
    return dest


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def test_probe_says_restorable_and_counts_rows_it_never_wrote(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    before = _fingerprint(snapshot)
    mtime = snapshot.stat().st_mtime_ns

    probe = probe_backup_restorable(snapshot)

    assert probe["verdict"] == OVERALL_OK, probe["reason"]
    assert probe["restorable"] is True
    assert probe["read_only"] is True
    assert probe["table_count"] == 3
    assert probe["total_rows"] == 15
    assert probe["important_rows"] == {"posts": 5, "raw_items": 5, "pipeline_runs": 5}
    assert probe["empty_tables"] == []
    assert _fingerprint(snapshot) == before
    assert snapshot.stat().st_mtime_ns == mtime
    assert not Path(str(snapshot) + "-wal").exists()
    assert not Path(str(snapshot) + "-shm").exists()


def test_probe_refuses_a_hollow_copy(tmp_path: Path):
    hollow = tmp_path / "hollow.db"
    with sqlite3.connect(hollow) as conn:
        for table in TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')

    probe = probe_backup_restorable(hollow)
    assert probe["restorable"] is False
    assert probe["verdict"] == OVERALL_FAILED
    assert probe["total_rows"] == 0
    assert set(probe["empty_tables"]) == set(TABLES)
    assert "hollow" in probe["reason"]


def test_probe_refuses_a_schema_only_zero_byte_and_missing_file(tmp_path: Path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert probe_backup_restorable(empty)["restorable"] is False
    assert "zero bytes" in probe_backup_restorable(empty)["reason"]

    missing = probe_backup_restorable(tmp_path / "nope.db")
    assert missing["restorable"] is False
    assert missing["verdict"] == OVERALL_UNKNOWN
    assert missing["reason"] == "backup file not found"

    text = tmp_path / "notes.txt"
    text.write_text("channel dump\n", encoding="utf-8")
    notdb = probe_backup_restorable(text)
    assert notdb["restorable"] is False
    assert "magic header" in notdb["reason"]
    # A file with a header but no rows at all is hollow, not restorable data.
    schemaless = tmp_path / "schemaless.db"
    with sqlite3.connect(schemaless) as conn:
        conn.execute("CREATE TABLE posts (x)")
    assert probe_backup_restorable(schemaless)["restorable"] is False


def test_probe_truncation_is_reported_before_any_query(tmp_path: Path):
    snapshot = _snapshot(tmp_path, rows=400)
    cut = tmp_path / "cut.db"
    data = snapshot.read_bytes()
    cut.write_bytes(data[: len(data) // 2])

    probe = probe_backup_restorable(cut)
    assert probe["restorable"] is False
    assert probe["verdict"] == OVERALL_FAILED
    assert "truncated" in probe["reason"]
    assert {"check": "file-geometry", "ok": False} in [
        {k: c[k] for k in ("check", "ok")} for c in probe["checks"]
    ]


def test_deep_probe_runs_integrity_and_a_bounded_budget_is_unknown(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    deep = probe_backup_restorable(snapshot, deep=True, timeout_seconds=30.0)
    assert deep["integrity_check"] == "ok"
    assert deep["restorable"] is True

    # A budget of zero cannot complete a check: that is reported as UNKNOWN,
    # never as a pass and never as a corruption claim.
    starved = probe_backup_restorable(snapshot, deep=True, timeout_seconds=0.0)
    assert starved["integrity_check"] is None
    assert starved["verdict"] == OVERALL_UNKNOWN
    assert starved["restorable"] is None
    assert "budget" in starved["reason"]


def test_probe_reports_a_pending_wal_instead_of_a_clean_bill_of_health(tmp_path: Path):
    live = tmp_path / "live.db"
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
    writer.execute("INSERT INTO posts(body) VALUES ('pending')")
    writer.commit()
    try:
        probe = probe_backup_restorable(live)
        assert probe["restorable"] is False
        assert probe["wal_sidecar"]["wal_pending"] is True
        assert "not self-contained" in probe["reason"] or "wal" in probe["reason"].lower()
    finally:
        writer.close()


def test_read_only_handle_rejects_every_way_of_touching_the_data(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    conn = open_read_only(snapshot)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO posts(body) VALUES ('nope')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM posts")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DROP TABLE posts")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE smuggled (x)")
    finally:
        conn.close()


def test_probe_never_bootstraps_or_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A probe must not reach the application at all.

    If it ever imported ``tgfarm.db.bootstrap`` (or anything that creates or
    migrates the schema), a read-only look at a backup would silently rewrite
    it.  The guard below is itself proven falsifiable: with the modules blocked,
    a direct import *does* fail, and the probe still returns a verdict.
    """
    import sys

    snapshot = _snapshot(tmp_path)
    blocked = ("tgfarm.db.models", "tgfarm.db.bootstrap", "tgfarm.db.session")
    for name in blocked:
        monkeypatch.setitem(sys.modules, name, None)

    with pytest.raises(ImportError):
        import tgfarm.db.bootstrap  # noqa: F401

    probe = probe_backup_restorable(snapshot)
    assert probe["verdict"] == OVERALL_OK, probe["reason"]

    from tgfarm.db import recovery

    verdict = recovery.verify_sqlite_backup_restore(snapshot)
    assert verdict["verdict"] == OVERALL_OK, verdict["reason"]
    for name in blocked:
        assert sys.modules[name] is None, f"{name} was re-imported by the probe"


def test_probe_of_the_backup_the_writer_published_is_consistent_with_the_full_test(tmp_path: Path):
    snapshot = _snapshot(tmp_path, rows=12)
    probe = probe_backup_restorable(snapshot)
    full = create_verified_backup(snapshot, tmp_path / "of-a-backup.db")
    assert probe["verdict"] == OVERALL_OK
    assert full["verified"] is True
    assert probe["total_rows"] == full["source_total_rows"] == 36
    assert (
        probe_backup_restorable(tmp_path / "of-a-backup.db")["important_rows"]["posts"] == 12
    )


def test_probe_does_not_leave_temporary_files_next_to_the_target(tmp_path: Path):
    source = tmp_path / "half.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode = WAL")
    for table in TABLES:
        writer.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')
        writer.execute(f"INSERT INTO \"{table}\"(body) VALUES ('x')")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.close()
    for suffix in ("-wal", "-shm"):
        victim = Path(str(source) + suffix)
        if victim.exists():
            victim.unlink()

    before = sorted(p.name for p in tmp_path.iterdir())
    probe = probe_backup_restorable(source)
    after = sorted(p.name for p in tmp_path.iterdir())

    assert probe["restorable"] is True, probe["reason"]
    assert after == before, "the probe wrote beside the operator's file"
    assert os.path.exists(source)

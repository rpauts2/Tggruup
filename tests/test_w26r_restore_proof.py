"""W26-R: a restore proof that actually proves something.

Acceptance bar exercised entirely offline in tmp_path (never against a live
database):

1. the restore is run against a real backup produced by the project's own
   backup path (``pipeline.backup_job`` -> ``sqlite3.Connection.backup()``);
2. verification checks *content*, not existence: per-table row counts, an
   ordered-row SHA-256 digest of the restored data, schema signature and
   integrity -- an empty (hollow) or truncated snapshot cannot pass;
3. the outcome is stated as data: backup path, table counts, per-table
   deltas, WAL sidecar state and a verdict;
4. every scenario below is a reproducible pytest run.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgfarm.db.backup_mirror import mirror_verified_backup
from tgfarm.db.recovery import (
    restore_to_temporary_database,
    verify_sqlite_backup_restore,
    wal_sidecar_state,
)


def _make_live_wal_db(path: Path) -> sqlite3.Connection:
    """Create a WAL-mode database and leave the writer connection open."""
    connection = sqlite3.connect(str(path), timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL)")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.executemany(
        "INSERT INTO posts(body) VALUES (?)", [("post-1",), ("post-2",), ("post-3",)]
    )
    connection.executemany("INSERT INTO raw_items(kind) VALUES (?)", [("raw-1",), ("raw-2",)])
    connection.execute("INSERT INTO meta VALUES ('schema', 'v1')")
    connection.commit()
    return connection


def _commit_last_wal_transaction(connection: sqlite3.Connection) -> None:
    """Commit one more transaction and keep it uncheckpointed in the -wal."""
    connection.execute("INSERT INTO posts(body) VALUES ('last-in-wal')")
    connection.commit()


def _flat_rows(db_path: Path, table: str) -> list[tuple]:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        quoted = '"' + table.replace('"', '""') + '"'
        return connection.execute(f"SELECT * FROM {quoted} ORDER BY ROWID").fetchall()


def _content_digest(db_path: Path, table: str) -> str:
    return hashlib.sha256(repr(_flat_rows(db_path, table)).encode("utf-8")).hexdigest()


@pytest.fixture
def live_db(tmp_path):
    path = tmp_path / "farm.db"
    connection = _make_live_wal_db(path)
    yield path, connection
    connection.close()


async def _run_project_backup(tmp_path, live: Path, monkeypatch) -> dict:
    from tgfarm import pipeline

    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{live.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_dir=str(backup_dir), keep_backups=3),
    )
    result = await pipeline.backup_job()
    result["backup_dir"] = backup_dir
    return result


@pytest.mark.asyncio
async def test_real_backup_path_snapshot_restores_the_last_wal_transaction(
    tmp_path, live_db, monkeypatch
):
    live, writer = live_db
    _commit_last_wal_transaction(writer)
    wal = live.with_name(live.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0, "expected uncheckpointed WAL frames"

    result = await _run_project_backup(tmp_path, live, monkeypatch)
    assert result["verified"] is True
    assert result["restore_tested"] is True
    snapshot = Path(result["backup"])
    assert snapshot.is_file()

    # The project's backup path (Connection.backup through a live connection)
    # must carry the last committed transaction, not just the main-file pages.
    report = verify_sqlite_backup_restore(snapshot, source_path=live)
    assert report["verified"] is True, report["reason"]
    assert report["verdict"] == "OK"
    assert report["source_verdict"] == "match"
    assert report["total_backup_rows"] == 7
    assert report["table_count"] == 3
    assert not report["hollow_backup"]

    restore = restore_to_temporary_database(snapshot, tmp_path / "restored.sqlite")
    assert restore["ok"] is True, restore["reason"]
    restored = tmp_path / "restored.sqlite"
    assert "last-in-wal" in {row[1] for row in _flat_rows(restored, "posts")}
    for table in ("posts", "raw_items", "meta"):
        assert _content_digest(restored, table) == _content_digest(live, table)
    assert _content_digest(snapshot, "posts") == _content_digest(live, "posts")


@pytest.mark.asyncio
async def test_flat_copy_that_lost_wal_frames_is_caught_by_content_comparison(live_db):
    live, writer = live_db
    # Fold the first pages into the main file, *then* commit the last
    # transaction so it lives only in the -wal: this reproduces the classic
    # half-snapshot where the flat copy is a restorable database that is
    # simply missing the newest committed rows.
    assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[2] >= 0
    _commit_last_wal_transaction(writer)
    flat_copy = live.parent / "flat-copy.db"
    shutil.copy2(live, flat_copy)
    writer.close()  # checkpoints: the live file now really has 4 posts

    sidecar = wal_sidecar_state(flat_copy)
    assert sidecar["declares_wal"] is True
    assert sidecar["wal_missing_sidecar"] is True
    assert sidecar["flat_copy_of_main_file_is_unsafe"] is True

    report = verify_sqlite_backup_restore(flat_copy, source_path=live)
    assert report["verified"] is False
    assert report["source_verdict"] == "mismatch"
    posts_row = next(row for row in report["source_comparison"] if row["table"] == "posts")
    assert posts_row["source_count"] == 4
    assert posts_row["target_count"] == 3
    assert posts_row["delta"] == -1
    # Internally the flat copy is perfectly restorable: only content
    # comparison against the source reveals the lost transaction.

    internal = verify_sqlite_backup_restore(flat_copy)
    assert internal["verified"] is True, "internally consistent copy still restores itself"


def test_hollow_and_zero_byte_snapshots_cannot_pass(tmp_path):
    hollow = tmp_path / "hollow.db"
    with closing(sqlite3.connect(str(hollow))) as connection:
        connection.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, kind TEXT)")
        connection.commit()
    report = verify_sqlite_backup_restore(hollow)
    assert report["verified"] is False
    assert report["hollow_backup"] is True
    assert "non-empty-data" in report["failed_checks"]
    assert "hollow" in report["reason"]

    empty_file = tmp_path / "empty.db"
    empty_file.write_bytes(b"")
    zero = verify_sqlite_backup_restore(empty_file)
    assert zero["verified"] is False
    assert "zero-byte-file" in zero["failed_checks"]


def test_truncated_snapshot_cannot_pass_even_with_a_good_row_count(tmp_path):
    source = tmp_path / "donor.db"
    with closing(sqlite3.connect(str(source))) as connection:
        connection.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO posts(body) VALUES (?)", [(f"row-{n}",) for n in range(2000)]
        )
        connection.commit()
    snapshot = tmp_path / "snapshot.db"
    assert restore_to_temporary_database(source, snapshot)["ok"] is True

    data = snapshot.read_bytes()
    (tmp_path / "truncated.db").write_bytes(data[: len(data) // 2])
    report = verify_sqlite_backup_restore(tmp_path / "truncated.db")
    assert report["verified"] is False
    assert report["geometry"]["truncated"] is True
    assert "file-truncated" in report["failed_checks"]


def test_row_truncated_snapshot_passes_internally_but_fails_vs_source(tmp_path):
    live = tmp_path / "live.db"
    with closing(sqlite3.connect(str(live))) as connection:
        connection.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO posts(body) VALUES (?)", [(f"p{n}",) for n in range(10)]
        )
        connection.execute("INSERT INTO meta VALUES ('schema', 'v1')")
        connection.commit()
    snapshot = tmp_path / "snapshot.db"
    assert restore_to_temporary_database(live, snapshot)["ok"] is True
    with closing(sqlite3.connect(str(snapshot))) as connection:
        connection.execute("DELETE FROM posts WHERE id > 5")
        connection.commit()

    internal = verify_sqlite_backup_restore(snapshot)
    assert internal["verified"] is True, "internally consistent: this is the known blind spot"

    report = verify_sqlite_backup_restore(snapshot, source_path=live)
    assert report["verified"] is False
    assert report["source_verdict"] == "mismatch"
    assert report["verdict"] == "FAILED"
    posts_row = next(row for row in report["source_comparison"] if row["table"] == "posts")
    assert (posts_row["source_count"], posts_row["target_count"], posts_row["delta"]) == (10, 5, -5)


@pytest.mark.asyncio
async def test_restore_outcome_is_reported_as_data_and_survives_the_mirror(
    tmp_path, live_db, monkeypatch
):
    live, writer = live_db
    _commit_last_wal_transaction(writer)
    result = await _run_project_backup(tmp_path, live, monkeypatch)
    snapshot = Path(result["backup"])

    mirror_dir = tmp_path / "mirror"
    mirrored = mirror_verified_backup(
        snapshot, mirror_dir, keep=3, local_backup_dir=result["backup_dir"]
    )
    assert mirrored["verified"] is True, mirrored["reason"]
    mirror_copy = Path(mirrored["backup"])

    report = verify_sqlite_backup_restore(mirror_copy, source_path=live)
    assert report["verified"] is True, report["reason"]
    assert report["backup"] == str(mirror_copy.resolve())
    assert report["integrity_check"] == "ok"
    assert report["restored_integrity_check"] == "ok"
    assert report["schema_object_count"] == 3
    assert report["restored_summary"]["match"] == 3
    assert report["source_summary"]["verified_rows"] == 7
    assert report["source_summary"]["clean"] is True
    assert {row["table"]: row["source_count"] for row in report["source_comparison"]} == {
        "meta": 1,
        "posts": 4,
        "raw_items": 2,
    }
    assert report["wal_sidecar"]["database"] == str(mirror_copy)
    assert _content_digest(mirror_copy, "posts") == _content_digest(live, "posts")

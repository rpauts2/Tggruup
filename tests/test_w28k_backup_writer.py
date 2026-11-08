"""W28-K: a backup is only a backup if its rows survived.

These tests never touch the shared session database and never import the
application's models: every fixture is a hand-made SQLite file inside
``tmp_path``, so the assertions are about *this* data and not about whatever
else the suite has written.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from tgfarm.db.backup_snapshot import (
    BACKUP_METHOD,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNKNOWN,
    create_verified_backup,
    exit_code_for,
    flat_copy_as_backup,
    verify_backup_file,
)
from tgfarm.db.recovery import (
    OVERALL_FAILED,
    OVERALL_OK,
    OVERALL_UNKNOWN,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_MISSING,
    probe_backup_restorable,
    sqlite_file_geometry,
    sqlite_header_ok,
)

TABLES = ("posts", "raw_items", "pipeline_runs")


def _row_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
        return {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in TABLES
        }


def _seed(conn: sqlite3.Connection, offset: int, batch: int) -> None:
    for table in TABLES:
        conn.executemany(
            f'INSERT INTO "{table}" (body) VALUES (?)',
            [(f"{table}-{i}",) for i in range(offset, offset + batch)],
        )
    conn.commit()


def _wal_source(tmp_path: Path, *, checkpointed: int, pending: int) -> tuple[Path, sqlite3.Connection]:
    """A live-style WAL database: some rows checkpointed, more rows still in -wal.

    The writer connection is returned still open on purpose -- that is the
    situation the operator was in when a ``cp`` of the live database produced a
    copy with zero posts.
    """
    path = tmp_path / "live.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    for table in TABLES:
        conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')
    conn.commit()
    _seed(conn, 0, checkpointed)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _seed(conn, checkpointed, pending)
    return path, conn


def test_create_verified_backup_copies_every_row_and_verdicts_ok(tmp_path: Path) -> None:
    source, writer = _wal_source(tmp_path, checkpointed=5, pending=5)
    try:
        report = create_verified_backup(source, tmp_path / "backup.db")
    finally:
        writer.close()

    assert report["method"] == BACKUP_METHOD
    assert report["verdict"] == OVERALL_OK, report["reason"]
    assert report["verified"] is True
    assert report["reason"] == "ok"
    assert report["data_loss_detected"] is False
    assert report["failed_checks"] == []
    assert _row_counts(tmp_path / "backup.db") == {table: 10 for table in TABLES}


def test_report_records_the_mechanism_and_never_a_byte_size_verdict(tmp_path: Path) -> None:
    source = tmp_path / "plain.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('one')")

    report = create_verified_backup(source, tmp_path / "b.db")

    assert report["method"] == "sqlite3.Connection.backup()"
    # The size is recorded as an observation, and no verdict key is derived
    # from it: a 0-row snapshot with the same size must fail the same way.
    assert report["size_bytes"] == (tmp_path / "b.db").stat().st_size
    assert report["summary"]["verified_rows"] == 1


def test_create_verified_backup_reports_per_table_truth(tmp_path: Path) -> None:
    source = tmp_path / "plain.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('a')")
        conn.executemany(
            "INSERT INTO raw_items(body) VALUES (?)", [("r1",), ("r2",), ("r3",)]
        )

    report = create_verified_backup(source, tmp_path / "b.db")
    per_table = {row["table"]: row for row in report["per_table"]}

    assert set(per_table) == set(TABLES)
    assert per_table["raw_items"]["source_count"] == 3
    assert per_table["raw_items"]["target_count"] == 3
    assert per_table["raw_items"]["delta"] == 0
    assert per_table["raw_items"]["verdict"] == VERDICT_MATCH
    assert per_table["posts"]["source_count"] == 1
    assert per_table["pipeline_runs"]["source_count"] == 0
    assert report["summary"] == {
        "table_total": 3,
        "match": 3,
        "mismatch": 0,
        "missing": 0,
        "unknown": 0,
        "verified_rows": 4,
        "clean": True,
    }


def test_flat_file_copy_of_a_live_wal_database_is_not_accepted_as_a_backup(tmp_path: Path) -> None:
    """The incident, made reproducible: cp of a WAL db loses the pending frames.

    The copy answers ``integrity_check`` with ``ok`` -- which is exactly why an
    integrity-based verdict was not enough.
    """
    source, writer = _wal_source(tmp_path, checkpointed=5, pending=5)
    copy = tmp_path / "cp.db"
    try:
        flat_copy_as_backup(source, copy)
        assert copy.stat().st_size > 0
        # The copy is a well-formed SQLite file with the right schema...
        assert probe_backup_restorable(copy)["checks"]

        verdict = verify_backup_file(copy, source_path=source)
    finally:
        writer.close()

    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    per_table = {row["table"]: row for row in verdict["source_comparison"]}
    # ...but it holds a fraction of the rows, and that is what the verdict reads.
    assert per_table["posts"]["source_count"] == 10
    assert per_table["posts"]["target_count"] == 5
    assert per_table["posts"]["delta"] == -5
    assert per_table["posts"]["verdict"] == VERDICT_MISMATCH
    assert "does not match the source" in verdict["reason"]
    assert "delta=-5" in verdict["reason"]
    assert verdict["integrity_check"] == "ok", "integrity alone was never the proof"
    # The half database was readable only because its sidecar is missing: the
    # report says so instead of hiding it.
    assert verdict["wal_sidecar"]["wal_missing_sidecar"] is True
    assert verdict["access"]["fallback_used"] is True
    assert verdict["access"]["access"] == "disposable-copy"


def test_a_copy_of_an_uncheckpointed_source_is_caught_as_hollow(tmp_path: Path) -> None:
    """Worst case from the incident report: schema never reached the main file."""
    source = tmp_path / "live2.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
    writer.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, body TEXT)")
    writer.execute("CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, body TEXT)")
    _seed(writer, 0, 40)
    copy = tmp_path / "cp2.db"
    try:
        flat_copy_as_backup(source, copy)
        verdict = verify_backup_file(copy, source_path=source)
    finally:
        writer.close()

    assert verdict["verified"] is False
    assert verdict["verdict"] in {OVERALL_FAILED, OVERALL_UNKNOWN}
    assert verdict["restore_tested"] is False
    assert "no application tables" in verdict["reason"] or "hollow" in verdict["reason"]


def test_verify_against_source_accepts_a_real_connection_backup_of_the_same_source(tmp_path: Path) -> None:
    source, writer = _wal_source(tmp_path, checkpointed=5, pending=5)
    try:
        backup = tmp_path / "good.db"
        made = create_verified_backup(source, backup)
        assert made["verified"] is True
        again = verify_backup_file(backup, source_path=source)
    finally:
        writer.close()

    assert again["verified"] is True, again["reason"]
    assert again["verdict"] == OVERALL_OK
    assert again["source_verdict"] == VERDICT_MATCH
    assert all(row["verdict"] == VERDICT_MATCH for row in again["source_comparison"])


def test_backup_of_an_empty_source_is_a_clear_non_zero_verdict(tmp_path: Path) -> None:
    source = tmp_path / "empty.db"
    with sqlite3.connect(source) as conn:
        for table in TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')

    report = create_verified_backup(source, tmp_path / "b.db")

    assert report["verified"] is False
    assert report["verdict"] == OVERALL_FAILED
    assert report["hollow_backup"] is True
    assert report["source_total_rows"] == 0
    assert "empty" in report["reason"]
    # Nothing was published under the final name.
    assert not (tmp_path / "b.db").exists()


def test_restoring_a_hollow_snapshot_reports_failed_not_ok(tmp_path: Path) -> None:
    empty_source = tmp_path / "es.db"
    with sqlite3.connect(empty_source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, body TEXT)")
    snapshot = tmp_path / "hollow.db"
    shutil.copy2(empty_source, snapshot)

    verdict = verify_backup_file(snapshot)

    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    assert verdict["restore_tested"] is True, "the restore ran; it proved nothing survived"
    assert verdict["hollow_backup"] is True
    assert "hollow" in verdict["reason"]
    assert "0 rows" in verdict["reason"]


def test_truncated_snapshot_is_named_as_truncated(tmp_path: Path) -> None:
    source = tmp_path / "s.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            "INSERT INTO posts(body) VALUES (?)", [(f"p{i}",) for i in range(500)]
        )
    cut = tmp_path / "cut.db"
    data = source.read_bytes()
    cut.write_bytes(data[: len(data) // 2])

    verdict = verify_backup_file(cut)

    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    assert verdict["failed_checks"] == ["file-truncated"]
    assert "truncated mid-file" in verdict["reason"]
    assert "declares" in verdict["reason"]

    from tgfarm.db.recovery import sqlite_file_geometry

    geometry = sqlite_file_geometry(cut)
    assert geometry["truncated"] is True
    assert geometry["actual_bytes"] < geometry["declared_bytes"]
    assert sqlite_file_geometry(source)["truncated"] is False


def test_source_that_is_not_a_sqlite_file_is_refused_before_being_opened(tmp_path: Path) -> None:
    text_file = tmp_path / "notes.db"
    text_file.write_text("this is a channel export, not a database\n", encoding="utf-8")

    verdict = verify_backup_file(text_file)
    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    assert verdict["failed_checks"] == ["sqlite-header"]
    assert "magic header" in verdict["reason"]

    made = create_verified_backup(text_file, tmp_path / "b.db")
    assert made["verified"] is False
    assert made["failed_checks"] == ["source-is-sqlite"]
    assert "magic header" in made["reason"]
    assert not (tmp_path / "b.db").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_a_corrupted_freelist_is_caught_by_integrity_and_by_nothing_else(tmp_path: Path):
    """Same size, same header, same page count -- and a database that is broken.

    This is the case a size/existence verdict cannot see and an ignored
    ``integrity_check`` answer cannot survive: SQLite *returns* the complaint
    instead of raising, so the only way to notice is to read what it said.
    """
    source = tmp_path / "s.db"
    with sqlite3.connect(source) as conn:
        for table in TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')
            conn.executemany(
                f'INSERT INTO "{table}" (body) VALUES (?)',
                [(f"{table}-{i}-{'x' * 100}",) for i in range(120)],
            )
    snapshot = tmp_path / "snap.db"
    assert create_verified_backup(source, snapshot)["verified"] is True
    good_bytes = snapshot.read_bytes()

    damaged = tmp_path / "damaged.db"
    flipped = bytearray(good_bytes)
    flipped[36:40] = b"\xff\xff\xff\xff"  # freelist page count
    damaged.write_bytes(bytes(flipped))

    assert damaged.stat().st_size == snapshot.stat().st_size
    assert sqlite_file_geometry(damaged)["truncated"] is False
    assert sqlite_header_ok(damaged)

    verdict = verify_backup_file(damaged)
    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    assert "integrity_check" in verdict["failed_checks"]
    assert verdict["integrity_check"] != "ok"
    assert "Freelist" in verdict["integrity_check"]

    # The cheap probe refuses it too, so no route quietly calls this restorable.
    assert probe_backup_restorable(damaged, deep=True)["restorable"] is False


def test_missing_source_file_yields_unknown_not_match(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    with sqlite3.connect(real) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('keep me')")
    backup = tmp_path / "b.db"
    assert create_verified_backup(real, backup)["verified"] is True

    verdict = verify_backup_file(backup, source_path=tmp_path / "does-not-exist.db")

    assert verdict["verified"] is False
    assert verdict["source_verdict"] == OVERALL_UNKNOWN
    assert verdict["verdict"] == OVERALL_UNKNOWN
    assert "source could not be read" in verdict["reason"]
    assert verdict["source"]["reason"] == "source database file not found"
    assert "source-readable" in verdict["failed_checks"]
    # Not a single table claims to match a source that was never read.
    assert all(
        row["verdict"] != VERDICT_MATCH for row in verdict["source_comparison"]
    ) or verdict["source_comparison"] == []
    assert exit_code_for(verdict) == EXIT_UNKNOWN


def test_source_that_is_not_a_database_is_also_unknown(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    with sqlite3.connect(real) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('x')")
    backup = tmp_path / "b.db"
    assert create_verified_backup(real, backup)["verified"] is True

    # A file carrying the SQLite magic but no usable database behind it.
    forged = tmp_path / "forged.db"
    forged.write_bytes(b"SQLite format 3\0" + b"\x00" * 96 + b"not really a database")
    verdict = verify_backup_file(backup, source_path=forged)
    assert verdict["source_verdict"] == OVERALL_UNKNOWN
    assert verdict["verdict"] == OVERALL_UNKNOWN
    assert verdict["source"]["readable"] is False
    assert verdict["source"]["reason"], "an unexplained UNKNOWN is a bug"

    # A file without the magic at all is refused before anything is opened.
    plain = tmp_path / "plain.txt"
    plain.write_text("hello\n", encoding="utf-8")
    verdict2 = verify_backup_file(backup, source_path=plain)
    assert verdict2["source_verdict"] == OVERALL_UNKNOWN
    assert "magic header" in verdict2["source"]["reason"]


def test_backup_refuses_to_overwrite_its_own_source(tmp_path: Path) -> None:
    source = tmp_path / "only.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('precious')")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    report = create_verified_backup(source, source)

    assert report["verified"] is False
    assert report["failed_checks"] == ["dest-is-source"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_failed_backup_never_leaves_a_file_the_retention_glob_would_find(tmp_path: Path) -> None:
    """A half-written snapshot must not be able to pose as a backup."""
    source = tmp_path / "s.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('only one row')")

    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, body TEXT)")

    assert create_verified_backup(empty, tmp_path / "tgfarm-x.db")["verified"] is False
    assert not list(tmp_path.glob("tgfarm-*.db"))
    assert not list(tmp_path.glob(".*.tmp")), "temp file survived a failed backup"


def test_exit_code_separates_proven_from_not_proven(tmp_path: Path) -> None:
    source = tmp_path / "s.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO posts(body) VALUES ('row')")
    good = tmp_path / "good.db"
    assert create_verified_backup(source, good)["verified"] is True

    assert exit_code_for(verify_backup_file(good)) == EXIT_OK
    assert exit_code_for(verify_backup_file(good, source_path=tmp_path / "gone.db")) == EXIT_UNKNOWN
    hollow = tmp_path / "hollow.db"
    with sqlite3.connect(hollow) as conn:
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE raw_items (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, body TEXT)")
    assert exit_code_for(verify_backup_file(hollow)) == EXIT_FAILED
    assert exit_code_for(None) == EXIT_FAILED


def test_table_deleted_from_a_snapshot_is_reported_missing_not_zero(tmp_path: Path) -> None:
    source = tmp_path / "s.db"
    with sqlite3.connect(source) as conn:
        for table in TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')
            conn.execute(f"INSERT INTO \"{table}\"(body) VALUES ('r')")
    snapshot = tmp_path / "lost-table.db"
    with sqlite3.connect(source) as reader, sqlite3.connect(snapshot) as writer_conn:
        reader.backup(writer_conn)
    with sqlite3.connect(snapshot) as conn:
        conn.execute("DROP TABLE raw_items")

    verdict = verify_backup_file(snapshot, source_path=source)

    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    row = {item["table"]: item for item in verdict["source_comparison"]}["raw_items"]
    assert row["verdict"] == VERDICT_MISSING
    assert row["source_count"] == 1
    assert row["target_count"] is None, "a dropped table is not a table with 0 rows"
    assert row["delta"] is None
    assert verdict["source_summary"]["missing"] == 1
    assert "raw_items" in verdict["reason"]


def test_rows_deleted_from_one_table_marks_only_that_table(tmp_path: Path) -> None:
    source, writer = _wal_source(tmp_path, checkpointed=4, pending=0)
    try:
        backup = tmp_path / "b.db"
        assert create_verified_backup(source, backup)["verified"] is True
        with sqlite3.connect(backup) as conn:
            conn.execute("DELETE FROM posts WHERE id > 2")
            conn.commit()
        verdict = verify_backup_file(backup, source_path=source)
    finally:
        writer.close()

    rows = {row["table"]: row for row in verdict["source_comparison"]}
    assert rows["posts"]["verdict"] == VERDICT_MISMATCH
    assert rows["posts"]["target_count"] == 2
    assert rows["raw_items"]["verdict"] == VERDICT_MATCH
    assert rows["pipeline_runs"]["verdict"] == VERDICT_MATCH
    assert verdict["source_summary"]["mismatch"] == 1
    assert verdict["verified"] is False

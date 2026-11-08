"""W28-K: the verdict vocabulary, and the discipline behind it.

Every test here checks that a report *says what it measured* -- and that a
number it could not measure is null rather than zero.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tgfarm.db.backup_snapshot import create_verified_backup, verify_backup_file
from tgfarm.db.recovery import (
    OVERALL_FAILED,
    OVERALL_OK,
    OVERALL_UNKNOWN,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_MISSING,
    VERDICT_UNKNOWN,
    compare_table_counts,
    sqlite_file_geometry,
    summarize_comparison,
    wal_sidecar_state,
)

TABLES = ("posts", "raw_items", "pipeline_runs")


def _farm_db(path: Path, *, rows: int = 3, tables: tuple[str, ...] = TABLES) -> Path:
    with sqlite3.connect(path) as conn:
        for table in tables:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT)')
            conn.executemany(
                f'INSERT INTO "{table}" (body) VALUES (?)',
                [(f"{table}-{i}",) for i in range(rows)],
            )
    return path


def test_verdict_vocabulary_is_exhaustive_and_named():
    rows = compare_table_counts({"posts": 5, "raw_items": 5}, {"posts": 5, "media": 1})
    by_table = {row["table"]: row for row in rows}
    assert by_table["posts"]["verdict"] == VERDICT_MATCH
    assert by_table["media"]["verdict"] == VERDICT_MISSING
    assert by_table["raw_items"]["verdict"] == VERDICT_MISSING
    summary = summarize_comparison(rows)
    assert summary == {
        "table_total": 3,
        "match": 1,
        "mismatch": 0,
        "missing": 2,
        "unknown": 0,
        "verified_rows": 5,
        "clean": False,
    }


def test_delta_is_signed_and_mismatch_is_named_per_table():
    rows = compare_table_counts(
        {"posts": 10, "raw_items": 10, "pipeline_runs": 10},
        {"posts": 4, "raw_items": 12, "pipeline_runs": 10},
    )
    by_table = {row["table"]: row for row in rows}
    assert by_table["posts"]["delta"] == -6
    assert by_table["posts"]["verdict"] == VERDICT_MISMATCH
    assert by_table["raw_items"]["delta"] == 2
    assert by_table["pipeline_runs"]["verdict"] == VERDICT_MATCH
    assert summarize_comparison(rows)["clean"] is False


def test_a_count_that_was_never_measured_is_none_and_never_read_as_zero():
    rows = compare_table_counts({"posts": 7}, {})
    row = rows[0]
    assert row["target_count"] is None
    assert row["delta"] is None
    assert row["verdict"] != VERDICT_MATCH
    # None must not be laundered into a zero anywhere in the summary either.
    assert summarize_comparison(rows)["verified_rows"] == 0
    assert row["present_in_target"] is False


def test_source_count_is_null_for_a_table_the_source_does_not_have():
    rows = compare_table_counts({}, {"editorial_plans": 4})
    row = rows[0]
    assert row["source_count"] is None
    assert row["target_count"] == 4
    assert row["verdict"] == VERDICT_MISSING
    assert row["delta"] is None


def test_report_exposes_source_target_delta_and_verdict_for_every_table(tmp_path: Path):
    source = _farm_db(tmp_path / "src.db", rows=6)
    backup = tmp_path / "b.db"
    report = create_verified_backup(source, backup)
    assert report["verified"] is True

    required = {"table", "source_count", "target_count", "delta", "verdict"}
    for row in report["per_table"]:
        assert required <= set(row)
    assert {row["table"] for row in report["per_table"]} == set(TABLES)
    assert [row["verdict"] for row in report["per_table"]] == [VERDICT_MATCH] * 3
    assert report["summary"]["verified_rows"] == 18


def test_overall_verdict_is_unknown_not_match_when_the_source_is_unreadable(tmp_path: Path):
    good = _farm_db(tmp_path / "good.db", rows=2)
    snapshot = tmp_path / "snap.db"
    assert create_verified_backup(good, snapshot)["verified"] is True

    unreadable = tmp_path / "unreadable.db"
    unreadable.write_bytes(b"PK\x03\x04 zip archive pretending to be a database")
    verdict = verify_backup_file(snapshot, source_path=unreadable)

    assert verdict["verdict"] == OVERALL_UNKNOWN
    assert verdict["verified"] is False
    assert verdict["source_verdict"] == VERDICT_UNKNOWN
    assert "NOT proven" in verdict["reason"]
    # The distinction the whole package exists for: UNKNOWN is neither OK nor a
    # mismatch, and it never carries a match claim.
    assert verdict["source_comparison"] == []
    assert verdict["source"]["per_table"] == []


def test_verdict_is_ok_only_when_every_check_actually_ran(tmp_path: Path):
    source = _farm_db(tmp_path / "s.db", rows=4)
    backup = tmp_path / "b.db"
    verdict = verify_backup_file(create_verified_backup(source, backup)["backup"])
    assert verdict["verdict"] == OVERALL_OK
    assert verdict["failed_checks"] == []
    assert verdict["restore_tested"] is True
    assert verdict["hollow_backup"] is False
    assert verdict["schema_match"] is True
    assert verdict["foreign_key_violations"] == 0


def test_wal_sidecar_condition_is_stated_in_the_verdict(tmp_path: Path):
    source = tmp_path / "wal.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
    writer.execute("INSERT INTO posts(body) VALUES ('in the wal')")
    writer.commit()
    state = wal_sidecar_state(source)
    assert state["declares_wal"] is True
    assert state["wal_pending"] is True
    assert state["flat_copy_of_main_file_is_unsafe"] is True
    assert "non-empty -wal" in state["note"]
    writer.close()

    # A snapshot published by the verified writer is self-contained.
    backup = tmp_path / "b.db"
    report = create_verified_backup(source, backup)
    assert report["verified"] is True, report["reason"]
    verify = verify_backup_file(backup)
    assert verify["wal_sidecar"]["wal_pending"] is False
    assert verify["wal_sidecar"]["flat_copy_of_main_file_is_unsafe"] is False


def test_missing_sidecar_is_named_as_a_half_database(tmp_path: Path):
    main = tmp_path / "lonely.db"
    writer = sqlite3.connect(main)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT)")
    writer.execute("INSERT INTO posts(body) VALUES ('x')")
    writer.commit()
    writer.close()
    for suffix in ("-wal", "-shm"):
        victim = Path(str(main) + suffix)
        if victim.exists():
            victim.unlink()

    state = wal_sidecar_state(main)
    assert state["declares_wal"] is True
    assert state["wal_present"] is False
    assert state["wal_missing_sidecar"] is True
    assert "no -wal beside it" in state["note"]
    assert state["flat_copy_of_main_file_is_unsafe"] is True


def test_an_older_backup_schema_than_the_models_is_reported_not_crashed(tmp_path: Path):
    """The realistic restore: the snapshot predates migrations the code has now.

    The live farm can lag the models by pending migrations, so a comparison must
    name the extra tables instead of raising on a column that does not exist.
    """
    old = _farm_db(tmp_path / "old.db", rows=3, tables=("posts", "raw_items"))
    newer = _farm_db(tmp_path / "new.db", rows=3, tables=TABLES)

    # 1. An old snapshot is still internally restorable: that is a true claim.
    standalone = verify_backup_file(old)
    assert standalone["verified"] is True, standalone["reason"]
    assert standalone["table_count"] == 2

    # 2. Compared against a database that has since been migrated, the snapshot
    #    is honestly incomplete and the report says which table.
    verdict = verify_backup_file(old, source_path=newer)
    assert verdict["verified"] is False
    assert verdict["verdict"] == OVERALL_FAILED
    assert verdict["source_summary"]["missing"] == 1
    missing = [row for row in verdict["source_comparison"] if row["verdict"] == VERDICT_MISSING]
    assert [row["table"] for row in missing] == ["pipeline_runs"]
    assert missing[0]["target_count"] is None
    assert "pipeline_runs" in verdict["reason"]


def test_geometry_reads_the_header_the_way_sqlite_does(tmp_path: Path):
    good = _farm_db(tmp_path / "g.db", rows=200)
    geometry = sqlite_file_geometry(good)
    assert geometry["truncated"] is False
    assert geometry["declared_bytes"] == geometry["actual_bytes"]
    assert geometry["page_size"] * geometry["declared_page_count"] == geometry["actual_bytes"]

    short = tmp_path / "short.db"
    short.write_bytes(good.read_bytes()[: geometry["actual_bytes"] // 3])
    cut = sqlite_file_geometry(short)
    assert cut["truncated"] is True
    assert cut["declared_bytes"] > cut["actual_bytes"]

    # A file shorter than the header itself is also truncation, not a mystery.
    stub = tmp_path / "stub.db"
    stub.write_bytes(b"SQLite format 3\x00tiny")
    assert sqlite_file_geometry(stub)["truncated"] is True
    assert not sqlite_file_geometry(tmp_path / "nope.db")["truncated"]
    assert sqlite_file_geometry(tmp_path / "nope.db")["reason"]

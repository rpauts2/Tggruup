"""Read-only verification that a SQLite backup can be restored successfully."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from tgfarm.core.security import redact_sensitive_text


def _table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _row_counts(connection: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        result[table] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return result


def _schema_signature(connection: sqlite3.Connection) -> list[tuple]:
    """Include application tables, indexes, views and triggers in the comparison."""
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    ]


def verify_sqlite_backup_restore(backup_path: str | Path) -> dict:
    """Restore a backup to a disposable temp file and compare schema/data counts.

    The supplied backup is opened read-only and never modified. The restored
    copy lives in a TemporaryDirectory and is removed even when verification fails.
    """
    path = Path(backup_path).expanduser().resolve()
    if not path.is_file():
        return {
            "verified": False,
            "restore_tested": False,
            "reason": "backup file not found",
        }

    try:
        source_uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
            source_integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
            if source_integrity != "ok":
                return {
                    "verified": False,
                    "restore_tested": False,
                    "integrity_check": source_integrity,
                    "reason": "backup integrity_check did not return ok",
                }
            source_tables = _table_names(source)
            if not source_tables:
                return {
                    "verified": False,
                    "restore_tested": False,
                    "integrity_check": source_integrity,
                    "reason": "backup contains no application tables",
                }
            source_counts = _row_counts(source, source_tables)
            source_schema = _schema_signature(source)

            with tempfile.TemporaryDirectory(prefix="tgfarm-restore-test-") as directory:
                restored_path = Path(directory) / "restored.sqlite"
                with closing(sqlite3.connect(str(restored_path), timeout=30)) as restored:
                    source.backup(restored)
                    restored.commit()
                    restored_integrity = str(
                        restored.execute("PRAGMA integrity_check").fetchone()[0]
                    )
                    restored_tables = _table_names(restored)
                    restored_counts = _row_counts(restored, restored_tables)
                    restored_schema = _schema_signature(restored)
                    foreign_key_violations = restored.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()

                tables_match = source_tables == restored_tables
                row_counts_match = source_counts == restored_counts
                schema_match = source_schema == restored_schema
                foreign_keys_ok = not foreign_key_violations
                verified = (
                    restored_integrity == "ok"
                    and tables_match
                    and row_counts_match
                    and schema_match
                    and foreign_keys_ok
                )
                return {
                    "verified": verified,
                    "restore_tested": True,
                    "integrity_check": source_integrity,
                    "restored_integrity_check": restored_integrity,
                    "table_count": len(source_tables),
                    "tables_match": tables_match,
                    "row_counts_match": row_counts_match,
                    "schema_match": schema_match,
                    "schema_object_count": len(source_schema),
                    "foreign_key_violations": len(foreign_key_violations),
                    "reason": "ok" if verified else "restored copy differs or failed integrity checks",
                }
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        return {
            "verified": False,
            "restore_tested": False,
            "reason": redact_sensitive_text(exc, limit=240),
        }

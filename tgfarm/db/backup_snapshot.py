"""The only backup writer this project should trust.

Why this file exists: ``cp live.db backup.db`` is not a backup of a database in
WAL mode.  Committed frames can still be sitting in ``live.db-wal``, so the bare
main file can open cleanly, answer ``PRAGMA integrity_check`` with ``ok`` and
still contain zero posts -- which is precisely what a ``cp`` of this farm's live
433 MB database produced once.  ``sqlite3.Connection.backup()`` reads *through*
the source connection, WAL included, and is the only mechanism used here.

The other half of the guarantee: after writing, the copy is re-opened read-only
and compared, table by table, against the counts measured from the source
immediately before the copy.  Counts decide the verdict; file size never does.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from tgfarm.db.recovery import (
    DEFAULT_IMPORTANT_TABLES,
    OVERALL_FAILED,
    OVERALL_OK,
    OVERALL_UNKNOWN,
    VERDICT_MISSING,
    compare_table_counts,
    describe_differences,
    failure_reason,
    integrity_check,
    open_read_only,
    schema_signature,
    sqlite_header_ok,
    summarize_comparison,
    table_names,
    table_row_counts,
    verify_sqlite_backup_restore,
    wal_sidecar_state,
)

__all__ = [
    "BACKUP_METHOD",
    "create_verified_backup",
    "exit_code_for",
    "flat_copy_as_backup",
    "verify_backup_file",
]

#: The mechanism this module uses, recorded in every report so a reader can see
#: that a snapshot was produced through a SQLite connection and not by copying
#: bytes.
BACKUP_METHOD = "sqlite3.Connection.backup()"

#: Exit codes for the CLI seam.  "Not proven" must not share the success code of
#: a proven backup, and should be tellable apart from a proven mismatch.
EXIT_OK = 0
EXIT_FAILED = 2
EXIT_UNKNOWN = 3


class _HollowSource(RuntimeError):
    """The source holds no data, so no snapshot of it can be called a backup."""


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return left == right


def exit_code_for(report: dict | None) -> int:
    """Translate a report into a process exit code without inventing success."""
    verdict = (report or {}).get("verdict")
    if (report or {}).get("verified") is True and verdict == OVERALL_OK:
        return EXIT_OK
    if verdict == OVERALL_UNKNOWN:
        return EXIT_UNKNOWN
    return EXIT_FAILED


def flat_copy_as_backup(source_path: str | Path, dest_path: str | Path) -> dict:
    """Deliberate anti-pattern: a ``shutil.copy`` presented as a backup.

    Exists so tests and operators can *see* the hole instead of being told about
    it.  It returns no verdict at all -- feed the result to
    :func:`verify_backup_file`, which is where the hollow copy gets caught.
    """
    shutil.copy2(Path(source_path), Path(dest_path))
    return {
        "backup": str(Path(dest_path)),
        "method": "shutil.copy2",
        "verified": False,
        "verdict": OVERALL_UNKNOWN,
        "reason": "a file copy was performed; it is not a backup until proven by row counts",
    }


def verify_backup_file(
    candidate_path: str | Path,
    *,
    source_path: str | Path | None = None,
    important_tables: tuple[str, ...] = DEFAULT_IMPORTANT_TABLES,
) -> dict:
    """Judge a file as a backup, by data -- never by existence or size.

    This is the entry point the ``backup-test`` verb should call.  A flat copy
    handed to it is measured against the source counts and comes back
    ``FAILED``; a source that cannot be read comes back ``UNKNOWN``, never
    ``match``.
    """
    return verify_sqlite_backup_restore(
        candidate_path,
        source_path=source_path,
        important_tables=important_tables,
    )


def create_verified_backup(
    source_path: str | Path,
    dest_path: str | Path,
    *,
    important_tables: tuple[str, ...] = DEFAULT_IMPORTANT_TABLES,
    restore_test: bool = True,
    timeout: int = 30,
) -> dict:
    """Back up a SQLite database and prove the copy holds the source's rows.

    The source is opened strictly read-only (``mode=ro``), so this function
    cannot write to, VACUUM, re-journal or migrate the live farm database even
    if a caller passes it by mistake.  The copy is written to a hidden temporary
    file in the destination directory and only published with its final name by
    ``os.replace`` after every check has passed.

    Because the source may be live, counts measured *before* the copy can drift
    while it runs.  That is reported, not hidden: negative drift (the copy holds
    fewer rows than the source had) is data loss and fails, while positive drift
    is recorded in ``live_drift`` with an explanation.
    """
    source = Path(source_path).expanduser().resolve()
    dest = Path(dest_path).expanduser().resolve()
    report: dict = {
        "source": str(source),
        "backup": str(dest),
        "method": BACKUP_METHOD,
        "created": False,
        "verified": False,
        "verdict": OVERALL_UNKNOWN,
        "reason": "backup did not run",
        "integrity_check": None,
        "schema_match": None,
        "hollow_backup": None,
        "table_count": None,
        "source_total_rows": None,
        "backup_total_rows": None,
        "wal_sidecar": wal_sidecar_state(source),
        "per_table": [],
        "summary": None,
        "row_counts_match": None,
        "drift_only": False,
        "live_drift": None,
        "data_loss_detected": None,
        "restore_test": None,
        "size_bytes": None,
        "failed_checks": [],
    }

    def fail(reason: object, *, failed: str) -> dict:
        report["reason"] = failure_reason(reason)
        report["verdict"] = OVERALL_FAILED
        report["failed_checks"].append(failed)
        return report

    if _same_file(source, dest):
        return fail("refusing to back a database up onto itself", failed="dest-is-source")

    if not source.is_file():
        return fail(f"source database file not found: {source}", failed="source-exists")
    if not sqlite_header_ok(source):
        return fail(
            f"source is not a SQLite database file (magic header absent): {source.name}",
            failed="source-is-sqlite",
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.parent / f".{dest.name}.{uuid.uuid4().hex}.tmp"

    try:
        # Step 1: measure the source before anything is written.  These are the
        # numbers the copy will be held against.
        with closing(open_read_only(source, timeout=timeout)) as src:
            source_integrity = integrity_check(src)
            if source_integrity != "ok":
                return fail(
                    f"source integrity_check returned {source_integrity}",
                    failed="source-integrity",
                )
            source_tables = table_names(src)
            if not source_tables:
                return fail(
                    "source contains no application tables; there is nothing to back up",
                    failed="source-tables",
                )
            source_counts = table_row_counts(src, source_tables)
            source_schema = schema_signature(src)
            report["source_total_rows"] = sum(source_counts.values())
            report["hollow_backup"] = report["source_total_rows"] == 0
            if report["hollow_backup"]:
                # Nothing to lose means nothing to prove: an "ok" here would be
                # the implied pass this module exists to prevent.
                raise _HollowSource(
                    "source holds 0 rows across "
                    f"{len(source_tables)} table(s): an empty database is not a backup"
                )

            # Step 2: the copy itself, through the connection so WAL frames are
            # included.  There is deliberately no file-copy branch here.
            with closing(sqlite3.connect(str(temp), timeout=timeout)) as dst:
                src.backup(dst)
                # backup() inherits the source's WAL header.  A snapshot that
                # declares WAL becomes a half database the moment somebody
                # carries the .db without its -wal -- the original incident --
                # so the published artifact is normalised to a rollback journal
                # and is complete by itself.
                dst.execute("PRAGMA journal_mode = DELETE")
                report["journal_mode"] = dst.execute("PRAGMA journal_mode").fetchone()[0]
                dst.commit()

        # Step 3: re-open what was written and judge it by data, not by bytes.
        with closing(open_read_only(temp, timeout=timeout)) as copy:
            copy_integrity = integrity_check(copy)
            copy_tables = table_names(copy)
            copy_counts = table_row_counts(copy, copy_tables)
            copy_schema = schema_signature(copy)

        report["created"] = True
        report["integrity_check"] = copy_integrity
        report["table_count"] = len(copy_tables)
        report["schema_match"] = copy_schema == source_schema
        report["source_total_rows"] = sum(source_counts.values())
        report["backup_total_rows"] = sum(copy_counts.values())

        if copy_integrity != "ok":
            return fail("backup integrity_check did not return ok", failed="backup-integrity")
        if not copy_tables:
            return fail("backup contains no application tables", failed="backup-table-set")
        if not report["schema_match"]:
            report["failed_checks"].append("backup-schema")

        rows = compare_table_counts(
            source_counts,
            copy_counts,
            source_tables=source_tables,
            target_tables=copy_tables,
            important_tables=important_tables,
        )
        report["per_table"] = rows
        report["summary"] = summarize_comparison(rows)
        report["row_counts_match"] = report["summary"]["clean"]

        loss = [row for row in rows if row["delta"] is not None and row["delta"] < 0]
        gain = [row for row in rows if row["delta"] is not None and row["delta"] > 0]
        absent = [row for row in rows if row["verdict"] == VERDICT_MISSING]
        report["data_loss_detected"] = bool(loss) or bool(absent)
        report["live_drift"] = {
            "gained_tables": [row["table"] for row in gain],
            "lost_tables": [row["table"] for row in loss],
            "absent_tables": [row["table"] for row in absent],
            "net_delta": sum(row["delta"] for row in rows if row["delta"] is not None),
            "explanation": (
                "the source is a live database: rows written after the counts were "
                "measured can land in the snapshot, and rows the snapshot lacks were "
                "already present in the source when it was read"
            ),
        }

        if report["data_loss_detected"]:
            # Fewer rows, or a table that does not exist on one side: the copy is
            # not a backup of this source.  This is the hollow-copy tripwire.
            report["failed_checks"].append("backup-row-counts")
            report["reason"] = (
                "backup lost data relative to the source: " + describe_differences(rows)
            )
        elif report["summary"]["clean"]:
            report["reason"] = "ok"
        else:
            # Only positive drift remains possible: the writer added rows between
            # the measurement and the snapshot.  Say so instead of pretending the
            # counts matched, but do not call a loss-free snapshot a failure.
            report["drift_only"] = True
            report["reason"] = (
                f"row counts match within live drift ({len(gain)} table(s) gained rows "
                "while the snapshot was taken): " + describe_differences(rows)
            )

        if restore_test and not report["failed_checks"]:
            report["restore_test"] = verify_sqlite_backup_restore(
                temp,
                source_path=None,
                important_tables=important_tables,
            )
            if not report["restore_test"].get("verified"):
                report["failed_checks"].append("disposable-restore")
                report["reason"] = (
                    "backup passed count verification but failed the disposable restore "
                    f"test: {report['restore_test'].get('reason')}"
                )

        if report["failed_checks"]:
            report["verified"] = False
            report["verdict"] = OVERALL_FAILED
            return report

        # Step 4: only a fully verified snapshot receives the public name.
        os.replace(temp, dest)
        temp = None
        report["size_bytes"] = dest.stat().st_size
        report["verified"] = True
        report["verdict"] = OVERALL_OK
        report["reason"] = "ok"
        return report
    except _HollowSource as exc:
        return fail(exc, failed="source-non-empty")
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        return fail(exc, failed="backup-error")
    finally:
        # A snapshot that never earned its name must not linger and be mistaken
        # for a backup by retention pruning or by a human.
        if temp is not None:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                pass

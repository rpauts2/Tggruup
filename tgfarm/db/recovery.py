"""Backup and restore *proving* for SQLite databases.

Two guarantees this module exists to provide.

1. A backup is only trusted once the data inside it has been compared, table by
   table, against the data the source held.  File existence, file size and even
   ``PRAGMA integrity_check`` only prove a file is readable.  A flat
   ``shutil.copy`` of a live WAL database passes ``integrity_check`` and can
   nevertheless contain zero rows -- and zero rows is exactly what a ``cp`` of
   this project's live 433 MB database once produced.  Counts, never bytes,
   decide the verdict.

2. A verification that cannot fail is worse than none.  Every verdict here is
   derived from measured numbers.  When the counterparty could not be read the
   verdict is ``UNKNOWN``, never an implied ``ok`` and never a fabricated zero:
   a count that was not measured is ``None``, and ``None`` is not 0.

Everything here opens databases with ``mode=ro`` and only ever writes inside a
``TemporaryDirectory``.  Importing this module must never bootstrap or migrate
an application database, so nothing here imports ``tgfarm.db.models``.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from tgfarm.core.security import redact_sensitive_text

__all__ = [
    "DEFAULT_IMPORTANT_TABLES",
    "OVERALL_FAILED",
    "OVERALL_OK",
    "OVERALL_UNKNOWN",
    "VERDICT_MATCH",
    "VERDICT_MISSING",
    "VERDICT_MISMATCH",
    "VERDICT_UNKNOWN",
    "compare_table_counts",
    "open_for_verification",
    "open_read_only",
    "probe_backup_restorable",
    "cleanup_verification_copy",
    "restore_to_temporary_database",
    "sqlite_file_geometry",
    "sqlite_header_ok",
    "summarize_comparison",
    "table_names",
    "table_row_counts",
    "verify_sqlite_backup_restore",
    "wal_sidecar_state",
]

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_MISSING = "missing"
VERDICT_UNKNOWN = "UNKNOWN"

OVERALL_OK = "OK"
OVERALL_FAILED = "FAILED"
OVERALL_UNKNOWN = "UNKNOWN"

#: Tables an operator cares about when asking "did my farm survive?".
#: Used only to *label* report rows -- never to fail a comparison on its own,
#: because an older backup legitimately predates tables added by later
#: migrations, and that is a realistic restore scenario to report, not to crash on.
DEFAULT_IMPORTANT_TABLES = (
    "posts",
    "raw_items",
    "clusters",
    "media",
    "channels",
    "sources",
    "pipeline_runs",
)

_SQLITE_HEADER = b"SQLite format 3\0"


def _text(value: object, limit: int = 240) -> str:
    return redact_sensitive_text(value, limit=limit)


def _reason(value: object) -> str:
    """A reason is never empty: unexplained failure is itself a finding."""
    rendered = _text(value).strip()
    return rendered or "unspecified failure"


def sqlite_header_ok(path: Path) -> bool:
    """True when the first 16 bytes are the SQLite magic header.

    Checked before any ``sqlite3.connect`` so a text file, a JSON export or a
    truncated archive is reported as *not a database* rather than as a corrupt
    one, and so no database handle is ever taken on a non-database file.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(16) == _SQLITE_HEADER
    except OSError:
        return False


def sqlite_file_geometry(path: Path) -> dict:
    """Compare the file's real length with what its own header declares.

    A database truncated mid-file is the classic half-written backup: the
    declared page count sits in bytes 28-31 of the header while the bytes after
    the cut are simply gone.  SQLite usually answers such a file with
    ``database disk image is malformed``, which is true but not helpful; naming
    the condition here gives the operator a reason they can act on.

    Only ``actual < declared`` is treated as truncation.  The declared count can
    legitimately lag behind a live WAL writer, so a *larger* file is never an
    alarm.
    """
    geometry: dict = {
        "path": str(path),
        "actual_bytes": None,
        "page_size": None,
        "declared_page_count": None,
        "declared_bytes": None,
        "truncated": None,
        "reason": None,
    }
    try:
        geometry["actual_bytes"] = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(100)
    except OSError as exc:
        geometry["reason"] = _reason(exc)
        return geometry

    if len(header) < 100:
        geometry["truncated"] = True
        geometry["reason"] = f"SQLite header is {len(header)} bytes; a complete header is 100"
        return geometry

    raw_page_size = int.from_bytes(header[16:18], "big")
    # 1 is the encoding for 65536; 0 means "not yet written".
    page_size = 65536 if raw_page_size == 1 else raw_page_size
    page_count = int.from_bytes(header[28:32], "big")
    geometry["page_size"] = page_size or None
    geometry["declared_page_count"] = page_count
    if not page_size:
        geometry["reason"] = "page size not readable from header"
        return geometry
    declared = page_size * page_count
    geometry["declared_bytes"] = declared
    if geometry["actual_bytes"] < declared:
        geometry["truncated"] = True
        geometry["reason"] = (
            f"file holds {geometry['actual_bytes']} bytes but declares "
            f"{page_count} pages of {page_size} = {declared} bytes: truncated mid-file"
        )
    else:
        geometry["truncated"] = False
    return geometry


def wal_sidecar_state(db_path: str | Path) -> dict:
    """Describe the ``-wal``/``-shm`` sidecars of a database file.

    A ``.db`` file is *not* the whole database while a writer is live: committed
    frames can still be sitting in ``-wal``.  Silently verifying the main file
    alone would then confirm a half database, so this condition is named in
    every verdict this module produces.
    """
    path = Path(db_path).expanduser()
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")

    def size(candidate: Path) -> int | None:
        try:
            return candidate.stat().st_size if candidate.exists() else None
        except OSError:  # pragma: no cover - defensive: unreadable sidecar
            return None

    wal_bytes = size(wal)
    shm_bytes = size(shm)
    pending = bool(wal_bytes)
    # Header bytes 18 and 19 are the read/write format versions; 2 means WAL.
    declares_wal = False
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
        declares_wal = len(header) >= 20 and header[18] == 2 and header[19] == 2
    except OSError:
        pass
    # A WAL database whose sidecars are gone is a *half* database: frames that
    # were still pending were never carried over with the main file.
    missing_sidecar = declares_wal and wal_bytes is None
    state = {
        "database": str(path),
        "wal_present": wal_bytes is not None,
        "wal_bytes": wal_bytes,
        "shm_present": shm_bytes is not None,
        "shm_bytes": shm_bytes,
        "declares_wal": declares_wal,
        # A non-empty -wal means the main file alone is not a complete database.
        "wal_pending": pending,
        "wal_missing_sidecar": missing_sidecar,
        "flat_copy_of_main_file_is_unsafe": bool(pending or missing_sidecar),
    }
    if pending:
        state["note"] = (
            "a non-empty -wal holds committed frames; copying only the .db file "
            "loses them, use sqlite3.Connection.backup() which reads through the "
            "connection"
        )
    elif missing_sidecar:
        state["note"] = (
            "the file declares WAL but has no -wal beside it: any frames that "
            "were still pending are absent from this file, which is what a copy "
            "of a live database's main file looks like"
        )
    else:
        state["note"] = "no pending WAL frames observed"
    return state


def open_read_only(path: str | Path, *, timeout: int = 30) -> sqlite3.Connection:
    """Open an existing database strictly read-only.

    ``mode=ro`` is structural, not advisory: no write, no VACUUM, no
    ``journal_mode`` change and no migration is possible through this handle
    even by accident.  ``query_only`` is a second, independent lock.
    """
    resolved = Path(path).expanduser().resolve()
    uri = resolved.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error:  # pragma: no cover - mode=ro already guarantees it
        pass
    return connection


def open_for_verification(
    path: str | Path,
    *,
    workdir: str | Path | None = None,
    timeout: int = 30,
) -> tuple[sqlite3.Connection, dict]:
    """Open a database for reading while keeping the original untouched.

    Read-only is the rule, not the default, and it is *proved* before being
    handed back: a query has to succeed, so a file that cannot really be read
    never pretends to be open.  When a WAL database lacks the sidecars it needs,
    the file is copied into a disposable directory and that copy is opened
    read-write, which lets SQLite recover it without rewriting a single byte of
    the file the operator cares about.

    Returns ``(connection, access_report)``.  When a disposable copy had to be
    made, its directory is named in the report and the caller is responsible for
    :func:`cleanup_verification_copy` once the connection is closed.
    """
    original = Path(path).expanduser().resolve()
    note: dict = {
        "access": "read-only",
        "opened_as": str(original),
        "fallback_used": False,
        "wal_sidecar": wal_sidecar_state(original),
        "reason": None,
    }
    sidecar = note["wal_sidecar"]
    if sidecar["wal_missing_sidecar"]:
        # A read-only open of a WAL database with no -shm/-wal beside it makes
        # SQLite create those files next to the operator's file: that is a
        # write.  Skip the attempt and read a disposable copy instead.
        first_error = (
            "declares WAL without a -wal/-shm beside it; opening it in place "
            "would create sidecar files next to it"
        )
    else:
        try:
            readonly = open_read_only(original, timeout=timeout)
            readonly.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            return readonly, note
        except sqlite3.Error as exc:
            first_error = _reason(exc)

    if workdir is not None:
        copy_dir = Path(workdir)
        copy_dir.mkdir(parents=True, exist_ok=True)
        owns_directory = False
    else:
        copy_dir = Path(tempfile.mkdtemp(prefix="tgfarm-verify-"))
        owns_directory = True
    note["copy_dir"] = str(copy_dir)
    note["owns_copy_dir"] = owns_directory
    copied = copy_dir / original.name
    try:
        shutil.copy2(original, copied)
        for suffix in ("-wal", "-shm"):
            neighbour = original.with_name(original.name + suffix)
            if neighbour.is_file():
                shutil.copy2(neighbour, copied.with_name(copied.name + suffix))
        connection = sqlite3.connect(str(copied), timeout=timeout)
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except (sqlite3.Error, OSError) as exc:
        error = sqlite3.DatabaseError(
            f"read-only open failed ({first_error}) and a disposable copy "
            f"answered {_reason(exc)}"
        )
        cleanup_verification_copy(note)
        raise error from exc

    note["access"] = "disposable-copy"
    note["opened_as"] = str(copied)
    note["fallback_used"] = True
    note["reason"] = (
        f"the original was not opened in place ({first_error}); it was restored "
        "into a disposable copy instead, which also proves the restore"
    )
    return connection, note


def cleanup_verification_copy(note: dict | None) -> None:
    """Remove a disposable verification copy now, not whenever the GC feels like it.

    Windows keeps an open SQLite file locked, so the copy directory is only
    removable once the handle over it has been closed.  Callers therefore close
    the connection first and then call this; leaving it to finalizers produced
    ``PermissionError`` noise at interpreter shutdown.
    """
    if not note or not note.get("owns_copy_dir"):
        return
    directory = note.pop("copy_dir", None)
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def table_row_counts(
    connection: sqlite3.Connection,
    tables: list[str] | None = None,
) -> dict[str, int]:
    """Row count per table.

    A table that cannot be counted raises rather than contributing a 0: an
    unmeasured number is UNKNOWN here, never zero.
    """
    target = table_names(connection) if tables is None else tables
    result: dict[str, int] = {}
    for table in target:
        quoted = '"' + str(table).replace('"', '""') + '"'
        result[str(table)] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
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


def _integrity_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "no result"


def compare_table_counts(
    source_counts: dict[str, int],
    target_counts: dict[str, int],
    *,
    source_tables: list[str] | None = None,
    target_tables: list[str] | None = None,
    important_tables: tuple[str, ...] = DEFAULT_IMPORTANT_TABLES,
) -> list[dict]:
    """Per-table truth: source count, target count, delta and a verdict.

    ``None`` means "this side does not have that table" and is preserved as
    ``None``; the delta is then ``None`` too, because a difference against a
    missing table is not a number.
    """
    left = source_tables if source_tables is not None else sorted(source_counts)
    right = target_tables if target_tables is not None else sorted(target_counts)
    names = sorted(set(left) | set(right) | set(source_counts) | set(target_counts))

    rows: list[dict] = []
    for name in names:
        in_source = name in source_counts
        in_target = name in target_counts
        source_count = source_counts.get(name)
        target_count = target_counts.get(name)
        if in_source and in_target:
            verdict = VERDICT_MATCH if source_count == target_count else VERDICT_MISMATCH
            delta = int(target_count) - int(source_count)
        else:
            verdict = VERDICT_MISSING
            delta = None
        rows.append({
            "table": name,
            "source_count": source_count,
            "target_count": target_count,
            "delta": delta,
            "verdict": verdict,
            "present_in_source": in_source,
            "present_in_target": in_target,
            "important": name in important_tables,
        })
    return rows


def summarize_comparison(rows: list[dict]) -> dict:
    """Tally verdicts; refuse to call a comparison clean when anything is unknown."""
    tally = {VERDICT_MATCH: 0, VERDICT_MISMATCH: 0, VERDICT_MISSING: 0, VERDICT_UNKNOWN: 0}
    for row in rows:
        tally[row["verdict"]] = tally.get(row["verdict"], 0) + 1
    return {
        "table_total": len(rows),
        "match": tally[VERDICT_MATCH],
        "mismatch": tally[VERDICT_MISMATCH],
        "missing": tally[VERDICT_MISSING],
        "unknown": tally[VERDICT_UNKNOWN],
        "verified_rows": sum(
            int(row["source_count"])
            for row in rows
            if row["verdict"] == VERDICT_MATCH and row["source_count"] is not None
        ),
        "clean": not (
            tally[VERDICT_MISMATCH] or tally[VERDICT_MISSING] or tally[VERDICT_UNKNOWN]
        ),
    }


def restore_to_temporary_database(backup_path: str | Path, destination: str | Path) -> dict:
    """Restore a snapshot into a disposable file using ``Connection.backup()``.

    Returns ``{"ok": bool, "reason": str}``.  The backup is only ever opened
    read-only, so this is safe to run against a file the operator cares about.
    """
    try:
        with closing(open_read_only(backup_path)) as source:
            target = Path(destination)
            if target.exists():
                target.unlink(missing_ok=True)
            with closing(sqlite3.connect(str(target), timeout=30)) as restored:
                source.backup(restored)
                restored.commit()
        return {"ok": True, "reason": "ok"}
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        return {"ok": False, "reason": _reason(exc)}


def _source_comparison(
    source: Path,
    *,
    snapshot_counts: dict[str, int],
    snapshot_tables: list[str],
    important_tables: tuple[str, ...],
) -> dict:
    """Compare a snapshot against the database it claims to be a backup of.

    Any failure to read the source yields ``verdict=UNKNOWN``: the snapshot is
    then *not proven*, which is a different claim from "it matches" and a
    different claim again from "it does not match".
    """
    target: dict = {
        "path": str(source),
        "verdict": VERDICT_UNKNOWN,
        "reason": None,
        "readable": False,
        "wal_sidecar": wal_sidecar_state(source),
        "per_table": [],
        "summary": None,
    }
    if not source.is_file():
        target["reason"] = "source database file not found"
        return target
    if not sqlite_header_ok(source):
        target["reason"] = "source is not a SQLite database file (magic header absent)"
        return target
    try:
        with closing(open_read_only(source)) as connection:
            integrity = _integrity_check(connection)
            target["source_integrity_check"] = integrity
            if integrity != "ok":
                target["reason"] = f"source integrity_check returned {integrity}"
                return target
            source_tables = table_names(connection)
            if not source_tables:
                target["reason"] = "source contains no application tables"
                return target
            source_counts = table_row_counts(connection, source_tables)
        rows = compare_table_counts(
            source_counts,
            dict(snapshot_counts),
            source_tables=source_tables,
            target_tables=snapshot_tables,
            important_tables=important_tables,
        )
        summary = summarize_comparison(rows)
        target["per_table"] = rows
        target["summary"] = summary
        target["readable"] = True
        target["verdict"] = VERDICT_MATCH if summary["clean"] else VERDICT_MISMATCH
        return target
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        target["reason"] = _reason(exc)
        return target


def _describe_differences(rows: list[dict] | None, limit: int = 5) -> str:
    if not rows:
        return "no per-table comparison available"
    bad = [row for row in rows if row["verdict"] != VERDICT_MATCH]
    if not bad:
        return "every table matches"
    head = "; ".join(
        f"{row['table']}: source={row['source_count']} backup={row['target_count']} "
        f"delta={row['delta']}"
        for row in bad[:limit]
    )
    return f"{len(bad)} of {len(rows)} table(s) differ -> {head}"


def verify_sqlite_backup_restore(
    backup_path: str | Path,
    *,
    source_path: str | Path | None = None,
    important_tables: tuple[str, ...] = DEFAULT_IMPORTANT_TABLES,
) -> dict:
    """Restore a backup into a disposable temp DB and prove what survived.

    Without ``source_path`` this proves the snapshot is *internally* restorable:
    it can be copied into a fresh database and come back with the same schema,
    the same tables and the same row counts.  Necessary, but not sufficient --
    a snapshot that captured nothing is perfectly restorable.

    With ``source_path`` the snapshot is additionally compared, table by table,
    against the live database it claims to come from, and the verdict becomes
    ``UNKNOWN`` (never ``match``) when that source could not be read.
    """
    path = Path(backup_path).expanduser().resolve()
    report: dict = {
        "backup": str(path),
        "verified": False,
        "restore_tested": False,
        "verdict": OVERALL_UNKNOWN,
        "reason": "verification did not run",
        "required_backup_method": "sqlite3.Connection.backup()",
        "integrity_check": None,
        "restored_integrity_check": None,
        "table_count": 0,
        "tables_match": False,
        "row_counts_match": False,
        "schema_match": False,
        "schema_object_count": 0,
        "foreign_key_violations": None,
        "hollow_backup": None,
        "total_backup_rows": None,
        "wal_sidecar": wal_sidecar_state(path),
        "geometry": None,
        "access": None,
        "source": None,
        "source_verdict": None,
        "source_read_error": None,
        "per_table": [],
        "source_comparison": None,
        "failed_checks": [],
    }

    def fail(reason: object, *, failed: str) -> dict:
        report["reason"] = _reason(reason)
        report["verdict"] = OVERALL_FAILED
        report["failed_checks"].append(failed)
        return report

    if not path.is_file():
        # Preserved verbatim: tests pin this exact reason and the absence of leftovers.
        report["reason"] = "backup file not found"
        report["verdict"] = OVERALL_UNKNOWN
        report["failed_checks"].append("file-exists")
        return report

    if not path.stat().st_size:
        return fail(
            "backup file is zero bytes: a SQLite database is never empty, this looks "
            "like a copy taken before the first page was written",
            failed="zero-byte-file",
        )

    if not sqlite_header_ok(path):
        return fail(
            f"backup is not a SQLite database file (magic header absent): {path.name}",
            failed="sqlite-header",
        )

    geometry = sqlite_file_geometry(path)
    report["geometry"] = geometry
    if geometry["truncated"]:
        return fail(geometry["reason"], failed="file-truncated")

    access: dict | None = None
    try:
        source, access = open_for_verification(path)
        report["access"] = access
        with closing(source):
            source_integrity = _integrity_check(source)
            report["integrity_check"] = source_integrity
            if source_integrity != "ok":
                return fail("backup integrity_check did not return ok", failed="integrity_check")

            source_tables = table_names(source)
            report["table_count"] = len(source_tables)
            if not source_tables:
                return fail("backup contains no application tables", failed="table-set")

            source_counts = table_row_counts(source, source_tables)
            source_schema = _schema_signature(source)
            report["total_backup_rows"] = sum(source_counts.values())
            report["hollow_backup"] = all(count == 0 for count in source_counts.values())

            with tempfile.TemporaryDirectory(prefix="tgfarm-restore-test-") as directory:
                restored_path = Path(directory) / "restored.sqlite"
                with closing(sqlite3.connect(str(restored_path), timeout=30)) as restored:
                    # Connection.backup(), not a file copy: this is the restore
                    # path an operator would actually take.
                    source.backup(restored)
                    restored.commit()
                    restored_integrity = _integrity_check(restored)
                    restored_tables = table_names(restored)
                    restored_counts = table_row_counts(restored, restored_tables)
                    restored_schema = _schema_signature(restored)
                    foreign_key_violations = restored.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()

                report["restore_tested"] = True
                report["restored_integrity_check"] = restored_integrity
                report["foreign_key_violations"] = len(foreign_key_violations)
                report["schema_object_count"] = len(source_schema)
                report["tables_match"] = source_tables == restored_tables
                report["schema_match"] = source_schema == restored_schema
                report["per_table"] = compare_table_counts(
                    source_counts,
                    restored_counts,
                    source_tables=source_tables,
                    target_tables=restored_tables,
                    important_tables=important_tables,
                )
                report["restored_summary"] = summarize_comparison(report["per_table"])
                report["row_counts_match"] = bool(report["restored_summary"]["clean"])

                for check, ok in (
                    ("restored-integrity", restored_integrity == "ok"),
                    ("restored-tables", report["tables_match"]),
                    ("restored-row-counts", report["row_counts_match"]),
                    ("restored-schema", report["schema_match"]),
                    ("restored-foreign-keys", not foreign_key_violations),
                ):
                    if not ok:
                        report["failed_checks"].append(check)
                if report["hollow_backup"]:
                    report["failed_checks"].append("non-empty-data")

                if source_path is not None:
                    comparison = _source_comparison(
                        Path(source_path).expanduser().resolve(),
                        snapshot_counts=source_counts,
                        snapshot_tables=source_tables,
                        important_tables=important_tables,
                    )
                    report["source"] = comparison
                    report["source_verdict"] = comparison["verdict"]
                    report["source_comparison"] = comparison["per_table"]
                    report["source_summary"] = comparison["summary"]
                    if not comparison["readable"]:
                        # An unreadable source is never a match: the backup is
                        # simply not proven.
                        report["source_read_error"] = comparison["reason"]
                        report["failed_checks"].append("source-readable")

                report["verified"], report["verdict"], report["reason"] = _decide(report)
                return report
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        return fail(exc, failed="open-or-query")
    finally:
        cleanup_verification_copy(access)


def _decide(report: dict) -> tuple[bool, str, str]:
    """One place where a verdict is made, so its precedence can be tested."""
    source_verdict = report["source_verdict"]
    if source_verdict == VERDICT_UNKNOWN:
        return (
            False,
            OVERALL_UNKNOWN,
            "source could not be read; the backup is NOT proven: "
            + _reason(report["source_read_error"]),
        )
    if report["failed_checks"]:
        if report["hollow_backup"]:
            return (
                False,
                OVERALL_FAILED,
                "backup is restorable but hollow: "
                f"{report['table_count']} table(s) hold 0 rows in total -- nothing to restore",
            )
        if source_verdict == VERDICT_MISMATCH:
            return (
                False,
                OVERALL_FAILED,
                "backup does not match the source: "
                + _describe_differences(report["source_comparison"]),
            )
        return (
            False,
            OVERALL_FAILED,
            "failed checks: " + ", ".join(str(item) for item in report["failed_checks"]),
        )
    if source_verdict == VERDICT_MISMATCH:
        # Unreachable today (a mismatch always records a failed check), kept as
        # a guard so a future refactor cannot turn "differs" into a silent pass.
        return (
            False,
            OVERALL_FAILED,
            "backup does not match the source: "
            + _describe_differences(report["source_comparison"]),
        )
    if not report["restore_tested"]:
        return False, OVERALL_UNKNOWN, "restore never ran"
    return True, OVERALL_OK, "ok"


def probe_backup_restorable(
    backup_path: str | Path,
    *,
    deep: bool = False,
    timeout_seconds: float = 20.0,
    important_tables: tuple[str, ...] = DEFAULT_IMPORTANT_TABLES,
) -> dict:
    """Cheap, strictly read-only "can I trust this file for a recovery, right now?".

    An operator runs this *before* trusting a backup.  It never imports the
    application, never bootstraps and never migrates: the file is opened with
    ``mode=ro`` plus ``query_only``, and no byte of it is rewritten.  The one
    exception is deliberate and named in the report -- a database that declares
    WAL without its sidecars cannot be opened read-only without SQLite
    creating ``-wal``/``-shm`` next to it, so a disposable copy is made in a
    temporary directory instead.  The report says which route was taken under
    ``access``.

    ``deep=True`` additionally runs ``PRAGMA integrity_check``, bounded by
    ``timeout_seconds``: the budget is refused before the work starts if it is
    already spent and is checked again between virtual machines, so a
    half-gigabyte file cannot hang the operator.  Exceeding the budget is
    UNKNOWN -- not a pass, and not a claim of corruption either.
    """
    path = Path(backup_path).expanduser().resolve()
    probe: dict = {
        "backup": str(path),
        "read_only": True,
        "restorable": None,
        "verdict": OVERALL_UNKNOWN,
        "reason": "probe did not run",
        "checks": [],
        "wal_sidecar": wal_sidecar_state(path),
        "geometry": None,
        "access": None,
        "table_count": None,
        "total_rows": None,
        "empty_tables": None,
        "important_rows": None,
        "integrity_check": None,
        "integrity_budget_seconds": timeout_seconds if deep else None,
    }

    def record(name: str, ok: bool, detail: object = None) -> None:
        probe["checks"].append({"check": name, "ok": ok, "detail": _text(detail, 160)})

    def stop(reason: str) -> dict:
        probe["reason"] = reason
        probe["restorable"] = False
        probe["verdict"] = OVERALL_FAILED
        return probe

    def stall(reason: str) -> dict:
        """Not proven, which is not the same claim as "not restorable"."""
        probe["reason"] = reason
        probe["restorable"] = None
        probe["verdict"] = OVERALL_UNKNOWN
        return probe

    geometry = sqlite_file_geometry(path)
    probe["geometry"] = geometry

    if not path.is_file():
        # "Not restorable" is the honest answer about a file that is not there;
        # UNKNOWN says why nothing further can be measured.
        record("file-exists", False, path.name)
        probe["reason"] = "backup file not found"
        probe["restorable"] = False
        probe["verdict"] = OVERALL_UNKNOWN
        return probe
    if not path.stat().st_size:
        record("non-zero-size", False, "0 bytes")
        return stop("backup file is zero bytes: a SQLite database is never empty")
    if not sqlite_header_ok(path):
        record("sqlite-header", False, path.name)
        return stop("not a SQLite database file (magic header absent)")
    record("sqlite-header", True)
    if geometry["truncated"] is True:
        record("file-geometry", False, geometry["reason"])
        return stop(f"file {geometry['reason']}")
    record("file-geometry", True, geometry["reason"])

    started = time.monotonic()
    access: dict | None = None
    try:
        connection, access = open_for_verification(path)
        probe["access"] = access
        with closing(connection):
            if deep:
                # A progress handler only runs between virtual machines, so on a
                # small file it never fires: without this pre-check a budget of
                # zero seconds would "pass" without ever bounding anything.
                if timeout_seconds <= 0 or time.monotonic() - started > timeout_seconds:
                    record("integrity-budget", False, timeout_seconds)
                    return stall(
                        f"the {timeout_seconds}s integrity budget was already spent before "
                        "integrity_check could run; nothing about this file is proven"
                    )
                budget = {"exceeded": False}

                def guard() -> int:
                    # A non-zero return aborts the running statement.
                    if time.monotonic() - started > timeout_seconds:
                        budget["exceeded"] = True
                        return 1
                    return 0

                connection.set_progress_handler(guard, 100_000)
                try:
                    integrity = _integrity_check(connection)
                except sqlite3.OperationalError as exc:
                    if budget["exceeded"]:
                        record("integrity-budget", False, exc)
                        return stall(
                            f"integrity_check exceeded the {timeout_seconds}s budget on a "
                            "large file; raise the budget or run a full restore test"
                        )
                    raise
                finally:
                    connection.set_progress_handler(None, 0)
                probe["integrity_check"] = integrity
                record("integrity_check", integrity == "ok", integrity)
                if integrity != "ok":
                    return stop(f"integrity_check returned {integrity}")

            tables = table_names(connection)
            probe["table_count"] = len(tables)
            record("has-application-tables", bool(tables), f"{len(tables)} table(s)")
            if not tables:
                return stop("database has no application tables; there is nothing to restore")

            counts = table_row_counts(connection, tables)
            total = sum(counts.values())
            probe["total_rows"] = total
            probe["empty_tables"] = sorted(name for name, value in counts.items() if value == 0)
            probe["important_rows"] = {
                name: counts.get(name) for name in important_tables if name in counts
            }
            record("holds-rows", total > 0, f"{total} row(s)")
            if total == 0:
                return stop(
                    "schema is readable but every table is empty: a hollow copy, not a backup"
                )

            if probe["wal_sidecar"]["wal_pending"]:
                record("wal-sidecar-contained", False, probe["wal_sidecar"]["wal_bytes"])
                return stop(
                    "a non-empty -wal sits beside this snapshot: the main file is not "
                    "self-contained and the writer was not closed cleanly"
                )

            probe["restorable"] = True
            probe["verdict"] = OVERALL_OK
            probe["reason"] = "ok"
            probe["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return probe
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        message = _reason(exc)
        record("open-read-only", False, message)
        probe["reason"] = (
            f"could not be read, not even through a disposable copy: {message}"
        )
        probe["restorable"] = None
        probe["verdict"] = OVERALL_UNKNOWN
        return probe
    finally:
        cleanup_verification_copy(access)


# Back-compatible private aliases: older callers reached for these names.
_table_names = table_names
_row_counts = table_row_counts

# Public spellings of the helpers tgfarm.db.backup_snapshot reuses. Cross-module
# imports of underscore names would advertise them as private and untested.
schema_signature = _schema_signature
integrity_check = _integrity_check
describe_differences = _describe_differences
failure_reason = _reason

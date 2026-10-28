"""Verified off-site mirror for already-created SQLite backup snapshots."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from tgfarm.core.security import redact_sensitive_text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity_check(path: Path) -> str:
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "no result"


def mirror_verified_backup(
    source_path: str | Path,
    mirror_dir: str | Path,
    *,
    keep: int = 8,
    local_backup_dir: str | Path | None = None,
) -> dict:
    """Copy one verified snapshot, verify the copy, then atomically expose it.

    The local snapshot remains untouched. Retention is applied to the mirror
    only after the new copy has passed both SQLite integrity and checksum checks.
    """
    source = Path(source_path).expanduser().resolve()
    mirror = Path(mirror_dir).expanduser().resolve()
    temp: Path | None = None

    def failure(reason: object) -> dict:
        return {
            "configured": True,
            "verified": False,
            "backup": None,
            "reason": redact_sensitive_text(reason, limit=240),
        }

    try:
        if not source.is_file():
            return failure("source backup file not found")
        local_dir = (
            Path(local_backup_dir).expanduser().resolve()
            if local_backup_dir is not None
            else source.parent
        )
        if mirror == local_dir or mirror == source.parent:
            return failure("backup mirror must differ from the local backup directory")
        source_integrity = _integrity_check(source)
        if source_integrity != "ok":
            return failure("source backup integrity_check failed")
        source_digest = _sha256(source)

        mirror.mkdir(parents=True, exist_ok=True)
        destination = mirror / source.name
        if destination.exists():
            if _integrity_check(destination) == "ok" and _sha256(destination) == source_digest:
                return {
                    "configured": True,
                    "verified": True,
                    "backup": str(destination),
                    "integrity_check": "ok",
                    "sha256": source_digest,
                    "size_bytes": destination.stat().st_size,
                    "kept": len(list(mirror.glob("tgfarm-*.db"))),
                    "already_present": True,
                }
            return failure("mirror target already exists with different content")

        temp = mirror / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        shutil.copy2(source, temp)
        copied_integrity = _integrity_check(temp)
        if copied_integrity != "ok":
            return failure("mirrored copy integrity_check failed")
        copied_digest = _sha256(temp)
        if copied_digest != source_digest:
            return failure("mirrored copy checksum does not match source")

        # A consumer sees only a fully copied and verified snapshot.
        os.replace(temp, destination)
        temp = None

        backups = sorted(mirror.glob("tgfarm-*.db"), key=lambda path: path.name)
        retention = max(1, int(keep))
        for old in backups[: max(0, len(backups) - retention)]:
            if old != destination:
                old.unlink(missing_ok=True)
        kept = len(list(mirror.glob("tgfarm-*.db")))
        return {
            "configured": True,
            "verified": True,
            "backup": str(destination),
            "integrity_check": "ok",
            "sha256": source_digest,
            "size_bytes": destination.stat().st_size,
            "kept": kept,
            "already_present": False,
        }
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
        return failure(exc)
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

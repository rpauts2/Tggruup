from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_backup_job_verifies_snapshot_and_keeps_only_latest_backups(tmp_path, monkeypatch):
    from tgfarm import pipeline

    source = tmp_path / "farm.db"
    backup_dir = tmp_path / "backup"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('preserve me')")

    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_dir=str(backup_dir), keep_backups=2),
    )

    results = [await pipeline.backup_job() for _ in range(3)]

    assert all(item["verified"] is True for item in results)
    assert all(item["integrity_check"] == "ok" for item in results)
    assert all(item["size_bytes"] > 0 for item in results)
    assert len({item["backup"] for item in results}) == 3, "manual backups must never overwrite each other"
    backups = sorted(backup_dir.glob("tgfarm-*.db"))
    assert len(backups) == 2
    assert not list(backup_dir.glob("*.tmp"))
    for path in backups:
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT value FROM sample").fetchone()[0] == "preserve me"


@pytest.mark.asyncio
async def test_backup_job_refuses_to_treat_postgres_database_name_as_sqlite_file(tmp_path, monkeypatch):
    from tgfarm import pipeline

    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        pipeline,
        "get_engine_url",
        lambda: "postgresql+psycopg://user:password@db.example.invalid/tgfarm",
    )
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_dir=str(backup_dir), keep_backups=3),
    )

    result = await pipeline.backup_job()

    assert result["verified"] is False
    assert "non-sqlite" in result["reason"]
    assert not backup_dir.exists()


@pytest.mark.asyncio
async def test_backup_job_reports_missing_database_instead_of_making_empty_backup(tmp_path, monkeypatch):
    from tgfarm import pipeline

    missing = tmp_path / "missing.db"
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{missing.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_dir=str(backup_dir), keep_backups=3),
    )

    result = await pipeline.backup_job()

    assert result["verified"] is False
    assert "not found" in result["reason"]
    assert not backup_dir.exists()

@pytest.mark.asyncio
async def test_failed_backup_keeps_previous_verified_backups_and_removes_temp_files(tmp_path, monkeypatch):
    from tgfarm import pipeline

    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not a sqlite database")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    old_backups = [
        backup_dir / "tgfarm-2026-10-01-000000-000001.db",
        backup_dir / "tgfarm-2026-10-02-000000-000001.db",
    ]
    for path in old_backups:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE verified_snapshot (value TEXT)")
            connection.execute("INSERT INTO verified_snapshot VALUES ('keep')")
    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_dir=str(backup_dir), keep_backups=1),
    )

    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        await pipeline.backup_job()

    assert all(path.exists() for path in old_backups), "failed copy must not prune verified backups"
    assert not list(backup_dir.glob("*.tmp"))
    assert not list(backup_dir.glob(".tgfarm-*.tmp"))
    assert {path.name for path in backup_dir.glob("tgfarm-*.db")} == {path.name for path in old_backups}

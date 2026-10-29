from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
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
    assert all(item["restore_tested"] is True for item in results)
    assert all(item["restore_verification"]["verified"] is True for item in results)
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
async def test_backup_job_restore_verification_failure_preserves_previous_backups(tmp_path, monkeypatch):
    from tgfarm import pipeline
    from tgfarm.db import recovery

    source = tmp_path / "farm.db"
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    previous = backup_dir / "tgfarm-2026-10-01-000000-000001.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('source')")
    with sqlite3.connect(previous) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('known good')")

    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_dir=str(backup_dir), keep_backups=1),
    )
    monkeypatch.setattr(
        recovery,
        "verify_sqlite_backup_restore",
        lambda _path: {
            "verified": False,
            "restore_tested": True,
            "reason": "simulated restore mismatch",
        },
    )

    with pytest.raises(sqlite3.DatabaseError, match="restore verification failed"):
        await pipeline.backup_job()

    assert previous.is_file(), "do not prune a known-good backup before restore verification"
    assert sorted(path.name for path in backup_dir.glob("tgfarm-*.db")) == [previous.name]
    assert not list(backup_dir.glob("*.tmp"))
    assert not list(backup_dir.glob(".tgfarm-*.tmp"))


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


def test_verify_sqlite_backup_restore_round_trip(tmp_path):
    from tgfarm.db.recovery import verify_sqlite_backup_restore

    backup = tmp_path / "verified-snapshot.db"
    with sqlite3.connect(backup) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL, "
            "FOREIGN KEY(parent_id) REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO parent(name) VALUES ('farm')")
        connection.execute("INSERT INTO child(parent_id) VALUES (1)")
        connection.execute("CREATE INDEX ix_parent_name ON parent(name)")
        connection.execute("CREATE VIEW child_parent_names AS "
                           "SELECT child.id, parent.name FROM child JOIN parent ON parent.id = child.parent_id")
        connection.execute("CREATE TRIGGER child_insert_guard AFTER INSERT ON child "
                           "BEGIN UPDATE parent SET name = name WHERE id = NEW.parent_id; END")

    result = verify_sqlite_backup_restore(backup)

    assert result["verified"] is True
    assert result["restore_tested"] is True
    assert result["integrity_check"] == "ok"
    assert result["restored_integrity_check"] == "ok"
    assert result["tables_match"] is True
    assert result["row_counts_match"] is True
    assert result["schema_match"] is True
    assert result["schema_object_count"] >= 5
    assert result["foreign_key_violations"] == 0
    assert backup.is_file()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT name FROM parent").fetchone()[0] == "farm"


def test_verify_sqlite_backup_restore_rejects_corrupt_or_missing_files(tmp_path):
    from tgfarm.db.recovery import verify_sqlite_backup_restore

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    result = verify_sqlite_backup_restore(corrupt)
    assert result["verified"] is False
    assert result["restore_tested"] is False

    missing = verify_sqlite_backup_restore(tmp_path / "missing.db")
    assert missing["verified"] is False
    assert missing["reason"] == "backup file not found"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["corrupt.db"]


def test_backup_mirror_copies_and_verifies_without_touching_source(tmp_path):
    import hashlib

    from tgfarm.db.backup_mirror import mirror_verified_backup

    local_dir = tmp_path / "local"
    mirror_dir = tmp_path / "mounted-backup"
    local_dir.mkdir()
    source = local_dir / "tgfarm-2026-10-28-120000-000001.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('keep me')")
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    result = mirror_verified_backup(source, mirror_dir, keep=2, local_backup_dir=local_dir)

    mirrored = mirror_dir / source.name
    assert result["verified"] is True
    assert result["integrity_check"] == "ok"
    assert result["sha256"] == source_hash
    assert result["size_bytes"] == len(source_bytes)
    assert mirrored.read_bytes() == source_bytes
    assert source.read_bytes() == source_bytes
    assert not list(mirror_dir.glob("*.tmp"))
    with sqlite3.connect(mirrored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "keep me"


def test_backup_mirror_refuses_same_directory_corrupt_copy_and_preserves_old_backup(tmp_path):
    from tgfarm.db.backup_mirror import mirror_verified_backup

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    source = local_dir / "tgfarm-2026-10-28-120000-000001.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('ok')")

    same_directory = mirror_verified_backup(source, local_dir, local_backup_dir=local_dir)
    assert same_directory["verified"] is False

    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    existing = mirror_dir / source.name
    existing.write_bytes(b"not the same snapshot")
    corrupt_target = mirror_verified_backup(source, mirror_dir, local_backup_dir=local_dir)
    assert corrupt_target["verified"] is False
    assert existing.read_bytes() == b"not the same snapshot"
    assert not list(mirror_dir.glob("*.tmp"))

    corrupt_source = tmp_path / "corrupt.db"
    corrupt_source.write_bytes(b"not sqlite")
    corrupt_result = mirror_verified_backup(corrupt_source, tmp_path / "other-mirror")
    assert corrupt_result["verified"] is False
    assert not (tmp_path / "other-mirror").exists()


@pytest.mark.asyncio
async def test_backup_job_verifies_local_and_mirror_snapshots(tmp_path, monkeypatch):
    from tgfarm import pipeline

    source = tmp_path / "farm.db"
    local_dir = tmp_path / "local-backups"
    mirror_dir = tmp_path / "mirror-backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('same data')")

    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            backup_dir=str(local_dir),
            backup_mirror_dir=str(mirror_dir),
            keep_backups=3,
        ),
    )

    result = await pipeline.backup_job()

    assert result["verified"] is True
    assert result["local_verified"] is True
    assert result["restore_tested"] is True
    assert result["restore_verification"]["verified"] is True
    assert result["mirror"]["configured"] is True
    assert result["mirror"]["verified"] is True
    assert Path(result["backup"]).is_file()
    assert Path(result["mirror"]["backup"]).is_file()
    assert Path(result["backup"]).read_bytes() == Path(result["mirror"]["backup"]).read_bytes()


@pytest.mark.asyncio
async def test_backup_job_reports_mirror_failure_but_keeps_verified_local_snapshot(tmp_path, monkeypatch):
    from tgfarm import pipeline

    source = tmp_path / "farm.db"
    local_dir = tmp_path / "local-backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('preserve local')")

    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            backup_dir=str(local_dir),
            backup_mirror_dir=str(local_dir),
            keep_backups=3,
        ),
    )

    result = await pipeline.backup_job()

    assert result["verified"] is False
    assert result["local_verified"] is True
    assert result["restore_tested"] is True
    assert result["restore_verification"]["verified"] is True
    assert result["mirror"]["verified"] is False
    assert Path(result["backup"]).is_file()
    with sqlite3.connect(result["backup"]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_backup_job_records_mirror_failure_for_watchdog_alerts(tmp_path, monkeypatch):
    from tgfarm import pipeline

    source = tmp_path / "farm.db"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('kept locally')")

    recorded = []
    monkeypatch.setattr(pipeline, "get_engine_url", lambda: f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            backup_dir=str(backup_dir),
            backup_mirror_dir=str(backup_dir),
            keep_backups=3,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_record",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    report = await pipeline.backup_job()

    assert report["verified"] is False
    assert report["local_verified"] is True
    assert report["restore_tested"] is True
    assert report["restore_verification"]["verified"] is True
    assert report["mirror"]["verified"] is False
    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args[1:3] == ("backup", "error")
    assert kwargs["meta"]["local_verified"] is True
    assert kwargs["meta"]["mirror_configured"] is True


def test_backup_failure_appears_in_watchdog_alerts():
    from sqlalchemy import select

    from tgfarm import pipeline
    from tgfarm.db.models import PipelineRun
    from tgfarm.db.session import session_scope

    run_id = "backup-alert-test-" + uuid.uuid4().hex[:10]
    row = PipelineRun(
        run_id=run_id,
        stage="backup",
        status="error",
        error="mirror verification failed",
        meta={"local_verified": True, "mirror_configured": True, "mirror_verified": False},
    )
    with session_scope() as session:
        session.add(row)

    try:
        alerts = pipeline.collect_alerts()
        alert = next(item for item in alerts if item["code"] == "backup_failed")
        assert alert["level"] == "error"
        assert "Локальная резервная копия создана" in alert["message"]
        assert "mirror verification failed" not in alert["message"]
    finally:
        with session_scope() as session:
            existing = session.execute(
                select(PipelineRun).where(PipelineRun.run_id == run_id)
            ).scalars().all()
            for item in existing:
                session.delete(item)


def test_backup_freshness_alert_warns_when_missing_and_errors_when_stale():
    from datetime import UTC, datetime, timedelta

    from tgfarm.pipeline import _backup_freshness_alert

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    missing = _backup_freshness_alert(None, now, 36)
    assert missing is not None
    assert missing["code"] == "backup_missing"
    assert missing["level"] == "warning"

    assert _backup_freshness_alert(now - timedelta(hours=24), now, 36) is None

    stale = _backup_freshness_alert(now - timedelta(hours=48), now, 36)
    assert stale is not None
    assert stale["code"] == "backup_stale"
    assert stale["level"] == "error"
    assert "48.0 ч назад" in stale["message"]


def test_backup_freshness_alert_normalizes_naive_sqlite_timestamps():
    from datetime import UTC, datetime, timedelta

    from tgfarm.pipeline import _backup_freshness_alert

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    naive_recent = (now - timedelta(hours=4)).replace(tzinfo=None)

    assert _backup_freshness_alert(naive_recent, now, 36) is None


def test_backup_scheduler_is_daily_and_has_a_stable_job_id():
    from tgfarm.pipeline import _build_scheduler

    scheduler = _build_scheduler()
    job = scheduler.get_job("daily-backup")

    assert job is not None
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["day_of_week"] == "*"
    assert fields["hour"] == "4"
    assert fields["minute"] == "0"
    assert job.name == "daily verified sqlite backup"
    startup_check = scheduler.get_job("startup-backup-check")
    assert startup_check is not None
    assert startup_check.name == "ensure a recent backup on startup"


@pytest.mark.asyncio
async def test_startup_backup_check_creates_initial_snapshot(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from tgfarm import pipeline

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeSession:
        def __init__(self):
            self.values = iter([None, None])

        def execute(self, _statement):
            return FakeResult(next(self.values))

    @contextmanager
    def fake_scope():
        yield FakeSession()

    calls = []

    async def fake_backup():
        calls.append(True)
        return {"verified": True, "backup": "test.db"}

    monkeypatch.setattr(pipeline, "session_scope", fake_scope)
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_max_age_hours=36),
    )
    monkeypatch.setattr(pipeline, "backup_job", fake_backup)

    result = await pipeline._startup_backup_check()

    assert result == {"verified": True, "backup": "test.db"}
    assert calls == [True]


@pytest.mark.asyncio
async def test_startup_backup_check_skips_when_snapshot_is_fresh(monkeypatch):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from tgfarm import pipeline

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeSession:
        def __init__(self):
            self.values = iter([datetime.now(UTC) - timedelta(hours=2), "ok"])

        def execute(self, _statement):
            return FakeResult(next(self.values))

    @contextmanager
    def fake_scope():
        yield FakeSession()

    async def fail_backup():
        pytest.fail("a fresh verified snapshot must not be copied on every restart")

    monkeypatch.setattr(pipeline, "session_scope", fake_scope)
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(backup_max_age_hours=36),
    )
    monkeypatch.setattr(pipeline, "backup_job", fail_backup)

    assert await pipeline._startup_backup_check() is None


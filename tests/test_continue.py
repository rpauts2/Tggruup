"""Tests: backup job, alert escalation, slow-stage metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace


def test_backup_job_creates_and_prunes(db, tmp_path, monkeypatch):
    import asyncio

    from tgfarm import pipeline

    monkeypatch.setattr(
        "tgfarm.pipeline.get_settings",
        lambda: SimpleNamespace(
            database_url=None,  # job uses engine url, not this
            backup_dir=str(tmp_path),
            keep_backups=2,
        ),
    )
    # fake old backups: three files, keep_backups=2 → oldest must go
    for name in ("tgfarm-2026-10-20-0400.db", "tgfarm-2026-10-21-0400.db", "tgfarm-2026-10-22-0400.db"):
        (tmp_path / name).write_bytes(b"old")

    result = asyncio.run(pipeline.backup_job())

    assert result["backup"], "бэкап должен создаться"
    files = sorted(tmp_path.glob("tgfarm-*.db"))
    assert len(files) == 2, f"должно остаться 2 бэкапа, лежит {len(files)}"
    import sqlite3

    con = sqlite3.connect(str(files[-1]))
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert "posts" in tables


def test_captcha_alert_escalates_to_error(db):
    from tgfarm import pipeline
    from tgfarm.db.models import Post

    for i in range(3):
        db.add(Post(public_id=f"esc-{i}", channel_id="zen_misc_ru", hook="хук", body="тело",
                    status="scheduled",
                    publish_error="капча не решена за отведённое время",
                    created_at=datetime.now(UTC)))
    db.commit()

    alerts = [a for a in pipeline.collect_alerts() if a["code"] == "dzen_captcha"]
    assert alerts and alerts[0]["level"] == "error", "три капчи за день — это error, не warning"


def test_metrics_contains_slowest_stages():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    client = TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1")
    headers = {"X-TGFarm-Token": ensure_token()}
    body = client.get("/api/metrics", headers=headers).json()
    assert "slowest_stages" in body
    assert isinstance(body["slowest_stages"], list)

from __future__ import annotations

from argparse import Namespace

from tgfarm.cli import _prepare_history_import


def test_history_apply_requires_successful_automatic_backup(monkeypatch):
    from tgfarm import pipeline

    async def failed_backup():
        return {"verified": False, "reason": "integrity check failed"}

    monkeypatch.setattr(pipeline, "backup_job", failed_backup)
    ready, backup = _prepare_history_import(Namespace(apply=True, skip_preimport_backup=False))

    assert ready is False
    assert backup["verified"] is False
    assert backup["reason"] == "integrity check failed"


def test_history_preview_does_not_create_backup(monkeypatch):
    from tgfarm import pipeline

    async def forbidden_backup():
        raise AssertionError("preview must not create a backup or write anything")

    monkeypatch.setattr(pipeline, "backup_job", forbidden_backup)
    ready, backup = _prepare_history_import(Namespace(apply=False, skip_preimport_backup=False))

    assert ready is True
    assert backup is None


def test_history_apply_can_only_skip_backup_with_explicit_flag():
    ready, backup = _prepare_history_import(Namespace(apply=True, skip_preimport_backup=True))

    assert ready is True
    assert backup["skipped"] is True
    assert backup["verified"] is False
    assert "external backup" in backup["reason"]

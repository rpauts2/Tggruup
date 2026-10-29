from __future__ import annotations

from types import SimpleNamespace

import pytest


def _usage(free_gb: float):
    return SimpleNamespace(free=int(free_gb * (1024**3)))


def test_disk_space_alert_warns_below_warning_threshold_without_exposing_paths(tmp_path, monkeypatch):
    from tgfarm import pipeline

    data = tmp_path / "private-data"
    backups = tmp_path / "private-backups"
    data.mkdir()
    backups.mkdir()
    calls = []

    def fake_disk_usage(path):
        calls.append(str(path))
        return _usage(7.5)

    monkeypatch.setattr(pipeline.shutil, "disk_usage", fake_disk_usage)

    alerts = pipeline._disk_space_alerts(
        [("данных фермы", data), ("резервных копий", backups)],
        warn_free_gb=10,
        critical_free_gb=3,
    )

    # Both directories are on the same filesystem, so only one check is made.
    assert len(calls) == 1
    assert len(alerts) == 1
    assert alerts[0]["level"] == "warning"
    assert alerts[0]["code"] == "disk_space_low"
    assert "7.5 ГБ" in alerts[0]["message"]
    assert "private-data" not in repr(alerts)
    assert "private-backups" not in repr(alerts)


def test_disk_space_alert_escalates_to_error_below_critical_threshold(tmp_path, monkeypatch):
    from tgfarm import pipeline

    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _path: _usage(1.2))

    alerts = pipeline._disk_space_alerts(
        [("данных фермы", tmp_path)],
        warn_free_gb=10,
        critical_free_gb=3,
    )

    assert len(alerts) == 1
    assert alerts[0]["level"] == "error"
    assert alerts[0]["code"] == "disk_space_critical"


def test_disk_space_alert_is_quiet_above_threshold_and_when_disabled(tmp_path, monkeypatch):
    from tgfarm import pipeline

    monkeypatch.setattr(
        pipeline.shutil,
        "disk_usage",
        lambda _path: pytest.fail("disabled thresholds must skip filesystem checks"),
    )

    assert pipeline._disk_space_alerts(
        [("данных фермы", tmp_path)],
        warn_free_gb=0,
        critical_free_gb=0,
    ) == []


def test_disk_space_alert_uses_warning_threshold_when_critical_is_higher(tmp_path, monkeypatch):
    from tgfarm import pipeline

    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _path: _usage(4.0))

    alerts = pipeline._disk_space_alerts(
        [("данных фермы", tmp_path)],
        warn_free_gb=2,
        critical_free_gb=5,
    )

    assert alerts[0]["code"] == "disk_space_critical"
    assert alerts[0]["level"] == "error"


def test_configured_disk_mounts_include_mirror_only_when_configured(tmp_path):
    from tgfarm.pipeline import _configured_disk_mounts

    settings = SimpleNamespace(
        data_root=lambda: tmp_path / "data",
        backup_dir=str(tmp_path / "backup"),
        backup_mirror_dir="",
    )
    mounts = _configured_disk_mounts(settings)

    assert [role for role, _ in mounts] == ["данных фермы", "резервных копий"]
    settings.backup_mirror_dir = str(tmp_path / "mirror")
    assert [role for role, _ in _configured_disk_mounts(settings)] == [
        "данных фермы", "резервных копий", "зеркала резервных копий"
    ]

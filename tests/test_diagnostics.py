from tgfarm.diagnostics import _directory_has_entries


def test_missing_dzen_profile_is_a_diagnostic_result_not_an_exception(tmp_path):
    assert _directory_has_entries(tmp_path / "not-created") is False


def test_empty_dzen_profile_is_not_ready(tmp_path):
    profile = tmp_path / "empty-profile"
    profile.mkdir()
    assert _directory_has_entries(profile) is False


def test_nonempty_dzen_profile_is_ready(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    assert _directory_has_entries(profile) is True


def test_operational_alert_summary_is_healthy_when_watchdog_has_no_alerts():
    from tgfarm.diagnostics import _operational_alert_summary

    assert _operational_alert_summary([]) == {
        "ok": True,
        "detail": "нет активных проблем по watchdog",
        "errors": 0,
        "warnings": 0,
    }


def test_operational_alert_summary_fails_on_errors_and_lists_only_safe_codes():
    from tgfarm.diagnostics import _operational_alert_summary

    result = _operational_alert_summary([
        {"level": "error", "code": "backup_stale", "message": "Bearer never-show-this"},
        {"level": "warning", "code": "source_collection_degraded", "message": "private URL"},
        {"level": "info", "code": "heartbeat", "message": "fine"},
    ])

    assert result["ok"] is False
    assert result["errors"] == 1
    assert result["warnings"] == 1
    assert "backup_stale" in result["detail"]
    assert "source_collection_degraded" in result["detail"]
    assert "never-show-this" not in repr(result)
    assert "private URL" not in repr(result)


def test_operational_alert_summary_does_not_fail_doctor_for_warnings_only():
    from tgfarm.diagnostics import _operational_alert_summary

    result = _operational_alert_summary([
        {"level": "warning", "code": "editorial_review_backlog", "message": "many drafts"},
    ])

    assert result["ok"] is True
    assert result["errors"] == 0
    assert result["warnings"] == 1


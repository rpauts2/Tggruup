from pathlib import Path

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

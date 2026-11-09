from __future__ import annotations

from tgfarm import browser_auth


def test_vkhost_parser_accepts_only_official_blank_html_redirect():
    result = browser_auth.vkhost_oauth_data(
        "https://oauth.vk.com/blank.html#access_token=abc123&expires_in=0&user_id=42"
    )
    assert result == {
        "access_token": "abc123",
        "user_id": "42",
        "expires_in": "0",
    }


def test_vkhost_parser_rejects_untrusted_hosts_and_paths():
    assert browser_auth.vkhost_oauth_data(
        "https://evil.invalid/blank.html#access_token=not-a-vk-token"
    ) is None
    assert browser_auth.vkhost_oauth_data(
        "https://oauth.vk.com/authorize#access_token=not-a-redirect"
    ) is None
    assert browser_auth.vkhost_oauth_data(
        "http://oauth.vk.com/blank.html#access_token=insecure"
    ) is None


def test_browser_session_status_does_not_create_empty_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_auth.get_settings(), "data_dir", str(tmp_path))
    status = browser_auth.browser_session_status("instagram")
    assert status["available"] is True
    assert status["profile_saved"] is False
    assert not (tmp_path / "platform_profiles" / "instagram").exists()


def test_browser_session_status_reports_only_safe_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_auth.get_settings(), "data_dir", str(tmp_path))
    profile = tmp_path / "platform_profiles" / "reddit"
    profile.mkdir(parents=True)
    (profile / "profile-marker").write_text("not a credential", encoding="utf-8")
    browser_auth._save_status("reddit", state="verified", profile_saved=True)

    status = browser_auth.browser_session_status("reddit")
    assert status["profile_saved"] is True
    assert status["verified"] is True
    assert "cookies" not in status
    assert "token" not in status

"""Admin API authentication and the desktop host process.

The dashboard can force-publish posts, so "who is allowed to call it" is a
security property, not a nicety. These tests pin the behaviour down.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tgfarm.core import apiauth
from tgfarm.core.apiauth import ensure_token, is_authorised, is_public_path, rotate_token


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


# ------------------------------------------------------------------- token store
def test_token_is_generated_once_and_stable():
    first = ensure_token()
    assert first and len(first) >= 32
    assert ensure_token() == first


def test_token_is_not_written_when_configured(monkeypatch):
    from tgfarm.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.get_settings(), "api_token", "pinned-token")
    assert ensure_token() == "pinned-token"


def test_rotate_invalidates_the_previous_token():
    first = ensure_token()
    second = rotate_token()
    assert second != first
    assert is_authorised(first) is False
    assert is_authorised(second) is True


def test_empty_token_is_never_authorised():
    assert is_authorised("") is False
    assert is_authorised(None) is False
    assert is_authorised("   ") is False


def test_protection_is_on_by_default():
    assert is_authorised(None) is False


def test_authorisation_can_be_disabled_for_trusted_environments(monkeypatch):
    from tgfarm.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.get_settings(), "require_api_token", False)
    assert is_authorised(None) is True
    assert is_authorised("anything") is True


@pytest.mark.parametrize(
    ("path", "public"),
    [
        ("/health", True),
        ("/favicon.ico", True),
        ("/ui/app.css", True),
        ("/ui/app.js", True),
        ("/api/status", False),
        ("/api/posts", False),
        ("/api/memory/topic", False),
    ],
)
def test_public_paths(path, public):
    assert is_public_path(path) is public


# ------------------------------------------------------------------------- the api
def test_health_needs_no_token(client):
    assert client.get("/health").status_code == 200


def test_api_rejects_a_missing_token(client):
    response = client.get("/api/status")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_api_rejects_a_wrong_token(client):
    assert client.get("/api/status", headers={apiauth.HEADER: "nope"}).status_code == 401


def test_topic_dossier_api_requires_authentication(client):
    response = client.get("/api/memory/topic?query=Kestrelion%20reactor")
    assert response.status_code == 401


def test_api_accepts_the_real_token(client):
    token = ensure_token()
    assert client.get("/api/status", headers={apiauth.HEADER: token}).status_code == 200


def test_api_rejects_token_supplied_in_query_string(client):
    token = ensure_token()
    response = client.get(f"/api/status?token={token}")
    assert response.status_code == 401


def test_api_accepts_the_token_from_httponly_cookie(client):
    from tgfarm.core.apiauth import COOKIE

    token = ensure_token()
    response = client.get("/api/status", cookies={COOKIE: token})
    assert response.status_code == 200


def test_token_is_removed_from_query_before_downstream_processing():
    assert apiauth.strip_token_from_query(b"token=secret&days=7") == b"days=7"
    assert apiauth.strip_token_from_query(b"days=7") == b"days=7"


def test_destructive_endpoints_are_protected(client):
    """The endpoints that matter most are the ones that spend real reputation."""
    for path, payload in (
        ("/api/cycle", {}),
        ("/api/posts/publish", {"post_public_ids": ["x"]}),
        ("/api/posts/reject", {"post_public_ids": ["x"]}),
        ("/api/crossposts/post-id/vk/reconcile", {"action": "confirm_posted"}),
    ):
        assert client.post(path, json=payload).status_code == 401, path


def test_the_dashboard_embeds_the_token(client):
    body = client.get("/").text
    assert "__TGFARM__" in body
    assert ensure_token() in body


def test_static_assets_are_reachable_without_a_token(client):
    assert client.get("/ui/app.css").status_code == 200
    assert client.get("/ui/app.js").status_code == 200


# ------------------------------------------------------------------------ desktop
def test_free_port_is_bindable():
    from tgfarm.desktop import free_port

    port = free_port()
    assert 1024 < port < 65536


def test_single_instance_blocks_a_second_holder(tmp_path):
    from tgfarm.desktop import SingleInstance

    lock = tmp_path / "desktop.lock"
    first = SingleInstance(lock)
    assert first.acquire() is True
    assert lock.exists()

    second = SingleInstance(lock)
    assert second.acquire() is False

    first.release()
    assert not lock.exists()
    assert second.acquire() is True


def test_single_instance_takes_over_a_stale_lock(tmp_path):
    """A lock left by a crash must not make the app permanently unstartable."""
    from tgfarm.desktop import SingleInstance

    lock = tmp_path / "desktop.lock"
    lock.write_text("999999", encoding="utf-8")
    fresh = SingleInstance(lock)
    assert fresh.acquire() is True


def test_single_instance_ignores_a_corrupt_lock(tmp_path):
    from tgfarm.desktop import SingleInstance

    lock = tmp_path / "desktop.lock"
    lock.write_text("not-a-pid", encoding="utf-8")
    fresh = SingleInstance(lock)
    assert fresh.acquire() is True


def test_desktop_api_reports_host_facts():
    from tgfarm.desktop import DesktopApi, DesktopApp

    info = DesktopApi(DesktopApp()).info()
    assert info["app"] == "TgFarm"
    assert info["desktop"] is True
    assert info["data_dir"]


def test_desktop_bridge_exposes_the_expected_surface():
    from tgfarm.desktop import DesktopApi

    surface = {m for m in dir(DesktopApi) if not m.startswith("_")}
    assert {"info", "launch_cycle", "notify", "open_data_dir", "quit"} <= surface


def test_desktop_module_imports_without_a_display():
    """The farm, the CLI and the test suite must not depend on pywebview."""
    import tgfarm.desktop as desktop

    assert hasattr(desktop, "DesktopApp")
    assert "webview" not in {name for name in dir(desktop) if name == "webview"}


def test_admin_api_rejects_untrusted_host_even_with_valid_token(client):
    response = client.get(
        "/api/status",
        headers={"Host": "attacker.example", apiauth.HEADER: ensure_token()},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "untrusted host"


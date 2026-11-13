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
        ("/api/platforms/mastodon/auth/callback", True),
        ("/api/platforms/x/auth/callback", True),
        ("/api/platforms/threads/auth/callback", True),
        ("/api/platforms/linkedin/auth/callback", True),
        ("/api/platforms/youtube/auth/callback", True),
        ("/api/platforms/tiktok/auth/callback", True),
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
        ("/api/posts/post-id/video/render", {}),
        ("/api/posts/post-id/tiktok/prepare", {}),
        ("/api/posts/post-id/tiktok/publish", {"caption": "body", "privacy_level": "SELF_ONLY", "consent": True}),
    ):
        assert client.post(path, json=payload).status_code == 401, path


def test_tiktok_direct_post_requires_explicit_consent(client):
    from tgfarm.core.apiauth import HEADER, ensure_token

    response = client.post(
        "/api/posts/this-post/tiktok/publish",
        json={"caption": "A real short", "privacy_level": "SELF_ONLY", "consent": False},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 400
    assert "Подтверди отправку" in response.json()["detail"]


def test_tiktok_oauth_callback_does_not_echo_code_and_is_not_cached(client):
    marker = "secret-tiktok-oauth-code"
    response = client.get(
        f"/api/platforms/tiktok/auth/callback?code={marker}&state=expired-state"
    )
    assert response.status_code == 400
    assert marker not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_short_form_ui_is_wired_and_vk_clips_is_an_explicit_manual_handoff(client):
    script = client.get("/ui/app.js").text
    html = client.get("/").text
    assert "tgfarmRenderShort" in script
    assert "tgfarmPrepareTikTok" in script
    assert "/tiktok/publish" in script
    assert 'id="tiktok-publish-consent"' in html
    assert "https://vk.com/clips" in script


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
    assert lock.exists()
    assert lock.read_text(encoding="ascii") == "0"
    assert second.acquire() is True
    second.release()


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


def test_video_oauth_callbacks_are_public_but_do_not_echo_provider_codes(client):
    for platform_id in ("youtube", "tiktok"):
        marker = f"secret-{platform_id}-authorization-code"
        response = client.get(
            f"/api/platforms/{platform_id}/auth/callback?code={marker}&state=expired-state"
        )
        assert response.status_code == 400
        assert marker not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_admin_api_rejects_untrusted_host_even_with_valid_token(client):
    response = client.get(
        "/api/status",
        headers={"Host": "attacker.example", apiauth.HEADER: ensure_token()},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "untrusted host"



def test_content_studio_and_analytics_workspaces_use_existing_safe_api_contracts(client):
    html = client.get("/").text
    script = client.get("/ui/app.js").text
    for view in ("studio", "analytics"):
        assert f'data-view="{view}"' in html
    for element_id in (
        "studio-sync-status", "studio-post-list", "studio-content", "studio-inspector",
        "studio-platform-select", "studio-preview", "analytics-sync-status",
        "analytics-kpis", "analytics-formats", "analytics-platforms",
        "analytics-ai", "analytics-freshness",
    ):
        assert f'id="{element_id}"' in html
    for contract in (
        "/api/posts?limit=80", "/api/metrics/formats?days=", "/api/metrics/freshness",
        "/api/ai/usage?days=", "/api/crossposts/health", "/api/platform-drafts/", "/api/posts/",
    ):
        assert contract in script
    preview = script[
        script.index("async function buildStudioPreview()"):
        script.index("function analyticsErrorText(", script.index("async function buildStudioPreview()"))
    ]
    assert "/api/platform-drafts/" in preview
    assert 'publish_action === "none"' in preview
    assert "/publish" not in preview
    analytics_loader = script[script.index("async function loadAnalytics("):]
    assert "Promise.allSettled" in analytics_loader
    assert "state.analyticsErrors" in analytics_loader



def test_desktop_open_url_accepts_http_and_https(monkeypatch):
    import webbrowser
    from tgfarm.desktop import DesktopApi, DesktopApp

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0, autoraise=True: opened.append(url) or True)
    bridge = DesktopApi(DesktopApp())
    assert bridge.open_url("https://accounts.example.test/authorize") == "ok"
    assert bridge.open_url("http://127.0.0.1:43121/callback") == "ok"
    assert opened == [
        "https://accounts.example.test/authorize",
        "http://127.0.0.1:43121/callback",
    ]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "file:///etc/passwd",
    "data:text/html,hello",
    "https://user:pass@example.test/",
    "/relative/path",
])


def test_desktop_open_url_rejects_non_web_or_embedded_credentials(url):
    from tgfarm.desktop import DesktopApi, DesktopApp

    with pytest.raises(ValueError):
        DesktopApi(DesktopApp()).open_url(url)


def test_desktop_oauth_port_is_stable_between_restarts(tmp_path):
    from tgfarm.desktop import stable_desktop_port

    first = stable_desktop_port(tmp_path)
    second = stable_desktop_port(tmp_path)
    assert 1024 < first < 65536
    assert second == first
    assert (tmp_path / "desktop.port").read_text(encoding="ascii") == str(first)


def test_desktop_oauth_port_is_not_changed_when_already_in_use(tmp_path):
    import socket

    from tgfarm.desktop import _bind_desktop_socket, stable_desktop_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        blocked = int(occupied.getsockname()[1])
        (tmp_path / "desktop.port").write_text(str(blocked), encoding="ascii")
        assert stable_desktop_port(tmp_path) == blocked
        with pytest.raises(RuntimeError, match="Redirect URI"):
            _bind_desktop_socket(tmp_path, blocked)
        assert (tmp_path / "desktop.port").read_text(encoding="ascii") == str(blocked)


def test_desktop_port_writes_use_unique_atomic_temp_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from tgfarm.desktop import _store_desktop_port

    values = [31000 + index for index in range(20)]
    with ThreadPoolExecutor(max_workers=len(values)) as pool:
        list(pool.map(lambda value: _store_desktop_port(tmp_path, value), values))
    persisted = int((tmp_path / "desktop.port").read_text(encoding="ascii"))
    assert persisted in values
    assert list(tmp_path.glob(".desktop.port.*.tmp")) == []


def test_single_instance_lock_is_atomic_under_concurrent_starts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tgfarm.desktop import SingleInstance

    barrier = Barrier(12)
    instances = [SingleInstance(tmp_path / "desktop.lock") for _ in range(12)]

    def compete(instance):
        barrier.wait()
        return instance if instance.acquire() else None

    with ThreadPoolExecutor(max_workers=len(instances)) as pool:
        winners = [winner for winner in pool.map(compete, instances) if winner is not None]
    assert len(winners) == 1
    winners[0].release()
    assert instances[0].acquire() is True
    instances[0].release()


def test_editorial_command_center_workspaces_are_wired_to_real_ui(client):
    html = client.get("/").text
    script = client.get("/ui/app.js").text
    for view in ("team", "studio", "analytics"):
        assert f'data-view="{view}"' in html
    for element_id in (
        "team-pipeline",
        "studio-post-list", "studio-content", "studio-inspector",
        "studio-platform-select", "studio-preview",
        "analytics-kpis", "analytics-formats", "analytics-platforms",
        "analytics-ai", "analytics-freshness",
    ):
        assert f'id="{element_id}"' in html
    assert 'class="team-role-grid"' in html
    for contract in (
        "/api/runs?limit=100",
        "/api/posts?limit=80",
        "/api/metrics/formats?days=",
        "/api/metrics/freshness",
        "/api/ai/usage?days=",
        "/api/crossposts/health",
        "/api/platform-drafts/",
        "/polish",
    ):
        assert contract in script


def test_oauth_and_desktop_auth_workspaces_keep_secrets_out_of_client_contract(client):
    script = client.get("/ui/app.js").text
    assert "data-connect-save-oauth" in script
    assert "window.pywebview.api.open_url" in script
    assert "row.oauth_allowed !== false" in script
    assert "row.oauth_blocker" in script
    matrix = client.get("/api/connect/matrix", headers={apiauth.HEADER: ensure_token()})
    assert matrix.status_code == 200
    assert "profile_saved" in matrix.text


def test_desktop_listener_reserves_the_selected_oauth_port(tmp_path):
    import socket

    from tgfarm.desktop import _bind_desktop_socket

    listener = _bind_desktop_socket(tmp_path, None)
    try:
        selected = int(listener.getsockname()[1])
        assert 1024 < selected < 65536
        assert (tmp_path / "desktop.port").read_text(encoding="ascii") == str(selected)
        # A second listener cannot steal the port while Uvicorn has not started.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            with pytest.raises(OSError):
                probe.bind(("127.0.0.1", selected))
    finally:
        listener.close()


def test_desktop_listener_refuses_to_change_registered_redirect_if_claimed(tmp_path):
    import socket

    from tgfarm.desktop import _bind_desktop_socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        blocked = int(occupied.getsockname()[1])
        (tmp_path / "desktop.port").write_text(str(blocked), encoding="ascii")
        with pytest.raises(RuntimeError, match="Redirect URI"):
            _bind_desktop_socket(tmp_path, blocked)
        assert (tmp_path / "desktop.port").read_text(encoding="ascii") == str(blocked)

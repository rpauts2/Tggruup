from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_catalog_separates_real_adapters_from_future_networks(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    catalog = platforms.get_platform_catalog()
    entries = {item["id"]: item for item in catalog["platforms"]}

    assert {"telegram", "vk", "dzen", "max", "x", "reddit", "bluesky", "mastodon", "threads", "linkedin", "youtube", "tiktok"} <= {
        key for key, item in entries.items() if item["supported"]
    }
    assert entries["reddit"]["supported"] is True
    assert entries["reddit"]["auth"]["method"] == "oauth"
    assert entries["instagram"]["auth"]["method"] == "oauth"
    assert entries["vk"]["auth"]["method"] == "vkhost"
    # vc.ru and Pikabu use isolated browser sign-in because no public
    # publishing API is available. Browser login must not mark them enabled.
    for platform_id in ("vc_ru", "pikabu"):
        assert entries[platform_id]["supported"] is False
        assert entries[platform_id]["planned"] is True
        assert entries[platform_id]["auth"]["method"] == "browser"
        assert entries[platform_id]["auth"]["browser_session"]["available"] is True
        assert "профиле браузера" in entries[platform_id]["auth"]["note"].lower()
    assert "публичный api" in entries["vc_ru"]["requirements"].lower()
    assert "публичный api" in entries["pikabu"]["requirements"].lower()
    # Every catalogue entry exposes a first-party sign-in URL. This only
    # creates a local browser profile; it does not claim API publish rights.
    assert all(item["auth"]["browser_session"]["available"] for item in entries.values())
    assert entries["bluesky"]["supported"] is True
    assert entries["bluesky"]["auth"]["method"] == "credentials"
    assert {field["key"] for field in entries["bluesky"]["auth"]["fields"]} == {
        "BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"
    }
    assert entries["x"]["auth"]["method"] == "oauth"
    assert {field["key"] for field in entries["x"]["auth"]["fields"]} == {
        "X_OAUTH_CLIENT_ID", "X_OAUTH_CLIENT_SECRET"
    }
    assert entries["mastodon"]["supported"] is True
    assert entries["mastodon"]["auth"]["method"] == "oauth"
    assert entries["mastodon"]["roles"] == ["crosspost"]
    assert entries["threads"]["supported"] is True
    assert entries["threads"]["auth"]["method"] == "oauth"
    assert entries["threads"]["roles"] == ["text", "crosspost"]
    assert {field["key"] for field in entries["threads"]["auth"]["fields"]} == {
        "THREADS_APP_ID", "THREADS_APP_SECRET"
    }
    assert entries["linkedin"]["supported"] is True
    assert entries["linkedin"]["auth"]["method"] == "oauth"
    assert entries["linkedin"]["roles"] == ["text", "professional", "crosspost"]
    assert {field["key"] for field in entries["linkedin"]["auth"]["fields"]} == {
        "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"
    }
    assert entries["reddit"]["planned"] is False
    assert entries["youtube"]["supported"] is True
    assert entries["youtube"]["planned"] is False
    assert entries["youtube"]["enabled"] is False
    assert entries["youtube"]["roles"] == ["video", "crosspost"]
    assert entries["youtube"]["auth"]["method"] == "oauth"
    assert entries["youtube"]["auth"]["integration_ready"] is True
    assert {field["key"] for field in entries["youtube"]["auth"]["fields"]} == {
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"
    }
    assert entries["tiktok"]["supported"] is True
    assert entries["tiktok"]["planned"] is False
    assert entries["tiktok"]["enabled"] is False
    assert entries["tiktok"]["roles"] == ["video"]
    assert entries["tiktok"]["auth"]["method"] == "oauth"
    assert entries["tiktok"]["auth"]["integration_ready"] is True
    assert {"TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"} == {
        field["key"] for field in entries["tiktok"]["auth"]["fields"]
    }
    assert entries["vk_clips"]["supported"] is False
    assert entries["vk_clips"]["planned"] is True
    assert entries["vk_clips"]["roles"] == ["video"]
    assert "не реализована" in entries["vk_clips"]["requirements"]
    assert catalog["summary"]["supported"] == 13
    assert catalog["summary"]["total"] >= 20
    # The public catalog may name environment variables, but never returns their values.
    assert all("access_token" not in item and "api_key" not in item for item in entries.values())
    assert all("secret" not in item for item in entries.values())


def test_disabling_a_supported_network_persists_and_fails_closed(tmp_path, monkeypatch):
    from tgfarm import platforms

    path = tmp_path / "platforms.yaml"
    monkeypatch.setattr(platforms, "_state_path", lambda: path)

    assert platforms.is_platform_enabled("vk") is True
    item = platforms.set_platform_enabled("vk", False)
    assert item["enabled"] is False
    assert item["status"] == "paused"
    assert platforms.is_platform_enabled("vk") is False

    assert platforms.is_platform_enabled("vk") is False
    raw = path.read_text(encoding="utf-8")
    assert "vk: false" in raw


def test_tiktok_switch_can_be_enabled_only_after_its_adapter_is_registered(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    item = platforms.set_platform_enabled("tiktok", True)
    assert item["enabled"] is True
    assert item["configured"] is False
    assert platforms.is_platform_enabled("tiktok") is True
    item = platforms.set_platform_enabled("tiktok", False)
    assert item["enabled"] is False
    assert platforms.is_platform_enabled("tiktok") is False


def test_tiktok_direct_post_is_never_a_background_crosspost_target(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    platforms.set_platform_enabled("tiktok", True)
    assert "tiktok" not in platforms.enabled_crosspost_platforms()


def test_vk_clips_is_visible_as_planned_but_cannot_be_enabled_as_publisher(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    catalog = {item["id"]: item for item in platforms.get_platform_catalog()["platforms"]}
    assert catalog["vk_clips"]["planned"] is True
    assert catalog["vk_clips"]["supported"] is False

    with pytest.raises(ValueError, match="ещё нет рабочего адаптера"):
        platforms.set_platform_enabled("vk_clips", True)
    assert platforms.is_platform_enabled("vk_clips") is False


def test_future_vk_clips_can_be_added_to_the_integration_plan(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    item = platforms.set_platform_planned("facebook", True)
    assert item["planned"] is True
    assert item["supported"] is False

    item = platforms.set_platform_planned("facebook", False)
    assert item["planned"] is False
    assert platforms.is_platform_enabled("tiktok") is False


def test_disabled_crosspost_network_never_reaches_the_post_loader(monkeypatch, tmp_path):
    from tgfarm import platforms, s9_growth

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    platforms.set_platform_enabled("vk", False)

    def fail_if_loaded(_post_pk):
        pytest.fail("disabled target must be rejected before loading or sending a post")

    monkeypatch.setattr(s9_growth, "_load_post", fail_if_loaded)
    assert asyncio.run(s9_growth.crosspost_post(123, ["vk"])) == []


def test_platform_catalog_is_available_only_through_authenticated_api(client, monkeypatch, tmp_path):
    from tgfarm import platforms
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    assert client.get("/api/platforms").status_code == 401

    response = client.get("/api/platforms", headers={HEADER: ensure_token()})
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["supported"] == 13
    entries = {item["id"]: item for item in payload["platforms"]}
    assert entries["reddit"]["supported"] is True


def test_api_cannot_enable_a_platform_without_an_adapter(client, monkeypatch, tmp_path):
    from tgfarm import platforms
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    response = client.put(
        "/api/platforms/facebook",
        json={"enabled": True},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 409
    assert "ещё нет рабочего адаптера" in response.json()["detail"]


def test_api_toggle_pauses_and_resumes_a_supported_platform(client, monkeypatch, tmp_path):
    from tgfarm import platforms
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    headers = {HEADER: ensure_token()}

    paused = client.put("/api/platforms/vk", json={"enabled": False}, headers=headers)
    assert paused.status_code == 200
    assert paused.json()["platform"]["enabled"] is False
    assert platforms.is_platform_enabled("vk") is False

    enabled = client.put("/api/platforms/vk", json={"enabled": True}, headers=headers)
    assert enabled.status_code == 200
    assert enabled.json()["platform"]["enabled"] is True
    assert platforms.is_platform_enabled("vk") is True


def test_dashboard_includes_platform_hub_and_accessible_switches(client):
    from tgfarm.core.apiauth import ensure_token

    response = client.get("/", headers={"X-TGFarm-Token": ensure_token()})
    assert response.status_code == 200
    assert 'data-view="platforms"' in response.text
    assert 'id="platforms-grid"' in response.text

    script = client.get("/ui/app.js", headers={"X-TGFarm-Token": ensure_token()})
    assert script.status_code == 200
    assert "updatePlatformSwitch" in script.text
    assert '"/api/platforms"' in script.text
    assert 'role="switch"' in script.text

def test_paused_primary_platform_stays_queued_and_returns_when_reenabled(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from tgfarm import platforms
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import due_posts

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    channel_id = "paused_platform_" + uuid4().hex[:10]
    post_id = "paused_post_" + uuid4().hex[:10]
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="Paused platform test", niche="general", platform="dzen"
        ))
        session.add(Post(
            public_id=post_id,
            channel_id=channel_id,
            hook="Queued article",
            body="It must remain in the queue while Dzen is paused.",
            status="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        ))

    platforms.set_platform_enabled("dzen", False)
    assert all(row[0] != post_id for row in due_posts(limit=1, post_public_ids=[post_id]))

    with session_scope() as session:
        queued = session.query(Post).filter_by(public_id=post_id).one()
        assert queued.status == "scheduled"

    platforms.set_platform_enabled("dzen", True)
    assert any(row[0] == queued.id for row in due_posts(limit=1000))


def test_crosspost_only_reddit_is_never_misrouted_to_telegram(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from tgfarm import platforms
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import due_posts

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    channel_id = "reddit_primary_" + uuid4().hex[:10]
    post_id = "reddit_queued_" + uuid4().hex[:10]
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                title="Reddit safety test",
                niche="general",
                platform="reddit",
            )
        )
        session.add(
            Post(
                public_id=post_id,
                channel_id=channel_id,
                hook="Reddit-only post",
                body="This must not be sent through Telegram.",
                status="scheduled",
                scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )

    platforms.set_platform_enabled("reddit", True)
    assert all(row[0] != post_id for row in due_posts(limit=1000))

    with session_scope() as session:
        queued = session.query(Post).filter_by(public_id=post_id).one()
        assert queued.status == "scheduled"


def test_platform_auth_metadata_distinguishes_saved_keys_from_real_access(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_auth_state_path", lambda: tmp_path / "auth-checks.json")
    catalog = platforms.get_platform_catalog()
    entries = {item["id"]: item for item in catalog["platforms"]}

    reddit = entries["reddit"]["auth"]
    assert reddit["method"] == "oauth"
    assert reddit["integration_ready"] is True
    assert {field["key"] for field in reddit["fields"]} >= {
        "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
        "REDDIT_SUBREDDIT", "REDDIT_USER_AGENT",
    }
    assert "REDDIT_REFRESH_TOKEN" not in {field["key"] for field in reddit["fields"]}
    assert all("value" not in field for field in reddit["fields"])
    assert all(isinstance(field["has_value"], bool) for field in reddit["fields"])

    assert entries["dzen"]["auth"]["method"] == "browser"
    assert entries["youtube"]["auth"]["method"] == "oauth"
    assert entries["youtube"]["auth"]["integration_ready"] is True
    assert {field["key"] for field in entries["youtube"]["auth"]["fields"]} == {
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"
    }


def test_save_platform_credentials_keeps_existing_values_and_removes_duplicate_key_lines(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tgfarm import platforms
    import tgfarm.core.settings as settings_module

    env_path = tmp_path / ".env"
    env_path.write_text(
        "VK_ACCESS_TOKEN='old-token'\nOTHER_SETTING=keep-me\n"
        "VK_ACCESS_TOKEN='stale-duplicate'\n",
        encoding="utf-8",
    )
    auth_path = tmp_path / "platform_auth_checks.json"
    settings = SimpleNamespace(vk_access_token="old-token", vk_group_id="123456")
    monkeypatch.setattr(platforms, "get_settings", lambda: settings)
    monkeypatch.setattr(platforms, "_auth_state_path", lambda: auth_path)
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    monkeypatch.setattr(settings_module, "reset_settings_cache", lambda: None)

    # Unknown fields must fail closed rather than being silently accepted.
    import pytest
    with pytest.raises(ValueError, match="не разрешено"):
        platforms.save_platform_credentials(
            "vk", {"VK_ACCESS_TOKEN": "fresh-token", "IGNORED": "bad"}
        )

    descriptor = platforms.save_platform_credentials(
        "vk", {"VK_ACCESS_TOKEN": "fresh-token", "VK_GROUP_ID": ""}
    )
    content = env_path.read_text(encoding="utf-8")
    assert content.count("VK_ACCESS_TOKEN=") == 1
    assert "VK_ACCESS_TOKEN='fresh-token'" in content
    assert "stale-duplicate" not in content
    assert "OTHER_SETTING=keep-me" in content
    assert "fresh-token" not in str(descriptor)
    assert descriptor["status"] == "unverified"


def test_reddit_metrics_report_api_is_authenticated_and_read_only(client):
    from tgfarm.core.apiauth import HEADER, ensure_token

    assert client.get("/api/reddit/metrics").status_code == 401
    response = client.get("/api/reddit/metrics", headers={HEADER: ensure_token()})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "communities" in payload
    assert "просмотры" in payload["note"].lower() or "показы" in payload["note"].lower()


def test_platform_auth_check_api_requires_auth_and_supports_video_platforms(client, monkeypatch):
    import tgfarm.platform_auth as auth_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    calls = []

    async def fake_check(platform_id):
        calls.append(platform_id)
        return {
            "ok": True,
            "platform": platform_id,
            "authorized": True,
            "account": "@test",
            "detail": "read-only probe passed",
            "auth": {"status": "authorized", "fields": []},
        }

    monkeypatch.setattr(auth_module, "check_platform_auth", fake_check)
    assert client.post("/api/platforms/vk/auth/check").status_code == 401
    headers = {HEADER: ensure_token()}
    for platform_id in ("vk", "youtube", "tiktok"):
        response = client.post(f"/api/platforms/{platform_id}/auth/check", headers=headers)
        assert response.status_code == 200
        assert response.json()["authorized"] is True
    assert calls == ["vk", "youtube", "tiktok"]


def test_platform_credentials_api_never_echoes_submitted_secret(client, monkeypatch):
    from tgfarm import platforms
    from tgfarm.core.apiauth import HEADER, ensure_token

    captured = {}

    def fake_save(platform_id, values):
        captured.update({"platform_id": platform_id, "values": values})
        return {
            "method": "credentials",
            "status": "unverified",
            "status_label": "Данные сохранены, нужна проверка",
            "fields": [{"key": "VK_ACCESS_TOKEN", "has_value": True, "secret": True}],
        }

    monkeypatch.setattr(platforms, "save_platform_credentials", fake_save)
    response = client.put(
        "/api/platforms/vk/credentials",
        json={"values": {"VK_ACCESS_TOKEN": "top-secret"}},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 200
    assert captured == {"platform_id": "vk", "values": {"VK_ACCESS_TOKEN": "top-secret"}}
    assert "top-secret" not in response.text

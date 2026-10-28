from __future__ import annotations

import asyncio

import pytest


def test_catalog_separates_real_adapters_from_future_networks(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    catalog = platforms.get_platform_catalog()
    entries = {item["id"]: item for item in catalog["platforms"]}

    assert {"telegram", "vk", "dzen", "max", "x"} <= {
        key for key, item in entries.items() if item["supported"]
    }
    assert entries["reddit"]["supported"] is False
    assert entries["reddit"]["planned"] is True
    assert entries["youtube"]["supported"] is False
    assert entries["tiktok"]["supported"] is False
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


def test_unimplemented_network_cannot_be_activated_as_a_publisher(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    with pytest.raises(ValueError, match="ещё нет рабочего адаптера"):
        platforms.set_platform_enabled("reddit", True)
    assert platforms.is_platform_enabled("reddit") is False


def test_future_network_can_be_added_to_the_integration_plan(tmp_path, monkeypatch):
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    item = platforms.set_platform_planned("reddit", True)
    assert item["planned"] is True
    assert item["supported"] is False

    item = platforms.set_platform_planned("reddit", False)
    assert item["planned"] is False
    assert platforms.is_platform_enabled("reddit") is False


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
    assert payload["summary"]["supported"] == 5
    assert any(item["id"] == "reddit" for item in payload["platforms"])


def test_api_cannot_enable_a_platform_without_an_adapter(client, monkeypatch, tmp_path):
    from tgfarm import platforms
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(platforms, "_state_path", lambda: tmp_path / "platforms.yaml")
    response = client.put(
        "/api/platforms/reddit",
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
    assert 'role="switch"' in script.text or "role=\\"switch\\"" in script.text

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

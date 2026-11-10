"""The connect hub must be a real contract, not a mock of one."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tgfarm.platforms import CATALOG


def _client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


def test_matrix_endpoint_lists_every_platform_with_a_mode():
    with _client() as client:
        response = client.get("/api/connect/matrix")
        assert response.status_code == 200
        body = response.json()
    rows = body["platforms"]
    assert len(rows) == len(CATALOG)
    assert {row["id"]: row["mode"] for row in rows}["vk"] == "vkhost"
    assert {row["id"]: row["mode"] for row in rows}["reddit"] == "oauth"
    for row in rows:
        assert row["mode"] in {"oauth", "vkhost", "browser", "token", "manual"}
        assert row["hint"]


def test_authorize_token_platform_returns_fields_without_network():
    with _client() as client:
        response = client.post("/api/connect/telegram/authorize")
        assert response.status_code == 200
        body = response.json()
    assert body["state"] == "awaiting_token"
    assert [field["key"] for field in body["fields"]] == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNELS"]


def test_authorize_oauth_platform_returns_the_start_url():
    with _client() as client:
        body = client.post("/api/connect/reddit/authorize").json()
    assert body["state"] == "awaiting_oauth"
    assert body["start_url"] == "/api/platforms/reddit/auth/start"


def test_authorize_unknown_platform_is_400():
    with TestClient(
        create_app_without_raising(), base_url="http://127.0.0.1"
    ) as client:
        from tgfarm.core.apiauth import ensure_token

        client.headers.update({"X-TGFarm-Token": ensure_token()})
        response = client.post("/api/connect/does_not_exist/authorize")
    assert response.status_code == 400


def test_status_endpoint_reports_mode_and_state():
    with _client() as client:
        body = client.get("/api/connect/vk/status").json()
    assert body["platform"] == "vk"
    assert body["mode"] == "vkhost"
    assert "authorized" in body


def create_app_without_raising():
    from tgfarm.api import create_app

    return create_app(with_scheduler=False)

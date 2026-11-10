"""One authorization contract for every platform must be complete and honest."""

from __future__ import annotations

import asyncio

import pytest

from tgfarm import platform_authorize as pa
from tgfarm.platforms import CATALOG


def test_matrix_covers_the_whole_catalogue():
    matrix = pa.auth_matrix()
    assert {row["id"] for row in matrix["platforms"]} == {str(item["id"]) for item in CATALOG}
    assert sum(matrix["modes"].values()) == len(CATALOG)


def test_modes_match_the_real_mechanism():
    matrix = {row["id"]: row for row in pa.auth_matrix()["platforms"]}
    assert matrix["vk"]["mode"] == "vkhost"
    assert matrix["dzen"]["mode"] == "browser"
    assert matrix["reddit"]["mode"] == "oauth"
    assert matrix["telegram"]["mode"] == "token"
    assert [field["key"] for field in matrix["telegram"]["fields"]] == [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNELS",
    ]
    assert matrix["pikabu"]["mode"] == "browser"
    assert matrix["pikabu"]["login_url"].startswith("https://")


def test_token_flow_does_not_call_the_network():
    result = asyncio.run(pa.authorize("bluesky"))
    assert result["state"] == "awaiting_token"
    assert {field["key"] for field in result["fields"]} == {"BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"}


def test_oauth_flow_returns_the_start_url():
    result = asyncio.run(pa.authorize("reddit"))
    assert result["state"] == "awaiting_oauth"
    assert result["start_url"] == "/api/platforms/reddit/auth/start"


def test_unknown_platform_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(pa.authorize("nope_platform"))


def test_browser_flow_maps_the_login_result(monkeypatch):
    async def fake_login(platform_id, timeout_s=420):
        return {"verified": True, "account": "Наука", "profile_saved": True, "detail": "ok"}

    monkeypatch.setattr("tgfarm.browser_auth.login_in_browser", fake_login)
    result = asyncio.run(pa.authorize("pikabu", timeout_s=60))
    assert result["state"] == "authorized"
    assert result["account"] == "Наука"
    assert result["profile_saved"] is True


def test_vk_flow_stores_the_token_without_returning_it(monkeypatch):
    stored: dict[str, str] = {}
    record: dict[str, object] = {}

    async def fake_capture(timeout_s=420):
        return {"access_token": "secret-token-value", "account": "Иван", "user_id": "1", "expires_in": 0}

    def fake_save(platform_id, values):
        stored.update(values)
        return {"saved": sorted(values)}

    def fake_record(platform_id, authorized, *, account="", detail=""):
        record.update({"platform": platform_id, "authorized": authorized, "account": account})
        return {"checked_at": "now"}

    monkeypatch.setattr("tgfarm.browser_auth.capture_vkhost_token", fake_capture)
    monkeypatch.setattr("tgfarm.platforms.save_platform_credentials", fake_save)
    monkeypatch.setattr("tgfarm.platforms.record_auth_check", fake_record)

    result = asyncio.run(pa.authorize("vk", timeout_s=60))
    assert result["state"] == "authorized"
    assert stored == {"VK_ACCESS_TOKEN": "secret-token-value"}
    assert record["authorized"] is True
    assert "secret-token-value" not in str(result)


def test_dzen_browser_flow_points_at_its_own_window():
    result = asyncio.run(pa.authorize("dzen"))
    assert result["state"] == "separate_window"
    assert result["endpoint"] == "/api/dzen/login"


def test_status_reports_what_is_known(monkeypatch):
    monkeypatch.setattr(
        pa,
        "_read_auth_checks",
        lambda: {
            "vk": {
                "authorized": True,
                "account": "Всякая всячина",
                "fingerprint": "x",
                "checked_at": "now",
            }
        },
    )
    status = pa.authorize_status("vk")
    assert status["mode"] == "vkhost"
    assert status["account"] == "Всякая всячина"

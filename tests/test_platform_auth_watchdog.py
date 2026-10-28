from __future__ import annotations

from types import SimpleNamespace

import pytest


def _platform(
    platform_id: str,
    *,
    supported: bool = True,
    enabled: bool = True,
    configured: bool = True,
    auth_status: str = "authorized",
) -> dict:
    return {
        "id": platform_id,
        "name": platform_id.upper(),
        "supported": supported,
        "enabled": enabled,
        "configured": configured,
        "auth": {"status": auth_status, "detail": "Bearer never-show-this"},
    }


def test_platform_auth_alert_only_flags_enabled_configured_failures():
    from tgfarm.pipeline import _platform_auth_alert

    alert = _platform_auth_alert([
        _platform("telegram", auth_status="failed"),
        _platform("vk", enabled=False, auth_status="failed"),
        _platform("reddit", configured=False, auth_status="failed"),
        _platform("youtube", supported=False, auth_status="failed"),
    ])

    assert alert is not None
    assert alert["level"] == "error"
    assert alert["code"] == "platform_auth_failed"
    assert "TELEGRAM" in alert["message"]
    assert "VK" not in alert["message"]
    assert "reddit" not in alert["message"]
    assert "Bearer" not in repr(alert)


def test_platform_auth_alert_is_quiet_without_confirmed_auth_failures():
    from tgfarm.pipeline import _platform_auth_alert

    assert _platform_auth_alert([]) is None
    assert _platform_auth_alert([
        _platform("telegram", auth_status="authorized"),
        _platform("vk", auth_status="unverified"),
        _platform("reddit", enabled=False, auth_status="failed"),
    ]) is None


@pytest.mark.asyncio
async def test_platform_auth_tick_checks_only_enabled_configured_supported_adapters(monkeypatch):
    from tgfarm import pipeline, platform_auth, platforms

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(online=True),
    )
    monkeypatch.setattr(
        platforms,
        "get_platform_catalog",
        lambda: {
            "platforms": [
                _platform("telegram"),
                _platform("vk", auth_status="failed"),
                _platform("vk", enabled=False),
                _platform("dzen"),  # browser session has a separate health tick
                _platform("youtube", supported=False),
                _platform("bluesky", configured=False),
            ]
        },
    )
    checked = []
    records = []

    async def fake_check(platform_id):
        checked.append(platform_id)
        return {"authorized": platform_id == "telegram", "detail": "private provider response"}

    monkeypatch.setattr(platform_auth, "check_platform_auth", fake_check)
    monkeypatch.setattr(
        pipeline,
        "_record",
        lambda *args, **kwargs: records.append((args, kwargs)),
    )

    report = await pipeline._platform_auth_tick()

    assert checked == ["telegram", "vk"]
    assert report == {
        "checked": 2,
        "authorized": 1,
        "failed": 1,
        "indeterminate": 0,
        "failed_platforms": ["vk"],
        "indeterminate_platforms": [],
    }
    assert "private provider response" not in repr(report)
    assert len(records) == 1
    assert records[0][0][1:3] == ("platform_auth", "error")
    assert records[0][1]["meta"]["failed_platforms"] == ["vk"]


@pytest.mark.asyncio
async def test_platform_auth_tick_does_not_call_network_in_off_mode(monkeypatch):
    from tgfarm import pipeline, platforms

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(online=False),
    )
    monkeypatch.setattr(
        platforms,
        "get_platform_catalog",
        lambda: pytest.fail("offline mode must not load the auth catalog or call providers"),
    )

    report = await pipeline._platform_auth_tick()

    assert report["skipped"] == "mode=off"
    assert report["checked"] == 0


def test_scheduler_registers_periodic_and_startup_auth_checks():
    from tgfarm.pipeline import _build_scheduler

    scheduler = _build_scheduler()

    recurring = scheduler.get_job("platform-auth-health")
    startup = scheduler.get_job("startup-platform-auth-check")

    assert recurring is not None
    assert recurring.name == "read-only platform auth health"
    assert startup is not None
    assert startup.name == "initial read-only platform auth check"

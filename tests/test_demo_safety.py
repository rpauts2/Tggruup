from __future__ import annotations

import asyncio


def test_offline_demo_forces_off_and_restores_live_mode(monkeypatch):
    from tgfarm import demo
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    original_mode = settings.mode

    async def fake_workflow(cycles: int, plan_writes: bool):
        assert cycles == 3
        assert settings.mode == "off"
        # these runs do not isolate, so the demo must not write a plan on them
        assert plan_writes is False
        return {"ran": True}

    monkeypatch.setattr(demo, "_run_demo_workflow", fake_workflow)
    try:
        settings.mode = "live"
        result = asyncio.run(demo.run_demo(cycles=3, offline=True))
        assert result == {"ran": True}
        assert settings.mode == "live"
    finally:
        settings.mode = original_mode


def test_explicit_non_offline_demo_preserves_requested_mode(monkeypatch):
    from tgfarm import demo
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    original_mode = settings.mode

    async def fake_workflow(cycles: int, plan_writes: bool):
        assert cycles == 2
        assert settings.mode == "live"
        assert plan_writes is False
        return {"live_path": True}

    monkeypatch.setattr(demo, "_run_demo_workflow", fake_workflow)
    try:
        settings.mode = "live"
        result = asyncio.run(demo.run_demo(cycles=2, offline=False))
        assert result == {"live_path": True}
        assert settings.mode == "live"
    finally:
        settings.mode = original_mode

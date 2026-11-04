"""Measurement and learning must not live at the mercy of the farm cycle (P20).

Live evidence on 2026-11-04: every cycle was cancelled at 2700 s while still
inside S5 (an LLM rewrite queue of 200 drafts, 60 s per request), so the stages
behind it — S6 visual, S8 analytics, S12 learning, maintenance — never ran.
`post_stats` was last written on 29.10, which is also why format performance has
15 measured posts against 446 published ones and cannot steer anything yet.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace


@pytest.mark.asyncio
async def test_the_tick_measures_then_learns(monkeypatch):
    from tgfarm import pipeline, s8_analytics, s12_learning

    calls: list[str] = []
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(online=True))
    monkeypatch.setattr(s8_analytics, "run", _stage(calls, "s8"))
    monkeypatch.setattr(s12_learning, "run", _stage(calls, "s12"))

    report = await pipeline._metrics_learning_tick()

    assert calls == ["s8", "s12"]
    assert set(report) == {"s8_analytics", "s12_learning"}


@pytest.mark.asyncio
async def test_a_broken_collector_does_not_stop_learning(monkeypatch):
    from tgfarm import pipeline, s8_analytics, s12_learning

    calls: list[str] = []

    async def broken():
        calls.append("s8")
        raise RuntimeError("dzen session expired")

    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(online=True))
    monkeypatch.setattr(s8_analytics, "run", broken)
    monkeypatch.setattr(s12_learning, "run", _stage(calls, "s12"))

    report = await pipeline._metrics_learning_tick()

    assert calls == ["s8", "s12"], "learning must still run when a platform is unreadable"
    assert "dzen session expired" in report["s8_analytics"]["error"]


@pytest.mark.asyncio
async def test_the_tick_does_nothing_in_off_mode(monkeypatch):
    from tgfarm import pipeline, s8_analytics, s12_learning

    calls: list[str] = []
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(online=False))
    monkeypatch.setattr(s8_analytics, "run", _stage(calls, "s8"))
    monkeypatch.setattr(s12_learning, "run", _stage(calls, "s12"))

    report = await pipeline._metrics_learning_tick()

    assert report == {"skipped": "mode=off"}
    assert calls == []


def test_scheduler_registers_periodic_and_startup_metrics_runs():
    from tgfarm.pipeline import _build_scheduler

    scheduler = _build_scheduler()

    recurring = scheduler.get_job("metrics-and-learning")
    startup = scheduler.get_job("startup-metrics-and-learning")

    assert recurring is not None
    assert recurring.name == "metrics collection and learning"
    assert startup is not None
    assert startup.name == "initial metrics collection and learning"


def _stage(calls: list[str], name: str):
    async def run():
        calls.append(name)
        return {"ran": name}

    return run

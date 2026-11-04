"""S5 gets a slice of the cycle, and the rest of the backlog waits (P22).

Live evidence (2026-11-04): `_pending_ids` handed S5 up to 200 drafts, each rewrite
costing an LLM request with a 60 s timeout, the stage never finished, and the
watchdog cancelled the cycle at 2700 s — so S6, S8, S12 and maintenance did not
run at all. A bounded slice is honest work: what is not gated stays a draft and is
picked up by the next cycle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgfarm.s5_quality import run as run_quality


class _Report:
    def __init__(self, passed: bool) -> None:
        self.passed = passed


def _clock(monkeypatch, values: list[float]) -> None:
    """Drive `time.monotonic()` inside S5 with a scripted clock."""
    ticks = iter(values)

    class _Time:
        @staticmethod
        def monotonic() -> float:
            return next(ticks)

    monkeypatch.setattr("tgfarm.s5_quality.time", _Time)


@pytest.mark.asyncio
async def test_the_backlog_pass_stops_at_its_budget_and_reports_the_rest(monkeypatch):
    gated: list[str] = []

    async def fake_gate(public_id: str):
        gated.append(public_id)
        return _Report(True)

    monkeypatch.setattr("tgfarm.s5_quality.gate_post", fake_gate)
    monkeypatch.setattr("tgfarm.s5_quality._pending_ids", lambda: ["d1", "d2", "d3", "d4"])
    # start, check d1 (0 s), check d2 (2 s), check d3 (12 s — past the 10 s budget)
    _clock(monkeypatch, [0.0, 0.0, 2.0, 12.0])

    result = await run_quality(budget_s=10)

    assert gated == ["d1", "d2"]
    assert result == {"in": 4, "passed": 2, "rejected": 0, "deferred": 2}


@pytest.mark.asyncio
async def test_an_explicit_list_of_drafts_is_never_truncated(monkeypatch):
    """The operator asked for these drafts; a time budget must not abandon them."""
    gated: list[str] = []

    async def fake_gate(public_id: str):
        gated.append(public_id)
        return _Report(False)

    monkeypatch.setattr("tgfarm.s5_quality.gate_post", fake_gate)
    _clock(monkeypatch, [0.0, 9_999.0, 19_999.0])

    result = await run_quality(["d1", "d2"], budget_s=1)

    assert gated == ["d1", "d2"]
    assert result["deferred"] == 0
    assert result["rejected"] == 2


@pytest.mark.asyncio
async def test_the_configured_budget_applies_when_the_caller_does_not_override(monkeypatch):
    seen: dict[str, float] = {}

    async def fake_gate(public_id: str):
        return _Report(True)

    monkeypatch.setattr("tgfarm.s5_quality.gate_post", fake_gate)
    monkeypatch.setattr("tgfarm.s5_quality._pending_ids", lambda: ["d1", "d2"])
    monkeypatch.setattr(
        "tgfarm.s5_quality.get_settings",
        lambda: SimpleNamespace(quality_stage_budget_s=42),
    )

    class _Time:
        @staticmethod
        def monotonic() -> float:
            value = seen.get("now", 0.0)
            seen["now"] = value + 30.0  # 0, then 30, then 60 — the second draft is late
            return value

    monkeypatch.setattr("tgfarm.s5_quality.time", _Time)

    result = await run_quality()

    assert result["deferred"] == 1


def test_the_budget_is_a_configurable_floor_not_a_zero(monkeypatch):
    from tgfarm.core.settings import get_settings

    settings = get_settings()

    assert settings.quality_stage_budget_s >= 1

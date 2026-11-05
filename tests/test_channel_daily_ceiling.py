"""MAX_POSTS_PER_CHANNEL_DAY must clamp the daily cap — and be a no-op by default.

This knob is a publishing ceiling, so the two failure modes are symmetric: a
value that does nothing makes `.env.example` a lie, and a value that raises a cap
is a production incident. The ceiling is therefore combined only with `min()`.

The default is 1000 while every platform rule is a single-digit literal, so the
identity at the default is not spot-checked, it is proven over all 21 platforms
and all three reliability regimes.

Only the *combination* is re-implemented below (the 8-attempt floor, the 0.95 and
0.70 thresholds, the one-step raise and clamp, the `min()` against `max_daily`).
The two status sets are read from the planner itself, so widening
`JOURNAL_ATTEMPT_STATUSES` or `JOURNAL_DELIVERED_STATUSES` moves this file's
expectations with it instead of leaving a fifth private vocabulary that keeps
passing while a real cap changes underneath it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tgfarm.distribution as distribution
from tgfarm.core.settings import Settings
from tgfarm.crosspost_journal import JOURNAL_STATUS_PERMANENT, JOURNAL_STATUS_POSTED
from tgfarm.distribution import PLATFORM_RULES, channel_daily_ceiling, clamp_channel_daily_cap

DEFAULT_CEILING = int(Settings.model_fields["max_posts_per_channel_day"].default)


def _cap_without_ceiling(platform_id: str, recent_logs) -> int:
    """`effective_daily_cap` exactly as it behaved before the ceiling was wired in."""
    rule = PLATFORM_RULES[platform_id]
    base = int(rule["daily"])
    attempts = [
        row
        for row in recent_logs
        if row.platform == platform_id and row.status in distribution.JOURNAL_ATTEMPT_STATUSES
    ]
    if len(attempts) < 8:
        return base
    success_rate = sum(
        1 for row in attempts if row.status in distribution.JOURNAL_DELIVERED_STATUSES
    ) / len(attempts)
    if success_rate >= 0.95:
        return min(int(rule["max_daily"]), base + 1)
    if success_rate < 0.70:
        return max(1, base - 1)
    return base


def _logs(platform_id: str, *, ok: int, bad: int):
    """`posted` rows are an attempt that landed; `permanent` is an attempt that did not."""
    rows = [SimpleNamespace(platform=platform_id, status=JOURNAL_STATUS_POSTED) for _ in range(ok)]
    rows += [
        SimpleNamespace(platform=platform_id, status=JOURNAL_STATUS_PERMANENT) for _ in range(bad)
    ]
    return rows


# (ok, bad) — success rate 1.0, 0.5, 0.0: the three branches of the reliability rule.
REGIMES = [(10, 0), (5, 5), (0, 10), (3, 0)]


@pytest.fixture
def ceiling(monkeypatch):
    """Set the knob the way a `.env` line would, without touching the real Settings."""

    def _install(value):
        monkeypatch.setattr(
            distribution,
            "get_settings",
            lambda: SimpleNamespace(max_posts_per_channel_day=value),
        )

    return _install


def test_ceiling_default_is_far_above_every_reachable_cap():
    """The proof that the default changes nothing: no computed cap can reach it."""
    reachable = max(
        _cap_without_ceiling(platform_id, _logs(platform_id, ok=ok, bad=bad))
        for platform_id in PLATFORM_RULES
        for ok, bad in REGIMES
    )
    assert reachable == max(int(rule["max_daily"]) for rule in PLATFORM_RULES.values())
    assert DEFAULT_CEILING == 1000
    assert reachable < DEFAULT_CEILING


def test_real_settings_reproduce_the_pre_knob_caps_exactly():
    """Unstubbed: conftest pins the env at the code default, so every platform in
    every regime must still return the number it returned before this knob worked."""
    for platform_id in PLATFORM_RULES:
        for ok, bad in REGIMES:
            logs = _logs(platform_id, ok=ok, bad=bad)
            assert distribution.effective_daily_cap(platform_id, logs) == _cap_without_ceiling(
                platform_id, logs
            ), platform_id


def test_ceiling_lowers_the_cap_on_every_platform(ceiling):
    ceiling(1)
    for platform_id in PLATFORM_RULES:
        before = _cap_without_ceiling(platform_id, [])
        assert before >= 1
        assert distribution.effective_daily_cap(platform_id, []) == 1
        assert distribution.effective_daily_cap(platform_id, _logs(platform_id, ok=10, bad=0)) == 1


def test_ceiling_never_raises_a_cap(ceiling):
    """A ceiling above the computed cap must leave it alone — min(), never max()."""
    ceiling(DEFAULT_CEILING * 10)
    for platform_id in PLATFORM_RULES:
        for ok, bad in REGIMES:
            logs = _logs(platform_id, ok=ok, bad=bad)
            assert distribution.effective_daily_cap(platform_id, logs) == _cap_without_ceiling(
                platform_id, logs
            )


def test_reliability_adjustment_still_runs_under_the_ceiling(ceiling):
    """The knob clamps the result; it does not replace the reliability logic."""
    ceiling(3)
    healthy = distribution.effective_daily_cap("vk", _logs("vk", ok=10, bad=0))
    degraded = distribution.effective_daily_cap("vk", _logs("vk", ok=0, bad=10))
    assert healthy == 3  # 4 raised by reliability, clamped to 3
    assert degraded == 2  # 3-1, below the ceiling, untouched
    assert _cap_without_ceiling("vk", _logs("vk", ok=10, bad=0)) == 4


def test_ceiling_is_read_on_every_call(ceiling):
    """One import, several settings values: the last one written is the one used."""
    ceiling(DEFAULT_CEILING)
    assert distribution.effective_daily_cap("vk", []) == 3
    ceiling(2)
    assert distribution.effective_daily_cap("vk", []) == 2
    ceiling(DEFAULT_CEILING)
    assert distribution.effective_daily_cap("vk", []) == 3


def test_unreadable_or_non_positive_ceiling_disables_the_clamp(ceiling):
    """A half-written `.env` line must not stop cross-posting entirely: no clamp.

    Zero is the literal "publish nothing" reading, but this knob had no effect at
    all until now, and the primary channels are still capped elsewhere, so a
    stray 0 here would silently starve every destination.
    """
    for broken in (0, -1, None, "", "не число", float("nan")):
        ceiling(broken)
        assert channel_daily_ceiling() == 0
        assert distribution.effective_daily_cap("vk", []) == _cap_without_ceiling("vk", [])


def test_missing_settings_attribute_falls_back(monkeypatch):
    """A stubbed `get_settings` without the field (older tests) must not raise."""
    monkeypatch.setattr(distribution, "get_settings", lambda: SimpleNamespace())
    assert channel_daily_ceiling() == 0
    assert clamp_channel_daily_cap(3) == 3


def test_clamp_helper_is_a_one_way_function():
    assert clamp_channel_daily_cap(4) <= 4
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(distribution, "get_settings", lambda: SimpleNamespace(max_posts_per_channel_day=2))
        assert clamp_channel_daily_cap(4) == 2
        assert clamp_channel_daily_cap(1) == 1

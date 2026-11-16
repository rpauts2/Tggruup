"""Tests for ``primary_platform_daily_cap`` — tunable daily caps for VK and Dzen.

Verifies the three-source priority:
1. Explicit override via ``PRIMARY_PLATFORM_DAILY_CAPS`` (test/operator edit)
2. Environment / Settings fields ``PRIMARY_VK_DAILY_CAP`` / ``PRIMARY_DZEN_DAILY_CAP``
3. Code defaults ``{"dzen": 2, "vk": 3}``
"""

from __future__ import annotations

import pytest

from tgfarm.core.settings import reset_settings_cache
from tgfarm.s7_publish import (
    PRIMARY_PLATFORM_DAILY_CAPS,
    primary_platform_daily_cap,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_caps():
    """Restore the code-default dict after every test so neighbours don't leak."""
    from tgfarm.s7_publish import _PRIMARY_DAILY_CAP_DEFAULTS

    PRIMARY_PLATFORM_DAILY_CAPS.clear()
    PRIMARY_PLATFORM_DAILY_CAPS.update(_PRIMARY_DAILY_CAP_DEFAULTS)
    reset_settings_cache()
    yield
    PRIMARY_PLATFORM_DAILY_CAPS.clear()
    PRIMARY_PLATFORM_DAILY_CAPS.update(_PRIMARY_DAILY_CAP_DEFAULTS)
    reset_settings_cache()


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaults:
    """The function returns 3 for VK and 2 for Dzen when nothing is overridden."""

    def test_vk_default(self):
        assert primary_platform_daily_cap("vk") == 3

    def test_dzen_default(self):
        assert primary_platform_daily_cap("dzen") == 2


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    """``PRIMARY_*_DAILY_CAP`` env vars move the numbers."""

    def test_vk_env(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_VK_DAILY_CAP", "99")
        reset_settings_cache()
        assert primary_platform_daily_cap("vk") == 99

    def test_dzen_env(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_DZEN_DAILY_CAP", "77")
        reset_settings_cache()
        assert primary_platform_daily_cap("dzen") == 77


# ---------------------------------------------------------------------------
# Explicit override wins over env
# ---------------------------------------------------------------------------


class TestExplicitOverridePriority:
    """``PRIMARY_PLATFORM_DAILY_CAPS`` takes precedence over env vars."""

    def test_explicit_vk_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_VK_DAILY_CAP", "999")
        reset_settings_cache()
        PRIMARY_PLATFORM_DAILY_CAPS["vk"] = 42
        assert primary_platform_daily_cap("vk") == 42

    def test_explicit_dzen_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_DZEN_DAILY_CAP", "999")
        reset_settings_cache()
        PRIMARY_PLATFORM_DAILY_CAPS["dzen"] = 13
        assert primary_platform_daily_cap("dzen") == 13


# ---------------------------------------------------------------------------
# Unknown platform
# ---------------------------------------------------------------------------


class TestUnknownPlatform:
    """A platform outside the cap dict is a caller bug, not a silent default."""

    def test_unknown_platform_raises_keyerror(self):
        # "reddit" is not a key in _PRIMARY_DAILY_CAP_DEFAULTS; the lookup must
        # fail loudly instead of borrowing another platform's budget.
        with pytest.raises(KeyError):
            primary_platform_daily_cap("reddit")


# ---------------------------------------------------------------------------
# Plan-report consistency
# ---------------------------------------------------------------------------


class TestPlanReportConsistency:
    """The cap the plan report shows equals the cap the publisher enforces."""

    def test_plan_report_uses_function(self):
        """Verify that ``_build_primary_platform_plan`` in distribution.py
        calls ``primary_platform_daily_cap`` instead of restating literal ``2``
        or ``3``.  This is a structural check — it looks for the function name in
        the source code of the module."""
        import inspect

        from tgfarm import distribution

        source = inspect.getsource(distribution._build_primary_platform_plan)
        assert "primary_platform_daily_cap" in source, (
            "distribution._build_primary_platform_plan should call "
            "primary_platform_daily_cap(platform) instead of a literal cap."
        )

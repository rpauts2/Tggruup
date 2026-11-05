"""The primary plan must promise exactly the capacity the publisher enforces.

`ad5d9ab` clamped `distribution.effective_daily_cap` (crosspost) and `48ad408`
clamped the two enforcement sites on the primary path (s7's scheduler, s3's
routing gate), but `_build_primary_platform_plan` still recomputed the three
primary caps by hand and reported them unclamped. At `MAX_POSTS_PER_CHANNEL_DAY=1`
the panel therefore kept advertising VK as `daily_cap 3 / remaining 3` while the
publisher stopped at 1: the one number an operator uses to judge whether the
dial worked was the one number the dial did not move.

The style is inherited from `tests/test_channel_daily_ceiling_primary.py`: the
enforced cap is not restated in the test, it is measured by running the real s7
scheduler over seeded channels and reading back how many posts it schedules. The
reported cap comes from the real plan builder. That makes the assertion a
comparison of two production code paths rather than a tautology.

The telegram line is clamped per persona, not once for the summed platform
budget, because `MAX_POSTS_PER_CHANNEL_DAY` is a *channel* ceiling and s7/s3
apply it to each channel separately. Clamping the sum would trade an
over-promise for an under-promise.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tgfarm.distribution as distribution
import tgfarm.s7_publish as publish
from tgfarm.config import Persona
from tgfarm.core.settings import Settings
from tgfarm.distribution import channel_daily_ceiling, clamp_channel_daily_cap
from tgfarm.s7_publish import PRIMARY_PLATFORM_DAILY_CAPS

DEFAULT_CEILING = int(Settings.model_fields["max_posts_per_channel_day"].default)

_SEQ = itertools.count()

# More gated posts than any quota under test, so the number the scheduler returns
# is the cap itself and never the size of the sample.
_CANDIDATES = 20
# Leftover gated posts from other tests share this database, and the scheduler
# reads them all before it reaches ours.
_SCAN_LIMIT = 5000

PLATFORMS = ("telegram", "vk", "dzen")
QUOTAS = (1, 3, 12)
CEILINGS = (1, 2, 5)
BROKEN_VALUES = (0, -1, None, "", "не число", float("nan"))


def _install_ceiling(monkeypatch, value) -> None:
    """Set the knob the way a `.env` line would, without touching real Settings."""
    monkeypatch.setattr(
        distribution,
        "get_settings",
        lambda: SimpleNamespace(max_posts_per_channel_day=value),
    )


@pytest.fixture
def ceiling(monkeypatch):
    def _install(value):
        _install_ceiling(monkeypatch, value)

    return _install


def _persona(platform: str, posts_per_day: int) -> Persona:
    channel_id = f"pl{next(_SEQ):03d}"
    return Persona(
        id=channel_id,
        title=channel_id,
        niche="general",
        platform=platform,
        posts_per_day=posts_per_day,
    )


def _reported_plan(monkeypatch, personas: list[Persona]) -> dict[str, dict]:
    """The plan the panel shows for these personas, keyed by platform."""
    monkeypatch.setattr(distribution, "get_config_personas", lambda: list(personas))
    settings = SimpleNamespace(mode="dry", approval_required=False)
    catalog = {
        platform: {"enabled": True, "configured": True, "name": platform.upper()}
        for platform in PLATFORMS
    }
    items = distribution._build_primary_platform_plan(datetime.now(UTC), settings, catalog)
    return {item["platform"]: item for item in items}


@dataclass
class _ScheduleConfig:
    """Only the parts of `FarmConfig` that `schedule_ready_posts` reads.

    `interval` cadence keeps the measurement honest: the weights mode would put
    every post through the peak-hour table and the per-hour fleet ceiling, a
    second limiter competing with the quota under test.
    """

    personas: dict[str, Persona]
    schedule: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(
            cadence={"mode": "interval", "min_gap_s": 60, "max_gap_s": 120},
            fleet_posts_per_hour=1000,
        )
    )

    def persona(self, channel_id: str) -> Persona | None:
        return self.personas.get(channel_id)


def _enforced_capacity(db, monkeypatch, personas: list[Persona], *, platform: str) -> int:
    """How many posts s7 schedules today for these channels of one platform.

    The per-channel branch re-reads the channel's own history on every post, so
    an empty channel needs no pinning. The platform branch reads a budget shared
    by every channel of that platform, which the throwaway test database cannot
    supply a clean baseline for, so its starting count is pinned to zero and the
    scheduler's own in-run increment decides the answer. Rows are removed
    afterwards, so nothing leaks into the next case.
    """
    from tgfarm.db.models import Channel
    from tgfarm.db.models import Post as PostRow

    channel_ids = [persona.id for persona in personas]
    for persona in personas:
        db.merge(
            Channel(
                id=persona.id,
                title=persona.title,
                niche=persona.niche,
                platform=platform,
                enabled=True,
            )
        )
        for index in range(_CANDIDATES):
            db.add(
                PostRow(
                    public_id=f"{persona.id}-{index:02d}",
                    channel_id=persona.id,
                    hook="Хук квоты.",
                    body="Тело поста, достаточно длинное для планировщика.",
                    status="gated",
                    generation_provider="llm",
                )
            )
    if platform in PRIMARY_PLATFORM_DAILY_CAPS:
        monkeypatch.setattr(publish, "_posts_platform_today", lambda session, plat, now: 0)
    db.commit()

    monkeypatch.setattr(publish, "get_config", lambda: _ScheduleConfig({p.id: p for p in personas}))
    try:
        scheduled = publish.schedule_ready_posts(limit=_SCAN_LIMIT, blocked={})
    finally:
        db.query(PostRow).filter(PostRow.channel_id.in_(channel_ids)).delete(synchronize_session=False)
        db.query(Channel).filter(Channel.id.in_(channel_ids)).delete(synchronize_session=False)
        db.commit()

    assert scheduled < _CANDIDATES * len(personas), "the quota must stop the run, not the sample size"
    return scheduled


# ------------------------------------------------------------------ the defect
@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("posts_per_day", QUOTAS)
@pytest.mark.parametrize("ceiling_value", CEILINGS)
def test_reported_plan_cap_is_the_cap_the_publisher_enforces(
    db, monkeypatch, ceiling, platform, posts_per_day, ceiling_value
):
    """Plan and publisher are read off one persona, one platform, one ceiling."""
    ceiling(ceiling_value)
    persona = _persona(platform, posts_per_day)
    enforced = _enforced_capacity(db, monkeypatch, [persona], platform=platform)
    item = _reported_plan(monkeypatch, [persona])[platform]

    assert item["daily_cap"] == enforced, "the panel promised a capacity the publisher refuses to use"
    assert item["remaining"] == max(0, item["daily_cap"] - int(item["daily_sent"]))


def test_a_ceiling_of_one_stops_promise_and_capacity_together(db, monkeypatch, ceiling):
    """The symptom as measured: VK was advertised at 3 while the publisher held at 1."""
    persona = _persona("vk", 12)
    ceiling(1)
    item = _reported_plan(monkeypatch, [persona])["vk"]

    assert item["daily_cap"] == 1
    assert item["daily_cap"] == _enforced_capacity(db, monkeypatch, [persona], platform="vk")

    dzen = _persona("dzen", 12)
    assert _reported_plan(monkeypatch, [dzen])["dzen"]["daily_cap"] == 1


@pytest.mark.parametrize("posts_per_day", QUOTAS)
@pytest.mark.parametrize("ceiling_value", CEILINGS)
def test_telegram_budget_is_clamped_per_channel_not_per_platform(
    db, monkeypatch, ceiling, posts_per_day, ceiling_value
):
    """Two telegram channels each get the ceiling, because the dial is per channel.

    `min(sum, ceiling)` would have been the naive reading of "clamp the plan":
    at two channels, quota 12 and ceiling 2 it reports 2 while the fleet may
    legitimately publish 4. The plan must say 4, which is what s7 schedules.
    """
    ceiling(ceiling_value)
    personas = [_persona("telegram", posts_per_day), _persona("telegram", posts_per_day)]
    enforced = _enforced_capacity(db, monkeypatch, personas, platform="telegram")
    item = _reported_plan(monkeypatch, personas)["telegram"]

    assert item["daily_cap"] == enforced
    assert item["daily_cap"] == 2 * clamp_channel_daily_cap(max(1, posts_per_day))


# --------------------------------------------------------------- invariance
def test_real_settings_leave_the_reported_caps_unchanged(monkeypatch):
    """Unstubbed: conftest pins the env at the code default, so nothing moves."""
    personas = [_persona("telegram", 5), _persona("vk", 5), _persona("dzen", 5)]
    plan = _reported_plan(monkeypatch, personas)

    assert plan["telegram"]["daily_cap"] == 5
    assert plan["vk"]["daily_cap"] == PRIMARY_PLATFORM_DAILY_CAPS["vk"]
    assert plan["dzen"]["daily_cap"] == PRIMARY_PLATFORM_DAILY_CAPS["dzen"]


def test_a_ceiling_above_the_cap_never_raises_it(monkeypatch, ceiling):
    ceiling(DEFAULT_CEILING * 10)
    personas = [_persona("telegram", 3), _persona("vk", 3), _persona("dzen", 3)]
    plan = _reported_plan(monkeypatch, personas)

    assert plan["telegram"]["daily_cap"] == 3
    assert plan["vk"]["daily_cap"] == 3
    assert plan["dzen"]["daily_cap"] == 2


@pytest.mark.parametrize("broken", BROKEN_VALUES)
def test_a_broken_ceiling_leaves_the_reported_plan_untouched(monkeypatch, ceiling, broken):
    """A half-written `.env` line must not starve the panel either: no clamp at all.

    Same reading as the three enforcement sites — 0, a negative, a missing
    attribute, garbage text or a NaN all mean "the operator said nothing".
    """
    ceiling(broken)
    assert channel_daily_ceiling() == 0
    personas = [_persona("telegram", 12), _persona("vk", 12), _persona("dzen", 12)]
    plan = _reported_plan(monkeypatch, personas)

    assert plan["telegram"]["daily_cap"] == 12
    assert plan["vk"]["daily_cap"] == 3
    assert plan["dzen"]["daily_cap"] == 2


@pytest.mark.parametrize("ceiling_value", CEILINGS)
def test_no_reported_cap_can_exceed_the_dial(monkeypatch, ceiling, ceiling_value):
    ceiling(ceiling_value)
    personas = [_persona("telegram", 12), _persona("vk", 12), _persona("dzen", 12)]
    plan = _reported_plan(monkeypatch, personas)

    assert all(item["daily_cap"] <= ceiling_value for item in plan.values())
    assert channel_daily_ceiling() == ceiling_value

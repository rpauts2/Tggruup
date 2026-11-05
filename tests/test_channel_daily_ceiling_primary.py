"""MAX_POSTS_PER_CHANNEL_DAY must reach the primary publish path too.

`ad5d9ab` clamped `distribution.effective_daily_cap`, which is the crosspost
budget. The telegram/vk/dzen quota is enforced in two other places — the s3
routing gate and the s7 scheduler — and both read the persona number alone, so
an operator who lowered the dial saw the reported plan shrink while the fleet
kept publishing at full speed. This is the mirror of
`tests/test_channel_daily_ceiling.py` for those two paths.

The style is the same for the same reason: the pre-change arithmetic is restated
by hand and compared case by case, so the default ceiling of 1000 is proven inert
over the whole matrix instead of spot-checked, and a ceiling below a cap is
proven to actually stop work. The caps are measured by running the real s7/s3
code and reading back where it stops, not by calling the helper directly.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tgfarm.distribution as distribution
import tgfarm.s3_router as router
import tgfarm.s7_publish as publish
from tgfarm.config import Persona
from tgfarm.core.settings import Settings
from tgfarm.distribution import channel_daily_ceiling, clamp_channel_daily_cap
from tgfarm.s7_publish import PRIMARY_PLATFORM_DAILY_CAPS

DEFAULT_CEILING = int(Settings.model_fields["max_posts_per_channel_day"].default)

_UNSTUBBED = object()
_SEQ = itertools.count()

# More gated posts than any quota under test, so the number the scheduler returns
# is the cap itself and never the size of the sample.
_CANDIDATES = 20
# Leftover gated posts from other tests share this database, and the scheduler
# reads them all before it reaches ours.
_SCAN_LIMIT = 5000

PLATFORMS = ("telegram", "vk", "dzen")
QUOTAS = (1, 2, 3, 5, 12)

# (platform, catch_all, niche): the niche loop and the catch-all loop carry
# separate copies of the gate, so both are exercised.
ROUTE_CASES = (
    ("telegram", False, "tech"),
    ("telegram", True, "general"),
    ("vk", True, "general"),
    ("dzen", True, "general"),
)


def _publish_cap_without_ceiling(platform: str, posts_per_day: int) -> int:
    """s7's `daily_cap` lines exactly as they stood before the clamp was wired in."""
    if platform in PRIMARY_PLATFORM_DAILY_CAPS:
        return int(PRIMARY_PLATFORM_DAILY_CAPS[platform])
    return max(1, int(posts_per_day))


def _route_gate_without_ceiling(posts_per_day: int) -> int:
    """s3's gate as it stood: the raw persona number, no floor and no clamp."""
    return int(posts_per_day)


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


# ------------------------------------------------------------------ s7 publish
@dataclass
class _ScheduleConfig:
    """Only the parts of `FarmConfig` that `schedule_ready_posts` reads.

    `interval` cadence keeps the measurement honest: the weights mode would put
    every post through the peak-hour table and the per-hour fleet ceiling, which
    is a second limiter competing with the quota under test.
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


def _publish_cap(
    db,
    monkeypatch,
    *,
    platform: str,
    posts_per_day: int,
    used_today: int = 0,
    ceiling=_UNSTUBBED,
) -> int:
    """How many posts s7 schedules today for one channel — its daily cap.

    The two budget branches are driven differently, because they count differently.
    The per-channel branch re-reads the channel's own history on every post, so it
    gets real rows and the scheduler's own counting decides the answer. The
    platform branch reads a budget shared by every channel of that platform, which
    the throwaway test database cannot supply a clean baseline for, so its starting
    count is pinned and the scheduler's in-run increment decides the rest. Rows are
    removed afterwards either way, so nothing leaks into the next case.
    """
    from tgfarm.db.models import Channel
    from tgfarm.db.models import Post as PostRow

    if ceiling is not _UNSTUBBED:
        _install_ceiling(monkeypatch, ceiling)

    channel_id = f"pc{next(_SEQ):03d}"
    persona = Persona(
        id=channel_id,
        title=channel_id,
        niche="general",
        platform=platform,
        posts_per_day=posts_per_day,
    )
    db.merge(Channel(id=channel_id, title=channel_id, niche="general", platform=platform, enabled=True))
    for index in range(_CANDIDATES):
        db.add(
            PostRow(
                public_id=f"{channel_id}-{index:02d}",
                channel_id=channel_id,
                hook="Хук квоты.",
                body="Тело поста, достаточно длинное для планировщика.",
                status="gated",
                generation_provider="llm",
            )
        )
    if platform in PRIMARY_PLATFORM_DAILY_CAPS:
        monkeypatch.setattr(publish, "_posts_platform_today", lambda session, plat, now: used_today)
    else:
        now = datetime.now(UTC)
        for index in range(used_today):
            db.add(
                PostRow(
                    public_id=f"{channel_id}-d{index:02d}",
                    channel_id=channel_id,
                    hook="Хук сегодня.",
                    body="Тело поста, опубликованного сегодня.",
                    status="published",
                    published_at=now,
                    generation_provider="llm",
                )
            )
    db.commit()

    monkeypatch.setattr(publish, "get_config", lambda: _ScheduleConfig({channel_id: persona}))
    try:
        scheduled = publish.schedule_ready_posts(limit=_SCAN_LIMIT, blocked={})
    finally:
        db.query(PostRow).filter(PostRow.channel_id == channel_id).delete()
        db.query(Channel).filter(Channel.id == channel_id).delete()
        db.commit()

    assert scheduled < _CANDIDATES, "the quota must stop the run, not the sample size"
    return scheduled


# ------------------------------------------------------------------ s3 routing
@dataclass
class _RouteConfig:
    """Only the parts of `FarmConfig` that `plan_cluster` reads."""

    personas: list[Persona]

    def by_platform(self, platform: str) -> list[Persona]:
        return [p for p in self.personas if p.platform == platform and p.enabled]

    @property
    def catch_all_personas(self) -> list[Persona]:
        return [p for p in self.personas if p.enabled and p.catch_all]


def _seed_cluster(public_id: str) -> None:
    """A routable cluster needs a niche and at least one member row."""
    from tgfarm.db.models import Cluster
    from tgfarm.db.models import RawItem as RawItemRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        cluster = Cluster(
            public_id=public_id, label="OpenAI выпустила GPT-5.5", niche="tech", heat=0.9, status="ready"
        )
        session.add(cluster)
        session.flush()
        session.add(
            RawItemRow(
                source_id="techcrunch_ai",
                external_id=public_id,
                url=f"https://example.invalid/{public_id}",
                title="OpenAI выпустила GPT-5.5",
                text="Модель выполняет 40 процентов задач инженеров.",
                niche_hint=["tech"],
                source_weight=0.7,
                cluster_id=cluster.id,
            )
        )


async def _route_cap(
    monkeypatch,
    *,
    platform: str,
    posts_per_day: int,
    catch_all: bool,
    niche: str = "tech",
    ceiling=_UNSTUBBED,
) -> int:
    """The `posts_today` at which s3 stops routing this persona — its daily cap."""
    if ceiling is not _UNSTUBBED:
        _install_ceiling(monkeypatch, ceiling)

    tag = f"rc{next(_SEQ):03d}"
    persona = Persona(
        id=f"ch_{tag}",
        title=f"ch_{tag}",
        niche=niche,
        platform=platform,
        catch_all=catch_all,
        posts_per_day=posts_per_day,
        angle_weights={"news": 1.0},
    )
    monkeypatch.setattr(router, "get_config", lambda: _RouteConfig([persona]))
    cluster_public_id = f"cl_{tag}"
    _seed_cluster(cluster_public_id)

    for posts_today in range(max(2, posts_per_day) + 3):
        monkeypatch.setattr(
            router,
            "channel_load",
            lambda _cid, _used=posts_today: {
                "posts_today": _used,
                "last_post_at": None,
                "recent_tokens": [],
                "recent_embeds": [],
                "covered_topics": set(),
            },
        )
        plans = await router.plan_cluster(cluster_public_id, rng=random.Random(7))
        if not plans:
            return posts_today

    pytest.fail(f"gate never closed for platform={platform} posts_per_day={posts_per_day}")


# ---------------------------------------------------------------------- proofs
def test_default_ceiling_is_unreachable_for_the_primary_paths():
    """The proof that the default changes nothing on these two paths as well:
    no persona quota and no platform cap can reach 1000."""
    reachable = max(
        _publish_cap_without_ceiling(platform, quota) for platform in PLATFORMS for quota in QUOTAS
    )
    assert DEFAULT_CEILING == 1000
    assert reachable == max(max(QUOTAS), max(PRIMARY_PLATFORM_DAILY_CAPS.values()))
    assert reachable < DEFAULT_CEILING


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("posts_per_day", QUOTAS)
@pytest.mark.parametrize("used_today", (0, 1))
def test_real_settings_leave_the_publish_cap_untouched(db, monkeypatch, platform, posts_per_day, used_today):
    """Unstubbed: conftest pins the env at the code default, so s7 must schedule
    exactly what it scheduled before this knob worked."""
    base = _publish_cap_without_ceiling(platform, posts_per_day)
    scheduled = _publish_cap(
        db, monkeypatch, platform=platform, posts_per_day=posts_per_day, used_today=used_today
    )
    assert scheduled == max(0, base - used_today)


_CASE_IDS = [f"{platform}-{'catchall' if catch_all else 'niche'}" for platform, catch_all, _ in ROUTE_CASES]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ROUTE_CASES, ids=_CASE_IDS)
@pytest.mark.parametrize("posts_per_day", QUOTAS)
async def test_real_settings_leave_the_route_gate_untouched(monkeypatch, case, posts_per_day):
    platform, catch_all, niche = case
    cap = await _route_cap(
        monkeypatch, platform=platform, posts_per_day=posts_per_day, catch_all=catch_all, niche=niche
    )
    assert cap == _route_gate_without_ceiling(posts_per_day)


def test_ceiling_below_the_cap_lowers_it_and_stops_scheduling(db, monkeypatch, ceiling):
    """A ceiling under the platform cap must actually hold the fleet back."""
    ceiling(1)
    for platform in PLATFORMS:
        assert _publish_cap(db, monkeypatch, platform=platform, posts_per_day=12, ceiling=1) == 1
        assert _publish_cap(db, monkeypatch, platform=platform, posts_per_day=2, ceiling=1) == 1
        assert _publish_cap(
            db, monkeypatch, platform=platform, posts_per_day=12, used_today=1, ceiling=1
        ) == 0

    ceiling(2)
    assert _publish_cap(db, monkeypatch, platform="dzen", posts_per_day=12, ceiling=2) == 2
    assert _publish_cap(db, monkeypatch, platform="telegram", posts_per_day=5, ceiling=2) == 2


@pytest.mark.asyncio
async def test_ceiling_below_the_quota_stops_routing(monkeypatch, ceiling):
    ceiling(1)
    for platform, catch_all, niche in ROUTE_CASES:
        for posts_per_day in (2, 5, 12):
            cap = await _route_cap(
                monkeypatch,
                platform=platform,
                posts_per_day=posts_per_day,
                catch_all=catch_all,
                niche=niche,
                ceiling=1,
            )
            assert cap == 1, f"{platform}/{posts_per_day} routed past the ceiling"

    ceiling(2)
    cap = await _route_cap(monkeypatch, platform="telegram", posts_per_day=12, catch_all=False, ceiling=2)
    assert cap == 2


def test_ceiling_never_raises_a_publish_cap(db, monkeypatch, ceiling):
    """A ceiling above the computed cap leaves it alone — min(), never max()."""
    for value in (DEFAULT_CEILING, DEFAULT_CEILING * 10):
        ceiling(value)
        for platform in PLATFORMS:
            for posts_per_day in QUOTAS:
                base = _publish_cap_without_ceiling(platform, posts_per_day)
                assert _publish_cap(
                    db, monkeypatch, platform=platform, posts_per_day=posts_per_day, ceiling=value
                ) == base


@pytest.mark.asyncio
async def test_ceiling_never_raises_a_route_gate(monkeypatch, ceiling):
    ceiling(DEFAULT_CEILING * 10)
    for platform, catch_all, niche in ROUTE_CASES:
        for posts_per_day in QUOTAS:
            cap = await _route_cap(
                monkeypatch,
                platform=platform,
                posts_per_day=posts_per_day,
                catch_all=catch_all,
                niche=niche,
                ceiling=DEFAULT_CEILING * 10,
            )
            assert cap == _route_gate_without_ceiling(posts_per_day)


def test_broken_ceiling_changes_nothing_on_either_primary_path(db, monkeypatch, ceiling):
    """A half-written `.env` line must not starve the fleet: no clamp at all.

    Same reading as the crosspost path — 0, a negative, a missing attribute or
    garbage text all mean "the operator said nothing".
    """
    for broken in (0, -1, None, "", "не число", float("nan")):
        ceiling(broken)
        assert channel_daily_ceiling() == 0
        for platform in PLATFORMS:
            for posts_per_day in (1, 3, 12):
                base = _publish_cap_without_ceiling(platform, posts_per_day)
                assert _publish_cap(
                    db, monkeypatch, platform=platform, posts_per_day=posts_per_day, ceiling=broken
                ) == base


@pytest.mark.asyncio
async def test_broken_ceiling_changes_nothing_at_the_route_gate(monkeypatch, ceiling):
    for broken in (0, -1, None, "", "не число", float("nan")):
        ceiling(broken)
        for platform, catch_all, niche in ROUTE_CASES:
            for posts_per_day in (1, 3, 12):
                cap = await _route_cap(
                    monkeypatch,
                    platform=platform,
                    posts_per_day=posts_per_day,
                    catch_all=catch_all,
                    niche=niche,
                    ceiling=broken,
                )
                assert cap == _route_gate_without_ceiling(posts_per_day)


@pytest.mark.parametrize("ceiling_value", (1, 2))
@pytest.mark.asyncio
async def test_gate_plan_and_publisher_obey_one_ceiling(db, monkeypatch, ceiling_value):
    """The whole point of the knob: no path may promise more than the ceiling.

    VK is the one platform with a cap in all three places — the persona quota,
    `PRIMARY_PLATFORM_DAILY_CAPS` and `PLATFORM_RULES` each describe it — so one
    ceiling number has to move all three together.
    """
    routed = await _route_cap(
        monkeypatch, platform="vk", posts_per_day=12, catch_all=True, ceiling=ceiling_value
    )
    scheduled = _publish_cap(db, monkeypatch, platform="vk", posts_per_day=12, ceiling=ceiling_value)
    planned = distribution.effective_daily_cap("vk", [])

    assert routed == scheduled == planned == ceiling_value
    assert distribution.channel_daily_ceiling() == ceiling_value


@pytest.mark.asyncio
async def test_without_a_ceiling_each_path_keeps_its_own_number(db, monkeypatch):
    """The control for the test above: their agreement is caused by the dial.

    Left alone, the persona quota (12), the primary platform cap (3) and the
    crosspost rule (3) are three different numbers, and that is the pre-existing
    behaviour this knob does not claim to change.
    """
    routed = await _route_cap(monkeypatch, platform="vk", posts_per_day=12, catch_all=True)
    scheduled = _publish_cap(db, monkeypatch, platform="vk", posts_per_day=12)
    planned = distribution.effective_daily_cap("vk", [])

    assert (routed, scheduled, planned) == (12, 3, 3)
    assert clamp_channel_daily_cap(12) == 12


@pytest.mark.asyncio
async def test_every_primary_path_stays_at_or_below_the_ceiling(db, monkeypatch):
    """No persona quota, platform cap or crosspost rule may exceed the dial."""
    for ceiling_value in (1, 2, 3, 5):
        _install_ceiling(monkeypatch, ceiling_value)
        for platform in PLATFORMS:
            for posts_per_day in QUOTAS:
                assert _publish_cap(
                    db, monkeypatch, platform=platform, posts_per_day=posts_per_day, ceiling=ceiling_value
                ) <= ceiling_value
        for quota in QUOTAS:
            routed = await _route_cap(
                monkeypatch,
                platform="telegram",
                posts_per_day=quota,
                catch_all=False,
                ceiling=ceiling_value,
            )
            assert routed <= ceiling_value
        assert distribution.effective_daily_cap("vk", []) <= ceiling_value

"""S3 must route by the honest capability level of every destination.

The panel explains a plan and why a destination is missing from it. A platform
that can only be read, or has no working scenario at all, must never be planned:
S4/S7 would otherwise promise a delivery that cannot happen. `draft_only` and
`manual_upload` may be planned, but the plan has to say that the last step is a
human's.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

import pytest

import tgfarm.s3_router as router
from tgfarm.config import Persona
from tgfarm.platform_capability import CAPABILITY_NOTES, capability_of as honest_capability_of
from tgfarm.s3_router import ChannelPlan


def _seed_cluster(public_id: str, label: str = "Нейросеть научилась считать") -> None:
    """A routable cluster needs at least one member row."""
    from tgfarm.db.models import Cluster
    from tgfarm.db.models import RawItem as RawItemRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        cluster = Cluster(public_id=public_id, label=label, niche="tech", heat=0.8, status="ready")
        session.add(cluster)
        session.flush()
        session.add(
            RawItemRow(
                source_id="techcrunch_ai",
                external_id=public_id,
                url=f"https://example.invalid/{public_id}",
                title=label,
                text="Модель решает задачи быстрее предыдущей версии.",
                niche_hint=["tech"],
                source_weight=0.7,
                cluster_id=cluster.id,
            )
        )


@dataclass
class _StubConfig:
    """Only the parts of `Config` that `plan_cluster` reads."""

    personas: list[Persona]

    def by_platform(self, platform: str) -> list[Persona]:
        return [p for p in self.personas if p.platform == platform and p.enabled]

    @property
    def catch_all_personas(self) -> list[Persona]:
        return [p for p in self.personas if p.enabled and p.catch_all]


def _persona(
    channel_id: str, platform: str, *, niche: str = "general", catch_all: bool = True
) -> Persona:
    return Persona(
        id=channel_id,
        title=channel_id,
        niche=niche,
        platform=platform,
        catch_all=catch_all,
        angle_weights={"news": 1.0},
    )


@pytest.mark.asyncio
async def test_unsupported_and_read_only_destinations_are_not_planned(monkeypatch):
    _seed_cluster("cl-cap-skip")
    personas = [
        _persona("zen_ok", "dzen"),
        _persona("ghost_chan", "no_such_platform"),
        _persona("read_only_chan", "read_only_probe"),
    ]
    monkeypatch.setattr(router, "get_config", lambda: _StubConfig(personas))

    # No catalogue destination is read-only today, so the level is injected for
    # that one probe; every other lookup goes through the real function.
    def _levels(platform_id: str, **kwargs):
        if platform_id == "read_only_probe":
            return "read_only"
        return honest_capability_of(platform_id, **kwargs)

    monkeypatch.setattr(router, "capability_of", _levels)

    plans = await router.plan_cluster("cl-cap-skip", rng=random.Random(11))

    assert [plan.channel_id for plan in plans] == ["zen_ok"]
    assert plans[0].capability == "publish"
    assert plans[0].requires_manual_action is False
    # The reason travels with the plan, so a panel rendering only plans still
    # shows why the two channels are absent.
    assert "ghost_chan" in plans[0].notes
    assert "read_only_chan" in plans[0].notes


@pytest.mark.asyncio
async def test_draft_only_and_manual_upload_are_planned_with_a_manual_step(monkeypatch):
    _seed_cluster("cl-cap-manual")
    personas = [
        _persona("zen_ok", "dzen"),
        _persona("vc_draft", "vc_ru"),
        _persona("clips_manual", "vk_clips"),
    ]
    monkeypatch.setattr(router, "get_config", lambda: _StubConfig(personas))

    plans = await router.plan_cluster("cl-cap-manual", rng=random.Random(5))
    by_id = {plan.channel_id: plan for plan in plans}

    assert set(by_id) == {"zen_ok", "vc_draft", "clips_manual"}

    assert by_id["vc_draft"].capability == "draft_only"
    assert by_id["vc_draft"].requires_manual_action is True
    assert by_id["vc_draft"].capability_label == "Заполняет черновик, публикует человек"
    assert by_id["vc_draft"].manual_step == CAPABILITY_NOTES["draft_only"]

    assert by_id["clips_manual"].capability == "manual_upload"
    assert by_id["clips_manual"].requires_manual_action is True
    assert by_id["clips_manual"].manual_step == CAPABILITY_NOTES["manual_upload"]

    # A publishing destination keeps the plain path: no manual flag, no noise.
    assert by_id["zen_ok"].capability == "publish"
    assert by_id["zen_ok"].requires_manual_action is False
    assert by_id["zen_ok"].manual_step == ""
    assert by_id["zen_ok"].notes == ""

    assert router.route_skips("cl-cap-manual") == []


@pytest.mark.asyncio
async def test_publish_destination_takes_the_regular_path(monkeypatch):
    _seed_cluster("cl-cap-publish", "Нейросеть научилась считать быстрее")
    personas = [
        _persona("tg_tech", "telegram", niche="tech", catch_all=False),
        _persona("zen_ok", "dzen"),
    ]
    monkeypatch.setattr(router, "get_config", lambda: _StubConfig(personas))

    plans = await router.plan_cluster("cl-cap-publish", rng=random.Random(3))
    by_id = {plan.channel_id: plan for plan in plans}

    assert set(by_id) == {"tg_tech", "zen_ok"}
    for plan in plans:
        assert plan.capability == "publish"
        assert plan.capability_label == "Публикует автоматически"
        assert plan.requires_manual_action is False
        assert plan.manual_step == ""
        assert plan.notes == ""

    # Existing consumers rely on these values; the capability work must not
    # reshape the niche route or the catch-all route.
    assert by_id["tg_tech"].reason.startswith("niche=tech")
    assert by_id["zen_ok"].reason == "catch_all"
    assert router.route_skips("cl-cap-publish") == []


@pytest.mark.asyncio
async def test_exclusion_reason_is_recorded_even_when_no_plan_remains(monkeypatch):
    _seed_cluster("cl-cap-empty")
    monkeypatch.setattr(
        router,
        "get_config",
        lambda: _StubConfig([_persona("ghost_chan", "no_such_platform")]),
    )

    plans = await router.plan_cluster("cl-cap-empty", rng=random.Random(1))

    assert plans == [], "an unsupported destination must never produce a plan"

    skips = router.route_skips("cl-cap-empty")
    assert len(skips) == 1
    skip = skips[0]
    assert skip.cluster_public_id == "cl-cap-empty"
    assert skip.channel_id == "ghost_chan"
    assert skip.platform == "no_such_platform"
    assert skip.capability == "unsupported"
    assert skip.capability_label == "Не поддерживается"
    assert "Не поддерживается" in skip.reason
    assert "Рабочего сценария нет" in skip.reason

    assert router.route_skips("cl-cap-never-routed") == []


def test_channel_plan_stays_compatible_for_existing_construction():
    """New fields are defaulted: old call sites and `replace()` keep working."""
    persona = _persona("legacy", "telegram", niche="tech", catch_all=False)
    plan = ChannelPlan(
        cluster_public_id="cl-legacy",
        channel_id="legacy",
        persona=persona,
        angle="news",
        score=0.5,
    )
    assert plan.reason == ""
    assert plan.capability == "publish"
    assert plan.requires_manual_action is False
    assert plan.manual_step == ""
    assert plan.notes == ""
    assert plan.key() == "legacy:cl-legacy"
    assert replace(plan, requires_manual_action=True).requires_manual_action is True

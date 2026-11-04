"""S4 must plan a format from measured performance of *that* platform (W3-F).

`format_performance()` and `plan_with_history()` both existed; nothing connected
them. A format that earned attention on Дзен must not decide what gets written
for VK, a published-but-never-measured post is not evidence, and a two-post
sample must not steer generation. These tests pin all three, plus the flag: with
`FORMAT_PERFORMANCE_PLANNER` off the planner is not consulted at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from tgfarm.config import Persona
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import format_plan_for
from tgfarm.s8_analytics.formats import format_performance, planner_history
from tgfarm.s10_format import LENGTH_BUDGET, MIN_HISTORY_SAMPLE, plan_for_cluster

RU = Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700)
TOPIC = "ЦБ сохранил ставку на уровне 19%"

#: Channels and posts this module writes. The session DB is shared with the rest
#: of the suite, and `format_performance` reads a time window, so leftovers here
#: would silently become another test's evidence.
_seeded_channels: list[str] = []


def _unique(platform: str) -> str:
    """A platform name nobody else's fixture writes to.

    The suite shares one database for the session, so a plain `vk` row here would
    be counted by another module's totals and vice versa.
    """
    return f"{platform}_{uuid4().hex[:8]}".lower()[:32]


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    yield
    from sqlalchemy import delete

    with session_scope() as session:
        post_ids = list(
            session.scalars(
                select(Post.id).where(Post.channel_id.in_(_seeded_channels))
            ).all()
        )
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
            session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()


def _cluster(n_items: int = 3, text_len: int = 900) -> Cluster:
    items = [
        ItemScored(
            source_id=f"s{i}",
            external_id=f"x{i}-{uuid4().hex[:6]}",
            url=f"https://example.invalid/s{i}/{uuid4().hex[:6]}",
            title=f"Заголовок новости номер {i}",
            text="текст " * (text_len // 6),
            media_kind=None,
        )
        for i in range(n_items)
    ]
    return Cluster(id=f"cl-{uuid4().hex[:6]}", label=TOPIC, items=items, heat=0.1)


def _seed(
    platform: str,
    *,
    fmt: str,
    measured: int,
    unmeasured: int,
    engagement: int = 40,
) -> None:
    """Publish `measured` posts with a stats row and `unmeasured` without one."""
    suffix = uuid4().hex[:8]
    channel_id = f"pl_{platform}_{suffix}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="План", niche="business", platform=platform))
        for index in range(measured + unmeasured):
            post = Post(
                public_id=f"plpost{suffix}{index}",
                channel_id=channel_id,
                status="published",
                post_format=fmt,
                published_at=datetime.now(UTC) - timedelta(days=1),
                hook="Заголовок",
                body="Текст",
            )
            session.add(post)
            session.flush()
            if index < measured:
                session.add(
                    PostStat(
                        post_id=post.id,
                        reactions=engagement,
                        comments=0,
                        forwards=0,
                        collected_at=datetime.now(UTC),
                    )
                )


def _plan(platform: str, cluster: Cluster) -> SimpleNamespace:
    return SimpleNamespace(
        persona=RU,
        channel_id=f"ch_{platform}",
        cluster_public_id=cluster.id,
        platform=platform,
    )


def _choose(platform: str, cluster: Cluster, history, *, flag: bool):
    return format_plan_for(
        _plan(platform, cluster),
        cluster_view=cluster,
        topic=TOPIC,
        media_kinds=set(),
        has_video=False,
        history=history,
        use_performance=flag,
    )


def _alternative_to(base_fmt: str) -> str:
    """A format the persona can hold, so `fit_persona` cannot cap the choice away."""
    for fmt in ("stat", "flash", "news", "analysis"):
        if fmt != base_fmt and LENGTH_BUDGET[fmt][1] <= RU.max_chars:
            return fmt
    raise AssertionError("no alternative format fits this persona")


# ------------------------------------------------------- platform scoping (S8)
def test_platform_scope_excludes_other_platforms():
    vk, dzen = _unique("vk"), _unique("dzen")
    _seed(vk, fmt="stat", measured=4, unmeasured=1, engagement=80)
    _seed(dzen, fmt="analysis", measured=9, unmeasured=2, engagement=999)

    vk_report = format_performance(days=30, platform=vk)
    dzen_report = format_performance(days=30, platform=dzen)

    assert vk_report["platform"] == vk
    assert vk_report["formats"]["stat"] == {
        "format": "stat",
        "published": 5,
        "measured": 4,
        "avg_engagement": 80.0,
        "reliable": True,
        "n": 4,
    }
    # Дзен's evidence stays out of VK's report, and the other way round.
    assert "analysis" not in vk_report["formats"]
    assert "stat" not in dzen_report["formats"]
    assert dzen_report["formats"]["analysis"]["measured"] == 9
    assert dzen_report["formats"]["analysis"]["avg_engagement"] == 999.0


def test_platform_scope_is_case_insensitive_and_unknown_platform_is_empty():
    pikabu = _unique("pikabu")
    _seed(pikabu, fmt="flash", measured=3, unmeasured=0)
    assert format_performance(days=30, platform=pikabu.upper())["measured_total"] == 3
    assert format_performance(days=30, platform="never-connected")["formats"] == {}


def test_planner_history_keeps_published_and_measured_apart():
    ok = _unique("ok")
    _seed(ok, fmt="news", measured=2, unmeasured=11, engagement=10)
    rows = planner_history(ok, days=30)
    assert rows["news"]["published"] == 13
    assert rows["news"]["measured"] == 2
    assert rows["news"]["reliable"] is False


# ------------------------------------------------------------------ the flag
def test_flag_off_ignores_the_planner_and_reproduces_the_rules_only_plan():
    cluster = _cluster()
    evidence = {"article": {"published": 40, "measured": 40, "avg_engagement": 5.0}}
    off = _choose("vk", cluster, evidence, flag=False)
    base = plan_for_cluster(cluster, RU)
    assert (off.fmt, off.min_chars, off.max_chars) == (base.fmt, base.min_chars, base.max_chars)
    assert off.reason == base.reason


def test_flag_on_without_evidence_keeps_the_base_format_and_says_why():
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    chosen = _choose("vk", cluster, None, flag=True)
    assert chosen.fmt == base.fmt
    assert "мало данных" in chosen.reason


def test_unmeasured_publications_are_not_evidence():
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    history = {strong: {"published": 50, "measured": 0, "avg_engagement": 99.0}}
    chosen = _choose("vk", cluster, history, flag=True)
    assert chosen.fmt == base.fmt


def test_sample_below_the_threshold_does_not_decide():
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    weak = MIN_HISTORY_SAMPLE - 1
    history = {strong: {"published": 50, "measured": weak, "avg_engagement": 99.0}}
    assert _choose("vk", cluster, history, flag=True).fmt == base.fmt


def test_sufficient_measured_sample_moves_the_format():
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    history = {
        strong: {
            "published": 9,
            "measured": MIN_HISTORY_SAMPLE,
            "avg_engagement": 4.0,
        }
    }
    chosen = _choose("vk", cluster, history, flag=True)
    assert chosen.fmt == strong
    assert "история" in chosen.reason
    low, high = LENGTH_BUDGET[strong]
    assert low <= chosen.min_chars <= chosen.max_chars <= high


def test_reason_survives_the_adaptation_s4_consumes():
    cluster = _cluster()
    history = {
        "analysis": {"published": 9, "measured": 5, "avg_engagement": 4.0},
    }
    chosen = _choose("vk", cluster, history, flag=True)
    assert chosen.reason
    assert chosen.signals


def test_a_destination_is_measured_only_against_its_own_platform():
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    rich, spare = _unique("dzen"), _unique("vk")
    _seed(rich, fmt=strong, measured=8, unmeasured=0, engagement=12.0)

    # The evidence is real, but it belongs to `rich`; `spare` has none of its own,
    # so its plan must stay on the rules-only format.
    assert _choose(rich, cluster, planner_history(rich, days=30), flag=True).fmt == strong
    assert _choose(spare, cluster, planner_history(spare, days=30), flag=True).fmt == base.fmt


@pytest.mark.parametrize("platform", ["vk", "telegram"])
def test_history_never_breaks_generation_with_a_broken_read(platform):
    cluster = _cluster()
    for broken in ({}, {"news": {}}, {"news": {"measured": "abc", "avg_engagement": None}}):
        chosen = _choose(platform, cluster, broken, flag=True)
        assert chosen.fmt in LENGTH_BUDGET
        assert chosen.min_chars > 0

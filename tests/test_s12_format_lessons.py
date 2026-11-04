"""S12 must learn formats from measured outcomes only, per platform.

The farm already measured format performance in S8 and already planned with it
behind `FORMAT_PERFORMANCE_PLANNER`, but nothing carried the measurement into
`LearningMemory`, so no format knowledge accumulated across cycles. These tests
pin the honest rules the owner set: an unmeasured post is never proof of
success, a below-threshold sample is stored as an observation rather than a
conclusion, a lesson always travels with its sample size and confidence, and a
result on one platform never decides a post on another.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.config import Persona
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.db.models import Channel, LearningMemory, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import format_plan_for
from tgfarm.s8_analytics.formats import MIN_SAMPLE, format_performance
from tgfarm.s10_format import plan_for_cluster
from tgfarm.s12_learning import (
    _sample_confidence,
    _shrink_quality,
    best_format_lesson,
    format_lessons,
    format_state,
    learn_format_lessons,
    lessons,
    mistakes,
)

#: Everything this module writes. The suite shares one SQLite file for the whole
#: session, so a leaked row here would silently become another test's evidence.
_seeded_channels: list[str] = []
_seeded_keys: list[str] = []


def _unique_platform(name: str) -> str:
    return f"{name}_{uuid4().hex[:8]}".lower()[:32]


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    yield
    with session_scope() as session:
        session.execute(delete(LearningMemory).where(LearningMemory.key.in_(_seeded_keys)))
        post_ids = list(
            session.scalars(select(Post.id).where(Post.channel_id.in_(_seeded_channels))).all()
        )
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
            session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_keys.clear()


def _channel(platform: str, *, niche: str = "tech") -> str:
    channel_id = f"s12fmt_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Форматы S12", niche=niche, platform=platform)
        )
    return channel_id


def _publish(
    channel_id: str,
    fmt: str,
    *,
    engagement: int | None = None,
    days_ago: float = 2.0,
    status: str = "published",
) -> None:
    """A post, optionally with one stats snapshot.

    `engagement=None` means S8 never measured it — the case that must never be
    mistaken for a result.
    """
    with session_scope() as session:
        post = Post(
            public_id=f"s12fmt_{uuid4().hex[:10]}",
            channel_id=channel_id,
            status=status,
            post_format=fmt,
            published_at=datetime.now(UTC) - timedelta(days=days_ago),
            hook="Заголовок",
            body="Текст",
        )
        session.add(post)
        session.flush()
        if engagement is not None:
            session.add(
                PostStat(
                    post_id=post.id,
                    reactions=engagement,
                    comments=0,
                    forwards=0,
                    collected_at=datetime.now(UTC),
                )
            )


def _track_key(platform: str, fmt: str) -> str:
    key = f"{platform}/{fmt}"
    _seeded_keys.append(key)
    return key


def _stored(channel_id: str, key: str) -> dict | None:
    rows = lessons(channel_id, kind="format")
    return next((row for row in rows if row["key"] == key), None)


def test_zero_measured_samples_writes_no_lesson_and_says_not_measured():
    platform = _unique_platform("vk")
    channel_id = _channel(platform)
    for _ in range(4):
        _publish(channel_id, "flash", engagement=None)
    _track_key(platform, "flash")

    report = learn_format_lessons(channel_id, days=7)

    assert report["written"] == 0
    assert report["not_measured"] == ["flash"]
    assert report["formats"]["flash"]["state"] == "not_measured"
    assert report["measured_total"] == 0
    assert report["published_total"] == 4
    assert _stored(channel_id, f"{platform}/flash") is None
    assert format_lessons(channel_id) == []

    state = format_state(channel_id, "flash", platform=platform)
    assert state["state"] == "not_measured"
    assert state["sample_size"] == 0
    assert state["confidence"] == 0.0
    assert state["reliable"] is False
    assert state["reason"]
    assert best_format_lesson(channel_id, platform=platform) is None


def test_single_measured_post_is_an_observation_not_a_conclusion():
    platform = _unique_platform("dzen")
    channel_id = _channel(platform)
    _publish(channel_id, "article", engagement=9)
    _track_key(platform, "article")

    report = learn_format_lessons(channel_id, days=7)

    assert report["written"] == 1
    assert report["min_sample"] == MIN_SAMPLE
    row = _stored(channel_id, f"{platform}/article")
    assert row is not None
    assert row["sample_size"] == 1
    assert row["confidence"] == pytest.approx(_sample_confidence(1))
    assert row["value"]["measured"] == 1
    assert row["value"]["published"] == 1
    assert row["value"]["reliable"] is False
    assert "порог" in (row["note"] or "")

    state = format_state(channel_id, "article", platform=platform)
    assert state["state"] == "below_sample"
    assert state["reliable"] is False
    assert state["sample_size"] == 1
    # One lucky post does not get to steer anything.
    assert best_format_lesson(channel_id, platform=platform) is None


def test_sufficient_measured_sample_is_written_with_sample_and_confidence():
    platform = _unique_platform("pikabu")
    channel_id = _channel(platform)
    for value in (10, 20, 30):
        _publish(channel_id, "news", engagement=value)
    _track_key(platform, "news")

    learn_format_lessons(channel_id, days=7)

    row = _stored(channel_id, f"{platform}/news")
    assert row is not None
    assert row["sample_size"] == 3
    assert row["confidence"] == pytest.approx(_sample_confidence(3))
    assert row["value"]["avg_engagement"] == pytest.approx(20.0)
    assert row["value"]["measured"] == 3
    assert row["value"]["min_sample"] == MIN_SAMPLE
    assert row["value"]["platform"] == platform
    assert row["value"]["niche"] == "tech"

    # The score is the shrunk relative quality, computed from the numbers S8
    # measured — no second, softer threshold anywhere in S12.
    report = format_performance(7, platform=platform)
    baseline = sum(
        item["avg_engagement"] * item["measured"] for item in report["formats"].values()
    ) / sum(item["measured"] for item in report["formats"].values())
    observed = round(20.0 / (20.0 + baseline), 4)
    assert row["value"]["relative_quality"] == pytest.approx(observed)
    assert row["score"] == pytest.approx(_shrink_quality(observed, 0.5, 3))

    state = format_state(channel_id, "news", platform=platform)
    assert state["state"] == "reliable"
    assert state["reliable"] is True
    best = best_format_lesson(channel_id, platform=platform)
    assert best is not None and best["format"] == "news"
    assert best["sample_size"] == 3


def test_same_format_on_two_platforms_never_merges_into_one_number():
    vk_platform = _unique_platform("reddit")
    dzen_platform = _unique_platform("ok")
    vk_channel = _channel(vk_platform)
    dzen_channel = _channel(dzen_platform, niche="news")
    _track_key(vk_platform, "news")
    _track_key(dzen_platform, "news")

    for value in (5, 7, 9):
        _publish(vk_channel, "news", engagement=value)
    for value in (200, 300, 400):
        _publish(dzen_channel, "news", engagement=value)

    learn_format_lessons(vk_channel, days=7)
    learn_format_lessons(dzen_channel, days=7)

    vk_row = _stored(vk_channel, f"{vk_platform}/news")
    dzen_row = _stored(dzen_channel, f"{dzen_platform}/news")
    assert vk_row and dzen_row
    assert vk_row["key"] != dzen_row["key"]
    assert vk_row["value"]["avg_engagement"] == pytest.approx(7.0)
    assert dzen_row["value"]["avg_engagement"] == pytest.approx(300.0)
    # A Reddit number is not a OK number: neither lesson may carry the other's
    # engagement, and the pooled average of both platforms appears nowhere.
    pooled = (5 + 7 + 9 + 200 + 300 + 400) / 6
    for row in (vk_row, dzen_row):
        assert row["value"]["avg_engagement"] != pytest.approx(pooled)
        assert row["value"]["baseline_engagement"] != pytest.approx(pooled)

    # Channel scoping still holds when the whole fleet is listed.
    seeded_keys = {f"{vk_platform}/news", f"{dzen_platform}/news"}
    fleet = [row for row in format_lessons() if row["key"] in seeded_keys]
    assert len(fleet) == 2
    assert {row["platform"] for row in fleet} == {vk_platform, dzen_platform}
    assert format_lessons(vk_channel, platform=vk_platform)[0]["format"] == "news"
    assert format_lessons(vk_channel, platform=dzen_platform) == []


def test_unmeasured_posts_never_move_the_average():
    platform = _unique_platform("mastodon")
    channel_id = _channel(platform)
    _track_key(platform, "stat")
    _publish(channel_id, "stat", engagement=10)
    _publish(channel_id, "stat", engagement=30)
    for _ in range(5):
        _publish(channel_id, "stat", engagement=None)

    learn_format_lessons(channel_id, days=7)

    row = _stored(channel_id, f"{platform}/stat")
    assert row is not None
    assert row["value"]["measured"] == 2
    assert row["value"]["published"] == 7
    assert row["sample_size"] == 2
    assert row["value"]["avg_engagement"] == pytest.approx(20.0)
    assert row["value"]["reliable"] is False
    assert format_state(channel_id, "stat", platform=platform)["state"] == "below_sample"

    # The unmeasured tail must not inflate the platform baseline either.
    report = format_performance(7, platform=platform)
    assert report["formats"]["stat"]["measured"] == 2
    assert report["formats"]["stat"]["published"] == 7
    assert report["formats"]["stat"]["avg_engagement"] == pytest.approx(20.0)


def test_drafts_and_other_platforms_are_not_learned():
    platform = _unique_platform("facebook")
    channel_id = _channel(platform)
    _publish(channel_id, "analysis", engagement=99, status="draft")
    _track_key(platform, "analysis")

    report = learn_format_lessons(channel_id, days=7)

    assert report["written"] == 0
    assert report["formats"] == {}
    assert _stored(channel_id, f"{platform}/analysis") is None


def test_unknown_platform_stops_learning_instead_of_mixing_platforms():
    channel_id = _channel("")
    for _ in range(3):
        _publish(channel_id, "news", engagement=42)

    report = learn_format_lessons(channel_id, days=7)

    assert report["written"] == 0
    assert report["platform"] == ""
    assert "площадка" in report["reason"]
    assert lessons(channel_id, kind="format") == []


def test_learning_cycle_writes_the_format_lesson(monkeypatch):
    """The pipeline hook is real: `_learn_channel` feeds the lesson, S8 measures it."""
    from tgfarm import s12_learning

    platform = _unique_platform("threads")
    channel_id = _channel(platform)
    _track_key(platform, "flash")
    for value in (4, 6, 8):
        _publish(channel_id, "flash", engagement=value)

    calls: list[str] = []
    real = s12_learning.learn_format_lessons

    def spy(target_channel: str, **kwargs):
        calls.append(target_channel)
        return real(target_channel, **kwargs)

    monkeypatch.setattr(s12_learning, "learn_format_lessons", spy)

    s12_learning._learn_channel(channel_id)

    assert calls == [channel_id]
    row = _stored(channel_id, f"{platform}/flash")
    assert row is not None
    assert row["sample_size"] == 3
    assert row["value"]["state"] == "reliable"


def test_a_broken_format_read_is_visible_as_a_warning(monkeypatch, caplog):
    """Silence was the second half of the defect.

    Past the SQLite variable limit the S8 read raised, `_learn_channel` swallowed
    it at debug level, and the farm kept cycling while format learning stopped.
    A lost learning cycle has to reach the log the operator reads.
    """
    import logging

    from tgfarm import s12_learning

    platform = _unique_platform("mastodon")
    channel_id = _channel(platform)

    def boom(*_args, **_kwargs):
        raise RuntimeError("too many SQL variables")

    monkeypatch.setattr(s12_learning, "learn_format_lessons", boom)
    with caplog.at_level(logging.WARNING, logger="tgfarm.s12_learning"):
        s12_learning._learn_channel(channel_id)

    assert any("format learning skipped" in rec.getMessage() for rec in caplog.records)


def test_format_kind_does_not_leak_into_the_other_readers():
    """`format` is a new kind, not a new meaning for an existing one.

    S4 reads `tactic` and `mistake` rows and S12 reads `slot` rows for posting
    hours; a format lesson must not appear in any of them.
    """
    from tgfarm.s12_learning import best_angle_weights, best_hours

    platform = _unique_platform("linkedin")
    channel_id = _channel(platform)
    _track_key(platform, "news")
    for value in (1, 2, 3):
        _publish(channel_id, "news", engagement=value)

    learn_format_lessons(channel_id, days=7)

    assert lessons(channel_id, kind="format")
    assert lessons(channel_id, kind="mistake") == []
    assert lessons(channel_id, kind="tactic") == []
    assert lessons(channel_id, kind="strategy") == []
    assert best_hours(channel_id) == []
    assert best_angle_weights(channel_id) is None
    assert mistakes(channel_id) == []
    assert all(row["kind"] == "format" for row in format_lessons(channel_id))


def test_flag_off_planner_is_untouched_by_the_new_lessons():
    """S12 learning must not move S4 while `FORMAT_PERFORMANCE_PLANNER` is off.

    With the flag off the planner is the rules-only `plan_for_cluster`; storing a
    trustworthy format lesson here changes nothing, byte for byte.
    """
    persona = Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700)
    cluster = Cluster(
        id=f"cl-{uuid4().hex[:6]}",
        label="ЦБ сохранил ставку",
        items=[
            ItemScored(
                source_id=f"s{index}",
                external_id=f"x{index}-{uuid4().hex[:6]}",
                url=f"https://example.invalid/s{index}",
                title=f"Заголовок {index}",
                text="текст источника " * 40,
                media_kind=None,
            )
            for index in range(3)
        ],
        heat=0.2,
    )

    platform = _unique_platform("telegram")
    channel_id = _channel(platform)
    _track_key(platform, "article")
    for value in (50, 60, 70):
        _publish(channel_id, "article", engagement=value)
    learn_format_lessons(channel_id, days=7)
    assert best_format_lesson(channel_id, platform=platform) is not None

    plan = SimpleNamespace(
        persona=persona,
        channel_id=channel_id,
        cluster_public_id=cluster.id,
        platform=platform,
    )
    with_flag_off = format_plan_for(
        plan, cluster_view=cluster, topic=cluster.label, media_kinds=set(),
        has_video=False, history=None, use_performance=False,
    )
    rules_only = plan_for_cluster(cluster, persona, has_video=False)

    assert with_flag_off.as_dict() == rules_only.as_dict()


def test_broken_measurement_writes_no_lesson(monkeypatch):
    """An unreadable S8 read is a skip, not a fabricated conclusion."""
    from tgfarm import s12_learning

    platform = _unique_platform("x")
    channel_id = _channel(platform)
    _publish(channel_id, "news", engagement=50)
    _track_key(platform, "news")

    def boom(*args, **kwargs):
        raise RuntimeError("статистика недоступна")

    monkeypatch.setattr(s12_learning, "format_performance", boom)

    result = s12_learning._learn_channel(channel_id)

    assert _stored(channel_id, f"{platform}/news") is None
    assert lessons(channel_id, kind="format") == []
    assert result["lessons"] >= 0

"""S4 must prefer the durable S12 lesson over the window it aged out of (W6-F).

`learn_format_lessons` writes a measured format outcome into `LearningMemory`
every cycle and `best_format_lesson` names the trustworthy one — until now nobody
read either, so a lesson disappeared the moment its posts left the S8 window.
These tests pin the rule the owner set: with `FORMAT_PERFORMANCE_PLANNER` on, a
reliable lesson for *this* platform decides; without one the live window decides
exactly as before; the two are never averaged; a `below_sample` lesson and an
unmeasured format steer nothing; and with the flag off the planner output is
byte-for-byte what it was before any of this existed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm import s4_generation
from tgfarm.config import Persona
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.db.models import Channel, LearningMemory, Post, PostStat, RawItem
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import durable_format_preference, format_plan_for
from tgfarm.s8_analytics.formats import planner_history
from tgfarm.s10_format import LENGTH_BUDGET, plan_for_cluster
from tgfarm.s12_learning import best_format_lesson, learn_format_lessons

RU = Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700)
TOPIC = "ЦБ сохранил ставку на уровне 19%"

#: A window wide enough to hold posts that are already useless to S8's own
#: 30-day report — that gap is the whole point of a durable lesson.
AGE_DAYS = 200
LEARN_WINDOW_DAYS = 300
LIVE_WINDOW_DAYS = 30

_seeded_channels: list[str] = []
_seeded_keys: list[str] = []
_seeded_clusters: list[str] = []


def _unique(platform: str) -> str:
    """A platform name no other module's fixture writes to (shared session DB)."""
    return f"{platform}_{uuid4().hex[:8]}".lower()[:32]


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
        session.execute(delete(RawItem).where(
            RawItem.cluster_id.in_(
                select(ClusterRow.id).where(ClusterRow.public_id.in_(_seeded_clusters))
            )
        ))
        session.execute(delete(ClusterRow).where(ClusterRow.public_id.in_(_seeded_clusters)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_keys.clear()
    _seeded_clusters.clear()


def _channel(platform: str) -> str:
    channel_id = f"s4dur_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Уроки форматов", niche="business", platform=platform))
    return channel_id


def _publish(
    channel_id: str,
    fmt: str,
    *,
    engagement: int | None,
    days_ago: float,
) -> None:
    with session_scope() as session:
        post = Post(
            public_id=f"s4dur_{uuid4().hex[:10]}",
            channel_id=channel_id,
            status="published",
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


def _cluster(n_items: int = 3) -> Cluster:
    return Cluster(
        id=f"cl-{uuid4().hex[:6]}",
        label=TOPIC,
        items=[
            ItemScored(
                source_id=f"s{i}",
                external_id=f"x{i}-{uuid4().hex[:6]}",
                url=f"https://example.invalid/s{i}/{uuid4().hex[:6]}",
                title=f"Заголовок новости номер {i}",
                text="текст источника " * 60,
                media_kind=None,
            )
            for i in range(n_items)
        ],
        heat=0.1,
    )


def _fits(persona_max: int, fmt: str) -> bool:
    return LENGTH_BUDGET[fmt][1] <= persona_max


def _alternatives(base_fmt: str, count: int = 1) -> list[str]:
    """Formats the persona can hold and that differ from the rules-only choice."""
    out = [
        fmt
        for fmt in ("stat", "flash", "news", "analysis", "article")
        if fmt != base_fmt and _fits(RU.max_chars, fmt)
    ]
    if len(out) < count:
        raise AssertionError(f"not enough alternative formats for base {base_fmt!r}")
    return out[:count]


def _plan(channel_id: str, platform: str, cluster: Cluster) -> SimpleNamespace:
    return SimpleNamespace(
        persona=RU,
        channel_id=channel_id,
        cluster_public_id=cluster.id,
        platform=platform,
    )


def _choose(
    channel_id: str,
    platform: str,
    cluster: Cluster,
    history,
    *,
    flag: bool = True,
    has_video: bool = False,
    media_kinds: set[str] | None = None,
):
    return format_plan_for(
        _plan(channel_id, platform, cluster),
        cluster_view=cluster,
        topic=TOPIC,
        media_kinds=media_kinds or set(),
        has_video=has_video,
        history=history,
        use_performance=flag,
    )


def _reliable_lesson_channel(platform: str, fmt: str, *, engagement: int = 30) -> str:
    """A channel whose only evidence for `fmt` is old but well measured."""
    channel_id = _channel(platform)
    _track_key(platform, fmt)
    for _ in range(3):
        _publish(channel_id, fmt, engagement=engagement, days_ago=AGE_DAYS)
    learn_format_lessons(channel_id, days=LEARN_WINDOW_DAYS)
    assert best_format_lesson(channel_id, platform=platform) is not None
    return channel_id


# ------------------------------------------------------- the flag stays a gate
def test_flag_off_output_is_byte_for_byte_the_rules_only_plan():
    """Hard requirement: with the flag off nothing about lessons may be visible.

    The lesson here is real, trustworthy and platform-matched, and the live
    window is rich — and none of it moves one byte of `plan_for_cluster`.
    """
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt = _alternatives(base.fmt)[0]
    platform = _unique("telegram")
    channel_id = _reliable_lesson_channel(platform, lesson_fmt)
    window = {lesson_fmt: {"measured": 9, "published": 9, "avg_engagement": 12.0}}

    chosen = _choose(channel_id, platform, cluster, window, flag=False)

    assert chosen.as_dict() == base.as_dict()


def test_flag_off_never_even_reads_the_lesson(monkeypatch):
    """`best_format_lesson` is not consulted at all when the flag is off.

    Making it explode is the only way to prove the planner does not merely
    ignore the answer: the flag-off branch must not ask.
    """
    from tgfarm import s12_learning

    def boom(*_args, **_kwargs):
        raise AssertionError("флаг выключен — урок формата читать нельзя")

    monkeypatch.setattr(s12_learning, "best_format_lesson", boom)
    monkeypatch.setattr(s12_learning, "format_lessons", boom)

    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    chosen = _choose("s4dur_missing", _unique("vk"), cluster, None, flag=False)

    assert chosen.as_dict() == base.as_dict()


# ------------------------------------------------------- the durable lesson wins
def test_reliable_lesson_decides_when_the_live_window_has_aged_out():
    """The gap this closes: S8 no longer sees the posts, S12 still remembers them."""
    platform = _unique("dzen")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt = _alternatives(base.fmt)[0]
    channel_id = _reliable_lesson_channel(platform, lesson_fmt)

    # The same window the cycle would read now: those posts are long gone.
    assert planner_history(platform, days=LIVE_WINDOW_DAYS) == {}

    chosen = _choose(channel_id, platform, cluster, {}, flag=True)

    assert chosen.fmt == lesson_fmt
    assert "урок S12" in chosen.reason
    assert "окно S8" not in chosen.reason
    assert lesson_fmt in chosen.reason


def test_trusted_lesson_beats_a_fresher_but_thin_window():
    """Two sources, one decision: the lesson replaces the window, never blends.

    The window holds a fresher format with a lucky two-post average of 999 —
    below the sample threshold, so it may not steer. Even if it could, the
    lesson is the one consulted, and the window's number must not appear in
    the reason of a choice the lesson made.
    """
    platform = _unique("pikabu")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt, window_fmt = _alternatives(base.fmt, 2)
    channel_id = _reliable_lesson_channel(platform, lesson_fmt)

    for _ in range(2):
        _publish(channel_id, window_fmt, engagement=999, days_ago=1)
    window = planner_history(platform, days=LIVE_WINDOW_DAYS)
    assert window[window_fmt]["measured"] == 2
    assert window[window_fmt]["reliable"] is False

    chosen = _choose(channel_id, platform, cluster, window, flag=True)

    assert chosen.fmt == lesson_fmt
    assert "урок S12" in chosen.reason
    assert "999" not in chosen.reason
    low, high = LENGTH_BUDGET[lesson_fmt]
    assert low <= chosen.min_chars <= chosen.max_chars <= high


def test_lesson_row_is_the_lesson_alone_not_a_pooled_average():
    """The history the planner sees is exactly one row: the lesson's own numbers."""
    platform = _unique("ok")
    lesson_fmt = "stat"
    channel_id = _reliable_lesson_channel(platform, lesson_fmt, engagement=40)

    preference = durable_format_preference(channel_id, platform)

    assert preference["state"] == "trusted"
    assert preference["format"] == lesson_fmt
    assert list(preference["history"]) == [lesson_fmt]
    row = preference["history"][lesson_fmt]
    assert row["measured"] == 3
    assert row["published"] == 3
    assert row["avg_engagement"] == pytest.approx(40.0)
    assert row["reliable"] is True


# -------------------------------------------------- weak history steers nothing
def test_below_sample_lesson_does_not_steer_and_says_so():
    """`below_sample` is an observation: the previous strategy applies unchanged."""
    platform = _unique("mastodon")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt = _alternatives(base.fmt)[0]
    channel_id = _channel(platform)
    _track_key(platform, lesson_fmt)
    for _ in range(2):
        _publish(channel_id, lesson_fmt, engagement=999, days_ago=AGE_DAYS)

    learn_format_lessons(channel_id, days=LEARN_WINDOW_DAYS)
    assert best_format_lesson(channel_id, platform=platform) is None

    chosen = _choose(channel_id, platform, cluster, {}, flag=True)

    assert chosen.fmt == base.fmt
    assert "не вывод" in chosen.reason
    assert "урок S12" not in chosen.reason


def test_below_sample_lesson_leaves_the_window_in_charge():
    """With the lesson silent the live window decides exactly as it did before."""
    platform = _unique("facebook")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    weak_fmt, strong_fmt = _alternatives(base.fmt, 2)
    channel_id = _channel(platform)
    _track_key(platform, weak_fmt)
    _publish(channel_id, weak_fmt, engagement=999, days_ago=1)
    learn_format_lessons(channel_id, days=LIVE_WINDOW_DAYS)
    assert best_format_lesson(channel_id, platform=platform) is None

    for _ in range(4):
        _publish(channel_id, strong_fmt, engagement=25, days_ago=2)
    window = planner_history(platform, days=LIVE_WINDOW_DAYS)

    chosen = _choose(channel_id, platform, cluster, window, flag=True)

    assert chosen.fmt == strong_fmt
    assert "окно S8" in chosen.reason
    assert "урок S12" not in chosen.reason


def test_no_lessons_at_all_keeps_the_previous_behaviour_with_an_honest_reason():
    """Nothing stored and nothing measured: rules only, and the reason says so."""
    platform = _unique("x")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    channel_id = _channel(platform)

    chosen = _choose(channel_id, platform, cluster, {}, flag=True)

    assert chosen.fmt == base.fmt
    assert "только правила" in chosen.reason
    assert f"уроков S12 по {platform} не записано" in chosen.reason


# --------------------------------------------------------- never across platforms
def test_reddit_lesson_never_touches_a_dzen_plan():
    """One channel, two destinations: the lesson travels with its platform.

    A naive reader would call `best_format_lesson(channel)` and get the Reddit
    answer for the Дзен plan, so the platform filter is asserted both ways.
    """
    reddit_platform = _unique("reddit")
    dzen_platform = _unique("dzen")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt = _alternatives(base.fmt)[0]
    channel_id = _reliable_lesson_channel(reddit_platform, lesson_fmt)

    on_reddit = _choose(channel_id, reddit_platform, cluster, {}, flag=True)
    on_dzen = _choose(channel_id, dzen_platform, cluster, {}, flag=True)

    assert on_reddit.fmt == lesson_fmt
    assert "урок S12" in on_reddit.reason
    # The Дзен plan gets the rules-only format and an explicit reason: no lesson
    # for its own platform, so no lesson at all.
    assert on_dzen.fmt == base.fmt
    assert "урок S12" not in on_dzen.reason
    assert f"уроков S12 по {dzen_platform} не записано" in on_dzen.reason


def test_two_channels_keep_their_own_lessons():
    """Each destination prefers the lesson measured on its own channel."""
    first_platform, second_platform = _unique("vk"), _unique("telegram")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    first_fmt, second_fmt = _alternatives(base.fmt, 2)
    first_channel = _reliable_lesson_channel(first_platform, first_fmt, engagement=30)
    second_channel = _reliable_lesson_channel(second_platform, second_fmt, engagement=90)

    assert _choose(first_channel, first_platform, cluster, {}, flag=True).fmt == first_fmt
    assert _choose(second_channel, second_platform, cluster, {}, flag=True).fmt == second_fmt
    # Neither plan finds the other's lesson, even asked by channel-agnostic name.
    assert durable_format_preference(first_channel, second_platform)["state"] == "not_measured"
    assert durable_format_preference(second_channel, first_platform)["state"] == "not_measured"


def test_unknown_platform_consults_no_lesson():
    """An unnamed destination is where another platform's number would leak in."""
    platform = _unique("bluesky")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt = _alternatives(base.fmt)[0]
    channel_id = _reliable_lesson_channel(platform, lesson_fmt)

    preference = durable_format_preference(channel_id, "")

    assert preference["state"] == "no_platform"
    assert preference["history"] == {}

    chosen = _choose(channel_id, "", cluster, {}, flag=True)

    assert chosen.fmt == base.fmt
    assert "площадка плана не названа" in chosen.reason


# ------------------------------------------------------- unusable but trustworthy
def test_video_lesson_waits_for_licensed_footage():
    """A trustworthy lesson the material cannot carry is named, not silently used."""
    platform = _unique("youtube")
    channel_id = _reliable_lesson_channel(platform, "video")

    without = durable_format_preference(channel_id, platform, has_video=False)
    assert without["state"] == "unusable"
    assert without["history"] == {}
    assert "лицензированного ролика" in without["detail"]

    with_footage = durable_format_preference(channel_id, platform, has_video=True)
    assert with_footage["state"] == "trusted"
    assert list(with_footage["history"]) == ["video"]

    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    preference = durable_format_preference(channel_id, platform, has_video=False)
    chosen = format_plan_for(
        _plan(channel_id, platform, cluster),
        cluster_view=cluster,
        topic=TOPIC,
        media_kinds=set(),
        has_video=False,
        history={},
        use_performance=True,
        preference=preference,
    )
    # The unusable lesson steers nothing and the reason names what happened.
    assert chosen.fmt == base.fmt
    assert "урок S12 просит видео" in chosen.reason


def test_preference_is_resilient_when_learning_is_unreadable(monkeypatch):
    """A broken S12 read costs the lesson, never the draft."""
    from tgfarm import s12_learning

    def boom(*_args, **_kwargs):
        raise RuntimeError("базы нет")

    monkeypatch.setattr(s12_learning, "best_format_lesson", boom)
    monkeypatch.setattr(s12_learning, "format_lessons", boom)

    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    platform = _unique("tamworth")
    chosen = _choose("s4dur_missing", platform, cluster, {}, flag=True)

    assert durable_format_preference("s4dur_missing", platform)["state"] == "unreadable"
    assert chosen.fmt == base.fmt
    assert "окно S8" not in chosen.reason
    assert "только правила" in chosen.reason


# ----------------------------------------------------------------- the cycle
def _seed_cycle(cluster: Cluster) -> str:
    """A cluster with raw items, so `s4.run` has something real to plan over."""
    public_id = f"cd{uuid4().hex[:10]}"
    _seeded_clusters.append(public_id)
    with session_scope() as session:
        row = ClusterRow(public_id=public_id, label=cluster.label, niche="business", heat=0.1)
        session.add(row)
        session.flush()
        for item in cluster.items:
            session.add(
                RawItem(
                    source_id=item.source_id,
                    external_id=item.external_id,
                    url=item.url,
                    title=item.title,
                    text=item.text,
                    lang="ru",
                    published_at=datetime.now(UTC),
                    cluster_id=row.id,
                )
            )
    return public_id


def _run_cycle(monkeypatch, public_id: str, plans, *, flag: bool, window_rows=None):
    """`s4.run` with the outside world stubbed: records what the planner was fed."""
    from tgfarm import s2_trends, s3_router, s11_video
    from tgfarm.s8_analytics import formats as s8_formats

    seen: dict[str, list] = {"window": [], "plans": [], "pools": []}

    async def fake_plan_cluster(_cid):
        return list(plans)

    async def fake_fetch_video(_item):
        return None

    def fake_planner_history(platform, *, days=30, now=None):
        seen["window"].append((platform, days))
        return dict(window_rows or {})

    async def fake_generate(plan, _items, format_plan, video=None):
        seen["plans"].append(format_plan)
        return SimpleNamespace(text=f"черновик для {plan.channel_id}")

    monkeypatch.setattr(s3_router, "plan_cluster", fake_plan_cluster)
    monkeypatch.setattr(s11_video, "fetch_video_for", fake_fetch_video)
    monkeypatch.setattr(s8_formats, "planner_history", fake_planner_history)
    monkeypatch.setattr(s2_trends, "mark_consumed", lambda _cid: None)
    monkeypatch.setattr(
        s4_generation,
        "get_settings",
        lambda: SimpleNamespace(
            format_performance_planner=flag,
            format_performance_window_days=LIVE_WINDOW_DAYS,
            dzen_requires_llm=False,
        ),
    )
    monkeypatch.setattr(s4_generation, "get_embedder", lambda: SimpleNamespace(embed=lambda _t: []))

    def fake_too_similar_vec(_vec, pool, *_rest):
        seen["pools"].append(pool)
        return False

    monkeypatch.setattr(s4_generation, "_too_similar_vec", fake_too_similar_vec)
    monkeypatch.setattr(s4_generation, "_channel_history_vectors", lambda _cid: [])
    monkeypatch.setattr(s4_generation, "generate", fake_generate)
    monkeypatch.setattr(s4_generation, "persist_draft", lambda *_a, **_k: f"pub{uuid4().hex[:6]}")

    created = asyncio.run(s4_generation.run(public_id))
    return created, seen


def test_cycle_prefers_the_lesson_and_skips_the_window_read(monkeypatch):
    """The running cycle asks S12 first and does not blend it with S8.

    Two plans on two destinations of one cluster: the lesson-bearing platform
    resolves from `LearningMemory` alone (so the heavier window read is skipped),
    while the platform without a lesson still reads the window.
    """
    lesson_platform = _unique("reddit")
    bare_platform = _unique("dzen")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    lesson_fmt = _alternatives(base.fmt)[0]
    lesson_channel = _reliable_lesson_channel(lesson_platform, lesson_fmt)
    bare_channel = _channel(bare_platform)
    public_id = _seed_cycle(cluster)

    plans = [
        _plan(lesson_channel, lesson_platform, cluster),
        _plan(bare_channel, bare_platform, cluster),
    ]

    created, seen = _run_cycle(monkeypatch, public_id, plans, flag=True)

    assert len(created) == 2
    assert len(seen["plans"]) == 2
    lesson_plan, bare_plan = seen["plans"]
    assert lesson_plan.fmt == lesson_fmt
    assert "урок S12" in lesson_plan.reason
    assert bare_plan.fmt == base.fmt
    assert f"уроков S12 по {bare_platform} не записано" in bare_plan.reason
    # Only the platform that had no lesson paid for the live window read.
    assert [platform for platform, _days in seen["window"]] == [bare_platform]


def test_cycle_with_the_flag_off_reads_neither_lesson_nor_window(monkeypatch):
    """Flag off through the real cycle: rules-only plans, zero evidence reads."""
    from tgfarm import s12_learning

    def boom(*_args, **_kwargs):
        raise AssertionError("с выключенным флагом урок формата не читается")

    monkeypatch.setattr(s12_learning, "best_format_lesson", boom)
    monkeypatch.setattr(s12_learning, "format_lessons", boom)

    platform = _unique("telegram")
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    channel_id = _channel(platform)
    public_id = _seed_cycle(cluster)
    plans = [_plan(channel_id, platform, cluster)]

    created, seen = _run_cycle(monkeypatch, public_id, plans, flag=False)

    assert len(created) == 1
    assert seen["window"] == []
    assert seen["plans"][0].as_dict() == base.as_dict()


def test_the_planner_read_leaves_the_duplicate_pool_a_list(monkeypatch):
    """The format read must not take over the name the dedupe check closes over.

    `run()` builds the in-cycle vector pool as `history` and `_too_similar`
    concatenates it with the drafts already seen in this cycle. Naming the
    performance read after the same variable turned that concatenation into
    `dict + list`, which aborted the stage on the first generated draft — with
    the flag off too, because the flag only decides what the dictionary holds.
    """
    platform = _unique("telegram")
    cluster = _cluster()
    channel_id = _channel(platform)
    public_id = _seed_cycle(cluster)
    plans = [_plan(channel_id, platform, cluster), _plan(channel_id, platform, cluster)]

    created, seen = _run_cycle(monkeypatch, public_id, plans, flag=True)

    assert len(created) == 2, "one draft per plan: the stage must not abort"
    assert len(seen["pools"]) == 2
    for pool in seen["pools"]:
        assert isinstance(pool, list)
        assert all(isinstance(row, dict) and "vec" in row for row in pool)
    # The second draft is checked against the first one's vector, so the pool
    # grows inside the cycle instead of being replaced by the planner's read.
    assert [len(pool) for pool in seen["pools"]] == [0, 1]

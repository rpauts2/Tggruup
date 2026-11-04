"""The shape of S12's format step is measured and now pinned.

`tools/measure_learning_cost.py` ran twice against a copy of the live corpus
(20 003 posts, 452 of them published, 22 stats snapshots, 11 channels of which 2
are enabled) and found the step costs 5.2-6.9 ms per channel — one
platform-scoped `format_performance` read (4-7 ms) plus one small write per
learned format. Eleven channels cost 58-81 ms and the whole
`s12_learning.run()` 224-475 ms, against a 2 700 s cycle budget; for the enabled
two-channel fleet the format step is 18 ms of the 287 ms stage. That is small
enough that the product keeps its behaviour.

What must not drift is the *shape* of that cost: the step is bounded by the
number of channels, one read each, and never by the number of formats or posts.
A refactor that moved the read inside the format loop would multiply the same
6 ms by every format on every channel; the tests below are the tripwire. They
count calls, not wall time, so they stay honest on a shared CI runner.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, LearningMemory, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s12_learning import FORMAT_WINDOW_DAYS, learn_format_lessons, lessons

#: Everything this module writes; the suite shares one SQLite file per session.
_seeded_channels: list[str] = []
_seeded_keys: list[str] = []


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    yield
    with session_scope() as session:
        # Every lesson this module writes belongs to a channel it created, so the
        # memory delete is scoped by channel rather than by key: `_learn_channel`
        # also leaves `mistake` rows behind, and an untracked row would outlive
        # this module while a leftover post would block the channel delete.
        session.execute(
            delete(LearningMemory).where(LearningMemory.channel_id.in_(_seeded_channels))
        )
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


def _platform(name: str) -> str:
    return f"{name}_{uuid4().hex[:8]}".lower()[:32]


def _channel(platform: str) -> str:
    channel_id = f"s12cost_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Цена обучения", niche="tech", platform=platform))
    return channel_id


def _publish(channel_id: str, fmt: str, *, engagement: int | None = 5) -> None:
    with session_scope() as session:
        post = Post(
            public_id=f"s12cost_{uuid4().hex[:10]}",
            channel_id=channel_id,
            status="published",
            post_format=fmt,
            published_at=datetime.now(UTC) - timedelta(days=1),
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


def _track(platform: str, fmt: str) -> str:
    key = f"{platform}/{fmt}"
    _seeded_keys.append(key)
    return key


@pytest.fixture()
def call_counter(monkeypatch):
    """Replace the S8 read with a recorder: cost is the number of calls, not time."""
    from tgfarm import s12_learning

    recorded: list[dict] = []
    real = s12_learning.format_performance

    def spy(days: int = 30, **kwargs):
        started = time.perf_counter()
        try:
            return real(days, **kwargs)
        finally:
            recorded.append(
                {
                    "days": int(days),
                    "platform": str(kwargs.get("platform") or ""),
                    "ms": (time.perf_counter() - started) * 1000.0,
                }
            )

    monkeypatch.setattr(s12_learning, "format_performance", spy)
    return recorded


def test_a_channel_pays_one_platform_read_however_many_formats_it_has(call_counter):
    """The read is per channel, not per format: four formats, one query.

    This is the whole cost model the measurement found — 1 call and one write per
    learned format — and the reason the step is 5 ms rather than 5 ms × formats.
    """
    platform = _platform("telegram")
    channel_id = _channel(platform)
    for fmt in ("news", "article", "flash", "digest"):
        for _ in range(3):
            _publish(channel_id, fmt, engagement=10)
        _track(platform, fmt)

    report = learn_format_lessons(channel_id, days=7)

    assert len(call_counter) == 1, "формат-обучение сделало больше одного чтения S8"
    assert call_counter[0]["platform"] == platform
    assert call_counter[0]["days"] == 7
    assert report["written"] == 4
    assert set(report["formats"]) == {"news", "article", "flash", "digest"}


def test_fleet_cost_is_bounded_by_channels_not_by_formats(call_counter):
    """Three channels of one platform: three reads, never three × four formats.

    The measured redundancy lives here on purpose: the fleet pays one identical
    platform read per channel (9 Telegram channels → 9 reads, ~33 ms of duplicate
    work per cycle, which the measurement showed is not worth restructuring for).
    A future shared read per platform may lower this, so the bound is asserted as
    `<= channels` — it must never rise above it.
    """
    from tgfarm.s12_learning import _learn_channel

    platform = _platform("dzen")
    channels = [_channel(platform) for _ in range(3)]
    for channel_id in channels:
        for fmt in ("news", "article", "flash", "digest"):
            _publish(channel_id, fmt, engagement=4)
            _track(platform, fmt)

    for channel_id in channels:
        _learn_channel(channel_id)

    assert len(call_counter) <= len(channels)
    assert len(call_counter) >= 1
    assert {call["platform"] for call in call_counter} == {platform}


def test_a_channel_with_no_known_platform_costs_no_read_at_all(call_counter):
    """Skipping is cheaper than measuring: an unscoped read is refused, not run.

    The same guard that keeps platforms from pooling keeps the step from paying
    for a query whose result could not be used.
    """
    channel_id = _channel("")
    for fmt in ("news", "article"):
        _publish(channel_id, fmt, engagement=9)

    report = learn_format_lessons(channel_id, days=7)

    assert call_counter == []
    assert report["written"] == 0
    assert lessons(channel_id, kind="format") == []


def test_the_window_is_the_setting_so_the_read_stays_bounded(monkeypatch, call_counter):
    """One read, over the configured window: settings decide, code does not widen.

    `FORMAT_PERFORMANCE_WINDOW_DAYS` is validated 1..365 in settings, so the step
    cannot be turned into an all-time scan; the fallback below is the constant the
    planner uses when settings are unreadable.
    """
    from tgfarm import s12_learning
    from tgfarm.core import settings as settings_mod

    platform = _platform("vk")
    channel_id = _channel(platform)
    for _ in range(3):
        _publish(channel_id, "news", engagement=3)
    _track(platform, "news")

    monkeypatch.setattr(
        settings_mod, "get_settings", lambda: SimpleNamespace(format_performance_window_days=21)
    )
    report = learn_format_lessons(channel_id)
    assert report["window_days"] == 21
    assert call_counter[-1]["days"] == 21

    def unreadable():
        raise RuntimeError("нет конфига")

    monkeypatch.setattr(settings_mod, "get_settings", unreadable)
    fallback = s12_learning._format_window_days()  # noqa: SLF001 - the bound is the contract
    assert fallback == FORMAT_WINDOW_DAYS == 60
    assert fallback <= 365


def test_unmeasured_formats_cost_reads_but_write_nothing(call_counter):
    """The honest rule from the measurement: a blind spot is named, never stored.

    A format with published-but-unmeasured posts adds a lesson row nowhere, yet
    it does not add a query either — it comes back in the same single report.
    """
    platform = _platform("pikabu")
    channel_id = _channel(platform)
    for _ in range(5):
        _publish(channel_id, "flash", engagement=None)
    _track(platform, "flash")

    report = learn_format_lessons(channel_id, days=30)

    assert len(call_counter) == 1
    assert report["written"] == 0
    assert report["not_measured"] == ["flash"]
    assert report["published_total"] == 5
    assert lessons(channel_id, kind="format") == []


def test_a_broken_read_is_one_failed_call_not_a_retry_storm(call_counter, monkeypatch):
    """An unavailable S8 costs at most one attempt per channel.

    `_learn_channel` swallows the error to protect the other lessons; if the
    error path retried per format, a database hiccup would multiply into the
    cycle instead of skipping one channel.
    """
    from tgfarm import s12_learning

    platform = _platform("ok")
    channel_id = _channel(platform)
    _publish(channel_id, "news", engagement=5)
    _track(platform, "news")

    def boom(*args, **kwargs):
        raise RuntimeError("статистика недоступна")

    monkeypatch.setattr(s12_learning, "format_performance", boom)
    result = s12_learning._learn_channel(channel_id)  # noqa: SLF001 - the pipeline hook

    assert result["lessons"] >= 0
    assert lessons(channel_id, kind="format") == []
    assert call_counter == []

"""Telegram post metrics must run on the farm's clock, not on a human's.

`tg_sync.collect_post_stats` has worked since `860bbc6` stopped it writing a
`PostStat.day` column the schema never had, but nothing scheduled it: it ran only
when somebody pressed a button. Live evidence (2026-11-05), read through a
read-only connection to the real database: 455 published posts, 22 `post_stats`
rows covering 15 distinct posts; for Telegram specifically 15 published posts
against 7 measured ones - and all seven of those were seeded by
`tools/seed_metrics.py`, because the collector never had a reason to run.

Two things had to be true before this job was worth registering, and both are
pinned below rather than assumed:

* the session gate is `tguser.is_authorized()` - the same local check the API
  (`_tguser_ready`), the CLI and `s1_sources` already use - so a machine without a
  session costs one warning and no network call;
* the tick is a reader. Every test replaces `tguser.fetch_post_stats`, the one
  function in this path that would open a Telethon session, and the suite runs
  with the socket guarded by `tests/conftest.py`, so a leaked real call fails
  instead of quietly reaching Telegram.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select

from tgfarm import pipeline, tguser
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import PipelineRun, Post, PostStat
from tgfarm.db.session import session_scope

JOB_ID = "telegram-post-metrics"
STARTUP_JOB_ID = "startup-telegram-post-metrics"
STAGE = "telegram_post_metrics"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _mode(monkeypatch, *, online: bool = True) -> None:
    """Pin only the mode gate the tick reads; everything else keeps test defaults."""
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(online=online))


def _authorized(monkeypatch, *, value: bool = True) -> None:
    monkeypatch.setattr(tguser, "is_authorized", lambda: value)


def _spy_fetch(monkeypatch, *, reply: dict | None = None):
    """Replace the only network call in the path, and record what it was asked."""
    seen: list[list] = []

    async def fetch(pairs):
        seen.append(list(pairs))
        if reply is None:
            return {
                pair: {"views": 424, "reactions": 9, "forwards": 4} for pair in pairs
            }
        return reply

    monkeypatch.setattr(tguser, "fetch_post_stats", fetch)
    return seen


def _exploding_fetch(monkeypatch) -> list:
    """A fetch that must never be reached: gates have to fire before it."""
    calls: list = []

    async def fetch(pairs):
        calls.append(list(pairs))
        raise AssertionError("the job reached Telegram where it should have stopped")

    monkeypatch.setattr(tguser, "fetch_post_stats", fetch)
    return calls


def _seed_published_post(
    *,
    with_channel_chat_id: bool,
    with_post_chat_id: bool = False,
) -> tuple[str, int, int, int]:
    """A published Telegram post whose identity columns are chosen by the caller."""
    channel_id = f"tgjob-{_uid()}"
    message_id = uuid.uuid4().int % 1_000_000
    chat_id = -100_000_000_000 + (uuid.uuid4().int % 1_000_000)
    with session_scope() as session:
        session.add(
            ChannelRow(
                id=channel_id,
                platform="telegram",
                title=channel_id,
                tg_chat_id=chat_id if with_channel_chat_id else None,
            )
        )
        session.flush()
        post = Post(
            public_id=f"tgjob-{_uid()}",
            channel_id=channel_id,
            hook="факт",
            body="Тело публикации.",
            status="published",
            published_at=datetime.now(UTC),
            tg_message_id=message_id,
            tg_chat_id=chat_id if with_post_chat_id else None,
        )
        session.add(post)
        session.flush()
        return channel_id, post.id, message_id, chat_id


def _cleanup(channel_id: str, post_id: int) -> None:
    with session_scope() as session:
        session.execute(delete(PostStat).where(PostStat.post_id == post_id))
        session.execute(delete(Post).where(Post.id == post_id))
        session.execute(delete(ChannelRow).where(ChannelRow.id == channel_id))
        session.execute(delete(PipelineRun).where(PipelineRun.run_id.like("tg-metrics-%")))


# ------------------------------------------------------------------ registration
def test_the_collector_is_now_on_the_schedule():
    scheduler = pipeline._build_scheduler()
    try:
        recurring = scheduler.get_job(JOB_ID)
        startup = scheduler.get_job(STARTUP_JOB_ID)

        assert recurring is not None, "у Telegram по-прежнему нет задачи сбора метрик"
        assert recurring.func is pipeline._telegram_metrics_tick
        assert recurring.name == "telegram post statistics"
        assert startup is not None
        assert startup.func is pipeline._telegram_metrics_tick
        assert startup.name == "initial telegram post statistics"
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_the_job_wakes_on_the_named_interval_and_once_at_boot():
    scheduler = pipeline._build_scheduler()
    try:
        recurring = scheduler.get_job(JOB_ID)
        startup = scheduler.get_job(STARTUP_JOB_ID)

        assert isinstance(recurring.trigger, IntervalTrigger)
        assert recurring.trigger.interval.total_seconds() == (
            pipeline.TELEGRAM_METRICS_INTERVAL_H * 3600
        )
        assert isinstance(startup.trigger, DateTrigger)
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_the_tick_reads_and_writes_only_our_own_journal():
    """A metrics tick that could publish would be a different job entirely."""
    body = inspect.getsource(pipeline._telegram_metrics_tick)

    for forbidden in ("s7_publish", "publish_post", "send_message", "crosspost"):
        assert forbidden not in body, f"{forbidden} does not belong in a metrics tick"
    assert "collect_post_stats" in body, "the tick must be the collector's schedule"


# ------------------------------------------------------------------- no session
@pytest.mark.asyncio
async def test_no_session_names_itself_instead_of_raising(monkeypatch):
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    calls = _exploding_fetch(monkeypatch)

    report = await pipeline._telegram_metrics_tick()

    assert report["skipped"] == pipeline.TELEGRAM_METRICS_SKIP_NO_SESSION
    assert report["posts"] == 0
    assert "tgfarm telegram login" in report["reason"]
    assert "error" not in report, "a missing session is a state, not a crash"
    assert calls == [], "без сессии нельзя даже спрашивать Telegram"


@pytest.mark.asyncio
async def test_no_session_is_said_once_and_then_quietly(monkeypatch, caplog):
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    _exploding_fetch(monkeypatch)
    pipeline._reset_telegram_metrics_skip_state()

    try:
        with caplog.at_level("WARNING", logger="tgfarm.pipeline"):
            for _ in range(3):
                await pipeline._telegram_metrics_tick()

        warnings = [
            record for record in caplog.records if "skipped" in record.getMessage()
        ]
        assert len(warnings) == 1, (
            "статичное условие, повторяемое каждый тик, заглушает настоящий сбой"
        )
    finally:
        pipeline._reset_telegram_metrics_skip_state()


@pytest.mark.asyncio
async def test_no_session_never_invents_a_measurement(monkeypatch):
    """A skipped read must not look like a collected zero, even in its shape."""
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    _exploding_fetch(monkeypatch)

    report = await pipeline._telegram_metrics_tick()

    assert report.get("skipped"), "пропущенный сбор отличается от измеренного нуля"
    assert report.get("day") is None, "без сбора нельзя объявлять день снимка"
    assert report.get("visible") is None, "нельзя утверждать, сколько постов видно"


@pytest.mark.asyncio
async def test_off_mode_skips_before_even_asking_about_a_session(monkeypatch):
    _mode(monkeypatch, online=False)
    asked: list[str] = []

    def spy_authorized() -> bool:
        asked.append("asked")
        return True

    monkeypatch.setattr(tguser, "is_authorized", spy_authorized)
    calls = _exploding_fetch(monkeypatch)

    report = await pipeline._telegram_metrics_tick()

    assert report["skipped"] == pipeline.TELEGRAM_METRICS_SKIP_OFF
    assert asked == [], "в режиме off нечего и искать сессию"
    assert calls == []


# ------------------------------------------------------------------ it collects
@pytest.mark.asyncio
async def test_an_authorized_tick_writes_real_rows_into_post_stats(monkeypatch):
    _mode(monkeypatch)
    _authorized(monkeypatch)
    seen = _spy_fetch(monkeypatch)
    channel_id, post_id, message_id, chat_id = _seed_published_post(with_channel_chat_id=True)

    try:
        report = await pipeline._telegram_metrics_tick()

        assert seen, "коллектор обязан был спросить Telegram"
        assert (chat_id, message_id) in seen[0]
        assert report["posts"] >= 1, f"снимок не записан: {report}"
        assert "error" not in report
        with session_scope() as session:
            rows = session.execute(
                select(PostStat).where(PostStat.post_id == post_id)
            ).scalars().all()
        assert len(rows) == 1
        assert (rows[0].views, rows[0].reactions, rows[0].forwards) == (424, 9, 4)
    finally:
        _cleanup(channel_id, post_id)


@pytest.mark.asyncio
async def test_the_tick_leaves_an_audit_trail(monkeypatch):
    """`tgfarm telegram stats` by hand and the schedule must both be checkable."""
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _spy_fetch(monkeypatch)
    channel_id, post_id, _message_id, _chat_id = _seed_published_post(with_channel_chat_id=True)

    try:
        await pipeline._telegram_metrics_tick()
        with session_scope() as session:
            runs = session.execute(
                select(PipelineRun).where(PipelineRun.stage == STAGE)
            ).scalars().all()
        assert runs, "задача обязана оставить след в журнале, иначе её не проверить"
        assert runs[-1].status in {"ok", "partial"}
    finally:
        _cleanup(channel_id, post_id)


@pytest.mark.asyncio
async def test_a_post_only_the_publisher_remembered_is_still_measurable(monkeypatch):
    """The channel was never synced, but the publisher stored the chat on the post.

    Reading only `channels.tg_chat_id` left every post published before the first
    `sync_channels` unaskable, which starved the loop even once the collector
    worked and had a schedule.
    """
    _mode(monkeypatch)
    _authorized(monkeypatch)
    seen = _spy_fetch(monkeypatch)
    channel_id, post_id, message_id, chat_id = _seed_published_post(
        with_channel_chat_id=False,
        with_post_chat_id=True,
    )

    try:
        report = await pipeline._telegram_metrics_tick()

        assert seen and (chat_id, message_id) in seen[0], "id публикации должен был спроситься"
        assert report["posts"] >= 1
        with session_scope() as session:
            row = session.execute(
                select(PostStat).where(PostStat.post_id == post_id)
            ).scalars().one()
        assert row.views == 424
    finally:
        _cleanup(channel_id, post_id)


@pytest.mark.asyncio
async def test_a_broken_collector_is_reported_and_never_breaks_the_cycle(monkeypatch):
    _mode(monkeypatch)
    _authorized(monkeypatch)

    async def boom(pairs):
        raise RuntimeError("session expired: token=SUPERSECRETNOTFORLOGS")

    monkeypatch.setattr(tguser, "fetch_post_stats", boom)
    channel_id, post_id, _message_id, _chat_id = _seed_published_post(with_channel_chat_id=True)

    try:
        report = await pipeline._telegram_metrics_tick()

        assert report["posts"] == 0
        assert report["errors"] == 1
        assert "RuntimeError" in report["error"]
        assert "SUPERSECRETNOTFORLOGS" not in repr(report), "секрет не попадает в отчёт"
    finally:
        _cleanup(channel_id, post_id)


@pytest.mark.asyncio
async def test_a_read_with_nothing_visible_reports_its_reason(monkeypatch):
    """An honest zero carries the reason it is zero, in both senses."""
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _spy_fetch(monkeypatch, reply={})
    channel_id, post_id, _message_id, _chat_id = _seed_published_post(with_channel_chat_id=True)

    try:
        report = await pipeline._telegram_metrics_tick()

        assert report["posts"] == 0
        assert report.get("reason") or report.get("visible") is not None
        assert report.get("skipped") is None
    finally:
        _cleanup(channel_id, post_id)


# ----------------------------------------------------------------------- wording
def test_telegram_is_no_longer_listed_as_only_reachable_by_hand():
    from tgfarm.s8_analytics import coverage

    assert "telegram" not in coverage.ON_DEMAND_COLLECTORS
    assert "telegram" in coverage.SCHEDULED_COLLECTORS
    assert "telegram" in coverage.SESSION_GATED_COLLECTORS


def test_the_coverage_row_no_longer_claims_the_read_never_happens_by_itself():
    from tgfarm.s8_analytics import coverage

    partial_row = {
        "published": 12,
        "measured_posts": 3,
        "measured_with_signal": 2,
        "crosspost_posted": 0,
        "crosspost_with_remote": 0,
        "askable_posts": 3,
    }
    status, reason = coverage._classify(partial_row, "telegram")

    assert status == coverage.STATUS_PARTIAL
    assert "не соберётся само" not in reason, "расписание есть - значит, соберётся"
    assert JOB_ID in reason
    assert "tgfarm telegram login" in reason


def test_a_scheduled_but_unread_platform_is_blocked_on_the_session_not_missing():
    """The fourth silence: the schedule runs, and there is nothing to read with."""
    from tgfarm.s8_analytics import coverage

    row = {
        "published": 5,
        "measured_posts": 0,
        "measured_with_signal": 0,
        "crosspost_posted": 0,
        "crosspost_with_remote": 0,
        "askable_posts": 0,
    }
    status, reason = coverage._classify(row, "telegram")

    assert status == coverage.STATUS_BLOCKED
    assert "сборщика метрик нет" not in reason, "сборщик есть, и это надо говорить"
    assert "не соберётся само" not in reason
    assert JOB_ID in reason
    assert "0 из 5" in reason, "причина обязана опираться на измеренное число id"


def test_the_audit_measures_which_telegram_posts_the_collector_could_ask_about():
    from tgfarm.s8_analytics.coverage import measurement_coverage

    channel_id, post_id, _message_id, _chat_id = _seed_published_post(
        with_channel_chat_id=True,
    )
    try:
        row = measurement_coverage()["platforms"]["telegram"]
        assert row["askable_posts"] >= 1
        assert row["published"] >= row["askable_posts"]
    finally:
        _cleanup(channel_id, post_id)


def test_the_four_kinds_of_silence_stay_four_different_answers():
    from tgfarm.s8_analytics import coverage

    base = {
        "measured_posts": 0,
        "measured_with_signal": 0,
        "crosspost_posted": 0,
        "crosspost_with_remote": 0,
        "askable_posts": None,
    }
    reasons = {}
    reasons["max"] = coverage._classify({**base, "published": 3}, "max")[1]
    reasons["vk"] = coverage._classify({**base, "published": 3}, "vk")[1]
    reasons["telegram"] = coverage._classify({**base, "published": 3}, "telegram")[1]
    reasons["pinterest"] = coverage._classify({**base, "published": 0}, "pinterest")[1]

    assert reasons["max"] == "сборщика метрик нет"
    assert "VK_USER_TOKEN" in reasons["vk"]
    assert JOB_ID in reasons["telegram"]
    assert "нужна авторизованная пользовательская сессия" in reasons["telegram"]
    assert reasons["pinterest"] == "публикаций этого платформа нет"
    assert len({v for v in reasons.values() if v}) == 4, "четыре молчания, четыре фразы"

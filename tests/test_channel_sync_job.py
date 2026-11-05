"""`channels.tg_chat_id` needs a clock, or the collector that reads it starves.

`telegram-post-metrics` gave `tg_sync.collect_post_stats` a schedule in the
previous commit. It reads `channels.tg_chat_id` to know which chat to ask about,
and `tg_sync.sync_channels` is the *only* writer of that column — and it was still
reachable only from the CLI, the desktop menu and `/api/telegram/sync`, three
human gestures. So the collector woke on time, found chat ids that had gone stale
or never existed, and measured nothing on schedule.

Every test here replaces the one function in this path that would open a Telethon
session (`tguser.list_chats`), and `tests/conftest.py` guards the socket, so a
leaked real call fails instead of quietly reaching Telegram. Nothing here
publishes anything: `sync_channels` reads Telegram and writes our own rows.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select

from tgfarm import pipeline, tg_sync, tguser
from tgfarm.config import Persona
from tgfarm.db.models import Channel, PipelineRun
from tgfarm.db.session import session_scope

JOB_ID = "telegram-channel-sync"
STARTUP_JOB_ID = "startup-telegram-channel-sync"
STAGE = "telegram_channel_sync"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _mode(monkeypatch, *, online: bool = True) -> None:
    """Pin only the mode gate the tick reads; everything else keeps test defaults."""
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(online=online))


def _authorized(monkeypatch, *, value: bool = True) -> None:
    monkeypatch.setattr(tguser, "is_authorized", lambda: value)


def _persona(channel_id: str, *, username: str) -> Persona:
    return Persona(id=channel_id, title=f"Канал {channel_id}", niche="tech", tg_username=username)


def _fake_config(monkeypatch, personas: list[Persona]) -> None:
    """A persona list of our own, so the test never depends on the shipped YAML."""
    config = SimpleNamespace(
        personas=personas,
        by_platform=lambda platform: [
            p for p in personas if p.platform == platform and p.enabled
        ],
    )
    from tgfarm import config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda: config)


def _spy_list_chats(monkeypatch, chats: list) -> list:
    """Replace the only Telethon read in the path, and record that it was called."""
    asked: list = []

    async def list_chats(limit: int = 200) -> list:
        asked.append(limit)
        return chats

    monkeypatch.setattr(tguser, "list_chats", list_chats)
    return asked


def _no_bot_lookup(monkeypatch) -> None:
    """`_bot_admin_map` is a second network read (the Bot API); it is not under test."""

    async def _no_admins() -> dict[int, bool]:
        return {}

    monkeypatch.setattr(tg_sync, "_bot_admin_map", _no_admins)


def _exploding_list_chats(monkeypatch) -> list:
    """A read that must never be reached: the gates have to fire before it."""
    calls: list = []

    async def list_chats(limit: int = 200) -> list:
        calls.append(limit)
        raise AssertionError("the job reached Telegram where it should have stopped")

    monkeypatch.setattr(tguser, "list_chats", list_chats)
    return calls


def _chat(*, chat_id: int, username: str, title: str, participants: int = 4200):
    from tgfarm.tguser import TgChat

    return TgChat(
        id=chat_id,
        title=title,
        username=username,
        kind="channel",
        participants=participants,
    )


def _seed_channel(channel_id: str) -> None:
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, platform="telegram", title=channel_id, niche="tech")
        )


def _channel_row(channel_id: str) -> Channel:
    with session_scope() as session:
        return session.execute(
            select(Channel).where(Channel.id == channel_id)
        ).scalar_one()


def _runs() -> list[PipelineRun]:
    with session_scope() as session:
        return list(
            session.execute(
                select(PipelineRun).where(PipelineRun.stage == STAGE).order_by(PipelineRun.id)
            ).scalars().all()
        )


def _cleanup(channel_id: str) -> None:
    with session_scope() as session:
        session.execute(delete(PipelineRun).where(PipelineRun.stage == STAGE))
        session.execute(delete(Channel).where(Channel.id == channel_id))


# ------------------------------------------------------------------ registration
def test_the_chat_id_writer_is_now_on_the_schedule():
    scheduler = pipeline._build_scheduler()
    try:
        recurring = scheduler.get_job(JOB_ID)
        startup = scheduler.get_job(STARTUP_JOB_ID)

        assert recurring is not None, "у sync_channels по-прежнему нет задачи"
        assert recurring.func is pipeline._channel_sync_tick
        assert recurring.name == "telegram channel chat ids"
        assert startup is not None
        assert startup.func is pipeline._channel_sync_tick
        assert startup.name == "initial telegram channel chat ids"
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_the_job_wakes_on_the_collector_cadence_and_once_at_boot():
    scheduler = pipeline._build_scheduler()
    try:
        recurring = scheduler.get_job(JOB_ID)
        startup = scheduler.get_job(STARTUP_JOB_ID)

        assert isinstance(recurring.trigger, IntervalTrigger)
        assert recurring.trigger.interval.total_seconds() == (
            pipeline.CHANNEL_SYNC_INTERVAL_H * 3600
        )
        assert isinstance(startup.trigger, DateTrigger)
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_the_two_telegram_jobs_cannot_drift_apart():
    """A collector on a faster clock than its own input source measures nothing.

    The cadence is derived, not repeated, so changing one cannot silently leave
    the other behind — which is the exact shape of the starvation this job ends.
    """
    assert pipeline.CHANNEL_SYNC_INTERVAL_H == pipeline.TELEGRAM_METRICS_INTERVAL_H


def test_the_job_cannot_stack_up_or_run_back_to_back():
    """`max_instances=1` on both, `coalesce=True` on the interval job.

    Two overlapping syncs would write each other's chat ids, and a backlog of
    missed ticks would spend them all on one Telegram read burst. The startup job
    fires once, so coalescing has nothing to collapse — it is registered exactly
    as every other `startup-*` job in `_build_scheduler` is, without the flag.
    """
    scheduler = pipeline._build_scheduler()
    try:
        recurring = scheduler.get_job(JOB_ID)
        startup = scheduler.get_job(STARTUP_JOB_ID)

        assert recurring.max_instances == 1
        assert recurring.coalesce is True
        assert startup.max_instances == 1
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_the_new_job_is_registered_the_way_the_collector_is():
    """The two Telegram jobs must be shaped alike, not merely both present.

    The task asked for `sync_channels` to be scheduled "the same way" the metrics
    collector already is; pinning the two against each other is what keeps a later
    edit from quietly giving one of them a different life cycle.
    """
    scheduler = pipeline._build_scheduler()
    try:
        for new_id, existing_id in (
            (JOB_ID, "telegram-post-metrics"),
            (STARTUP_JOB_ID, "startup-telegram-post-metrics"),
        ):
            new, existing = scheduler.get_job(new_id), scheduler.get_job(existing_id)
            assert type(new.trigger) is type(existing.trigger), new_id
            assert new.max_instances == existing.max_instances, new_id
            assert bool(getattr(new, "coalesce", None)) == bool(
                getattr(existing, "coalesce", None)
            ), new_id
            assert new.misfire_grace_time == existing.misfire_grace_time, new_id
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_every_existing_job_id_still_exists():
    """Registering a job must not rename or displace one the suite already pins."""
    scheduler = pipeline._build_scheduler()
    try:
        for job_id in (
            "cycle",
            "publish-tick",
            "crosspost-tick",
            "watchdog",
            "daily-backup",
            "weekly-report",
            "dzen-warm",
            "branding-rotation",
            "startup-backup-check",
            "platform-auth-health",
            "startup-platform-auth-check",
            "telegraph-metrics",
            "startup-telegraph-metrics",
            "telegram-post-metrics",
            "startup-telegram-post-metrics",
            "metrics-and-learning",
            "startup-metrics-and-learning",
        ):
            assert scheduler.get_job(job_id) is not None, job_id
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_the_tick_reads_telegram_and_writes_only_our_own_rows():
    """A sync tick that could publish would be a different job entirely."""
    body = inspect.getsource(pipeline._channel_sync_tick)

    for forbidden in ("s7_publish", "publish_post", "send_message", "crosspost"):
        assert forbidden not in body, f"{forbidden} does not belong in a sync tick"
    assert "sync_channels" in body, "the tick must be the chat-id writer's schedule"


# ------------------------------------------------------------------- the gates
@pytest.mark.asyncio
async def test_off_mode_skips_before_even_asking_about_a_session(monkeypatch):
    _mode(monkeypatch, online=False)
    asked: list = []

    def spy_authorized() -> bool:
        asked.append("asked")
        return True

    monkeypatch.setattr(tguser, "is_authorized", spy_authorized)
    calls = _exploding_list_chats(monkeypatch)

    report = await pipeline._channel_sync_tick()

    assert report["skipped"] == pipeline.CHANNEL_SYNC_SKIP_OFF
    assert asked == [], "в режиме off нечего и искать сессию"
    assert calls == []


@pytest.mark.asyncio
async def test_no_session_names_itself_instead_of_raising(monkeypatch):
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    calls = _exploding_list_chats(monkeypatch)

    report = await pipeline._channel_sync_tick()

    assert report["skipped"] == pipeline.CHANNEL_SYNC_SKIP_NO_SESSION
    assert report["matched"] == 0
    assert "tgfarm telegram login" in report["reason"]
    assert "error" not in report, "a missing session is a state, not a crash"
    assert calls == []


@pytest.mark.asyncio
async def test_no_session_never_invents_a_measurement(monkeypatch):
    """A skipped read must not look like a synced result, even in its shape."""
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    _exploding_list_chats(monkeypatch)

    report = await pipeline._channel_sync_tick()

    assert report.get("skipped"), "пропущенная синхронизация отличается от пустой"
    assert report.get("telegram_chats") is None, "нельзя утверждать, сколько чатов видно"
    assert report.get("configured") is None, "нельзя утверждать, сколько каналов настроено"


@pytest.mark.asyncio
async def test_no_session_is_said_once_and_then_quietly(monkeypatch, caplog):
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    _exploding_list_chats(monkeypatch)
    pipeline._reset_channel_sync_skip_state()

    try:
        with caplog.at_level("WARNING", logger="tgfarm.pipeline"):
            for _ in range(3):
                await pipeline._channel_sync_tick()

        warnings = [r for r in caplog.records if "skipped" in r.getMessage()]
        assert len(warnings) == 1, (
            "статичное условие, повторяемое каждый тик, заглушает настоящий сбой"
        )
    finally:
        pipeline._reset_channel_sync_skip_state()


@pytest.mark.asyncio
async def test_the_two_telegram_jobs_do_not_silence_each_other(monkeypatch, caplog):
    """Skip state is remembered per job, so one job's code cannot hush the other.

    Both gates emit the same `no_session` code because they read the same session
    file. A shared set would let whichever ran first swallow the other's warning,
    and an operator would see one message for two failing jobs.
    """
    _mode(monkeypatch)
    _authorized(monkeypatch, value=False)
    _exploding_list_chats(monkeypatch)

    async def _no_metrics():
        raise AssertionError("the collector must not be reached either")

    monkeypatch.setattr(tg_sync, "collect_post_stats", _no_metrics)
    pipeline._reset_channel_sync_skip_state()
    pipeline._reset_telegram_metrics_skip_state()

    try:
        with caplog.at_level("WARNING", logger="tgfarm.pipeline"):
            await pipeline._channel_sync_tick()
            await pipeline._telegram_metrics_tick()

        warnings = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
        assert len(warnings) == 2, warnings
        assert any("channel sync" in m for m in warnings)
        assert any("metrics" in m for m in warnings)
    finally:
        pipeline._reset_channel_sync_skip_state()
        pipeline._reset_telegram_metrics_skip_state()


# -------------------------------------------------------------------- it syncs
@pytest.mark.asyncio
async def test_an_authorized_tick_refreshes_the_chat_id(monkeypatch):
    channel_id = f"chsync-{_uid()}"
    chat_id = -100_000_000_000 + (uuid.uuid4().int % 1_000_000)
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _fake_config(monkeypatch, [_persona(channel_id, username="farmchannel")])
    chats = [_chat(chat_id=chat_id, username="farmchannel", title="Канал теста")]
    asked = _spy_list_chats(monkeypatch, chats)
    _no_bot_lookup(monkeypatch)
    _seed_channel(channel_id)

    try:
        report = await pipeline._channel_sync_tick()

        assert asked, "коллектор chat id обязан был спросить Telegram"
        assert report["matched"] == 1
        assert report["ok"] is True
        row = _channel_row(channel_id)
        assert row.tg_chat_id == chat_id, "без chat id последующий сбор метрик нечего спрашивать"
        assert row.tg_synced_at is not None
    finally:
        _cleanup(channel_id)


@pytest.mark.asyncio
async def test_the_tick_leaves_an_audit_trail(monkeypatch):
    """`tgfarm telegram sync` by hand and the schedule must both be checkable."""
    channel_id = f"chsync-{_uid()}"
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _fake_config(monkeypatch, [_persona(channel_id, username="farmaudit")])
    _spy_list_chats(
        monkeypatch, [_chat(chat_id=-100123, username="farmaudit", title="Аудит")]
    )
    _no_bot_lookup(monkeypatch)
    _seed_channel(channel_id)

    try:
        await pipeline._channel_sync_tick()
        runs = _runs()
        assert runs, "задача обязана оставить след в журнале, иначе её не проверить"
        row = runs[-1]
        assert row.status in {"ok", "partial"}
        assert row.run_id.startswith("tg-sync-")
        assert (row.items_in, row.items_out) == (1, 1)
        assert row.meta["counters"] == "measured"
    finally:
        _cleanup(channel_id)


@pytest.mark.asyncio
async def test_a_channel_the_account_cannot_see_is_counted_not_hidden(monkeypatch):
    """A persona with no matching chat is the failure an operator has to be told."""
    channel_id = f"chsync-{_uid()}"
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _fake_config(monkeypatch, [_persona(channel_id, username="farmghost")])
    _spy_list_chats(monkeypatch, [])
    _no_bot_lookup(monkeypatch)
    _seed_channel(channel_id)

    try:
        report = await pipeline._channel_sync_tick()

        assert report["matched"] == 0
        assert report["ok"] is False
        await pipeline._channel_sync_tick()
        row = _runs()[-1]
        assert row.status == "partial", "не найденный канал — не «всё хорошо»"
        assert row.meta["missing_in_telegram"] == 1
    finally:
        _cleanup(channel_id)


@pytest.mark.asyncio
async def test_a_broken_sync_is_reported_and_never_breaks_the_scheduler(monkeypatch):
    channel_id = f"chsync-{_uid()}"
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _fake_config(monkeypatch, [_persona(channel_id, username="farmbroken")])
    _no_bot_lookup(monkeypatch)

    async def boom(limit: int = 200) -> list:
        raise RuntimeError("session expired: token=SUPERSECRETNOTFORLOGS")

    monkeypatch.setattr(tguser, "list_chats", boom)

    try:
        report = await pipeline._channel_sync_tick()

        assert report["matched"] == 0
        assert report["errors"] == 1
        assert "RuntimeError" in report["error"]
        assert "SUPERSECRETNOTFORLOGS" not in repr(report), "секрет не попадает в отчёт"
        assert "SUPERSECRETNOTFORLOGS" not in repr(_runs()[-1].meta)
    finally:
        _cleanup(channel_id)


@pytest.mark.asyncio
async def test_foreign_chat_titles_never_enter_our_journal(monkeypatch):
    """Telegram's titles are untrusted data: counted here, never stored verbatim.

    The report `sync_channels` hands back names chats the account can see, and the
    scheduler keeps whatever its job returns in memory. The journal row is the
    durable, panel-visible artifact, so it carries counts only.
    """
    marker = f"чужой-заголовок-{_uid()}"
    channel_id = f"chsync-{_uid()}"
    _mode(monkeypatch)
    _authorized(monkeypatch)
    _fake_config(monkeypatch, [_persona(channel_id, username="farmours")])
    _spy_list_chats(
        monkeypatch,
        [
            _chat(chat_id=-100777, username="farmours", title="Наш канал"),
            _chat(chat_id=-100888, username=None, title=marker, participants=7),
        ],
    )
    _no_bot_lookup(monkeypatch)
    _seed_channel(channel_id)

    try:
        await pipeline._channel_sync_tick()
        row = _runs()[-1]
        assert marker not in str(row.meta), "журнал не храниит текст извне"
        assert isinstance(row.meta["telegram_chats"], int)
    finally:
        _cleanup(channel_id)

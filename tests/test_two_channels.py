"""Two-channel mode: VK as a publishing surface, interval cadence, safe claims."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest


def _make_post(session, public_id: str, channel_id: str, **kwargs):
    from tgfarm.db.models import Post

    defaults = dict(
        hook="Заголовок поста для проверки.",
        body="Тело поста, достаточное для проверки сборки текста.",
        status="scheduled",
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    defaults.update(kwargs)
    post = Post(public_id=public_id, channel_id=channel_id, **defaults)
    session.add(post)
    session.flush()
    return post


def _make_channel(session, channel_id: str, platform: str):
    from tgfarm.db.models import Channel

    channel = Channel(
        id=channel_id,
        title=channel_id,
        niche="general",
        platform=platform,
        posts_per_day=48,
    )
    session.merge(channel)
    return channel


def test_assemble_text_uses_vk_limit_and_keeps_the_hook():
    from tgfarm.db.models import Post as PostRow
    from tgfarm.s7_publish import PLATFORM_TEXT_LIMITS, assemble_text

    post = PostRow(
        public_id="vk-text",
        channel_id="vk_misc_ru",
        hook="Хук поста.",
        body="Тело поста.",
        sources=[{"title": "Источник", "url": "https://example.invalid/a"}],
        hashtags=["ВК"],
    )
    text = assemble_text(post, "vk")
    assert text.startswith("Хук поста.")
    assert "https://example.invalid/a" in text
    assert PLATFORM_TEXT_LIMITS["vk"] == 15_000


def test_interval_slot_spreads_the_daily_quota():
    from tgfarm.s7_publish import _interval_slot

    cadence = {"min_gap_s": 300, "max_gap_s": 620}
    rng = random.Random(7)
    now = datetime.now(UTC)

    # VK: 48 posts/day -> one per ~30 minutes, never less than the platform gap.
    vk = _interval_slot(now, None, cadence, rng, 48)
    assert 1_500 <= (vk - now).total_seconds() <= 2_100

    # Dzen: 120 posts/day -> one per ~12 minutes, above the 10-minute floor.
    dzen = _interval_slot(now, None, cadence, rng, 120)
    assert 640 <= (dzen - now).total_seconds() <= 830


def test_interval_slot_never_schedules_in_the_past():
    from tgfarm.s7_publish import _interval_slot

    now = datetime.now(UTC)
    last = now - timedelta(hours=3)
    slot = _interval_slot(now, last, {"min_gap_s": 300, "max_gap_s": 300}, random.Random(1), 0)
    assert slot > now


def test_due_posts_routes_vk_channel_to_vk():
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import due_posts

    with session_scope() as session:
        _make_channel(session, "vk_route", "vk")
        _make_post(session, "vk-due", "vk_route")

    due = [row for row in due_posts(50) if row[0] == session_id("vk-due")]
    assert due, "пост должен попасть в очередь публикации"
    _, text, target, platform = due[0]
    assert platform == "vk"
    assert target == "vk"
    assert "Заголовок" in text


def session_id(public_id: str) -> int:
    from sqlalchemy import select

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        return int(
            session.execute(select(Post.id).where(Post.public_id == public_id)).scalar_one()
        )


@pytest.mark.asyncio
async def test_vk_publish_dry_run_marks_published_and_logs():
    from sqlalchemy import select

    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _publish_one

    with session_scope() as session:
        _make_channel(session, "vk_dry", "vk")
        post = _make_post(session, "vk-dry", "vk_dry")
        post_id = post.id

    result = await _publish_one(post_id, "vk", "Текст для ВК", None, "vk")
    assert result["ok"] is True

    with session_scope() as session:
        from tgfarm.db.models import Post

        row = session.get(Post, post_id)
        assert row.status == "published"
        log = session.execute(
            select(CrosspostLog).where(CrosspostLog.post_public_id == "vk-dry")
        ).scalar_one()
        assert log.platform == "vk"
        assert log.status == "dry_run"
        assert log.remote_id


def test_publish_claim_is_atomic():
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _claim_post

    with session_scope() as session:
        _make_channel(session, "claim_chan", "vk")
        post = _make_post(session, "claim-post", "claim_chan")
        post_id = post.id

    assert _claim_post(post_id) is True
    assert _claim_post(post_id) is False, "второй воркер не должен взять тот же пост"


def test_stuck_publishing_posts_require_reconciliation():
    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _requeue_stuck

    with session_scope() as session:
        _make_channel(session, "stuck_chan", "vk")
        post = _make_post(
            session,
            "stuck-post",
            "stuck_chan",
            status="publishing",
            scheduled_at=datetime.now(UTC) - timedelta(hours=2),
        )
        post_id = post.id

    # В общей тестовой БД могут быть застрявшие посты из других тестов.
    assert _requeue_stuck() >= 1
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row.status == "publish_unknown"
        assert "outcome unknown" in (row.publish_error or "")

def test_offline_draft_cannot_pass_the_dzen_length_floor():
    """An offline draft may be short for Telegram, never for a Dzen article."""
    from tgfarm.config import Persona
    from tgfarm.core.schemas import DraftPost
    from tgfarm.s5_quality import gate_length

    persona = Persona(
        id="zen_probe",
        title="Дзен",
        niche="general",
        platform="dzen",
        min_chars=900,
        max_chars=3000,
    )
    draft = DraftPost(
        hook="Короткий заголовок поста.",
        body="Тело на двести символов, которого не хватит для статьи. " * 4,
        sources=[{"title": "t", "url": "https://example.invalid/x"}],
    )
    draft.generation_provider = "offline"
    outcome = gate_length(draft, persona)
    assert outcome.passed is False
    assert outcome.gate == "length"

"""Dzen refuses text-only articles; the farm must never retry one forever.

Live state (2026-11-04): seven Dzen posts reached 13 attempts each with zero
media rows and the error "publication without image is forbidden".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from tgfarm.db.models import Channel, Media, Post
from tgfarm.db.session import session_scope


def _seed(platform: str = "dzen", channel_id: str | None = None) -> tuple[int, str]:
    suffix = uuid4().hex[:8]
    if channel_id is None:
        channel_id = f"cover_chan_{suffix}"
        with session_scope() as session:
            session.add(
                Channel(id=channel_id, title="Обложки", niche="tech", platform=platform)
            )
    public_id = f"cover{suffix}"
    with session_scope() as session:
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            hook="Тестовый заголовок про технологии",
            body="Тестовый текст, достаточный для рендера обложки.",
            generation_provider="offline",
        )
        session.add(post)
        session.flush()
        return post.id, public_id


def test_cover_is_rendered_when_missing():
    from tgfarm import s7_publish

    post_id, public_id = _seed(channel_id="zen_misc_ru")
    path = s7_publish._ensure_cover_for_post(post_id)

    assert path is not None
    assert Path(path).exists()
    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(Media).where(Media.post_id == post_id)
        ).scalar()
    assert count == 1


def test_existing_cover_is_reused(monkeypatch):
    from tgfarm import s7_publish

    post_id, _public_id = _seed(channel_id="zen_misc_ru")
    first = s7_publish._ensure_cover_for_post(post_id)
    assert first

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("an existing cover must be reused, not re-rendered")

    monkeypatch.setattr("tgfarm.s6_visual.render_for_post", boom)
    assert s7_publish._ensure_cover_for_post(post_id) == first


def test_no_cover_is_a_terminal_failure_not_a_retry_storm(monkeypatch):
    from tgfarm import platforms, s7_publish

    post_id, public_id = _seed()
    called = {"dzen": 0}

    async def fake_dzen(*args, **kwargs):  # noqa: ANN002, ANN003
        called["dzen"] += 1
        return {"message_id": 1}

    async def noop_heartbeat(*args, **kwargs):  # noqa: ANN002, ANN003
        await asyncio.Event().wait()

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _p: True)
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(live=True, dzen_publish_timeout_s=5, primary_publish_max_attempts=5),
    )
    monkeypatch.setattr(s7_publish, "_publish_dzen", fake_dzen)
    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", noop_heartbeat)
    monkeypatch.setattr(s7_publish, "_ensure_cover_for_post", lambda _post_id: None)

    result = asyncio.run(s7_publish._publish_one(post_id, "dzen", "text", None, "dzen"))

    assert result["ok"] is False
    assert called["dzen"] == 0  # the browser was never touched
    with session_scope() as session:
        post = session.get(Post, post_id)
        assert post is not None
        assert post.status == "failed"
        assert "cover" in (post.publish_error or "").lower()


def test_image_required_error_is_permanent():
    from tgfarm import s7_publish

    assert s7_publish._is_permanent_publish_error(
        s7_publish.PublishImageRequired("no cover")
    )

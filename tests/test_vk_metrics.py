"""VK wall metrics: the only honest source for the volume channel.

Live finding (2026-11-04): ``post_stats`` held zero VK rows, and the configured
group token is rejected by ``wall.getById`` with error 27 ("method is unavailable
with group auth"). The collector must therefore report the exact requirement
instead of silently measuring nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import vk_stats


def _seed_vk_post(remote_id: str = "406") -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"vk_metrics_{suffix}"
    public_id = f"vkstat{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Метрики", niche="tech", platform="vk")
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook="Хук",
                body="Текст",
            )
        )
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="vk",
                status="posted",
                remote_id=remote_id,
                url=f"https://vk.com/wall-234474988_{remote_id}",
                created_at=datetime.now(UTC),
            )
        )
    return public_id


def _settings(**overrides):
    base = dict(live=True, vk_user_token="user-token", vk_group_id="-234474988")
    base.update(overrides)
    return SimpleNamespace(**base)


def test_owner_id_normalisation():
    assert vk_stats._owner_id("-234") == "-234"
    assert vk_stats._owner_id("234") == "-234"
    assert vk_stats._owner_id("") == ""


def test_item_shapes_are_normalised():
    items = [{"id": 1}]
    assert vk_stats._items({"response": {"items": items}}) == items
    assert vk_stats._items({"response": items}) == items
    assert vk_stats._items({"response": {"count": 0}}) == []
    assert vk_stats._items({"error": {"error_code": 27}}) == []


def test_metrics_are_recorded(monkeypatch):
    public_id = _seed_vk_post("406")
    monkeypatch.setattr(vk_stats, "get_settings", lambda: _settings())
    captured: list[list[str]] = []

    async def fake_fetch(ids, *, token, timeout=20.0):  # noqa: ANN001, ANN202
        captured.append(list(ids))
        return [
            {
                "id": 406,
                "views": {"count": 1200},
                "likes": {"count": 42},
                "comments": {"count": 7},
                "reposts": {"count": 3},
            }
        ]

    monkeypatch.setattr(vk_stats, "_fetch", fake_fetch)
    result = asyncio.run(vk_stats.collect_and_record(days=7))

    assert result["saved"] == 1
    assert captured and captured[0][0] == "-234474988_406"
    with session_scope() as session:
        rows = session.execute(
            select(PostStat)
            .join(Post, Post.id == PostStat.post_id)
            .where(Post.public_id == public_id)
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.views, row.reactions, row.comments, row.forwards) == (1200, 42, 7, 3)


def test_group_token_is_reported_instead_of_silently_measuring_nothing(monkeypatch):
    _seed_vk_post("407")
    monkeypatch.setattr(
        vk_stats, "get_settings", lambda: _settings(vk_user_token="")
    )

    async def must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("no request must be made without a user token")

    monkeypatch.setattr(vk_stats, "_fetch", must_not_run)
    result = asyncio.run(vk_stats.collect_and_record(days=7))
    assert result["saved"] == 0
    assert "VK_USER_TOKEN" in result["skipped"]


def test_error_27_becomes_an_actionable_skip(monkeypatch):
    _seed_vk_post("408")
    monkeypatch.setattr(vk_stats, "get_settings", lambda: _settings())

    async def fake_fetch(ids, *, token, timeout=20.0):  # noqa: ANN001, ANN202
        raise RuntimeError("vk wall.getById error 27")

    monkeypatch.setattr(vk_stats, "_fetch", fake_fetch)
    result = asyncio.run(vk_stats.collect_and_record(days=7))
    assert result["saved"] == 0
    assert "error 27" in result["skipped"]
    assert "user token" in result["skipped"]

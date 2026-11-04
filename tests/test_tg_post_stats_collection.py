"""Telegram post metrics: the collector that could never write.

`collect_post_stats` upserted its daily snapshot on `PostStat.day` — a column the
schema never had — so the very first published post raised before a row landed,
and the farm read that as "telegram returned nothing". Every Telegram number in
the live database came from `tools/seed_metrics.py`, not from this function. The
tests below run the real collector and swap only the network.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select

from tgfarm import tg_sync, tguser
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import Post, PostStat
from tgfarm.db.session import session_scope


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _seed_channel_and_post() -> tuple[str, int, int]:
    channel_id = f"tgstats-{_uid()}"
    chat_id = -100_000_000_000 + uuid.uuid4().int % 1_000_000
    message_id = uuid.uuid4().int % 1_000_000
    with session_scope() as session:
        session.add(
            ChannelRow(
                id=channel_id,
                platform="telegram",
                tg_chat_id=chat_id,
                title=channel_id,
            )
        )
        session.flush()
        post = Post(
            public_id=f"tg-{_uid()}",
            channel_id=channel_id,
            hook="факт",
            body="Тело публикации.",
            status="published",
            published_at=datetime.now(UTC),
            tg_message_id=message_id,
        )
        session.add(post)
        session.flush()
        return channel_id, post.id, message_id


def _drop(channel_id: str, post_id: int) -> None:
    with session_scope() as session:
        session.execute(delete(PostStat).where(PostStat.post_id == post_id))
        session.execute(delete(Post).where(Post.id == post_id))
        session.execute(delete(ChannelRow).where(ChannelRow.id == channel_id))


def _stub_fetch(monkeypatch, *, views: int, reactions: int, forwards: int) -> None:
    async def fetch(pairs):
        return {pair: {"views": views, "reactions": reactions, "forwards": forwards} for pair in pairs}

    monkeypatch.setattr(tguser, "fetch_post_stats", fetch)


def _rows_for(post_id: int) -> list[PostStat]:
    with session_scope() as session:
        return list(
            session.execute(select(PostStat).where(PostStat.post_id == post_id)).scalars().all()
        )


def test_a_live_snapshot_reaches_post_stats(monkeypatch):
    channel_id, post_id, _message_id = _seed_channel_and_post()
    _stub_fetch(monkeypatch, views=611, reactions=12, forwards=3)
    try:
        report = asyncio.run(tg_sync.collect_post_stats())

        assert report["ok"] is True
        rows = _rows_for(post_id)
        assert len(rows) == 1, f"снимок не записан: {report}"
        assert (rows[0].views, rows[0].reactions, rows[0].forwards) == (611, 12, 3)
        assert rows[0].comments == 0
    finally:
        _drop(channel_id, post_id)


def test_a_second_collection_the_same_day_updates_instead_of_stacking(monkeypatch):
    channel_id, post_id, _message_id = _seed_channel_and_post()
    try:
        _stub_fetch(monkeypatch, views=100, reactions=1, forwards=1)
        asyncio.run(tg_sync.collect_post_stats())
        _stub_fetch(monkeypatch, views=180, reactions=4, forwards=2)
        asyncio.run(tg_sync.collect_post_stats())

        rows = _rows_for(post_id)
        assert len(rows) == 1, "дневной снимок должен обновляться на месте"
        assert rows[0].views == 180
    finally:
        _drop(channel_id, post_id)


def test_a_channel_without_a_chat_id_yields_nothing_to_fetch(monkeypatch):
    channel_id = f"tgstats-{_uid()}"
    with session_scope() as session:
        session.add(ChannelRow(id=channel_id, platform="telegram", title=channel_id))
        post = Post(
            public_id=f"tg-{_uid()}",
            channel_id=channel_id,
            status="published",
            published_at=datetime.now(UTC),
            tg_message_id=123456,
        )
        session.add(post)
        session.flush()
        post_id = post.id

    calls: list[list] = []

    async def spy(pairs):
        calls.append(list(pairs))
        return {}

    monkeypatch.setattr(tguser, "fetch_post_stats", spy)
    try:
        report = asyncio.run(tg_sync.collect_post_stats())
        assert report["posts"] == 0
        assert not calls, "без tg_chat_id нечего и спрашивать у Telegram"
    finally:
        _drop(channel_id, post_id)

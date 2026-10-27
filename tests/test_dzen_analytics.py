from __future__ import annotations

import random
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import delete, select

from tgfarm.db.models import ChannelStat, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s3_router import _sample_angle
from tgfarm.s8_analytics import angle_performance
from tgfarm.s8_analytics.dzen_web import (
    DzenPublicationMetrics,
    _to_int,
    parse_table_rows,
    publication_id,
    record_publication_metrics,
    record_subscribers,
)


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def test_to_int_parses_dzen_counters():
    assert _to_int("0") == 0
    assert _to_int("1 234") == 1234
    assert _to_int("1,2 тыс.") == 1200
    assert _to_int("3 млн") == 3_000_000
    assert _to_int("—") == 0
    assert _to_int("Считаем") == 0


def test_publication_id_accepts_article_and_media_urls():
    assert publication_id("https://dzen.ru/a/atd9lJF8oAR8ZrJO?share=1") == "atd9lJF8oAR8ZrJO"
    assert publication_id("https://dzen.ru/media/example/slug-abc123") == "slug-abc123"
    assert publication_id("https://example.com/nope") is None


def test_parse_table_rows_maps_studio_columns():
    rows = [
        {
            "href": "https://dzen.ru/a/first?utm=1",
            "title": "Первая",
            "cells": ["", "", "Первая", "Вчера", "1 000", "120", "80", "2 мин", "3", "4", "5", "6", ""],
        },
        {
            "href": "https://dzen.ru/a/first?utm=2",
            "title": "Дубликат",
            "cells": ["", "", "Дубликат", "Вчера", "9", "9", "9", "0", "0", "0", "0", "0", ""],
        },
    ]
    parsed = parse_table_rows(rows)
    assert len(parsed) == 1
    item = parsed[0]
    assert item.remote_id == "first"
    assert item.impressions == 1000
    assert item.opens == 120
    assert item.views == 80
    assert item.comments == 4
    assert item.reactions == 5
    assert item.forwards == 6
    assert item.ctr == 0.12
    assert item.read_rate == 0.6667


def test_record_publication_metrics_matches_crosspost_and_skips_unchanged():
    public_id = f"dz-{_uid()}"
    remote_id = f"remote-{_uid()}"
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id="zen_misc_ru",
                angle="news",
                hook="Тест",
                body="Тело",
                status="published",
                published_at=datetime.now(UTC),
            )
        )
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="dzen",
                status="posted",
                remote_id=remote_id,
                url=f"https://dzen.ru/a/{remote_id}",
            )
        )
    try:
        item = DzenPublicationMetrics(
            url=f"https://dzen.ru/a/{remote_id}",
            remote_id=remote_id,
            impressions=500,
            opens=50,
            views=30,
            reactions=7,
            comments=2,
            forwards=1,
        )
        first = record_publication_metrics([item])
        assert first["saved"] == 1
        second = record_publication_metrics([item])
        assert second["saved"] == 0
        assert second["unchanged"] == 1

        with session_scope() as session:
            post = session.execute(
                select(Post).where(Post.public_id == public_id)
            ).scalar_one()
            stat = session.execute(
                select(PostStat).where(PostStat.post_id == post.id)
            ).scalar_one()
            assert (stat.impressions, stat.opens, stat.views) == (500, 50, 30)
            assert (stat.reactions, stat.comments, stat.forwards) == (7, 2, 1)
    finally:
        with session_scope() as session:
            post = session.execute(
                select(Post).where(Post.public_id == public_id)
            ).scalar_one_or_none()
            if post is not None:
                session.execute(delete(PostStat).where(PostStat.post_id == post.id))
                session.execute(delete(Post).where(Post.id == post.id))


def test_angle_performance_uses_ctr_and_read_through():
    ids = [f"ap-{_uid()}", f"ap-{_uid()}"]
    with session_scope() as session:
        session.add_all(
            [
                Post(
                    public_id=ids[0],
                    channel_id="zen_misc_ru",
                    angle="comparison",
                    hook="A",
                    body="A",
                    status="published",
                    published_at=datetime.now(UTC),
                ),
                Post(
                    public_id=ids[1],
                    channel_id="zen_misc_ru",
                    angle="checklist",
                    hook="B",
                    body="B",
                    status="published",
                    published_at=datetime.now(UTC),
                ),
            ]
        )
        session.flush()
        posts = session.execute(select(Post).where(Post.public_id.in_(ids))).scalars().all()
        by_hook = {p.hook: p for p in posts}
        session.add_all(
            [
                PostStat(
                    post_id=by_hook["A"].id,
                    impressions=1000,
                    opens=200,
                    views=150,
                    reactions=10,
                ),
                PostStat(
                    post_id=by_hook["B"].id,
                    impressions=1000,
                    opens=50,
                    views=10,
                    reactions=0,
                ),
            ]
        )
    try:
        performance = angle_performance(14, "zen_misc_ru")
        assert performance["comparison"]["quality"] > performance["checklist"]["quality"]
        assert performance["comparison"]["ctr"] > performance["checklist"]["ctr"]
    finally:
        with session_scope() as session:
            posts = session.execute(select(Post).where(Post.public_id.in_(ids))).scalars().all()
            for post in posts:
                session.execute(delete(PostStat).where(PostStat.post_id == post.id))
                session.execute(delete(Post).where(Post.id == post.id))


def test_angle_sampling_is_nudged_by_measured_quality(cfg):
    persona = replace(
        cfg.persona("zen_misc_ru"),
        angle_weights={"comparison": 1.0, "checklist": 1.0},
    )
    performance = {
        "comparison": {"n": 5, "quality": 0.05},
        "checklist": {"n": 5, "quality": 0.01},
    }
    rng = random.Random(7)
    samples = [_sample_angle(persona, rng, performance) for _ in range(400)]
    assert samples.count("comparison") > samples.count("checklist")


def test_record_subscribers_updates_dzen_channel_stat():
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    assert record_subscribers(7) is True
    with session_scope() as session:
        row = session.execute(
            select(ChannelStat).where(
                ChannelStat.channel_id == "zen_misc_ru", ChannelStat.day == day
            )
        ).scalar_one()
        assert row.subscribers == 7

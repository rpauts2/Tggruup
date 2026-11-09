"""Daily ERR needs views; large recent windows must retain every latest snapshot."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from tgfarm import s8_analytics
from tgfarm.db import session as session_mod
from tgfarm.db.models import Base, Channel, ChannelStat, Post, PostStat
from tgfarm.db.session import session_scope


@pytest.fixture(autouse=True)
def _temporary_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'analytics.db').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(
        session_mod, "_SessionFactory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setattr(
        s8_analytics, "get_config", lambda: SimpleNamespace(persona=lambda channel_id: None)
    )
    with session_scope() as session:
        session.add(Channel(id="honesty", title="Test", niche="tech"))
    yield engine
    engine.dispose()


@pytest.mark.parametrize(
    ("views", "reactions", "expected_err"),
    [(None, 0, None), (0, 0, None), (0, 5, None), (100, 0, 0.0), (100, 10, 0.1)],
    ids=["unmeasured", "zero-views", "engagement-without-views", "measured-zero", "rate"],
)
def test_daily_err_uses_views_denominator_and_replaces_existing_rate(views, reactions, expected_err):
    # A max(1, views) denominator invents a rate in the first three cases.
    published = datetime.now(UTC) - timedelta(hours=2)
    with session_scope() as session:
        post = Post(
            public_id="daily", channel_id="honesty", status="published", published_at=published
        )
        session.add(post)
        session.flush()
        if views is not None:
            session.add(PostStat(post_id=post.id, views=views, reactions=reactions))
        session.add(
            ChannelStat(channel_id="honesty", day=published.strftime("%Y-%m-%d"), err=0.5)
        )

    assert s8_analytics.write_daily_stats() == 1
    with session_scope() as session:
        row = session.scalars(select(ChannelStat)).one()
    assert row.err == expected_err
    assert (row.posts, row.views_sum, row.reactions_sum) == (1, views or 0, reactions)


def test_recent_metrics_chunks_large_window_and_keeps_latest_snapshot(_temporary_database):
    # One unbounded IN fails at 500; first/last/max snapshot selection loses the newer data.
    now = datetime.now(UTC)
    public_ids = {f"bulk-{index}" for index in range(1003)}
    with session_scope() as session:
        posts = [
            Post(
                public_id=f"bulk-{index}", channel_id="honesty", status="published",
                published_at=now - timedelta(hours=2), angle="news", hook_style="fact", variant="b",
            )
            for index in range(1003)
        ]
        session.add_all(posts)
        session.add_all([
            Post(public_id="draft", channel_id="honesty", status="draft", published_at=now),
            Post(
                public_id="expired", channel_id="honesty", status="published",
                published_at=now - timedelta(days=8),
            ),
        ])
        session.flush()
        for index, post in enumerate(posts[:-1]):
            old = PostStat(
                post_id=post.id, collected_at=now - timedelta(hours=1),
                impressions=900, opens=300, views=200, reactions=30, comments=20, forwards=10,
            )
            latest = PostStat(
                post_id=post.id, collected_at=now - timedelta(minutes=5),
                impressions=800, opens=200, views=100, reactions=4, comments=2, forwards=1,
            )
            session.add_all([old, latest] if index % 2 else [latest, old])

    # Set the actual SQLite limit after seeding so inserts are not the subject of the test.
    @event.listens_for(_temporary_database, "checkout")
    def _limit_parameters(connection, _record, _proxy):
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)

    metrics = s8_analytics.recent_metrics(7)
    assert len(metrics) == 1003
    assert {metric.post_public_id for metric in metrics} == public_ids
    for metric in metrics:
        assert (metric.channel_id, metric.angle, metric.hook_style, metric.variant) == (
            "honesty", "news", "fact", "b"
        )
        values = (
            metric.impressions, metric.opens, metric.views, metric.reactions,
            metric.comments, metric.forwards, metric.measured,
        )
        if metric.post_public_id == "bulk-1002":
            assert values == (0, 0, 0, 0, 0, 0, False)
        else:
            assert values == (800, 200, 100, 4, 2, 1, True)


def test_recent_metrics_empty_window():
    assert s8_analytics.recent_metrics(7) == []

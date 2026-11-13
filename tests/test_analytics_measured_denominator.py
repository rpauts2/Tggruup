"""An unmeasured post is not a post that earned zero views.

``s8_analytics.channel_report`` and ``fleet_summary`` divided ``avg_views`` and
``avg_er`` by every published row, including the rows ``recent_metrics`` fills
with zeros because no ``post_stats`` snapshot was ever collected. The two facts —
«nobody measured this» and «measured, and the audience did nothing» — collapsed
into one number, and every average was dragged toward zero by exactly the share
of unmeasured posts.

Measured on the live base 2026-11-05 (``mode=ro``): the ``vk`` channel had 372
published posts inside 30 days and zero snapshots, so its ``avg_views`` read a
confident ``0.0``. That is the same fabricated zero the P34 rule already removed
from ``s8_analytics.formats``, still standing in the two functions the digest and
the panel read.

What changes and what does not:

* ``posts``/``posts_7d``, ``views_total``, ``engagement_total`` and
  ``impressions_total`` keep counting every published post — a silent
  redefinition would change the panel with the planner switched off;
* the rates divide by the measured subset only, and report ``None`` (never
  ``0.0``) when that subset is empty;
* the measured count travels beside the rate as ``measured`` /
  ``measured_7d``/ ``unmeasured_posts``, so the panel can say what the number
  was taken over.

Hermetic by construction: throwaway channel ids, so ``recent_metrics`` inside the
window contains only the rows this file creates for its own channels.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import channel_report, fleet_summary, recent_metrics

_seeded_channels: list[str] = []


def _channel() -> str:
    channel_id = f"den_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Знаменатель", niche="business", platform="telegram")
        )
    return channel_id


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    yield
    with session_scope() as session:
        post_ids = list(
            session.scalars(select(Post.id).where(Post.channel_id.in_(_seeded_channels))).all()
        )
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
        if post_ids:
            session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()


def _publish(channel_id: str, *, views: int | None = None, reactions: int = 0) -> str:
    """One published post; ``views=None`` means no snapshot was ever collected."""
    public_id = f"den_{uuid4().hex[:10]}"
    with session_scope() as session:
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status="published",
            angle="news",
            published_at=datetime.now(UTC) - timedelta(days=1),
            hook="Заголовок поста для проверки знаменателя.",
            body="Тело поста, достаточное для записи в базу.",
        )
        session.add(post)
        session.flush()
        if views is not None:
            session.add(
                PostStat(
                    post_id=int(post.id),
                    views=views,
                    reactions=reactions,
                    impressions=0,
                    opens=0,
                    comments=0,
                    forwards=0,
                    collected_at=datetime.now(UTC),
                )
            )
    return public_id


def test_channel_report_divides_by_measured_posts_only():
    channel_id = _channel()
    _publish(channel_id, views=100, reactions=10)
    _publish(channel_id, views=200, reactions=20)
    _publish(channel_id)  # published, never measured

    report = channel_report(channel_id, days=7)
    assert report.posts_7d == 3, "every published post still counts"
    assert report.measured_7d == 2
    assert report.avg_views == pytest.approx(150.0), "the unmeasured row is not a zero"
    assert report.avg_er == pytest.approx(0.1), "150 views, 30 engagements"


def test_channel_report_reports_no_rate_when_nothing_was_measured():
    channel_id = _channel()
    _publish(channel_id)
    _publish(channel_id)

    report = channel_report(channel_id, days=7)
    assert report.posts_7d == 2
    assert report.measured_7d == 0
    assert report.avg_views is None, "no measurement is not a measured zero"
    assert report.avg_er is None
    assert report.avg_ctr is None
    assert report.avg_read_rate is None


def test_measured_zero_views_are_real_but_er_has_no_denominator():
    channel_id = _channel()
    _publish(channel_id, views=0, reactions=0)

    report = channel_report(channel_id, days=7)
    assert report.measured_7d == 1
    assert report.avg_views == 0.0, "a snapshot exists, and it says zero"
    assert report.avg_er is None, "zero views cannot define an engagement rate"


def test_fleet_summary_divides_by_measured_posts_only():
    channel_id = _channel()
    _publish(channel_id, views=100, reactions=10)
    _publish(channel_id, views=300, reactions=30)
    _publish(channel_id)
    _publish(channel_id)

    fleet = fleet_summary(7)
    row = next(item for item in fleet["channels"] if item["channel_id"] == channel_id)
    assert row["posts"] == 4
    assert row["measured"] == 2
    assert row["unmeasured_posts"] == 2
    assert row["views_total"] == 400, "totals still cover every published post"
    assert row["avg_views"] == pytest.approx(200.0)
    assert row["avg_er"] == pytest.approx(0.1)


def test_fleet_summary_names_the_absence_instead_of_zero():
    channel_id = _channel()
    _publish(channel_id)
    _publish(channel_id)

    fleet = fleet_summary(7)
    row = next(item for item in fleet["channels"] if item["channel_id"] == channel_id)
    assert row["measured"] == 0
    assert row["unmeasured_posts"] == 2
    assert row["avg_views"] is None
    assert row["avg_er"] is None


def test_recent_metrics_marks_which_posts_were_measured():
    channel_id = _channel()
    measured_id = _publish(channel_id, views=42)
    unmeasured_id = _publish(channel_id)

    by_id = {m.post_public_id: m for m in recent_metrics(7)}
    assert by_id[measured_id].measured is True
    assert by_id[measured_id].views == 42
    assert by_id[unmeasured_id].measured is False
    assert by_id[unmeasured_id].views == 0

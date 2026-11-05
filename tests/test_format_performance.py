"""Format performance must be measured, not assumed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics.formats import MIN_SAMPLE, best_format, format_performance


def _post(fmt: str, *, engagement: int | None = None, days_ago: float = 2.0, status: str = "published") -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"fmt_{suffix}"
    public_id = f"fmtpost{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Форматы", niche="tech", platform="vk"))
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status=status,
            post_format=fmt,
            published_at=datetime.now(UTC) - timedelta(days=days_ago),
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
    return public_id


def test_empty_window_is_honest():
    report = format_performance(days=30)
    assert report["published_total"] >= 0
    assert isinstance(report["formats"], dict)
    assert report["min_sample"] == MIN_SAMPLE


def test_unmeasured_posts_do_not_inflate_average():
    _post("news_unmeasured", engagement=None)
    _post("news_unmeasured", engagement=None)
    report = format_performance(days=7)
    row = report["formats"]["news_unmeasured"]
    assert row["published"] >= 2
    assert row["measured"] == 0
    # Never measured is not measured-and-zero: the report has no rate to give here,
    # and a `0.0` would be the one number a reader could not tell from an audience
    # that engaged with nothing. (A measured format whose snapshots are all zero
    # still reports a real `0.0` — see `test_unmeasured_format_rate.py`.)
    assert row["avg_engagement"] is None
    assert row["reliable"] is False


def test_measured_engagement_and_reliability():
    for value in (10, 20, 30):
        _post("listicle", engagement=value)
    report = format_performance(days=7)
    row = report["formats"]["listicle"]
    assert row["measured"] >= 3
    assert row["avg_engagement"] >= 20
    assert row["reliable"] is True


def test_old_posts_leave_the_window():
    _post("analysis", engagement=100, days_ago=40)
    report = format_performance(days=7)
    assert "analysis" not in report["formats"]


def test_drafts_are_not_counted():
    _post("news_draft_case", engagement=50, status="draft")
    report = format_performance(days=7)
    published = report["formats"].get("news_draft_case", {}).get("published", 0)
    assert published == 0


def test_best_format_requires_a_reliable_sample():
    _post("news_single", engagement=5)  # alone: not reliable
    result = best_format(days=7)
    assert result is None or result["reliable"] is True


def test_chunking_the_stat_read_does_not_lose_a_post(monkeypatch):
    """Every published post must reach the grouped stats read, chunked or not.

    Measured on a copy of the live database: past the SQLite variable limit the
    single `IN` list died outright, so the format lesson simply stopped. Chunking
    fixes that, and a chunk loop is exactly where an off-by-one would hide.
    """
    from tgfarm.s8_analytics import formats as formats_module

    probe = f"chunk_probe_{uuid4().hex[:6]}"
    for _ in range(5):
        _post(probe, engagement=40)

    monkeypatch.setattr(formats_module, "_IN_CHUNK", 2)
    chunked = format_performance(days=7)["formats"][probe]
    monkeypatch.setattr(formats_module, "_IN_CHUNK", 10_000)
    whole = format_performance(days=7)["formats"][probe]

    assert chunked["published"] == whole["published"]
    assert chunked["measured"] == whole["measured"] == 5


def test_the_chunk_stays_under_the_smallest_sqlite_variable_limit():
    from tgfarm.s8_analytics import formats as formats_module

    assert 0 < formats_module._IN_CHUNK <= 999

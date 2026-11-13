"""Each rate needs its own positive denominator; a measured zero is not always a rate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tgfarm import s8_analytics
from tgfarm.db import session as session_mod
from tgfarm.db.models import Base, Channel, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import PostMetrics


@pytest.fixture(autouse=True)
def _temporary_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'rates.db').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(
        session_mod, "_SessionFactory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setattr(
        s8_analytics, "get_config", lambda: SimpleNamespace(persona=lambda channel_id: None)
    )
    with session_scope() as session:
        session.add_all([
            Channel(id="rates", title="Rates", niche="tech"),
            Channel(id="unknown", title="Unknown", niche="tech"),
        ])
    yield
    engine.dispose()


def _metric(**counts):
    return PostMetrics(
        post_public_id="direct", channel_id="rates", angle="news",
        published_at=datetime.now(UTC) - timedelta(days=1), **counts,
    )


@pytest.mark.parametrize("measured", [False, True])
@pytest.mark.parametrize("denominator", [0, -1])
@pytest.mark.parametrize(
    ("rate", "denominator_field"), [("er", "views"), ("ctr", "impressions"), ("read_rate", "opens")]
)
def test_rate_is_unknown_without_its_own_positive_denominator(rate, denominator_field, denominator, measured):
    counts = {"views": 100, "reactions": 10, "impressions": 200, "opens": 20}
    counts[denominator_field] = denominator
    metric = _metric(measured=measured, **counts)
    assert getattr(metric, rate) is None


@pytest.mark.parametrize("measured", [False, True])
@pytest.mark.parametrize(
    ("rate", "counts"),
    [("er", {"views": 100}), ("ctr", {"impressions": 100}), ("read_rate", {"opens": 100})],
)
def test_positive_denominator_with_zero_numerator_is_a_numeric_zero(rate, counts, measured):
    value = getattr(_metric(measured=measured, **counts), rate)
    assert value == 0.0
    assert isinstance(value, float)


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({}, (None, None, None, None)),
        ({"impressions": 99}, (None, 0.0, None, None)),
        ({"impressions": 100}, (None, 0.0, None, 0.0)),
        ({"impressions": 200, "opens": 20}, (None, 0.1, 0.0, 0.0)),
        ({"views": 100, "reactions": 20, "impressions": 99}, (0.2, 0.0, None, 0.2)),
        ({"views": 100, "reactions": 20, "impressions": 100}, (0.2, 0.0, None, 0.0)),
        ({"views": 333, "reactions": 1, "impressions": 300, "opens": 100},
         (0.003, 0.3333, 1.0, 0.3333)),
        ({"views": 3, "reactions": 5, "impressions": 100, "opens": 200},
         (1.6667, 1.0, 0.015, 0.015)),
        ({"views": 200, "impressions": 100, "opens": 150}, (0.0, 1.0, 1.0, 1.0)),
    ],
    ids=["missing", "below-feed-threshold", "feed-zero-opens", "feed-zero-reads", "er-fallback",
         "feed-overrides-er", "rounding", "ctr-clipping-er-unclipped", "read-clipping"],
)
def test_quality_keeps_feed_threshold_zero_and_rounding_contract(counts, expected):
    metric = _metric(**counts)  # Direct counted constructors need not set measured=True.
    assert metric.measured is False
    assert (metric.er, metric.ctr, metric.read_rate, metric.quality) == expected
    payload = metric.as_dict()
    assert tuple(payload[key] for key in ("er", "ctr", "read_rate", "quality")) == expected


def _publish(public_id, *, channel_id="rates", hour=12, angle="unknown", hook_style="fact", variant="a",
             counts=None):
    published = (datetime.now(UTC) - timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
    with session_scope() as session:
        post = Post(
            public_id=public_id, channel_id=channel_id, status="published", published_at=published,
            angle=angle, hook_style=hook_style, variant=variant,
        )
        session.add(post)
        session.flush()
        if counts is not None:
            session.add(PostStat(post_id=post.id, **counts))


@pytest.fixture
def _mixed_posts():
    _publish("unmeasured", hour=1)
    _publish("snapshot-zero", hour=2, counts={})
    _publish("engagement-without-views", hour=3, counts={"reactions": 7})
    _publish("feed-zero", hour=4, angle="feed", counts={"impressions": 200})
    _publish("positive", hour=5, angle="news",
             counts={"views": 100, "reactions": 10, "impressions": 1000, "opens": 50})
    _publish("opens-only", hour=6, counts={"opens": 20})
    _publish("reads-without-feed", hour=7, angle="zero", counts={"views": 50, "opens": 100})
    _publish("below-feed", hour=8, counts={"impressions": 50})


def test_channel_report_uses_independent_valid_rate_subsets(_mixed_posts):
    report = s8_analytics.channel_report("rates")
    assert (report.posts_7d, report.measured_7d) == (8, 7)
    assert report.avg_views == 21.4286  # All seven snapshots, including zero views.
    assert report.avg_er == 0.05  # Only the two posts with positive views.
    assert report.avg_ctr == 0.0167  # Three positive-impression denominators.
    assert report.avg_read_rate == 0.5  # Three positive-open denominators, independent of impressions.
    assert report.impressions_total == 1250
    assert report.by_angle == {"feed": 0.0, "news": 0.05, "zero": 0.0}
    assert report.by_hour == {4: 0.0, 5: 0.05, 7: 0.0}
    assert (report.best_angle, report.best_hour) == ("news", 5)
    metrics = {metric.post_public_id: metric for metric in s8_analytics.recent_metrics()}
    assert metrics["unmeasured"].measured is False
    assert metrics["snapshot-zero"].measured is True
    assert metrics["snapshot-zero"].er is None
    assert metrics["feed-zero"].read_rate is None
    assert metrics["opens-only"].read_rate == 0.0


def test_fleet_and_channel_er_ignore_undefined_rates_without_changing_counts_or_totals(_mixed_posts):
    _publish("unknown-unmeasured", channel_id="unknown")
    _publish("unknown-zero", channel_id="unknown", counts={})
    fleet = s8_analytics.fleet_summary()
    assert (fleet["posts"], fleet["measured"], fleet["unmeasured_posts"]) == (10, 8, 2)
    assert (fleet["views_total"], fleet["engagement_total"]) == (150, 17)
    assert fleet["avg_er"] == 0.05
    by_channel = {row["channel_id"]: row for row in fleet["channels"]}
    assert by_channel["rates"] == {
        "channel_id": "rates", "posts": 8, "measured": 7, "unmeasured_posts": 1,
        "views_total": 150, "avg_views": 21.4286, "avg_er": 0.05,
    }
    assert by_channel["unknown"] == {
        "channel_id": "unknown", "posts": 2, "measured": 1, "unmeasured_posts": 1,
        "views_total": 0, "avg_views": 0.0, "avg_er": None,
    }


@pytest.mark.parametrize("counts", [None, {}, {"impressions": 99}, {"reactions": 5}])
def test_unknown_quality_cannot_invent_best_hour_or_angle(counts):
    _publish("no-quality", counts=counts)
    report = s8_analytics.channel_report("rates")
    assert report.by_hour == {}
    assert report.by_angle == {}
    assert report.best_hour is None
    assert report.best_angle is None
    assert report.avg_er is None
    assert s8_analytics.fleet_summary()["avg_er"] is None


def test_defined_rates_do_not_require_measured_flag_but_hourly_evidence_does(monkeypatch):
    metric = _metric(views=10, reactions=1, impressions=200, opens=20)
    monkeypatch.setattr(s8_analytics, "recent_metrics", lambda days: [metric])
    report = s8_analytics.channel_report("rates")
    assert report.measured_7d == 0
    assert report.avg_views is None
    assert (report.avg_er, report.avg_ctr, report.avg_read_rate) == (0.1, 0.1, 0.5)
    assert report.by_angle == {"news": 0.05}
    assert report.by_hour == {}
    assert report.best_hour is None
    fleet = s8_analytics.fleet_summary()
    assert fleet["avg_er"] == 0.1
    assert fleet["channels"][0]["avg_er"] == 0.1
    assert fleet["channels"][0]["avg_views"] is None


def test_genuine_zero_quality_remains_hourly_evidence():
    _publish("unknown-hour", hour=1)
    _publish("zero-hour", hour=2, angle="feed", counts={"impressions": 100})
    report = s8_analytics.channel_report("rates")
    assert report.by_hour == {2: 0.0}
    assert report.best_hour == 2
    assert report.by_angle == {"feed": 0.0}
    assert report.best_angle == "feed"


@pytest.mark.parametrize("performance", [s8_analytics.angle_performance, s8_analytics.hook_style_performance])
def test_angle_and_hook_quality_can_exist_without_er_and_ctr_can_be_absent(performance):
    for public_id, counts in [
        ("views", {"views": 100, "reactions": 10}),
        ("feed", {"impressions": 100}),
        ("no-reads", {"impressions": 200, "opens": 20}),
        ("zero-er", {"views": 30}),
        ("too-small", {"views": 29, "reactions": 29, "impressions": 99, "opens": 99}),
        ("unmeasured", None),
    ]:
        _publish(public_id, angle="mixed", hook_style="mixed", counts=counts)
    _publish("feed-only", angle="feed-only", hook_style="feed-only", counts={"impressions": 100})
    _publish("views-only", angle="views-only", hook_style="views-only", counts={"views": 30})
    _publish("other-channel", channel_id="unknown", angle="mixed", hook_style="mixed",
             counts={"views": 100, "reactions": 100})
    report = performance(14, "rates")
    assert report == {
        "mixed": {"n": 4, "er": 0.05, "ctr": 0.0667, "quality": 0.025},
        "feed-only": {"n": 1, "er": None, "ctr": 0.0, "quality": 0.0},
        "views-only": {"n": 1, "er": 0.0, "ctr": None, "quality": 0.0},
    }


def test_holdout_keeps_published_count_but_averages_only_defined_quality(_mixed_posts):
    _publish("holdout-unknown", variant="holdout")
    _publish("holdout-snapshot-zero", variant="holdout", counts={})
    _publish("zero-variant", variant="zero", counts={"impressions": 100})
    assert s8_analytics.holdout_report() == {
        "a": {"n": 8, "quality": 0.0167, "views": 18.8},
        "holdout": {"n": 2, "quality": None, "views": 0.0},
        "zero": {"n": 1, "quality": 0.0, "views": 0.0},
    }


@pytest.mark.parametrize("counts", [{"views": 30, "reactions": 3}, {"impressions": 100}])
def test_slots_require_measurement_even_when_direct_constructor_defines_quality(monkeypatch, counts):
    unmeasured = _metric(**counts)
    measured = _metric(measured=True, **counts)
    unmeasured.published_at = unmeasured.published_at.replace(hour=1)
    measured.published_at = measured.published_at.replace(hour=2)
    assert unmeasured.quality == measured.quality  # Scalar rates do not require a snapshot flag.
    monkeypatch.setattr(s8_analytics, "recent_metrics", lambda days: [unmeasured, measured])
    assert s8_analytics.slot_performance() == {2: measured.quality}


def test_slots_keep_qualified_zero_quality_and_ignore_thin_unknown_samples():
    _publish("unmeasured-slot", hour=1)
    _publish("thin-slot", hour=2, counts={"impressions": 99})
    _publish("feed-zero-slot", hour=3, counts={"impressions": 100})
    _publish("read-zero-slot", hour=3, counts={"impressions": 100, "opens": 10})
    _publish("er-slot", hour=4, counts={"views": 30, "reactions": 3})
    assert s8_analytics.slot_performance() == {3: 0.0, 4: 0.1}


@pytest.mark.parametrize("measured", [False, True])
def test_slot_performance_excludes_none_quality_even_with_thirty_views(monkeypatch, measured):
    """DEFECT 1: a metric whose quality is None must never enter the slot sample,
    even when views>=30 clears the distribution threshold. Absence is not a zero:
    the hour it sits in must not appear, and no 0.0 may be fabricated for it."""
    absent = _metric(measured=measured, views=30, impressions=100, opens=-1)
    absent.published_at = absent.published_at.replace(hour=1)
    # ctr=-0.01 (a non-zero numerator over a positive denominator) but read_rate
    # is undefined (opens<=0), so the feed quality is None, not a measured zero.
    assert absent.quality is None
    assert absent.views >= 30
    real = _metric(measured=True, views=30, reactions=3)
    real.published_at = real.published_at.replace(hour=2)
    assert real.quality == 0.1
    monkeypatch.setattr(s8_analytics, "recent_metrics", lambda days: [absent, real])
    assert s8_analytics.slot_performance() == {2: 0.1}

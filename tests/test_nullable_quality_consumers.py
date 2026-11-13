"""Missing quality is neither a zero score nor an observation for downstream learning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tgfarm import config, s8_analytics, s12_learning
from tgfarm.db import session as session_mod
from tgfarm.db.models import Base, Channel, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import PostMetrics
from tgfarm.s8_analytics.portfolio import build_portfolio_report

CHANNEL = "nullable-quality"
NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


@pytest.fixture
def temporary_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'quality.db').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(
        session_mod, "_SessionFactory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(persona=lambda channel_id: None))
    with session_scope() as session:
        session.add(Channel(id=CHANNEL, title="Nullable quality", niche="tech", timezone="UTC"))
    yield
    engine.dispose()


def _publish(public_id, hour, counts):
    published = (datetime.now(UTC) - timedelta(days=2)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    with session_scope() as session:
        post = Post(
            public_id=public_id, channel_id=CHANNEL, status="published", published_at=published,
            angle=public_id, hook_style=None,
        )
        session.add(post)
        session.flush()
        if counts is not None:
            session.add(PostStat(post_id=post.id, **counts))


def test_s12_persisted_missing_quality_is_excluded_from_slot_scores_and_confidence(temporary_database):
    for i in range(3):
        _publish(f"missing-{i}", 2, {"impressions": 100, "opens": -1})
        _publish(f"zero-{i}", 2, {"impressions": 100})
        _publish(f"positive-{i}", 3, {"views": 30, "reactions": 3})
    _publish("never-measured", 4, None)
    metrics = s8_analytics.recent_metrics(14)
    missing = [metric for metric in metrics if metric.post_public_id.startswith("missing-")]
    assert len(missing) == 3
    assert all(metric.measured and metric.quality is None for metric in missing)

    s12_learning._learn_channel(CHANNEL)

    slots = {row["key"]: row for row in s12_learning.lessons(CHANNEL, kind="slot")}
    assert set(slots) == {"2", "3"}
    for hour, observed, score in [("2", 0.0, 0.0364), ("3", 0.1, 0.0636)]:
        row = slots[hour]
        assert row["sample_size"] == 3
        assert row["confidence"] == pytest.approx(3 / 11)
        assert row["score"] == score
        assert row["value"] == {
            "hour": int(hour), "n": 3, "quality": score,
            "observed_quality": observed, "baseline": 0.05,
        }
    fleet = s8_analytics.fleet_summary(14)
    assert (fleet["posts"], fleet["measured"], fleet["unmeasured_posts"]) == (10, 9, 1)


def test_s12_all_missing_persisted_scores_create_no_slot_lesson(temporary_database):
    for i in range(3):
        _publish(f"missing-{i}", 1, {"impressions": 100, "opens": -1})

    s12_learning._learn_channel(CHANNEL)

    assert s12_learning.lessons(CHANNEL, kind="slot") == []
    assert s12_learning.best_hours(CHANNEL) == []


def test_s12_direct_unmeasured_quality_cannot_supply_hourly_evidence(temporary_database, monkeypatch):
    published = datetime.now(UTC) - timedelta(days=2)
    metrics = [
        PostMetrics(
            post_public_id=f"direct-{hour}-{i}", channel_id=CHANNEL, angle="news",
            published_at=published.replace(hour=hour), measured=measured, views=30,
        )
        for hour, measured in [(1, False), (2, True)]
        for i in range(3)
    ]
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: metrics)

    s12_learning._learn_channel(CHANNEL)

    slots = s12_learning.lessons(CHANNEL, kind="slot")
    assert [row["key"] for row in slots] == ["2"]
    assert slots[0]["sample_size"] == 3
    assert slots[0]["confidence"] == pytest.approx(3 / 11)
    assert slots[0]["score"] == 0.0


def test_s12_direct_none_and_real_quality_mix_excludes_none_from_mean(temporary_database, monkeypatch):
    """DEFECT 2: a mix of None and real qualities must not raise TypeError when
    summing qualified_scores, and None must be excluded from the baseline mean —
    three real 0.1 scores average to 0.1, never a fabricated 0.05 from None==0."""
    published = datetime.now(UTC) - timedelta(days=2)
    metrics = [
        PostMetrics(
            post_public_id=f"none-2-{i}", channel_id=CHANNEL, angle="news",
            published_at=published.replace(hour=2), measured=True,
            views=30, impressions=100, opens=-1,
        )
        for i in range(3)
    ] + [
        PostMetrics(
            post_public_id=f"real-3-{i}", channel_id=CHANNEL, angle="news",
            published_at=published.replace(hour=3), measured=True,
            views=30, reactions=3,
        )
        for i in range(3)
    ]
    assert all(metric.quality is None for metric in metrics[:3])
    assert all(metric.quality == 0.1 for metric in metrics[3:])
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: metrics)

    s12_learning._learn_channel(CHANNEL)  # must not raise TypeError on sum(None)

    slots = {row["key"]: row for row in s12_learning.lessons(CHANNEL, kind="slot")}
    assert set(slots) == {"3"}  # the all-None hour supplies no evidence
    assert slots["3"]["sample_size"] == 3
    assert slots["3"]["value"]["observed_quality"] == 0.1
    assert slots["3"]["value"]["baseline"] == 0.1  # None excluded, not halved to 0.05


def _portfolio_metric(public_id, kind, days_ago=2):
    counts = {
        "missing": {"impressions": 100, "opens": -1},
        "zero": {"impressions": 100},
        "positive": {"views": 30, "reactions": 3},
    }[kind]
    return PostMetrics(
        post_public_id=public_id, channel_id=CHANNEL, angle=kind, hook_style="fact",
        published_at=NOW - timedelta(days=days_ago), measured=True, **counts,
    )


def test_portfolio_missing_quality_does_not_halve_valid_baseline():
    metrics = [_portfolio_metric("missing", "missing"), _portfolio_metric("valid", "positive")]
    report = build_portfolio_report(metrics, now=NOW)
    channel = report["channels"][0]
    assert channel["baseline_quality"] == 0.1
    assert report["measured_posts"] == channel["measured_posts"] == 2
    assert report["scored_posts"] == channel["scored_posts"] == 1
    assert report["unmeasured_posts"] == 0
    assert channel["recommendations"] == []


def test_portfolio_mixed_missing_zero_and_positive_scores_keep_honest_samples():
    metrics = [
        _portfolio_metric(f"{kind}-{i}", kind, days_ago=20 if kind == "missing" else 2)
        for kind in ["missing", "zero", "positive"]
        for i in range(3)
    ]
    formats = {metric.post_public_id: metric.angle for metric in metrics}
    report = build_portfolio_report(metrics, formats, now=NOW)
    channel = report["channels"][0]
    assert channel["baseline_quality"] == 0.05
    assert report["measured_posts"] == channel["measured_posts"] == 9
    assert report["scored_posts"] == channel["scored_posts"] == 6
    assert report["unmeasured_posts"] == report["immature_posts"] == 0
    assert channel["observation_span_days"] == 0.0
    assert channel["recommendations_ready"] is False
    groups = {(row["dimension"], row["key"]): row for row in channel["recommendations"]}
    assert set(groups) == {
        ("angle", "zero"), ("angle", "positive"), ("hook_style", "fact"),
        ("format", "zero"), ("format", "positive"),
    }
    for dimension in ["angle", "format"]:
        for kind, observed, adjusted in [("zero", 0.0, 0.0364), ("positive", 0.1, 0.0636)]:
            row = groups[(dimension, kind)]
            assert row["sample_size"] == 3
            assert row["confidence"] == 0.2727
            assert row["observed_quality"] == observed
            assert row["adjusted_quality"] == adjusted
    assert groups[("hook_style", "fact")]["sample_size"] == 6
    assert groups[("hook_style", "fact")]["confidence"] == 0.4286


def test_portfolio_all_missing_scores_keep_coverage_without_inventing_quality():
    metrics = [_portfolio_metric(f"missing-{i}", "missing", 2 + 15 * i) for i in range(3)]
    report = build_portfolio_report(metrics, now=NOW)
    channel = report["channels"][0]
    assert channel["baseline_quality"] is None
    assert report["measured_posts"] == channel["measured_posts"] == 3
    assert report["scored_posts"] == channel["scored_posts"] == 0
    assert report["unmeasured_posts"] == report["immature_posts"] == 0
    assert channel["observation_span_days"] == 0.0
    assert channel["recommendations_ready"] is False
    assert channel["recommendations"] == []

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tgfarm.s8_analytics.portfolio import build_portfolio_report, estimate_group


def test_estimate_group_shrinks_small_sample_toward_channel_baseline():
    estimate = estimate_group([0.9, 0.9, 0.9], baseline=0.5)
    assert estimate["sample_size"] == 3
    assert estimate["observed_quality"] == 0.9
    assert estimate["adjusted_quality"] == 0.6091
    assert estimate["confidence"] == 0.2727
    assert estimate["decision"] == "test"


def test_estimate_group_classifies_repeated_strong_and_weak_evidence():
    strong = estimate_group([0.8] * 10, baseline=0.4)
    weak = estimate_group([0.1] * 10, baseline=0.4)
    assert strong["decision"] == "promote"
    assert weak["decision"] == "reduce"
    assert strong["lift"] > 0
    assert weak["lift"] < 0


def test_portfolio_ignores_immature_and_unmeasured_posts():
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    metrics = [
        SimpleNamespace(post_public_id="young", channel_id="a", angle="news", hook_style="fact",
                        quality=0.9, views=100, impressions=0, published_at=now - timedelta(hours=2)),
        SimpleNamespace(post_public_id="quiet", channel_id="a", angle="news", hook_style="fact",
                        quality=0.0, views=2, impressions=20, published_at=now - timedelta(days=2)),
        SimpleNamespace(post_public_id="ready", channel_id="a", angle="news", hook_style="fact",
                        quality=0.6, views=100, impressions=0, published_at=now - timedelta(days=2)),
    ]
    report = build_portfolio_report(metrics, now=now)
    assert report["immature_posts"] == 1
    assert report["unmeasured_posts"] == 1
    assert report["measured_posts"] == 1
    assert report["channels"][0]["measured_posts"] == 1
    assert report["channels"][0]["recommendations"] == []


def test_portfolio_compares_groups_within_each_channel_and_reports_format():
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    metrics = []
    formats = {}
    for i in range(10):
        pid = f"strong-{i}"
        formats[pid] = "story"
        metrics.append(SimpleNamespace(
            post_public_id=pid, channel_id="a", angle="story", hook_style="question",
            quality=0.9, views=200, impressions=0, published_at=now - timedelta(days=1 + (15 * i / 9)),
        ))
    for i in range(10):
        pid = f"weak-{i}"
        formats[pid] = "digest"
        metrics.append(SimpleNamespace(
            post_public_id=pid, channel_id="a", angle="digest", hook_style="fact",
            quality=0.2, views=200, impressions=0, published_at=now - timedelta(days=2),
        ))
    for i in range(10):
        pid = f"other-{i}"
        formats[pid] = "digest"
        metrics.append(SimpleNamespace(
            post_public_id=pid, channel_id="b", angle="digest", hook_style="fact",
            quality=0.5, views=200, impressions=0, published_at=now - timedelta(days=2),
        ))

    report = build_portfolio_report(metrics, formats, now=now, channel_titles={"a": "Alpha", "b": "Beta"})
    alpha = next(channel for channel in report["channels"] if channel["channel_id"] == "a")
    beta = next(channel for channel in report["channels"] if channel["channel_id"] == "b")
    alpha_angles = {item["key"]: item for item in alpha["recommendations"] if item["dimension"] == "angle"}
    alpha_formats = {item["key"]: item for item in alpha["recommendations"] if item["dimension"] == "format"}
    beta_angles = {item["key"]: item for item in beta["recommendations"] if item["dimension"] == "angle"}

    assert alpha["title"] == "Alpha"
    assert alpha_angles["story"]["decision"] == "promote"
    assert alpha_angles["digest"]["decision"] == "reduce"
    assert alpha_formats["story"]["decision"] == "promote"
    assert beta_angles["digest"]["decision"] == "hold"
    assert report["measured_posts"] == 30

def test_portfolio_holds_decisions_until_fourteen_days_of_observations():
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    metrics = [
        SimpleNamespace(
            post_public_id=f"recent-{i}",
            channel_id="a",
            angle="story",
            hook_style="question",
            quality=0.9,
            views=200,
            impressions=0,
            published_at=now - timedelta(days=1.5 + i * 0.5),
        )
        for i in range(10)
    ]
    metrics.extend(
        SimpleNamespace(
            post_public_id=f"baseline-{i}",
            channel_id="a",
            angle="digest",
            hook_style="fact",
            quality=0.2,
            views=200,
            impressions=0,
            published_at=now - timedelta(days=1.5 + i * 0.5),
        )
        for i in range(10)
    )

    report = build_portfolio_report(metrics, now=now)
    channel = report["channels"][0]
    angle = next(
        item for item in channel["recommendations"]
        if item["dimension"] == "angle" and item["key"] == "story"
    )

    assert channel["observation_span_days"] == 4.5
    assert channel["recommendations_ready"] is False
    assert angle["proposed_decision"] == "promote"
    assert angle["decision"] == "hold"
    assert angle["actionable"] is False
    assert "14" in angle["reason"]


def test_portfolio_explains_recommendation_and_confidence_semantics_after_two_weeks():
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    metrics = [
        SimpleNamespace(
            post_public_id=f"older-{i}",
            channel_id="a",
            angle="story",
            hook_style="question",
            quality=0.9,
            views=200,
            impressions=0,
            published_at=now - timedelta(days=1 + (15 * i / 9)),
        )
        for i in range(10)
    ]
    metrics.extend(
        SimpleNamespace(
            post_public_id=f"baseline-older-{i}",
            channel_id="a",
            angle="digest",
            hook_style="fact",
            quality=0.2,
            views=200,
            impressions=0,
            published_at=now - timedelta(days=1 + (15 * i / 9)),
        )
        for i in range(10)
    )

    report = build_portfolio_report(metrics, now=now)
    channel = report["channels"][0]
    angle = next(
        item for item in channel["recommendations"]
        if item["dimension"] == "angle" and item["key"] == "story"
    )

    assert channel["recommendations_ready"] is True
    assert angle["decision"] == "promote"
    assert angle["actionable"] is True
    assert "выборка 10 постов" in angle["reason"]
    assert angle["confidence_kind"] == "shrinkage_weight_not_probability"
    assert "rather than a probability" in report["confidence_definition"]

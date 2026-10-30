from __future__ import annotations

from tgfarm.s9_growth.reddit_metrics import (
    _aggregate_communities,
    _community_from_url,
    _post_id,
)


def test_extract_reddit_community_and_post_ids_from_permalink():
    url = "https://www.reddit.com/r/Python/comments/abc123/helpful_title/"
    assert _community_from_url(url) == "python"
    assert _post_id(None, url) == "t3_abc123"
    assert _post_id("abc123", url) == "t3_abc123"
    assert _post_id("t3_abc123", url) == "t3_abc123"


def test_performance_aggregation_uses_real_metrics_and_shows_sample_size():
    records = [
        {
            "subreddit": "python",
            "score": 40,
            "comments": 10,
            "upvote_ratio": 0.9,
            "collected_at": "2026-10-28T10:00:00+00:00",
        },
        {
            "subreddit": "python",
            "score": 20,
            "comments": 4,
            "upvote_ratio": 0.8,
            "collected_at": "2026-10-28T11:00:00+00:00",
        },
        {
            "subreddit": "learnpython",
            "score": None,
            "comments": None,
            "upvote_ratio": None,
            "collected_at": None,
        },
    ]
    communities = {
        item["subreddit"]: item for item in _aggregate_communities(records)
    }

    python = communities["python"]
    assert python["published_posts"] == 2
    assert python["metric_samples"] == 2
    assert python["avg_score"] == 30
    assert python["avg_comments"] == 7
    assert python["avg_upvote_ratio"] == 0.85
    assert python["small_sample"] is True
    assert -5 <= python["learning_bonus"] <= 15

    low_data = communities["learnpython"]
    assert low_data["metric_samples"] == 0
    assert low_data["avg_score"] is None
    assert low_data["small_sample"] is True
    assert 0 <= low_data["learning_bonus"] <= 3


def test_extreme_performance_does_not_overpower_topic_match():
    rows = [
        {
            "subreddit": "popular",
            "score": 1_000_000,
            "comments": 1_000_000,
            "upvote_ratio": 1.0,
        },
        {
            "subreddit": "quiet",
            "score": -100,
            "comments": 0,
            "upvote_ratio": 0.0,
        },
    ]
    communities = {item["subreddit"]: item for item in _aggregate_communities(rows)}
    assert communities["popular"]["learning_bonus"] <= 15
    assert communities["quiet"]["learning_bonus"] >= -5

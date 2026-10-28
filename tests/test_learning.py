"""Tests for confidence-aware strategy updates."""
from __future__ import annotations

import pytest

from tgfarm.s12_learning import (
    PRIOR_STRENGTH,
    _confidence_weighted_qualities,
    _sample_confidence,
    _shrink_quality,
)


def test_small_samples_are_shrunk_more_than_large_samples():
    baseline = 0.5
    small = _shrink_quality(0.95, baseline, 3)
    large = _shrink_quality(0.95, baseline, 30)

    assert baseline < small < large < 0.95
    assert abs(small - baseline) < abs(large - baseline)


def test_quality_estimates_use_sample_weighted_baseline_and_ignore_tiny_groups():
    baseline, evidence = _confidence_weighted_qualities(
        {
            "lucky": {"n": 3, "quality": 0.99},
            "stable": {"n": 30, "quality": 0.60},
            "too_small": {"n": 2, "quality": 1.0},
            "invalid": {"n": 20, "quality": float("nan")},
        }
    )

    assert set(evidence) == {"lucky", "stable"}
    assert 0.60 < baseline < 0.99
    assert evidence["lucky"]["quality"] < evidence["lucky"]["observed_quality"]
    assert evidence["stable"]["quality"] > evidence["stable"]["observed_quality"]
    assert evidence["lucky"]["n"] == 3


def test_sample_confidence_increases_with_evidence():
    assert _sample_confidence(0) == 0.0
    assert _sample_confidence(3) == pytest.approx(3 / (3 + PRIOR_STRENGTH))
    assert _sample_confidence(30) > _sample_confidence(3)


def test_strategy_is_not_replaced_by_below_threshold_samples(monkeypatch):
    from tgfarm import s12_learning

    writes = []

    def capture(channel_id, kind, key, value, score, sample_size, confidence, note=None):
        writes.append(
            {
                "channel_id": channel_id,
                "kind": kind,
                "key": key,
                "value": value,
                "score": score,
                "sample_size": sample_size,
                "confidence": confidence,
                "note": note,
            }
        )

    monkeypatch.setattr(
        s12_learning,
        "angle_performance",
        lambda days, channel_id=None: {"lucky": {"n": 2, "quality": 0.99}},
    )
    monkeypatch.setattr(
        s12_learning,
        "hook_style_performance",
        lambda days, channel_id=None: {"fact": {"n": 2, "quality": 0.99}},
    )
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: [])
    monkeypatch.setattr(s12_learning, "_upsert", capture)

    s12_learning._learn_channel("learning-test-channel")

    assert not any(row["kind"] == "strategy" for row in writes)
    assert not any(row["kind"] == "angle" for row in writes)


def test_learning_records_observed_and_shrunk_quality(monkeypatch):
    from tgfarm import s12_learning

    writes = []

    def capture(channel_id, kind, key, value, score, sample_size, confidence, note=None):
        writes.append(
            {
                "channel_id": channel_id,
                "kind": kind,
                "key": key,
                "value": value,
                "score": score,
                "sample_size": sample_size,
                "confidence": confidence,
                "note": note,
            }
        )

    monkeypatch.setattr(
        s12_learning,
        "angle_performance",
        lambda days, channel_id=None: {
            "lucky": {"n": 3, "quality": 0.99, "er": 0.05, "ctr": 0.12},
            "stable": {"n": 30, "quality": 0.60, "er": 0.04, "ctr": 0.10},
        },
    )
    monkeypatch.setattr(s12_learning, "hook_style_performance", lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: [])
    monkeypatch.setattr(s12_learning, "_upsert", capture)

    s12_learning._learn_channel("learning-test-channel")

    lucky = next(row for row in writes if row["kind"] == "angle" and row["key"] == "lucky")
    assert lucky["value"]["observed_quality"] == pytest.approx(0.99)
    assert lucky["value"]["quality"] < lucky["value"]["observed_quality"]
    assert lucky["confidence"] == pytest.approx(_sample_confidence(3))
    strategy = next(row for row in writes if row["kind"] == "strategy" and row["key"] == "angle_weights")
    assert strategy["sample_size"] == 33
    assert sum(strategy["value"].values()) == pytest.approx(1.0, abs=0.002)

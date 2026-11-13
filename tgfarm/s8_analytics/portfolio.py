"""Portfolio intelligence: confidence-aware recommendations, never auto-apply changes.

The report compares formats, angles and hook styles *within each channel*.
Only mature posts with meaningful distribution data are scored. Estimates are
shrunk toward the channel baseline, so a tiny lucky sample cannot dominate.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Iterable

PRIOR_STRENGTH = 8.0
MIN_GROUP_SAMPLES = 3
MIN_VIEWS = 30
MIN_IMPRESSIONS = 100
DEFAULT_MIN_AGE_HOURS = 24
MIN_OBSERVATION_DAYS = 14


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _quality(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))


def estimate_group(scores: Iterable[float], baseline: float) -> dict:
    """Estimate a group's quality with an eight-post prior from its channel."""
    values = [_quality(value) for value in scores]
    n = len(values)
    base = _quality(baseline)
    observed = sum(values) / n if n else base
    confidence = n / (n + PRIOR_STRENGTH) if n else 0.0
    adjusted = (observed * n + base * PRIOR_STRENGTH) / (n + PRIOR_STRENGTH) if n else base
    lift = adjusted - base
    if n >= 8 and lift >= 0.025:
        decision = "promote"
    elif n >= 8 and lift <= -0.025:
        decision = "reduce"
    elif n >= MIN_GROUP_SAMPLES and abs(lift) >= 0.015:
        decision = "test"
    else:
        decision = "hold"
    return {
        "sample_size": n,
        "observed_quality": round(observed, 4),
        "adjusted_quality": round(adjusted, 4),
        "baseline_quality": round(base, 4),
        "lift": round(lift, 4),
        "confidence": round(confidence, 4),
        "decision": decision,
    }


def build_portfolio_report(
    metrics: Iterable,
    formats: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
    days: int = 30,
    min_age_hours: int = DEFAULT_MIN_AGE_HOURS,
    channel_titles: dict[str, str] | None = None,
) -> dict:
    """Pure aggregation helper, kept independent from the database for testing."""
    current = _utc(now or datetime.now(UTC))
    assert current is not None
    formats = formats or {}
    channel_titles = channel_titles or {}
    source = list(metrics)
    mature = []
    immature = 0
    unmeasured = 0
    min_age = max(0, int(min_age_hours))

    for metric in source:
        published = _utc(getattr(metric, "published_at", None))
        if published is None or (current - published) < timedelta(hours=min_age):
            immature += 1
            continue
        impressions = max(0, int(getattr(metric, "impressions", 0) or 0))
        views = max(0, int(getattr(metric, "views", 0) or 0))
        if impressions < MIN_IMPRESSIONS and views < MIN_VIEWS:
            unmeasured += 1
            continue
        mature.append(metric)

    by_channel: dict[str, list] = defaultdict(list)
    for metric in mature:
        by_channel[str(getattr(metric, "channel_id", "") or "unknown")].append(metric)

    channel_reports = []
    for channel_id, items in by_channel.items():
        # Keep distribution coverage separate from the defined quality evidence.
        scored = [item for item in items if getattr(item, "quality", None) is not None]
        baseline = sum(_quality(item.quality) for item in scored) / len(scored) if scored else None
        observation_times = [
            published
            for item in scored
            if (published := _utc(getattr(item, "published_at", None))) is not None
        ]
        observation_span_days = (
            max(0.0, (max(observation_times) - min(observation_times)).total_seconds() / 86400)
            if observation_times else 0.0
        )
        recommendations_ready = observation_span_days >= MIN_OBSERVATION_DAYS
        readiness_reason = (
            "Достаточный период наблюдений для предварительных рекомендаций."
            if recommendations_ready
            else (
                f"Рано менять стратегию: наблюдения охватывают "
                f"{observation_span_days:.1f} из {MIN_OBSERVATION_DAYS} требуемых дней."
            )
        )
        dimensions = {
            "angle": lambda item: str(getattr(item, "angle", "") or "unknown"),
            "hook_style": lambda item: str(getattr(item, "hook_style", "") or ""),
            "format": lambda item: str(formats.get(str(getattr(item, "post_public_id", "")), "unknown") or "unknown"),
        }
        recommendations = []
        for dimension, key_fn in dimensions.items():
            groups: dict[str, list[float]] = defaultdict(list)
            for item in scored:
                key = key_fn(item)
                if key:
                    groups[key].append(_quality(item.quality))
            for key, scores in groups.items():
                if len(scores) < MIN_GROUP_SAMPLES:
                    continue
                estimate = estimate_group(scores, baseline)
                proposed_decision = estimate["decision"]
                if not recommendations_ready:
                    estimate["proposed_decision"] = proposed_decision
                    estimate["decision"] = "hold"
                    estimate["actionable"] = False
                    estimate["reason"] = readiness_reason
                else:
                    estimate["actionable"] = proposed_decision in {"promote", "reduce", "test"}
                    lift_pct = estimate["lift"] * 100
                    if proposed_decision == "promote":
                        estimate["reason"] = (
                            f"Скорректированное качество выше базы на {lift_pct:.1f} п.п.; "
                            f"выборка {estimate['sample_size']} постов."
                        )
                    elif proposed_decision == "reduce":
                        estimate["reason"] = (
                            f"Скорректированное качество ниже базы на {abs(lift_pct):.1f} п.п.; "
                            f"выборка {estimate['sample_size']} постов."
                        )
                    elif proposed_decision == "test":
                        estimate["reason"] = (
                            f"Есть предварительный отрыв {lift_pct:+.1f} п.п., "
                            f"но выборки недостаточно для сильного вывода."
                        )
                    else:
                        estimate["reason"] = (
                            f"Отрыв {lift_pct:+.1f} п.п. пока меньше порога принятия решения."
                        )
                estimate["confidence_kind"] = "shrinkage_weight_not_probability"
                estimate.update({"dimension": dimension, "key": key})
                recommendations.append(estimate)

        priority = {"promote": 0, "reduce": 1, "test": 2, "hold": 3}
        recommendations.sort(
            key=lambda item: (priority[item["decision"]], -abs(item["lift"]), item["dimension"], item["key"])
        )
        channel_reports.append({
            "channel_id": channel_id,
            "title": channel_titles.get(channel_id, channel_id),
            "measured_posts": len(items),
            "scored_posts": len(scored),
            "baseline_quality": round(baseline, 4) if baseline is not None else None,
            "observation_span_days": round(observation_span_days, 1),
            "minimum_observation_days": MIN_OBSERVATION_DAYS,
            "recommendations_ready": recommendations_ready,
            "readiness_reason": readiness_reason,
            "recommendations": recommendations[:12],
        })

    channel_reports.sort(key=lambda item: (-item["measured_posts"], item["channel_id"]))
    return {
        "window_days": max(1, int(days)),
        "min_age_hours": min_age,
        "min_group_samples": MIN_GROUP_SAMPLES,
        "prior_strength": int(PRIOR_STRENGTH),
        "minimum_observation_days": MIN_OBSERVATION_DAYS,
        "confidence_definition": "sample_size / (sample_size + prior_strength), a shrinkage weight rather than a probability",
        "measured_posts": len(mature),
        "scored_posts": sum(channel["scored_posts"] for channel in channel_reports),
        "immature_posts": immature,
        "unmeasured_posts": unmeasured,
        "channels": channel_reports,
        "policy": "Recommendations only. No schedules, weights, or channel settings are changed automatically.",
    }


def portfolio_report(days: int = 30, min_age_hours: int = DEFAULT_MIN_AGE_HOURS) -> dict:
    """Load recent measurements and expose a conservative, operator-facing report."""
    from sqlalchemy import select

    from tgfarm.config import get_config
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope
    from tgfarm.s8_analytics import recent_metrics

    window = max(1, min(90, int(days)))
    metrics = recent_metrics(window)
    formats: dict[str, str] = {}
    titles: dict[str, str] = {}
    public_ids = [metric.post_public_id for metric in metrics if getattr(metric, "post_public_id", None)]
    if public_ids:
        with session_scope() as session:
            rows = session.execute(
                select(PostRow.public_id, PostRow.post_format).where(PostRow.public_id.in_(public_ids))
            ).all()
        formats = {str(public_id): str(post_format or "unknown") for public_id, post_format in rows}

    try:
        config = get_config()
        for channel_id in {metric.channel_id for metric in metrics}:
            persona = config.persona(channel_id)
            titles[channel_id] = persona.title if persona else channel_id
    except Exception:
        titles = {}

    return build_portfolio_report(
        metrics,
        formats,
        days=window,
        min_age_hours=min_age_hours,
        channel_titles=titles,
    )

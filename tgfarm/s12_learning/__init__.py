"""S12 — self-learning memory for the farm."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.run import to_thread
from tgfarm.db.models import LearningMemory, Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import (
    angle_performance,
    hook_style_performance,
    recent_metrics,
    slot_performance,
)

log = get_logger(__name__)

MIN_SAMPLES = 3
# Treat the equivalent of eight posts as prior evidence. Small samples move
# strategy weights only a little; confidence rises as real observations accrue.
PRIOR_STRENGTH = 8.0


def _clamp_quality(value: float, default: float = 0.5) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(score):
        return default
    return min(1.0, max(0.0, score))


def _shrink_quality(
    score: float,
    baseline: float,
    sample_size: int,
    prior_strength: float = PRIOR_STRENGTH,
) -> float:
    """Shrink a noisy observed score toward its channel baseline.

    The sample size is the weight of the observed data; the prior strength is
    the weight of the baseline. A lucky three-post streak therefore cannot
    outweigh a pattern measured across dozens of posts.
    """
    prior = max(0.0, float(prior_strength))
    n = max(0, int(sample_size))
    base = _clamp_quality(baseline)
    observed = _clamp_quality(score)
    if n == 0:
        return round(base, 4)
    if prior == 0:
        return round(observed, 4)
    return round((observed * n + base * prior) / (n + prior), 4)


def _sample_confidence(sample_size: int) -> float:
    """Confidence as the share of observed evidence after prior smoothing."""
    n = max(0, int(sample_size))
    return round(n / (n + PRIOR_STRENGTH), 4) if n else 0.0


def _confidence_weighted_qualities(performance: dict) -> tuple[float, dict[str, dict]]:
    """Return a sample-weighted baseline and shrinkage estimates for eligible groups."""
    observed: dict[str, tuple[float, int]] = {}
    for key, data in performance.items():
        try:
            n = int(data.get("n", 0))
            score = float(data.get("quality", 0.5))
        except (TypeError, ValueError):
            continue
        if n < MIN_SAMPLES or not math.isfinite(score):
            continue
        observed[str(key)] = (_clamp_quality(score), n)

    total_n = sum(n for _, n in observed.values())
    if total_n == 0:
        return 0.5, {}

    baseline = sum(score * n for score, n in observed.values()) / total_n
    evidence = {
        key: {
            "observed_quality": score,
            "quality": _shrink_quality(score, baseline, n),
            "n": n,
        }
        for key, (score, n) in observed.items()
    }
    return round(baseline, 4), evidence


def _upsert(channel_id, kind, key, value, score, sample_size, confidence, note=None):
    with session_scope() as session:
        row = session.execute(
            select(LearningMemory).where(
                LearningMemory.channel_id == channel_id,
                LearningMemory.kind == kind,
                LearningMemory.key == key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = LearningMemory(
                channel_id=channel_id, kind=kind, key=key, value=value,
                score=score, sample_size=sample_size, confidence=confidence, note=note,
            )
            session.add(row)
        else:
            row.value = value
            row.score = score
            row.sample_size = sample_size
            row.confidence = confidence
            row.note = note


def lessons(channel_id="", kind=None):
    with session_scope() as session:
        stmt = select(LearningMemory)
        if channel_id:
            stmt = stmt.where(LearningMemory.channel_id == channel_id)
        if kind:
            stmt = stmt.where(LearningMemory.kind == kind)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "channel_id": r.channel_id,
                "kind": r.kind,
                "key": r.key,
                "value": r.value or {},
                "score": r.score,
                "sample_size": r.sample_size,
                "confidence": r.confidence,
                "note": r.note,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


def best_angle_weights(channel_id):
    rows = lessons(channel_id, kind="strategy")
    for r in rows:
        if r["key"] == "angle_weights" and isinstance(r["value"], dict):
            return {str(k): float(v) for k, v in r["value"].items()}
    return None


def best_hook_style_weights(channel_id):
    rows = lessons(channel_id, kind="strategy")
    for r in rows:
        if r["key"] == "hook_style_weights" and isinstance(r["value"], dict):
            return {str(k): float(v) for k, v in r["value"].items()}
    return None


def record_gate_failure(channel_id: str, gate: str, reason: str) -> None:
    """A rejected draft is a lesson: the most frequent gate failures go back
    into the generation prompt as explicit "do not" notes."""
    if not channel_id:
        return
    _upsert(
        channel_id,
        "mistake",
        f"gate:{gate}",
        {"gate": gate, "reason": reason[:300]},
        0.0,
        1,
        0.3,
        f"гейт {gate} отклоняет посты: {reason[:120]}",
    )


def best_hours(channel_id):
    rows = lessons(channel_id, kind="slot")
    out = sorted(rows, key=lambda r: r["score"], reverse=True)
    return [int(r["key"]) for r in out if r["sample_size"] >= MIN_SAMPLES][:5]


def mistakes(channel_id):
    return lessons(channel_id, kind="mistake")
def _channel_ids():
    from tgfarm.config import get_config
    return [p.id for p in get_config().enabled_personas]


def _learn_channel(channel_id):
    learned = 0
    perf = angle_performance(14, channel_id=channel_id)
    if perf:
        baseline, evidence = _confidence_weighted_qualities(perf)
        for angle, data in perf.items():
            lesson = evidence.get(angle)
            if lesson is None:
                continue
            n = lesson["n"]
            score = lesson["quality"]
            observed = lesson["observed_quality"]
            _upsert(
                channel_id, "angle", angle,
                {
                    "er": data.get("er"),
                    "ctr": data.get("ctr"),
                    "quality": score,
                    "observed_quality": observed,
                    "baseline": baseline,
                    "n": n,
                },
                score, n, _sample_confidence(n),
                "angle %s: learned=%.3f observed=%.3f baseline=%.3f n=%d"
                % (angle, score, observed, baseline, n),
            )
            learned += 1

        # Do not overwrite a previously learned strategy until at least one
        # angle has enough observations to clear the sample-size threshold.
        qualities = {angle: item["quality"] for angle, item in evidence.items()}
        if qualities:
            try:
                from tgfarm.config import get_config
                persona = get_config().persona(channel_id)
                base = dict(persona.angle_weights or {"news": 1.0}) if persona else {"news": 1.0}
            except Exception:
                base = {"news": 1.0}

            adjusted = {}
            for angle, weight in base.items():
                q = qualities.get(angle, baseline)
                factor = 1.0 + (q - baseline)
                adjusted[angle] = max(0.05, round(float(weight) * factor, 4))
            for angle, q in qualities.items():
                if angle not in adjusted:
                    adjusted[angle] = max(0.05, round(q, 4))
            total = sum(adjusted.values()) or 1.0
            adjusted = {key: round(value / total, 4) for key, value in adjusted.items()}
            best = max(qualities, key=lambda angle: qualities[angle])
            worst = min(qualities, key=lambda angle: qualities[angle])
            sample_total = sum(item["n"] for item in evidence.values())
            _upsert(
                channel_id, "strategy", "angle_weights", adjusted,
                max(qualities.values()), sample_total, _sample_confidence(sample_total),
                "baseline=%.3f best=%s worst=%s" % (baseline, best, worst),
            )
            learned += 1
            _upsert(
                channel_id, "tactic", "best_angle", {"angle": best}, qualities[best],
                evidence[best]["n"], _sample_confidence(evidence[best]["n"]),
                "best angle: %s" % best,
            )
            if worst != best:
                _upsert(
                    channel_id, "mistake", "worst_angle",
                    {"angle": worst, "quality": qualities[worst]}, qualities[worst],
                    evidence[worst]["n"], _sample_confidence(evidence[worst]["n"]),
                    "weak angle: %s" % worst,
                )
                learned += 1

    style_perf = hook_style_performance(14, channel_id=channel_id)
    if style_perf:
        style_baseline, style_evidence = _confidence_weighted_qualities(style_perf)
        style_qualities = {
            style: item["quality"] for style, item in style_evidence.items()
        }
        if style_qualities:
            base_styles = {"fact": 0.55, "stat": 0.30, "question": 0.15}
            adjusted_styles: dict[str, float] = {}
            for style, weight in base_styles.items():
                q = style_qualities.get(style, style_baseline)
                factor = 1.0 + (q - style_baseline)
                adjusted_styles[style] = max(0.05, round(float(weight) * factor, 4))
            for style, q in style_qualities.items():
                if style not in adjusted_styles:
                    adjusted_styles[style] = max(0.05, round(q, 4))
            style_total = sum(adjusted_styles.values()) or 1.0
            adjusted_styles = {
                key: round(value / style_total, 4) for key, value in adjusted_styles.items()
            }
            best_style = max(style_qualities, key=lambda style: style_qualities[style])
            style_sample_total = sum(item["n"] for item in style_evidence.values())
            _upsert(
                channel_id, "strategy", "hook_style_weights", adjusted_styles,
                max(style_qualities.values()), style_sample_total,
                _sample_confidence(style_sample_total),
                "baseline=%.3f best hook style=%s" % (style_baseline, best_style),
            )
            learned += 1

    from tgfarm.core.run import ensure_utc
    channel_metrics = [m for m in recent_metrics(14) if m.channel_id == channel_id]
    buckets = defaultdict(list)
    qualified_scores = []
    for metric in channel_metrics:
        published = ensure_utc(metric.published_at)
        if published and (metric.impressions >= 100 or metric.views >= 30):
            buckets[published.hour].append(metric.quality)
            qualified_scores.append(metric.quality)
    slot_baseline = (
        sum(qualified_scores) / len(qualified_scores) if qualified_scores else 0.5
    )
    for hour, values in buckets.items():
        if len(values) < MIN_SAMPLES:
            continue
        observed = sum(values) / len(values)
        score = _shrink_quality(observed, slot_baseline, len(values))
        _upsert(
            channel_id, "slot", str(hour),
            {
                "hour": hour,
                "n": len(values),
                "quality": score,
                "observed_quality": round(observed, 4),
                "baseline": round(slot_baseline, 4),
            },
            score, len(values), _sample_confidence(len(values)),
            "slot %d: learned=%.3f observed=%.3f baseline=%.3f n=%d"
            % (hour, score, observed, slot_baseline, len(values)),
        )
        learned += 1

    try:
        from tgfarm.db.models import CrosspostLog, PipelineRun
        with session_scope() as session:
            runs = session.execute(
                select(PipelineRun).where(PipelineRun.status == "error").order_by(PipelineRun.created_at.desc()).limit(20)
            ).scalars().all()
            for r in runs:
                if r.error:
                    _upsert(channel_id, "mistake", "pipeline:%s" % r.stage,
                            {"stage": r.stage, "error": r.error[:300]}, 0.0, 1, 0.2,
                            "stage %s failed: %s" % (r.stage, r.error[:120]))
            logs = session.execute(
                select(CrosspostLog).where(CrosspostLog.status == "failed").order_by(CrosspostLog.created_at.desc()).limit(20)
            ).scalars().all()
            for lg in logs:
                if lg.error:
                    _upsert(channel_id, "mistake", "crosspost:%s" % lg.platform,
                            {"platform": lg.platform, "error": lg.error[:300]}, 0.0, 1, 0.2,
                            "crosspost %s failed: %s" % (lg.platform, lg.error[:120]))
    except Exception as exc:
        log.debug("mistake collection failed: %s", exc)

    return {"lessons": learned}


async def run():
    total = 0
    for cid in _channel_ids():
        try:
            res = await to_thread(_learn_channel, cid)
            total += int(res.get("lessons", 0))
        except Exception as exc:
            log.warning("s12: learning failed for %s: %s", cid, exc)
    log.info("s12: %d lessons updated across %d channels", total, len(_channel_ids()))
    return {"lessons": total, "channels": len(_channel_ids())}


__all__ = [
    "run",
    "lessons",
    "best_angle_weights",
    "best_hook_style_weights",
    "best_hours",
    "mistakes",
]


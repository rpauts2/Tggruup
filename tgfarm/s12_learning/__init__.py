"""S12 — self-learning memory for the farm."""

from __future__ import annotations

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
        for angle, data in perf.items():
            n = int(data.get("n", 0))
            if n < MIN_SAMPLES:
                continue
            score = float(data.get("quality", 0.0))
            _upsert(
                channel_id, "angle", angle,
                {"er": data.get("er"), "ctr": data.get("ctr"), "quality": score, "n": n},
                score, n, min(1.0, n / 20.0),
                "angle %s: quality=%.3f n=%d" % (angle, score, n),
            )
            learned += 1
        try:
            from tgfarm.config import get_config
            persona = get_config().persona(channel_id)
            base = dict(persona.angle_weights or {"news": 1.0}) if persona else {"news": 1.0}
        except Exception:
            base = {"news": 1.0}
        qualities = {a: float(d.get("quality", 0.0)) for a, d in perf.items() if int(d.get("n", 0)) >= MIN_SAMPLES}
        avg_q = (sum(qualities.values()) / len(qualities)) if qualities else 0.0
        adjusted = {}
        for angle, w in base.items():
            q = qualities.get(angle, avg_q or 0.5)
            factor = 1.0 + (q - (avg_q or 0.5))
            adjusted[angle] = max(0.05, round(float(w) * factor, 4))
        for angle, q in qualities.items():
            if angle not in adjusted:
                adjusted[angle] = max(0.05, round(q, 4))
        total = sum(adjusted.values()) or 1.0
        adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}
        best = max(qualities, key=lambda a: qualities[a]) if qualities else None
        worst = min(qualities, key=lambda a: qualities[a]) if qualities else None
        _upsert(
            channel_id, "strategy", "angle_weights", adjusted,
            (max(qualities.values()) if qualities else 0.0),
            sum(int(d.get("n", 0)) for d in perf.values()),
            min(1.0, sum(int(d.get("n", 0)) for d in perf.values()) / 30.0),
            "best=%s worst=%s" % (best, worst),
        )
        learned += 1
        if best:
            _upsert(channel_id, "tactic", "best_angle", {"angle": best}, qualities.get(best, 0.0),
                    perf[best].get("n", 0), min(1.0, perf[best].get("n", 0) / 20.0),
                    "best angle: %s" % best)
        if worst and worst != best:
            _upsert(channel_id, "mistake", "worst_angle",
                    {"angle": worst, "quality": qualities.get(worst, 0.0)}, qualities.get(worst, 0.0),
                    perf[worst].get("n", 0), min(1.0, perf[worst].get("n", 0) / 20.0),
                    "weak angle: %s" % worst)
            learned += 1

    style_perf = hook_style_performance(14, channel_id=channel_id)
    if style_perf:
        base_styles = {"fact": 0.55, "stat": 0.30, "question": 0.15}
        style_qualities = {
            s: float(d.get("quality", 0.0))
            for s, d in style_perf.items()
            if int(d.get("n", 0)) >= MIN_SAMPLES
        }
        avg_style = (
            sum(style_qualities.values()) / len(style_qualities) if style_qualities else 0.0
        )
        adjusted_styles: dict[str, float] = {}
        for style, weight in base_styles.items():
            q = style_qualities.get(style, avg_style or 0.5)
            adjusted_styles[style] = max(0.05, round(float(weight) * (1.0 + (q - (avg_style or 0.5))), 4))
        for style, q in style_qualities.items():
            if style not in adjusted_styles:
                adjusted_styles[style] = max(0.05, round(q, 4))
        style_total = sum(adjusted_styles.values()) or 1.0
        adjusted_styles = {k: round(v / style_total, 4) for k, v in adjusted_styles.items()}
        best_style = max(style_qualities, key=lambda s: style_qualities[s]) if style_qualities else None
        _upsert(
            channel_id, "strategy", "hook_style_weights", adjusted_styles,
            (max(style_qualities.values()) if style_qualities else 0.0),
            sum(int(d.get("n", 0)) for d in style_perf.values()),
            min(1.0, sum(int(d.get("n", 0)) for d in style_perf.values()) / 30.0),
            "best hook style=%s" % best_style,
        )
        learned += 1

    from tgfarm.core.run import ensure_utc
    channel_metrics = [m for m in recent_metrics(14) if m.channel_id == channel_id]
    buckets = defaultdict(list)
    for m in channel_metrics:
        published = ensure_utc(m.published_at)
        if published and (m.impressions >= 100 or m.views >= 30):
            buckets[published.hour].append(m.quality)
    for hour, vals in buckets.items():
        if len(vals) < MIN_SAMPLES:
            continue
        score = sum(vals) / len(vals)
        _upsert(channel_id, "slot", str(hour), {"hour": hour, "n": len(vals), "quality": score},
                score, len(vals), min(1.0, len(vals) / 10.0),
                "slot %d:00 quality=%.3f n=%d" % (hour, score, len(vals)))
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


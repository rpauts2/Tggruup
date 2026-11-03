"""Paired, non-publishing evaluation of the editorial strategist."""
from __future__ import annotations

import json
import random
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from sqlalchemy import select

from tgfarm.core.settings import DATA_DIR, get_settings
from tgfarm.db.models import AIUsage, Cluster as ClusterRow, RawItem as RawItemRow
from tgfarm.db.session import session_scope

SCHEMA_VERSION = 1
MIN_PAIRS_FOR_SIGNAL = 10
ESTIMATED_CALLS_PER_PAIR = 13
S5_STAGES = {"s5_tone", "s5_relevance", "s5_quality"}
FAILED_CRITIC_STAGE = {"tone": "s5_tone", "relevance": "s5_relevance", "quality": "s5_quality"}


class AgentBenchmarkError(RuntimeError):
    """The requested paired comparison cannot be run safely."""


def _quality_index(report: Any) -> float:
    values = [
        max(0.0, min(1.0, float(value)))
        for value in (getattr(report, "scores", {}) or {}).values()
        if isinstance(value, (int, float))
    ]
    return round(mean(values), 4) if values else 0.0


def _winner(control: dict[str, Any], strategy: dict[str, Any]) -> str:
    if bool(control.get("passed")) != bool(strategy.get("passed")):
        return "strategy" if strategy.get("passed") else "control"
    delta = float(strategy.get("quality_index", 0.0)) - float(control.get("quality_index", 0.0))
    if delta >= 0.015:
        return "strategy"
    if delta <= -0.015:
        return "control"
    return "tie"


def _draft_metrics(draft: Any, report: Any) -> dict[str, Any]:
    claims = list(getattr(draft, "claims", []) or [])
    verified = sum(1 for claim in claims if bool(getattr(claim, "verified", False)))
    return {
        "passed": bool(getattr(report, "passed", False)),
        "failed_gate": getattr(report, "gate", None),
        "quality_index": _quality_index(report),
        "scores": {
            str(key): round(float(value), 4)
            for key, value in (getattr(report, "scores", {}) or {}).items()
            if isinstance(value, (int, float))
        },
        "claim_count": len(claims),
        "verified_claims": verified,
        "verified_ratio": round(verified / len(claims), 4) if claims else None,
        "source_count": len(getattr(draft, "sources", []) or []),
        "editorial_chars": int(getattr(draft, "editorial_len", 0) or 0),
        "writer_provider": str(getattr(draft, "generation_provider", "") or ""),
        "writer_model": str(getattr(draft, "generation_model", "") or ""),
    }


def _usage_snapshot(scope: str) -> dict[str, Any]:
    with session_scope() as session:
        rows = session.execute(
            select(AIUsage).where(AIUsage.cluster_public_id == scope).order_by(AIUsage.id.asc())
        ).scalars().all()
    by_stage: dict[str, dict[str, int]] = {}
    used_rows = [row for row in rows if row.used]
    for row in rows:
        item = by_stage.setdefault(str(row.stage), {
            "calls": 0, "used": 0, "charged": 0, "latency_ms": 0,
        })
        item["calls"] += 1
        item["used"] += int(bool(row.used))
        item["charged"] += int(bool(row.charged))
        item["latency_ms"] += max(0, int(row.latency_ms or 0))
    return {
        "calls": len(rows),
        "used": len(used_rows),
        "charged": sum(1 for row in rows if row.charged),
        "latency_ms": sum(max(0, int(row.latency_ms or 0)) for row in used_rows),
        "by_stage": by_stage,
        "currency_cost": None,
        "token_cost": None,
    }


def _stage_used(usage: dict[str, Any], stage: str) -> bool:
    return int(((usage.get("by_stage") or {}).get(stage) or {}).get("used", 0)) > 0


def _critic_measurement_complete(report: Any, usage: dict[str, Any]) -> bool:
    stages = usage.get("by_stage") or {}
    if bool(getattr(report, "passed", False)):
        required = ("s5_tone", "s5_relevance", "s5_quality")
    else:
        failed_stage = FAILED_CRITIC_STAGE.get(str(getattr(report, "gate", "") or ""))
        order = ("s5_tone", "s5_relevance", "s5_quality")
        if failed_stage not in order:
            return False
        required = order[: order.index(failed_stage) + 1]
    return all(int((stages.get(stage) or {}).get("used", 0)) > 0 for stage in required)


def _write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA_VERSION:
            records.append(payload)
    return records


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in records if row.get("status") == "completed"]
    winners = [str(row.get("winner") or "tie") for row in valid]
    deltas = [
        float(row["quality_delta"])
        for row in valid
        if isinstance(row.get("quality_delta"), (int, float))
    ]
    controls = [row.get("control") or {} for row in valid]
    strategies = [row.get("strategy") or {} for row in valid]
    control_pass = sum(bool(item.get("passed")) for item in controls)
    strategy_pass = sum(bool(item.get("passed")) for item in strategies)
    factual_pairs = [
        (float(a["verified_ratio"]), float(b["verified_ratio"]))
        for a, b in zip(controls, strategies)
        if isinstance(a.get("verified_ratio"), (int, float))
        and isinstance(b.get("verified_ratio"), (int, float))
    ]
    factual_delta = (
        round(mean(b - a for a, b in factual_pairs), 4) if factual_pairs else None
    )
    count = len(valid)
    strategy_wins = winners.count("strategy")
    control_wins = winners.count("control")
    ties = winners.count("tie")
    quality_delta = round(mean(deltas), 4) if deltas else None
    strategy_pass_rate = round(strategy_pass / count, 4) if count else None
    control_pass_rate = round(control_pass / count, 4) if count else None

    conclusion = "insufficient_sample"
    if count >= MIN_PAIRS_FOR_SIGNAL:
        if (
            strategy_wins / count >= 0.65
            and (quality_delta or 0.0) >= 0.02
            and (strategy_pass_rate or 0.0) >= (control_pass_rate or 0.0)
            and factual_delta is not None
            and factual_delta >= -0.01
        ):
            conclusion = "positive_signal"
        elif (
            control_wins / count >= 0.65
            and (quality_delta or 0.0) <= -0.02
        ):
            conclusion = "negative_signal"
        else:
            conclusion = "inconclusive"

    return {
        "schema_version": SCHEMA_VERSION,
        "pairs_total": len(records),
        "valid_pairs": count,
        "minimum_pairs_for_signal": MIN_PAIRS_FOR_SIGNAL,
        "strategy_wins": strategy_wins,
        "control_wins": control_wins,
        "ties": ties,
        "strategy_pass_rate": strategy_pass_rate,
        "control_pass_rate": control_pass_rate,
        "mean_quality_delta": quality_delta,
        "mean_verified_claim_ratio_delta": factual_delta,
        "conclusion": conclusion,
        "statistical_significance_claimed": False,
    }


def aggregate_benchmarks(directory: Path | None = None) -> dict[str, Any]:
    """Read experiment summaries without exposing post text or local paths."""
    folder = directory or (DATA_DIR / "agent_benchmarks")
    records = _read_records(folder)
    summary = summarize_records(records)
    recent: list[dict[str, Any]] = []
    for row in sorted(records, key=lambda item: str(item.get("started_at") or ""), reverse=True)[:8]:
        control = row.get("control") or {}
        strategy = row.get("strategy") or {}
        control_usage = row.get("control_usage") or {}
        strategy_usage = row.get("strategy_usage") or {}
        recent.append({
            "run_id": str(row.get("run_id") or ""),
            "status": str(row.get("status") or "unknown"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "cluster_public_id": str(row.get("cluster_public_id") or ""),
            "channel_id": str(row.get("channel_id") or ""),
            "angle": str(row.get("angle") or ""),
            "format": str(row.get("format") or ""),
            "winner": str(row.get("winner") or ""),
            "quality_delta": row.get("quality_delta"),
            "control": {
                "passed": control.get("passed"),
                "quality_index": control.get("quality_index"),
                "verified_ratio": control.get("verified_ratio"),
            },
            "strategy": {
                "passed": strategy.get("passed"),
                "quality_index": strategy.get("quality_index"),
                "verified_ratio": strategy.get("verified_ratio"),
            },
            "calls": {
                "control": int(control_usage.get("used") or 0),
                "strategy": int(strategy_usage.get("used") or 0),
            },
            "inconclusive_reasons": list(row.get("inconclusive_reasons") or [])[:3],
        })
    return {**summary, "recent_runs": recent}


async def run_paired_benchmark(
    cluster_public_id: str,
    *,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Compare a baseline writer with the same writer plus editorial strategist."""
    cluster_public_id = str(cluster_public_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", cluster_public_id):
        raise AgentBenchmarkError("Некорректный public ID кластера.")
    settings = get_settings()
    from tgfarm.llm import policy

    if not policy.has_online_provider() or settings.ai_policy == "off":
        raise AgentBenchmarkError("Для парного эксперимента нужен включённый online LLM provider.")
    available = max(0, int(settings.ai_daily_budget) - policy.charged_today())
    if available < ESTIMATED_CALLS_PER_PAIR:
        raise AgentBenchmarkError(
            f"Недостаточно AI-бюджета: доступно {available} вызовов, "
            f"для парного эксперимента нужен резерв {ESTIMATED_CALLS_PER_PAIR}."
        )

    run_id = uuid.uuid4().hex
    usage_root = f"ab{run_id[:24]}"
    control_scope = f"{usage_root}c"
    strategy_scope = f"{usage_root}t"
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cluster_public_id": cluster_public_id,
        "channel_id": channel_id or "",
        "publishing_attempted": False,
        "drafts_persisted": False,
        "usage_scopes": {"control": control_scope, "strategy": strategy_scope},
    }
    artifact = DATA_DIR / "agent_benchmarks" / f"{run_id}.json"
    _write_record(artifact, record)

    try:
        from tgfarm.core.run import to_thread
        from tgfarm.core.schemas import Cluster as ClusterSchema
        from tgfarm.s10_format import plan_for_cluster
        from tgfarm.s3_router import plan_cluster
        from tgfarm.s4_generation import (
            _best_examples,
            _pick_hook_style,
            _to_scored,
            compose_offline_post,
            generate,
        )
        from tgfarm.s5_quality import evaluate

        with session_scope() as session:
            cluster = session.execute(
                select(ClusterRow).where(ClusterRow.public_id == cluster_public_id)
            ).scalar_one_or_none()
            if cluster is None:
                raise AgentBenchmarkError("Кластер не найден.")
            raw_items = list(
                session.execute(
                    select(RawItemRow)
                    .where(RawItemRow.cluster_id == cluster.id)
                    .order_by(RawItemRow.source_weight.desc(), RawItemRow.id.asc())
                ).scalars().all()
            )
            cluster_label = str(cluster.label or "")
            cluster_niche = cluster.niche
            cluster_heat = float(cluster.heat or 0.0)

        if not raw_items:
            raise AgentBenchmarkError("В кластере нет исходных материалов.")
        plans = await plan_cluster(cluster_public_id, rng=random.Random(run_id))
        if channel_id:
            plans = [plan for plan in plans if plan.channel_id == channel_id]
        if not plans:
            raise AgentBenchmarkError("Маршрутизатор не нашёл подходящий включённый канал.")
        plan = plans[0]
        scored_items = _to_scored(raw_items)
        cluster_view = ClusterSchema(
            id=cluster_public_id,
            label=cluster_label,
            items=scored_items,
            niche=cluster_niche,
            heat=cluster_heat,
        )
        format_plan = plan_for_cluster(cluster_view, plan.persona, has_video=False)
        offline = compose_offline_post(
            scored_items, plan.persona.as_dict(), plan.angle, format_plan
        )
        use_ai, reason = policy.should_use_ai(
            "s4_draft",
            angle=plan.angle,
            source_count=len({item.source_id for item in scored_items}),
            offline_chars=len(f"{offline.get('hook', '')} {offline.get('body', '')}"),
            persona=plan.persona,
        )
        if not use_ai:
            raise AgentBenchmarkError(
                f"Текущая AI-политика не выбрала бы online writer для этой темы: {reason}."
            )

        examples = await to_thread(_best_examples, plan.channel_id)
        hook_style = _pick_hook_style(plan.channel_id, random.Random(run_id))
        common = {
            "video": None,
            "holdout_override": False,
            "examples_override": examples,
            "hook_style_override": hook_style,
        }
        control_draft = await generate(
            plan, raw_items, format_plan,
            strategy_enabled=False, usage_scope=control_scope, **common,
        )
        strategy_draft = await generate(
            plan, raw_items, format_plan,
            strategy_enabled=True, usage_scope=strategy_scope, **common,
        )
        control_report = await evaluate(
            control_draft,
            plan.persona,
            generation_provider=control_draft.generation_provider,
            usage_scope=control_scope,
        )
        strategy_report = await evaluate(
            strategy_draft,
            plan.persona,
            generation_provider=strategy_draft.generation_provider,
            usage_scope=strategy_scope,
        )
        control_usage = _usage_snapshot(control_scope)
        strategy_usage = _usage_snapshot(strategy_scope)
        control = _draft_metrics(control_draft, control_report)
        treatment = _draft_metrics(strategy_draft, strategy_report)
        reasons: list[str] = []

        if control_draft.generation_provider == "offline" or strategy_draft.generation_provider == "offline":
            reasons.append("writer used offline fallback")
        if (
            control_draft.generation_provider != strategy_draft.generation_provider
            or control_draft.generation_model != strategy_draft.generation_model
        ):
            reasons.append("writer provider/model differed between arms")
        if (strategy_draft.editorial_strategy or {}).get("status") != "planned":
            reasons.append("editorial strategist did not return a valid plan")
        if not _stage_used(control_usage, "s4_draft") or not _stage_used(control_usage, "s4_verify"):
            reasons.append("control writer or fact-checker was not completed online")
        if not _stage_used(strategy_usage, "s4_draft") or not _stage_used(strategy_usage, "s4_verify"):
            reasons.append("strategy writer or fact-checker was not completed online")
        if not _stage_used(strategy_usage, "s4_strategy"):
            reasons.append("strategy call was not charged as an online use")
        if not _critic_measurement_complete(control_report, control_usage):
            reasons.append("control quality review used an offline fallback")
        if not _critic_measurement_complete(strategy_report, strategy_usage):
            reasons.append("strategy quality review used an offline fallback")

        record.update({
            "status": "completed" if not reasons else "inconclusive",
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "channel_id": plan.channel_id,
            "angle": plan.angle,
            "format": getattr(format_plan, "fmt", ""),
            "shared_hook_style": hook_style,
            "shared_examples_count": len(examples),
            "holdout": False,
            "control": control,
            "strategy": treatment,
            "control_usage": control_usage,
            "strategy_usage": strategy_usage,
            "quality_delta": round(treatment["quality_index"] - control["quality_index"], 4),
            "winner": _winner(control, treatment),
            "inconclusive_reasons": reasons,
            "cost_attribution": (
                "S4 writer/strategist/fact-check and S5 critic calls only; "
                "S3 routing/format-planning calls are outside the usage scopes."
            ),
            "monetary_cost_available": False,
        })
    except Exception as exc:  # noqa: BLE001 - persist a safe failure receipt
        record.update({
            "status": "failed",
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "failure_type": type(exc).__name__,
        })
    _write_record(artifact, record)
    record["artifact_id"] = run_id
    return record

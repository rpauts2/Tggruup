from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tgfarm.agents.benchmark import _winner, summarize_records


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _pair(control_score=0.8, strategy_score=0.9, control_facts=0.95, strategy_facts=0.95):
    control = {"passed": True, "quality_index": control_score, "verified_ratio": control_facts}
    strategy = {"passed": True, "quality_index": strategy_score, "verified_ratio": strategy_facts}
    return {
        "status": "completed",
        "winner": _winner(control, strategy),
        "quality_delta": strategy_score - control_score,
        "control": control,
        "strategy": strategy,
    }


def test_benchmark_prioritizes_hard_quality_gate_over_average_score():
    assert _winner(
        {"passed": False, "quality_index": 0.98},
        {"passed": True, "quality_index": 0.70},
    ) == "strategy"


def test_benchmark_refuses_to_draw_signal_from_a_small_sample():
    result = summarize_records([_pair() for _ in range(9)])
    assert result["valid_pairs"] == 9
    assert result["conclusion"] == "insufficient_sample"
    assert result["statistical_significance_claimed"] is False


def test_benchmark_reports_positive_signal_only_after_ten_consistent_pairs():
    result = summarize_records([_pair() for _ in range(10)])
    assert result["valid_pairs"] == 10
    assert result["strategy_wins"] == 10
    assert result["mean_quality_delta"] == 0.1
    assert result["conclusion"] == "positive_signal"


def test_benchmark_ignores_inconclusive_and_failed_runs():
    records = [_pair() for _ in range(4)]
    records.extend([
        {"status": "inconclusive", "winner": "strategy"},
        {"status": "failed", "winner": "control"},
    ])
    result = summarize_records(records)
    assert result["pairs_total"] == 6
    assert result["valid_pairs"] == 4
    assert result["conclusion"] == "insufficient_sample"


def test_agent_benchmark_cli_supports_run_and_report_modes():
    from tgfarm.cli import build_parser

    run_args = build_parser().parse_args(["agent-benchmark", "--cluster", "cluster-123"])
    report_args = build_parser().parse_args(["agent-benchmark", "--report"])
    assert run_args.func.__name__ == "cmd_agent_benchmark"
    assert run_args.cluster == "cluster-123"
    assert run_args.report is False
    assert report_args.report is True

def test_benchmark_requires_all_critic_stages_reached_before_counting_a_review():
    from types import SimpleNamespace

    from tgfarm.agents.benchmark import _critic_measurement_complete

    tone_only = {"by_stage": {"s5_tone": {"used": 1}}}
    assert _critic_measurement_complete(SimpleNamespace(passed=False, gate="tone"), tone_only)
    assert not _critic_measurement_complete(SimpleNamespace(passed=False, gate="relevance"), tone_only)
    assert not _critic_measurement_complete(SimpleNamespace(passed=False, gate="facts"), tone_only)


def test_aggregate_report_projects_recent_runs_without_text_or_local_paths(tmp_path):
    import json

    from tgfarm.agents.benchmark import aggregate_benchmarks

    (tmp_path / "run-001.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": "run-001",
        "status": "inconclusive",
        "started_at": "2026-11-03T08:00:00+00:00",
        "cluster_public_id": "cluster-001",
        "channel_id": "tech-channel",
        "control": {"passed": True, "quality_index": 0.82},
        "strategy": {"passed": False, "quality_index": 0.71},
        "control_usage": {"used": 5},
        "strategy_usage": {"used": 6},
        "post_text": "MUST NOT BE EXPOSED",
        "artifact": "/private/local/path/run-001.json",
    }), encoding="utf-8")

    report = aggregate_benchmarks(tmp_path)
    assert report["pairs_total"] == 1
    assert report["valid_pairs"] == 0
    assert report["recent_runs"][0]["run_id"] == "run-001"
    assert report["recent_runs"][0]["calls"] == {"control": 5, "strategy": 6}
    assert "post_text" not in repr(report)
    assert "/private/local/path" not in repr(report)


def test_agent_benchmark_dashboard_api_is_authenticated_and_returns_safe_summary(client, monkeypatch):
    from tgfarm.agents import benchmark
    from tgfarm.core.apiauth import HEADER, ensure_token

    sample = {
        "valid_pairs": 0,
        "pairs_total": 1,
        "conclusion": "insufficient_sample",
        "recent_runs": [],
    }
    monkeypatch.setattr(benchmark, "aggregate_benchmarks", lambda: sample)
    assert client.get("/api/agents/benchmark").status_code == 401
    response = client.get("/api/agents/benchmark", headers={HEADER: ensure_token()})
    assert response.status_code == 200
    assert response.json() == sample


def test_agent_benchmark_dashboard_run_calls_paired_runner_only(client, monkeypatch):
    from tgfarm.agents import benchmark
    from tgfarm.core.apiauth import HEADER, ensure_token

    calls = []

    async def fake_run(cluster_public_id, *, channel_id=None):
        calls.append((cluster_public_id, channel_id))
        return {
            "run_id": "benchmark-001",
            "status": "completed",
            "publishing_attempted": False,
            "drafts_persisted": False,
        }

    monkeypatch.setattr(benchmark, "run_paired_benchmark", fake_run)
    assert client.post(
        "/api/agents/benchmark", json={"cluster_public_id": "cluster-001"}
    ).status_code == 401
    response = client.post(
        "/api/agents/benchmark",
        json={"cluster_public_id": "cluster-001"},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 200
    assert response.json()["publishing_attempted"] is False
    assert calls == [("cluster-001", None)]

from __future__ import annotations

from tgfarm.agents.benchmark import _winner, summarize_records


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

"""Run report renderers against absent quality and genuine numeric zeroes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "tgfarm" / "ui" / "app.js"
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const report = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
function slice(start, end) {
  const begin = src.indexOf(start);
  if (begin < 0) throw new Error("missing start: " + start);
  const finish = src.indexOf(end, begin);
  if (finish < 0) throw new Error("missing end: " + end);
  return src.slice(begin, finish);
}
const ids = ["#report-chart", "#report-breakdown", "#portfolio-insights", "#portfolio-meta"];
const nodes = Object.fromEntries(ids.map(id => [id, {innerHTML: "", textContent: ""}]));
const document = {querySelector: id => nodes[id] || null};
const body = "const state = {bestHour: null};\n"
  + slice("  const $ = (sel, root = document)", "  // The admin API can force-publish")
  + slice("  function renderReport(report)", "  function renderMemory(data)")
  + slice("  function experimentsBlock(report)", "  function renderAIUsage(data)")
  + "\nrenderReport(report); return state.bestHour;";
const bestHour = new Function("document", "report", body)(document, report);
process.stdout.write(JSON.stringify({
  chart: nodes["#report-chart"].innerHTML,
  breakdown: nodes["#report-breakdown"].innerHTML,
  bestHour,
}));
"""


def _render(tmp_path: Path, **report) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable: report rendering cannot be exercised")
    harness = tmp_path / "report.cjs"
    payload = tmp_path / "report.json"
    harness.write_text(HARNESS, encoding="utf-8")
    payload.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    done = subprocess.run(
        [node, str(harness), str(UI), str(payload)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _bar(chart: str, hour: int) -> tuple[str, str, float]:
    columns = re.findall(
        r'<div class="bar-col ([^"]*)"\s+title="([^"]*)">\s*'
        r'<div class="bar" style="height:([0-9.]+)%">', chart,
    )
    assert len(columns) == 24
    classes, tooltip, height = columns[hour]
    return classes.split(), unescape(tooltip), float(height)


def _text(markup: str) -> str:
    return unescape(re.sub(r"<[^>]*>", " ", markup))


def test_measured_zero_hour_is_visible_evidence_not_an_empty_column(tmp_path):
    rendered = _render(tmp_path, hours={"2": 0.0})
    classes, tooltip, height = _bar(rendered["chart"], 2)
    assert "is-empty" not in classes
    assert "is-best" in classes
    assert tooltip == "2:00 — качество 0.0%"
    assert height > 0
    assert rendered["bestHour"] == 2
    assert "2:00 (0.0%)" in _text(rendered["breakdown"])
    assert _bar(rendered["chart"], 1)[1] == "1:00 — нет данных"


def test_null_hour_does_not_rank_or_claim_a_measurement(tmp_path):
    rendered = _render(tmp_path, hours={"1": None})
    assert rendered["bestHour"] is None
    assert "Нет опубликованных постов с метриками" in rendered["chart"]
    assert "1:00" not in _text(rendered["breakdown"])


def test_mixed_hourly_report_keeps_zero_but_excludes_null(tmp_path):
    rendered = _render(tmp_path, hours={"1": None, "2": 0.0, "3": 0.25})
    assert rendered["bestHour"] == 3
    assert "is-empty" in _bar(rendered["chart"], 1)[0]
    assert "is-empty" not in _bar(rendered["chart"], 2)[0]
    assert _bar(rendered["chart"], 2)[1] == "2:00 — качество 0.0%"
    assert _bar(rendered["chart"], 3)[1] == "3:00 — качество 25.0%"


def test_unscored_angle_cannot_win_a_tie_against_measured_zero(tmp_path):
    rendered = _render(tmp_path, angles={
        "unknown": {"quality": None, "er": None},
        "measured": {"quality": 0.0, "er": None},
    })
    text = _text(rendered["breakdown"])
    assert "measured (0.0%)" in text
    assert "unknown" not in text


def test_only_unknown_angles_leave_no_best_angle(tmp_path):
    rendered = _render(tmp_path, angles={"unknown": {"quality": None, "er": None}})
    assert "unknown" not in _text(rendered["breakdown"])
    assert "0.0%" not in _text(rendered["breakdown"])


def test_angle_er_fallback_and_zero_quality_remain_distinct(tmp_path):
    rendered = _render(tmp_path, angles={
        "zero": {"quality": 0.0, "er": 0.9},
        "fallback": {"quality": None, "er": 0.2},
    })
    assert "fallback (20.0%)" in _text(rendered["breakdown"])
    assert "zero (90.0%)" not in _text(rendered["breakdown"])


def test_holdout_and_hook_styles_admit_unmeasured_quality(tmp_path):
    rendered = _render(tmp_path,
        holdout={"a": {"n": 2, "quality": None}, "holdout": {"n": 1, "quality": None}},
        hook_styles={"unmeasured": {"n": 1, "quality": None}},
    )
    text = _text(rendered["breakdown"])
    assert text.count("нет отчёта") == 4
    assert "0.0%" not in text
    assert "holdout: 1 постов" in text


def test_experiment_quality_zero_is_still_a_number(tmp_path):
    rendered = _render(tmp_path,
        holdout={"a": {"n": 2, "quality": 0.0}, "holdout": {"n": 1, "quality": 0.0}},
        hook_styles={"quiet": {"n": 1, "quality": 0.0}},
    )
    text = _text(rendered["breakdown"])
    assert text.count("0.0%") == 3
    assert "quiet 0.0%" in text


def test_server_angle_and_style_names_are_escaped_once(tmp_path):
    name = '<img src=x onerror="throw 1"> & <script>throw 2</script>'
    rendered = _render(tmp_path,
        angles={name: {"quality": 0.1}}, hook_styles={name: {"quality": None}},
    )
    assert "<img" not in rendered["breakdown"]
    assert "<script" not in rendered["breakdown"]
    assert _text(rendered["breakdown"]).count(name) == 2

"""W26-T: task telemetry must distinguish "cost nothing" from "nobody measured it".

The two incidents this file exists for: an executor that spent 2 164 461
subagent tokens / 42 tool calls / 40 minutes and produced zero commits, and one
that died at 1 726 160 tokens with uncommitted work. Both numbers were written
down nowhere, so an audit marked durations UNKNOWN. Everything below defends one
sentence from directive 8: *unavailable numbers are recorded as unavailable,
never invented.*

* an absent flag becomes JSON `null`, never `0`, and a non-integer is refused
  rather than quietly nulled;
* a rate whose denominator is null — or legitimately zero commits — is null,
  and says which of the two it is;
* records are append-only and hash-chained: a hand edit, a renumbered `seq`, a
  grafted record or a deleted tail line each fail `--verify`;
* the writer refuses to append onto a chain it cannot verify;
* `--summary` prints `not measured`, never a `0`, so a reader cannot mistake
  absence for free work;
* and the tool touches no database, reads no environment and imports no app —
  an import of `tgfarm` here could bootstrap or migrate the live `tgfarm.db`.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import agent_record as ar  # noqa: E402

TOOL = ROOT / "tools" / "agent_record.py"


class Args:
    """A CLI namespace with every nullable flag at None, like argparse defaults."""

    def __init__(self, **overrides) -> None:
        defaults = {
            "task": "W99-X",
            "agent": "agent-fixture",
            "role": None,
            "base_sha": None,
            "branch": None,
            "worktree": None,
            "started_at": None,
            "ended_at": None,
            "duration_s": None,
            "tool_calls": None,
            "subagent_tokens": None,
            "commits": "1",
            "files_touched": None,
            "own_tests_passed": None,
            "defects_found": None,
            "failure_reason": None,
            "state": "DONE",
        }
        for key, value in defaults.items():
            setattr(self, key, overrides.get(key, value))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    target = tmp_path / "telemetry"
    target.mkdir()
    return target


def record(**overrides) -> dict:
    return ar.build_record(Args(**overrides), recorded_by="test-fixture")


# ---------------------------------------------------------------------------
# unknown is not zero
# ---------------------------------------------------------------------------


def test_every_unreported_field_is_null_not_zero() -> None:
    payload = record()
    for field in ar.NULLABLE_FIELDS:
        if field == "commits":
            continue  # countable from git, so it is required, not nullable in practice
        assert payload[field] is None, f"{field} must be null when nobody measured it"
    assert payload["subagent_tokens"] is None
    assert payload["tool_calls"] is None
    assert payload["duration_s"] is None


def test_summary_recomputes_a_stale_derived_block_and_says_so(telemetry_dir: Path) -> None:
    """A record whose stored rates contradict its own fields must not be trusted blindly."""
    file = ar.append_record(telemetry_dir, record(task="W24-A", commits="0", duration_s="2446"))
    record_line = json.loads(file.read_text(encoding="utf-8"))
    record_line["derived"]["duration_s_per_commit"] = 1.0
    file.write_text(json.dumps(record_line, sort_keys=True) + "\n", encoding="utf-8")
    lines = ar.summary_lines(telemetry_dir)
    text = "\n".join(lines)
    assert "DERIVED BLOCK DISAGREES" in text
    assert "s/commit=not measured" in text, "the recomputed null wins over the stored number"


def test_a_record_without_a_commit_count_is_refused() -> None:
    """A null commit count would read as "nobody checked", never as "no commits"."""
    with pytest.raises(ar.RecordError, match="commits"):
        record(commits="null")


def test_a_measured_zero_is_kept_as_zero() -> None:
    """The W24-A fact: 0 commits IS a measurement and must not be softened to null."""
    payload = record(
        commits="0", tool_calls="42", subagent_tokens="2164461", duration_s="2446",
        state="FAILED", failure_reason="harness_error",
    )
    assert payload["commits"] == 0
    assert payload["tool_calls"] == 42
    assert payload["subagent_tokens"] == 2164461


def test_the_literal_word_null_is_accepted_as_null() -> None:
    payload = record(tool_calls="null", duration_s="null", subagent_tokens="null")
    assert payload["tool_calls"] is None
    assert payload["duration_s"] is None
    assert payload["subagent_tokens"] is None


@pytest.mark.parametrize("value", ["42x", "unknown", "many", "-1"])
def test_a_garbage_or_negative_number_is_refused_not_nulled(value: str) -> None:
    """A guessed number is the thing directive 8 forbids; so is a silent downgrade."""
    with pytest.raises(ar.RecordError):
        record(tool_calls=value)


def test_duration_is_derived_from_the_two_stamps_and_only_from_comparable_ones() -> None:
    payload = record(started_at="2026-11-06T10:00:00+00:00", ended_at="2026-11-06T10:40:46+00:00")
    assert payload["duration_s"] == 2446
    with pytest.raises(ar.RecordError):
        record(started_at="2026-11-06T10:00:00", ended_at="2026-11-06T10:40:46+00:00")


# ---------------------------------------------------------------------------
# rates: a null or zero denominator yields null, never 0.0
# ---------------------------------------------------------------------------


def test_rate_with_null_denominator_is_null_and_explained() -> None:
    derived = record(commits="5", duration_s="100")["derived"]
    assert derived["subagent_tokens_per_tool_call"] is None
    assert derived["duration_s_per_commit"] == 20.0
    assert "tool_calls is not measured" in derived["not_computed"]["subagent_tokens_per_tool_call"]


def test_rate_with_measured_zero_denominator_is_null_not_infinity() -> None:
    """42 calls, 0 commits: `s/commit` is undefined, and `0` would read as instant work."""
    derived = record(commits="0", duration_s="2446", tool_calls="42", subagent_tokens="2164461")[
        "derived"
    ]
    assert derived["duration_s_per_commit"] is None
    assert "commits is 0" in derived["not_computed"]["duration_s_per_commit"]
    assert derived["subagent_tokens_per_tool_call"] == 51534.79


def test_no_derived_value_is_ever_zero_by_default() -> None:
    derived = record()["derived"]
    for key, value in derived.items():
        if key == "not_computed":
            continue
        assert value is None, f"{key} invented {value} for an unmeasured task"


# ---------------------------------------------------------------------------
# states and failure reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["FAILED", "BLOCKED", "SUPERSEDED"])
def test_a_failure_state_without_a_reason_is_refused(state: str) -> None:
    with pytest.raises(ar.RecordError, match="failure-reason"):
        record(state=state)


def test_done_with_a_failure_reason_is_refused() -> None:
    with pytest.raises(ar.RecordError):
        record(state="DONE", failure_reason="harness_error")


def test_unknown_state_is_rejected_and_unknown_is_a_state() -> None:
    assert "UNKNOWN" in ar.STATES
    assert record(state="UNKNOWN")["state"] == "UNKNOWN"
    with pytest.raises(ar.RecordError):
        record(state="probably fine")


def test_an_unattributed_record_is_refused() -> None:
    with pytest.raises(ar.RecordError):
        record(agent=None)


# ---------------------------------------------------------------------------
# the hash chain: any later edit is detectable
# ---------------------------------------------------------------------------


def test_append_then_verify_round_trip(telemetry_dir: Path) -> None:
    file = ar.append_record(telemetry_dir, record(task="W26-T", commits="3"))
    ar.append_record(telemetry_dir, record(task="W26-T", commits="4", state="PARTIAL"))
    results, problems = ar.verify_dir(telemetry_dir)
    assert problems == []
    assert [r.ok for r in results] == [True]
    assert results[0].records == 2
    assert file.read_text(encoding="utf-8").count("\n") == 2


def test_a_hand_edited_field_fails_verification(telemetry_dir: Path) -> None:
    file = ar.append_record(telemetry_dir, record(task="W24-A", commits="0"))
    records = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    # exactly what an agent trying to look cheaper would do: 2 164 461 -> 2164461 tokens kept,
    # duration softened from 2446 s to 60 s
    records[0]["duration_s"] = 60
    file.write_text(json.dumps(records[0], sort_keys=True) + "\n", encoding="utf-8")
    result = ar.verify_file(file)
    assert not result.ok
    assert "edited after it was written" in (result.problem or "")


def test_an_edited_null_to_zero_fails_verification(telemetry_dir: Path) -> None:
    file = ar.append_record(telemetry_dir, record(task="W24-A", tool_calls="null"))
    line = file.read_text(encoding="utf-8")
    file.write_text(line.replace('"tool_calls": null', '"tool_calls": 0'), encoding="utf-8")
    assert not ar.verify_file(file).ok


def test_a_grafted_record_without_the_writer_fails_verification(telemetry_dir: Path) -> None:
    ar.append_record(telemetry_dir, record(task="W24-A", commits="1"))
    forged = record(task="W24-A", commits="99")
    file = telemetry_dir / "W24-A.jsonl"
    with file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")
    assert not ar.verify_file(file).ok


def test_a_deleted_tail_line_is_caught_by_the_anchor(telemetry_dir: Path) -> None:
    ar.append_record(telemetry_dir, record(task="W24-A", commits="1"))
    ar.append_record(telemetry_dir, record(task="W24-A", commits="2", state="PARTIAL"))
    file = telemetry_dir / "W24-A.jsonl"
    kept = file.read_text(encoding="utf-8").splitlines()[0]
    file.write_text(kept + "\n", encoding="utf-8")
    _results, problems = ar.verify_dir(telemetry_dir)
    text = "\n".join(problems)
    # both anchor guards must fire: the head hash (a tail edit at all) and the
    # record count (a tail delete). Asserting on the wording is what keeps each
    # of them load-bearing instead of interchangeable.
    assert "does not match file head" in text, problems
    assert "anchor says" in text, problems


def test_a_whole_deleted_task_file_is_caught_by_the_anchor(telemetry_dir: Path) -> None:
    """A task record that vanishes leaves no broken chain behind: only the anchor knows."""
    ar.append_record(telemetry_dir, record(task="W24-A", commits="1"))
    (telemetry_dir / "W24-A.jsonl").unlink()
    _results, problems = ar.verify_dir(telemetry_dir)
    assert any("deleted" in problem for problem in problems), problems


def test_appending_refuses_to_chain_onto_a_broken_file(telemetry_dir: Path) -> None:
    file = ar.append_record(telemetry_dir, record(task="W24-A", commits="1"))
    tampered = json.loads(file.read_text(encoding="utf-8"))
    tampered["commits"] = 9
    file.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ar.RecordError, match="broken chain"):
        ar.append_record(telemetry_dir, record(task="W24-A", commits="2"))


def test_seq_and_prev_hash_link_the_chain(telemetry_dir: Path) -> None:
    for index in range(3):
        ar.append_record(telemetry_dir, record(task="W26-T", commits=str(index)))
    lines = [json.loads(x) for x in (telemetry_dir / "W26-T.jsonl").read_text("utf-8").splitlines()]
    assert [r["seq"] for r in lines] == [0, 1, 2]
    assert lines[0]["prev_hash"] == ar.GENESIS
    assert lines[1]["prev_hash"] == lines[0]["hash"]
    assert lines[2]["prev_hash"] == lines[1]["hash"]


def test_a_reordered_file_fails_verification(telemetry_dir: Path) -> None:
    for index in range(2):
        ar.append_record(telemetry_dir, record(task="W26-T", commits=str(index)))
    file = telemetry_dir / "W26-T.jsonl"
    lines = file.read_text(encoding="utf-8").splitlines()
    file.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    assert not ar.verify_file(file).ok


def test_a_corrupt_line_is_reported_not_skipped(telemetry_dir: Path) -> None:
    ar.append_record(telemetry_dir, record(task="W24-A"))
    file = telemetry_dir / "W24-A.jsonl"
    file.write_text(file.read_text(encoding="utf-8") + "not json at all\n", encoding="utf-8")
    assert not ar.verify_file(file).ok


# ---------------------------------------------------------------------------
# write-safety: no database, no secrets, no rewrite of a telemetry file
# ---------------------------------------------------------------------------


def test_target_refuses_anything_but_a_telemetry_json_file(telemetry_dir: Path) -> None:
    with pytest.raises(ar.RecordError, match="database"):
        ar.safe_target(telemetry_dir, "tgfarm.db")
    with pytest.raises(ar.RecordError, match="outside"):
        ar.safe_target(telemetry_dir, "../evil.jsonl")
    with pytest.raises(ar.RecordError):
        ar.safe_target(telemetry_dir, "notes.md")


def test_the_writer_never_rewrites_a_record_file(telemetry_dir: Path) -> None:
    file = ar.append_record(telemetry_dir, record(task="W26-T", commits="1"))
    before = file.read_bytes()
    ar.append_record(telemetry_dir, record(task="W26-T", commits="2"))
    assert file.read_bytes().startswith(before), "append-only: earlier bytes never change"


def test_tool_source_reads_no_env_no_db_and_no_app(tmp_path: Path) -> None:
    source = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {n for n in imported if n.split(".")[0] in {"tgfarm", "os", "dotenv"}}, imported
    assert "sqlite3" not in imported
    assert "os.environ" not in source, "no environment read: a secret could be picked up"
    assert "getenv" not in source
    assert "dotenv" not in source
    del tmp_path


# ---------------------------------------------------------------------------
# the CLI, exactly as the coordinator types it
# ---------------------------------------------------------------------------


def test_cli_append_verify_and_summary(tmp_path: Path) -> None:
    target = tmp_path / "tel"
    appended = cli(
        "--task", "W24-A", "--agent", "ageneral-purpose-e0078278",
        "--tool-calls", "42", "--subagent-tokens", "2164461", "--duration-s", "2446",
        "--state", "FAILED", "--failure-reason", "harness_error", "--commits", "0",
        "--telemetry-dir", str(target),
    )
    assert appended.returncode == 0, appended.stderr
    assert "appended 1 record" in appended.stdout
    assert '"commits": 0' in appended.stdout
    assert '"files_touched": null' in appended.stdout

    verified = cli("--verify", "--telemetry-dir", str(target))
    assert verified.returncode == 0, verified.stdout
    assert "W24-A.jsonl: OK" in verified.stdout

    summary = cli("--summary", "--telemetry-dir", str(target))
    assert summary.returncode == 0, summary.stdout
    assert "duration_s=2446" in summary.stdout
    assert "commits=0" in summary.stdout
    assert "not measured" in summary.stdout


def test_summary_never_prints_a_zero_for_an_unmeasured_cost(tmp_path: Path) -> None:
    target = tmp_path / "tel"
    cli("--task", "W26-T", "--agent", "agent-a", "--state", "DONE", "--commits", "2",
        "--telemetry-dir", str(target))
    out = cli("--summary", "--telemetry-dir", str(target)).stdout
    assert "subagent_tokens=not measured" in out
    assert "tool_calls=not measured" in out
    assert "tokens/call=not measured" in out
    for forbidden in ("subagent_tokens=0", "tool_calls=0", "tokens/call=0", "duration_s=0"):
        assert forbidden not in out, f"{forbidden} reads as a free task"
    assert "duration_s: null in 1 record(s)" in out
    assert "commits total = 2 over 1 measured record(s)" in out


def test_cli_refuses_a_garbage_number_and_explains_null(tmp_path: Path) -> None:
    result = cli("--task", "W26-T", "--agent", "agent-a", "--state", "DONE", "--commits", "1",
                 "--tool-calls", "lots", "--telemetry-dir", str(tmp_path / "tel"))
    assert result.returncode == 2
    assert "recorded as null, not guessed" in result.stderr


def test_cli_fails_verification_after_a_hand_edit(tmp_path: Path) -> None:
    target = tmp_path / "tel"
    cli("--task", "W24-A", "--agent", "agent-a", "--state", "DONE", "--commits", "1",
        "--telemetry-dir", str(target))
    file = target / "W24-A.jsonl"
    file.write_text(
        file.read_text(encoding="utf-8").replace('"commits": 1', '"commits": 41'),
        encoding="utf-8",
    )
    result = cli("--verify", "--telemetry-dir", str(target))
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert "verify: FAIL" in result.stdout


def test_cli_requires_task_and_state_to_write(tmp_path: Path) -> None:
    result = cli("--agent", "agent-a", "--telemetry-dir", str(tmp_path / "tel"))
    assert result.returncode != 0


def test_verify_on_an_empty_dir_is_not_measured_not_clean(tmp_path: Path) -> None:
    result = cli("--verify", "--telemetry-dir", str(tmp_path / "nothing-here"))
    assert result.returncode == 0
    assert "NOT_MEASURED" in result.stdout


def test_show_schema_lists_the_null_rules(tmp_path: Path) -> None:
    result = cli("--show-schema")
    assert result.returncode == 0
    assert "null = not measured, never 0" in result.stdout
    assert "UNKNOWN" in result.stdout
    del tmp_path

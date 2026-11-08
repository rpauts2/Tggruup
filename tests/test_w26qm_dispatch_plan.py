"""Tests for `tools/dispatch_plan.py` — the planner that exists because of one measured defect.

Wave 25 dispatched nine executors onto `cd21f29` (the stale tip of the second remote)
instead of the stated base `183daa4`, 253 commits apart; half of them reported the
wrong tree as the project. So every test here pins one of the four promises the
planner makes:

* the SHA it was given is the SHA in the command — verbatim, in every command;
* the command form is `git worktree add -b <branch> <path> <sha>`, never the bare
  `git worktree add <path> <sha>`: without `-b`, git reads the SHA as the branch
  name — a defect this project already recorded once, which is why the exact token
  order is asserted rather than eyeballed;
* overlaps — inside the plan, against the queue's claiming/reserving records, and
  against foreign claims handed in — refuse with the pair named and no commands;
* no base, or a short base, is a refusal, never a guessed plan.

Plus the read-only guarantee held both behaviourally (a run leaves no new file and
the queue bytes untouched) and structurally (an AST guard on imports and call
targets: the directive forbids project code pretending to be an Agent Dispatcher,
and prose saying "read-only" is not a guarantee). The guard lives HERE because the
current `tests/test_w25a_queue.py` guards only `queue_check.py`; the coordinator
owns that file and may extend its guard set later — until then this file pins the
planner itself.
"""

from __future__ import annotations

import hashlib
import io
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "dispatch_plan.py"
SHIPPED_QUEUE = REPO_ROOT / "docs" / "orchestration" / "queue.yaml"

BASE = "b" * 40
OTHER_BASE = "c" * 40


def _load_tool():
    spec = spec_from_file_location("dispatch_plan_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dp = _load_tool()


def _task(**overrides):
    base = {
        "id": "T-1",
        "title": "example task",
        "status": "READY",
        "owner_agent": "ageneral-purpose-abc",
        "base_sha": None,
        "worktree": None,
        "branch": None,
        "allowed_paths": ["tgfarm/one.py"],
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, *tasks, name: str = "queue.yaml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump({"meta": {"schema_version": 1}, "tasks": list(tasks)},
                                   allow_unicode=True), encoding="utf-8")
    return path


def _commands(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if "git worktree add" in line]


# ---------------------------------------------------------------- pinning
def test_the_plan_pins_the_sha_it_was_given(tmp_path, capsys) -> None:
    """The literal SHA must be in BOTH commands — that is the whole wave-25 defect."""
    queue = _write(tmp_path,
                   _task(id="T-1", branch="w26qm-one", allowed_paths=["tgfarm/one.py"]),
                   _task(id="T-2", branch="w26qm-two", allowed_paths=["tgfarm/two.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 0
    out = capsys.readouterr().out
    commands = _commands(out)
    assert len(commands) == 2, out
    for command in commands:
        assert BASE in command, command
    assert f"{BASE} {BASE}" not in out  # each command names the base exactly once


def test_the_command_form_keeps_the_branch_separate_from_the_path(tmp_path, capsys) -> None:
    """`git worktree add <path> <sha>` alone fails: git reads the SHA as the branch.

    This project recorded that defect once, so the token order `-b <branch> <path>
    <sha>` is asserted position by position, and the path is asserted absolute.
    """
    queue = _write(tmp_path, _task(id="T-1", branch="w26qm-pin",
                                   allowed_paths=["tgfarm/one.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 0
    commands = _commands(capsys.readouterr().out)
    assert len(commands) == 1
    parts = commands[0].removeprefix("command:").split()
    assert parts[:5] == ["git", "worktree", "add", "-b", "w26qm-pin"], parts
    assert Path(parts[5]).is_absolute(), parts[5]
    assert parts[5] == (Path(dp.DEFAULT_WORKTREE_ROOT) / "w26qm-pin").as_posix()
    assert parts[6] == BASE, parts
    assert " " not in parts[5] and "\\" not in parts[5], parts[5]  # one token, posix form


def test_the_pinned_base_of_the_record_is_enough(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", base_sha=OTHER_BASE))
    assert dp.main(["--queue", str(queue)]) == 0
    commands = _commands(capsys.readouterr().out)
    assert commands and OTHER_BASE in commands[0]


# ---------------------------------------------------------------- refusals
def test_a_missing_base_is_a_refusal_not_a_plan(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", base_sha=None))
    assert dp.main(["--queue", str(queue)]) == 1
    out = capsys.readouterr().out
    assert _commands(out) == [], out
    assert "no base given" in out, out


def test_a_short_base_is_refused_wherever_it_came_from(tmp_path, capsys) -> None:
    pinned = _write(tmp_path, _task(id="T-1", base_sha="183daa4"), name="pinned.yaml")
    assert dp.main(["--queue", str(pinned)]) == 1
    out = capsys.readouterr().out
    assert "not a full 40-character SHA" in out, out
    assert _commands(out) == [], out

    given = _write(tmp_path, _task(id="T-2", allowed_paths=["tgfarm/two.py"]), name="arg.yaml")
    assert dp.main(["--queue", str(given), "--base", "183daa4"]) == 1
    assert "not a full 40-character SHA" in capsys.readouterr().out


def test_two_bases_that_disagree_refuse_instead_of_choosing(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", base_sha=OTHER_BASE))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 1
    out = capsys.readouterr().out
    assert "refuse to pick" in out, out
    assert BASE in out and OTHER_BASE in out, out
    assert _commands(out) == []


def test_overlapping_tasks_are_refused_and_the_pair_is_named(tmp_path, capsys) -> None:
    """The intro promise: no command survives an ownership overlap inside the plan."""
    queue = _write(tmp_path,
                   _task(id="T-1", branch="w26qm-one", allowed_paths=["tgfarm/api.py"]),
                   _task(id="T-2", branch="w26qm-two", allowed_paths=["tgfarm/api.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 1
    out = capsys.readouterr().out
    assert "plan-owner-conflict" in out, out
    assert "T-1" in out and "T-2" in out and "tgfarm/api.py" in out, out
    assert _commands(out) == [], "commands were emitted despite the overlap"


def test_an_overlapping_directory_prefix_refuses_the_pair(tmp_path, capsys) -> None:
    """`tgfarm/llm/` vs `tgfarm/llm/client.py`: queue_check's overlap rule, imported not duplicated."""
    queue = _write(tmp_path,
                   _task(id="T-1", branch="w26qm-one", allowed_paths=["tgfarm/llm/"]),
                   _task(id="T-2", branch="w26qm-two", allowed_paths=["tgfarm/llm/client.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 1
    out = capsys.readouterr().out
    assert "plan-owner-conflict" in out and "T-1" in out and "T-2" in out, out
    assert _commands(out) == []


def test_a_file_held_by_a_live_queue_record_blocks_the_plan(tmp_path, capsys) -> None:
    queue = _write(tmp_path,
                   _task(id="W23-A", status="RUNNING", branch="w23a",
                         allowed_paths=["tgfarm/api.py"]),
                   _task(id="T-1", branch="w26qm-one", allowed_paths=["tgfarm/api.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 1
    out = capsys.readouterr().out
    assert "queue-ownership" in out and "W23-A" in out and "RUNNING" in out, out
    assert _commands(out) == []


def test_a_reservation_of_a_state_still_reserving_blocks_the_plan(tmp_path, capsys) -> None:
    """W23-B's own note: files stay marked busy so a parallel launch does not sit twice."""
    queue = _write(tmp_path,
                   _task(id="W23-B", status="RECOVERY_REQUIRED",
                         allowed_paths=["tgfarm/plan_execution.py"]),
                   _task(id="T-1", branch="w26qm-one",
                         allowed_paths=["tgfarm/plan_execution.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 1
    out = capsys.readouterr().out
    assert "W23-B" in out and "RECOVERY_REQUIRED" in out, out


def test_a_foreign_claim_on_a_planned_file_is_reported(tmp_path, capsys) -> None:
    """The W25-B `--foreign-claims` style, reused — porcelain inline is a legal claim source."""
    queue = _write(tmp_path, _task(id="T-1", branch="w26qm-one",
                                   allowed_paths=["tgfarm/cli.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE,
                    "--foreign-claims", " M tgfarm/cli.py"]) == 1
    out = capsys.readouterr().out
    assert "duplicate-active" in out and "tgfarm/cli.py" in out, out
    assert _commands(out) == []


def test_a_foreign_claim_on_an_unrelated_file_leaves_the_plan_alone(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", branch="w26qm-one",
                                   allowed_paths=["tgfarm/cli.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE,
                    "--foreign-claims", "?? tgfarm/somewhere_else.py"]) == 0
    assert len(_commands(capsys.readouterr().out)) == 1


# ---------------------------------------------------------------- states and selection
def test_no_dispatchable_record_is_honest_not_a_guessed_plan(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", status="INTEGRATED"),
                   _task(id="T-2", status="BLOCKED", allowed_paths=["tgfarm/two.py"]))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 0
    out = capsys.readouterr().out
    assert "no dispatchable records" in out, out
    assert _commands(out) == []


def test_an_explicit_id_in_a_non_dispatchable_state_is_refused_by_name(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-7", status="RUNNING", branch="t-7"))
    assert dp.main(["--queue", str(queue), "--base", BASE, "--id", "T-7"]) == 1
    out = capsys.readouterr().out
    assert "T-7" in out and "not dispatchable" in out and "RUNNING" in out, out


def test_the_dispatchable_words_are_queue_check_words_not_new_ones() -> None:
    """No second state machine: DISPATCHABLE is a subset of the validator's own STATES.

    The current queue_check.py does not export DISPATCHABLE (that export belonged
    to a delta the coordinator reconciles personally), so the planner defines the
    subset locally — and THIS test pins that it stays inside the validator's
    vocabulary instead of becoming a state machine of its own.
    """
    assert set(dp.DISPATCHABLE) <= set(dp.qc.STATES)
    assert set(dp.DISPATCHABLE) == {"READY", "ASSIGNED"}


# ---------------------------------------------------------------- paths
def test_the_record_paths_are_honoured_and_foreign_worktree_fields_are_not(tmp_path, capsys) -> None:
    queue = _write(tmp_path,
                   _task(id="T-1", branch="w26qm-nice",
                         worktree=".qoder/worktrees/w26qm-nice",
                         allowed_paths=["tgfarm/one.py"]),
                   _task(id="T-2", branch="w26qm-strange", worktree=".",
                         allowed_paths=["tgfarm/two.py"], status="READY"))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 0
    out = capsys.readouterr().out
    root = Path(dp.DEFAULT_WORKTREE_ROOT).as_posix()
    assert f"{root}/w26qm-nice" in out, out
    assert f"git worktree add -b w26qm-strange {root}/w26qm-strange" in out, out
    assert "add -b w26qm-strange ." not in out, "the record's `.` must not become the worktree"


def test_the_selfcheck_line_names_the_one_verifying_command(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", branch="w26qm-self"))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 0
    out = capsys.readouterr().out
    path = (Path(dp.DEFAULT_WORKTREE_ROOT) / "w26qm-self").as_posix()
    assert f"git -C {path} rev-parse HEAD" in out, out
    assert BASE in out, out
    assert "not evidence" in out, out  # an agent's report of its own base is not evidence


# ---------------------------------------------------------------- the read-only guarantee
def test_the_planner_writes_nothing(tmp_path, capsys) -> None:
    queue = _write(tmp_path, _task(id="T-1", branch="w26qm-ro"),
                   _task(id="T-2", branch="w26qm-ro2", allowed_paths=["tgfarm/two.py"]))
    digest_before = hashlib.sha256(queue.read_bytes()).hexdigest()
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert dp.main(["--queue", str(queue), "--base", BASE]) == 0
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after == ["queue.yaml"]
    assert hashlib.sha256(queue.read_bytes()).hexdigest() == digest_before, (
        "the queue file was rewritten in place — read-only means byte-identical, not just present")
    assert not (tmp_path / ".qoder").exists()
    assert len(_commands(capsys.readouterr().out)) == 2


def test_the_planner_imports_the_rules_it_uses(tmp_path) -> None:
    """The overlap machinery lives in queue_check.py; duplicating it here would rot."""
    source = TOOL_PATH.read_text(encoding="utf-8")
    for shared in ("def _path_overlap", "def _check_foreign_claims", "def _load_foreign_claims",
                   "def _configure_streams", "def _known_shas"):
        assert shared not in source, f"dispatch_plan.py re-implements {shared}"
    assert dp.qc._path_overlap("tgfarm/llm/", "tgfarm/llm/client.py")


def test_the_planner_cannot_dispatch_or_mutate_the_repository() -> None:
    """Structural, because prose saying "read-only" is not a guarantee.

    The directive: project code must not pretend to be a full Agent Dispatcher.
    Checked on the syntax tree, not by grepping the text: the module docstring
    contains the words `subprocess.run` as an explanation of this very rule, and
    a substring test therefore fails on a compliant file. Imports and call
    targets are what actually grant the power to launch an agent, write telemetry
    or change git state. This mirrors the guard `tests/test_w25a_queue.py` applies
    to queue_check.py; the planner's allowlist adds exactly one module —
    queue_check, because the rules must be shared, not duplicated.
    """
    import ast

    tree = ast.parse(TOOL_PATH.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "sys", "pathlib", "typing",
               "queue_check", "dispatch_plan_under_test"}
    assert imported <= allowed, f"dispatch_plan.py gained a capability: {sorted(imported - allowed)}"

    call_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)
    for forbidden in ("system", "popen", "run", "Popen", "check_call", "check_output",
                      "rmtree", "unlink", "mkdir", "write_text", "write_bytes", "open"):
        assert forbidden not in call_names, f"the planner may not call {forbidden}()"

    assert "subprocess" not in imported, "the planner may not shell out"
    assert "socket" not in imported, "the planner may not reach the network"
    assert "queue_check" in imported, (
        "the planner must import the shared rules; deleting this allowance instead of "
        "duplicating them in the tool is the drift this guards against")


def test_the_summary_counts_are_the_records_not_invented_telemetry(tmp_path, capsys) -> None:
    """Every number in the report is derived from the queue rows handed in.

    The planner owns no agents and no telemetry: a summary that does not match
    the actual records would be an invented measurement, which the directive
    forbids as strictly as launching a process.
    """
    queue = _write(tmp_path,
                   _task(id="T-1", branch="w26qm-c1", base_sha=BASE),
                   _task(id="T-2", branch="w26qm-c2", base_sha=BASE,
                         allowed_paths=["tgfarm/two.py"]),
                   _task(id="T-3", status="INTEGRATED", allowed_paths=["tgfarm/three.py"]))
    assert dp.main(["--queue", str(queue)]) == 0
    out = capsys.readouterr().out
    summary = [line for line in out.splitlines() if line.startswith("summary:")]
    assert len(summary) == 1, out
    assert "2 planned, 0 refused, 0 fatal overlap(s); queue: 3 records" in summary[0], summary[0]
    assert "INTEGRATED=1, READY=2" in summary[0], summary[0]


# ---------------------------------------------------------------- encoding
def test_a_russian_title_survives_the_cp1252_console(tmp_path, monkeypatch) -> None:
    """Same guard as queue_check's: reporting that crashes hides every line behind it."""
    queue = _write(tmp_path,
                   _task(id="T-1", title="Планировщик диспетчера", branch="w26qm-ru",
                         base_sha=BASE),
                   _task(id="T-2", title="база не названа", branch="w26qm-ru2",
                         allowed_paths=["tgfarm/two.py"], base_sha=None))
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    assert dp.main(["--queue", str(queue)]) == 1
    stream.flush()
    out = buffer.getvalue().decode("utf-8", errors="replace")
    assert "Планировщик диспетчера" in out, out
    assert "база не названа" in out, out
    assert "\ufffd" not in out, "the console fallback replaced Cyrillic with U+FFFD"


# ---------------------------------------------------------------- the shipped queue
def test_the_shipped_queue_currently_asks_for_no_dispatch(capsys) -> None:
    """Not a permanent truth — today it is the measured one: zero READY/ASSIGNED records.

    The planner's answer must be the honest empty report, never a plan on a guessed base.
    """
    assert dp.main([str(SHIPPED_QUEUE)]) == 0
    out = capsys.readouterr().out
    assert "no dispatchable records" in out, out
    assert _commands(out) == []
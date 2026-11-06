"""Tests for `tools/queue_check.py` — the rules, and the promises about what the tool may not do.

Three things are pinned here, in this order of importance:

* the shipped `docs/orchestration/queue.yaml` must stay consistent, so a future
  session cannot record a second owner for a file an executor is already editing,
  claim a task is integrated while `integration_status` says otherwise, or cite a
  commit that does not exist;
* every rule bites on a minimal queue built for it — a validator with a rule nobody
  exercised is decoration;
* the tool cannot launch agents or change repository state. That is not a style
  preference: the directive says a Python file in this project must not pretend to
  be an Agent Dispatcher, so the honest version asserts on its own imports.
"""

from __future__ import annotations

import io
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "queue_check.py"
SHIPPED_QUEUE = REPO_ROOT / "docs" / "orchestration" / "queue.yaml"


def _load_tool():
    spec = spec_from_file_location("queue_check_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


qc = _load_tool()


def _task(**overrides):
    base = {
        "id": "T-1",
        "title": "example task",
        "description": "what and why",
        "priority": "medium",
        "category": "product",
        "status": "RUNNING",
        "owner_role": "Backend Engineer",
        "owner_agent": "ageneral-purpose-abc",
        "base_sha": "a" * 40,
        "worktree": ".qoder/worktrees/t-1",
        "branch": "t-1",
        "allowed_paths": ["tgfarm/example.py"],
        "depends_on": [],
        "blocked_by": [],
        "acceptance": "measured",
        "test_commands": ["python -m pytest -q tests/test_x.py"],
        "review_status": "NOT_STARTED",
        "integration_status": "NOT_INTEGRATED",
        "retry_count": 0,
        "attempt_limit": 3,
        "tool_calls_used": None,
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "evidence": [],
        "next_action": None,
    }
    base.update(overrides)
    return base


def _rules(problems) -> set[str]:
    return {p.rule for p in problems}


def _write(tmp_path: Path, *tasks, name: str = "queue.yaml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump({"meta": {"schema_version": 1}, "tasks": list(tasks)},
                                   allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------- the shipped record
def test_the_shipped_queue_is_consistent() -> None:
    """The file this session relies on must be defensible in CI, not in my memory."""
    loaded = qc._load_raw(SHIPPED_QUEUE)
    problems, _ = qc.validate(loaded["tasks"])
    assert not problems, "\n".join(str(p) for p in problems)


def test_the_shipped_queue_records_every_live_executor_once() -> None:
    loaded = qc._load_raw(SHIPPED_QUEUE)
    ids = [str(t["id"]) for t in loaded["tasks"]]
    assert len(ids) == len(set(ids)), f"duplicate task ids: {ids}"
    for required in ("W23-A", "W23-B", "W24-A", "W25-A", "W28-A", "OPS-1"):
        assert required in ids, f"{required} vanished from the queue"


# ---------------------------------------------------------------- field and state rules
def test_a_missing_required_field_is_named(tmp_path) -> None:
    task = _task()
    del task["acceptance"]
    problems, _ = qc.validate([task])
    assert "missing-field" in _rules(problems)
    assert any("acceptance" in str(p) for p in problems)


def test_an_unknown_field_is_rejected(tmp_path) -> None:
    problems, _ = qc.validate([_task(assigned_to="someone")])
    assert "unknown-field" in _rules(problems)


def test_an_unknown_state_is_rejected() -> None:
    problems, _ = qc.validate([_task(status="DONE_I_THINK")])
    assert "bad-state" in _rules(problems)


def test_a_task_may_not_be_done_and_unintegrated_at_once() -> None:
    """One-state rule, the cheapest way to lose track of real progress."""
    problems, _ = qc.validate([_task(status="VERIFIED", integration_status="NOT_INTEGRATED",
                                     completed_at="2026-11-06T14:00:00+03:00")])
    assert "two-states" in _rules(problems)


def test_running_without_a_location_needs_the_coordinator_marker() -> None:
    problems, _ = qc.validate([_task(worktree=None, branch=None)])
    assert "no-location" in _rules(problems)

    problems, _ = qc.validate([_task(worktree=None, branch=None, owner_agent="coordinator")])
    assert "no-location" not in _rules(problems)


def test_a_finished_state_without_completed_at_cannot_be_ordered() -> None:
    problems, _ = qc.validate([_task(status="INTEGRATED", integration_status="INTEGRATED",
                                     completed_at=None)])
    assert "no-completion-time" in _rules(problems)


def test_a_running_task_may_not_already_claim_completion() -> None:
    problems, _ = qc.validate([_task(completed_at="2026-11-06T14:00:00+03:00")])
    assert "early-completion" in _rules(problems)


# ---------------------------------------------------------------- dependencies
def test_ready_requires_satisfied_dependencies() -> None:
    blocker = _task(id="T-0", status="FAILED", allowed_paths=["tgfarm/other.py"])
    ready = _task(id="T-1", status="READY", depends_on=["T-0"])
    problems, _ = qc.validate([blocker, ready])
    assert "not-ready" in _rules(problems)


def test_a_satisfied_dependency_stops_the_complaint() -> None:
    done = _task(id="T-0", status="VERIFIED", allowed_paths=["tgfarm/other.py"],
                 integration_status="VERIFIED", completed_at="2026-11-06T13:00:00+03:00")
    ready = _task(id="T-1", status="READY", depends_on=["T-0"])
    problems, _ = qc.validate([done, ready])
    assert "not-ready" not in _rules(problems)


def test_a_dependency_on_a_missing_task_is_named() -> None:
    problems, _ = qc.validate([_task(depends_on=["T-GHOST"])])
    assert "unknown-dependency" in _rules(problems)


def test_a_dependency_cycle_is_printed_as_a_cycle() -> None:
    problems, _ = qc.validate([
        _task(id="T-1", depends_on=["T-2"]),
        _task(id="T-2", depends_on=["T-1"], allowed_paths=["tgfarm/two.py"]),
    ])
    cycles = [p for p in problems if p.rule == "dependency-cycle"]
    assert cycles, problems
    assert "->" in cycles[0].detail


def test_explain_says_which_dependency_and_which_state() -> None:
    blocker = _task(id="T-0", status="RUNNING", allowed_paths=["tgfarm/other.py"])
    waiting = _task(id="T-1", status="BLOCKED", depends_on=["T-0"],
                    next_action="wait for T-0 to be integrated")
    by_id = {"T-0": blocker, "T-1": waiting}
    reasons = qc._explain(waiting, by_id)
    assert any("T-0" in r and "RUNNING" in r for r in reasons), reasons


def test_ready_lists_only_startable_backlog() -> None:
    done = _task(id="T-0", status="VERIFIED", allowed_paths=["tgfarm/other.py"],
                 integration_status="VERIFIED", completed_at="2026-11-06T13:00:00+03:00")
    free = _task(id="T-2", status="BACKLOG", allowed_paths=["tgfarm/free.py"], depends_on=["T-0"])
    waiting = _task(id="T-3", status="BACKLOG", allowed_paths=["tgfarm/wait.py"], depends_on=["T-1"])
    blocked = _task(id="T-1", status="RUNNING", allowed_paths=["tgfarm/blocked.py"])
    assert qc.ready_now([done, blocked, free, waiting]) == ["T-2"]


# ---------------------------------------------------------------- ownership
def test_two_active_tasks_may_not_claim_the_same_file() -> None:
    problems, _ = qc.validate([
        _task(id="T-1"),
        _task(id="T-2", allowed_paths=["tgfarm/example.py"],
              worktree=".qoder/worktrees/t-2", branch="t-2"),
    ])
    assert "owner-conflict" in _rules(problems)


def test_a_directory_claim_collides_with_a_file_inside_it() -> None:
    problems, _ = qc.validate([
        _task(id="T-1", allowed_paths=["tgfarm/llm/"]),
        _task(id="T-2", allowed_paths=["tgfarm/llm/client.py"],
              worktree=".qoder/worktrees/t-2", branch="t-2"),
    ])
    assert "owner-conflict" in _rules(problems)


def test_a_glob_claim_collides_with_a_concrete_path() -> None:
    problems, _ = qc.validate([
        _task(id="T-1", allowed_paths=["tests/test_w25a_*.py"]),
        _task(id="T-2", allowed_paths=["tests/test_w25a_queue.py"],
              worktree=".qoder/worktrees/t-2", branch="t-2"),
    ])
    assert "owner-conflict" in _rules(problems)


def test_a_terminal_task_releases_its_files() -> None:
    problems, _ = qc.validate([
        _task(id="T-1", status="FAILED", allowed_paths=[], base_sha="b" * 40),
        _task(id="T-2", allowed_paths=["tgfarm/example.py"],
              worktree=".qoder/worktrees/t-2", branch="t-2"),
    ])
    assert "owner-conflict" not in _rules(problems)


def test_the_same_goal_may_not_be_active_twice() -> None:
    problems, _ = qc.validate([
        _task(id="T-1", title="Split the no-slot reason"),
        _task(id="T-2", title="split the no-slot reason ", allowed_paths=["tgfarm/other.py"],
              worktree=".qoder/worktrees/t-2", branch="t-2"),
    ])
    assert "duplicate-active" in _rules(problems)


# ---------------------------------------------------------------- sha honesty
def test_a_base_sha_that_the_repo_does_not_advertise_is_flagged() -> None:
    problems, _ = qc.validate([_task(base_sha="f" * 40)])
    assert "unknown-sha" in _rules(problems)


def test_a_real_sha_is_not_flagged() -> None:
    """The check must not be theatre: a commit that exists has to pass it."""
    problems, _ = qc.validate([_task(base_sha=_real_sha())])
    assert "unknown-sha" not in _rules(problems), problems


# ---------------------------------------------------------------- entry point
def _real_sha() -> str:
    """Resolve HEAD the way git does: loose ref first, then packed-refs.

    A bare `refs/heads/main` lookup skips silently on any repository that has been
    packed, and a skipped guard is how the encoding mutant survived its first run:
    the test that was supposed to prove it never executed at all.
    """
    head = (REPO_ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head[5:] if len(head) == 40 else head
    ref_name = head[5:]
    loose = REPO_ROOT / ".git" / ref_name
    if loose.exists():
        return loose.read_text(encoding="utf-8").strip()
    packed = REPO_ROOT / ".git" / "packed-refs"
    if packed.exists():
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == ref_name:
                return parts[0]
    pytest.skip(f"{ref_name} is neither a loose nor a packed ref here")
    return ""


def test_head_sha_is_resolvable_for_the_assertions_below() -> None:
    """The other tests trust this helper; a silent skip must be visible as a failure."""
    assert len(_real_sha()) == 40


def test_exit_codes(tmp_path) -> None:
    """0 clean, 1 violation, 2 unreadable — and `clean` means a commit that exists."""
    clean = _write(tmp_path, _task(base_sha=_real_sha()))
    assert qc.main(["--queue", str(clean)]) == 0

    broken = _write(tmp_path, _task(id="T-2", title="second", status="DONE",
                                    allowed_paths=["tgfarm/other.py"],
                                    worktree=".qoder/worktrees/t-2", branch="t-2"),
                   name="broken.yaml")
    assert qc.main(["--queue", str(broken)]) == 1
    assert broken.name != clean.name, "_write overwrote the clean queue instead of adding one"

    assert qc.main(["--queue", str(tmp_path / "absent.yaml")]) == 2


def test_a_non_mapping_file_is_unreadable_not_empty(tmp_path) -> None:
    path = tmp_path / "queue.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert qc.main(["--queue", str(path)]) == 2


def test_the_report_survives_a_console_that_cannot_encode_cyrillic(tmp_path, monkeypatch) -> None:
    """A validator that dies while reporting hides every problem behind the crash.

    Measured in this session: the first run on the real queue printed 7 of 15
    problems and raised `UnicodeEncodeError` in cp1252. Most rule messages are ASCII,
    so the test is written around the two that are not — `duplicate-active` echoes the
    task title, `--explain` echoes the recorded next action, and both are Russian in
    this project's queue. The first version of this test used a `bad-state` problem and
    the encoding guard could be deleted without turning it red; the mutant caught that,
    so the test was rewritten rather than the guard being dropped.
    """
    import yaml

    head = _real_sha()
    twin = _task(title="Русская задача очереди", description="Описание первой",
                 base_sha=head, allowed_paths=["tgfarm/one.py"])
    other = _task(id="T-2", title="русская задача очереди ", description="Описание второй",
                  base_sha=head, allowed_paths=["tgfarm/two.py"],
                  worktree=".qoder/worktrees/t-2", branch="t-2")
    blocked = _task(id="T-3", status="BLOCKED", title="Операторский шаг",
                    description="Внешнее действие", base_sha=head,
                    allowed_paths=["tgfarm/three.py"],
                    worktree=".qoder/worktrees/t-3", branch="t-3",
                    next_action="Действие оператора, код без согласия не запускается")

    path = tmp_path / "queue.yaml"
    path.write_text(yaml.safe_dump({"meta": {}, "tasks": [twin, other, blocked]},
                                   allow_unicode=True), encoding="utf-8")

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)

    assert qc.main(["--queue", str(path)]) == 1
    assert qc.main(["--queue", str(path), "--explain", "T-3"]) == 0
    stream.flush()
    report = buffer.getvalue().decode("utf-8", errors="replace")

    assert "duplicate-active" in report, report
    assert "Действие оператора" in report, (
        "the Russian next_action was dropped or mangled instead of reported"
    )
    assert "\ufffd" not in report, "the console fallback replaced Cyrillic with U+FFFD"


# ---------------------------------------------------------------- the honesty promise
def test_the_validator_cannot_dispatch_or_mutate_the_repository() -> None:
    """Structural, because prose saying "read-only" is not a guarantee.

    Checked on the syntax tree, not by grepping the text: my own module docstring
    contains the words `subprocess.run` as an explanation of this very rule, and a
    substring test therefore failed on a compliant file. Imports and call targets are
    what actually grant the power to launch an agent or change git state.
    """
    import ast

    tree = ast.parse(TOOL_PATH.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "fnmatch", "sys", "dataclasses",
               "pathlib", "typing", "yaml", "queue_check_under_test"}
    assert imported <= allowed, f"queue_check.py gained a capability: {sorted(imported - allowed)}"

    call_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)
    for forbidden in ("system", "popen", "run", "Popen", "check_call", "check_output",
                      "rmtree", "unlink", "mkdir", "write_text", "write_bytes", "open"):
        assert forbidden not in call_names, f"the validator may not call {forbidden}()"
    # `set.remove`/`list.remove` are the cycle walker's own bookkeeping, not a file
    # operation, so the destructive guarantee is stated by the names above rather
    # than by banning the verb.

    assert "subprocess" not in imported
    assert "socket" not in imported

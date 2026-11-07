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


# ------------------------------------------------------------- foreign (outside-queue) claims
# The real W25-B collision, stated once for the whole file: while W28-A delivered
# LLM reliability as a reviewed package on its branch, a second session was editing
# the same `tgfarm/llm/client.py` bytes, uncommitted, in the shared main tree. The
# queue alone cannot see that; a caller-fed `git status` input can.

_MISSIONS_OWNER = "missions/t1-t4 session"
_W28A_OWNER = "ageneral-purpose-43372317f891f137"


def test_a_foreign_live_claim_on_a_queue_file_is_duplicate_active_naming_both_owners() -> None:
    """Regression of the client.py collision: two owners, one file, one problem."""
    w28a = _task(id="W28-A", owner_agent=_W28A_OWNER, allowed_paths=["tgfarm/llm/client.py"])
    claims = [{"owner": _MISSIONS_OWNER, "paths": ["tgfarm/llm/client.py"]}]
    problems, _ = qc.validate([w28a], foreign_claims=claims)
    hits = [p for p in problems if p.rule == "duplicate-active"]
    assert hits, problems
    detail = hits[0].detail
    assert _MISSIONS_OWNER in detail and _W28A_OWNER in detail, detail
    assert "tgfarm/llm/client.py" in detail, detail


def test_a_foreign_claim_on_a_reserved_but_idle_file_is_foreign_claim() -> None:
    reserved = _task(id="T-9", status="BLOCKED", owner_agent="someone-else",
                     allowed_paths=["tgfarm/editorial_plan.py"],
                     worktree=None, branch=None)
    claims = [{"owner": _MISSIONS_OWNER, "paths": ["tgfarm/editorial_plan.py"]}]
    problems, _ = qc.validate([reserved], foreign_claims=claims)
    assert "foreign-claim" in _rules(problems)


def test_a_backlog_plan_does_not_block_a_foreign_tree() -> None:
    """Planning paths are intentions; flagging them would bury the real findings."""
    planned = _task(id="T-8", status="BACKLOG", owner_agent="",
                    allowed_paths=["tgfarm/cli.py"], worktree=None, branch=None)
    claims = [{"owner": _MISSIONS_OWNER, "paths": ["tgfarm/cli.py"]}]
    problems, _ = qc.validate([planned], foreign_claims=claims)
    assert not [p for p in problems if p.rule in ("foreign-claim", "duplicate-active")], problems


def test_a_registered_foreign_owner_produces_no_problem() -> None:
    """When the session writes its own claim into the queue, reality is known."""
    registered = _task(id="MS-T1", status="BLOCKED", owner_agent=_MISSIONS_OWNER,
                       allowed_paths=["tgfarm/llm/client.py"], worktree=None, branch=None)
    claims = [{"owner": _MISSIONS_OWNER, "paths": ["tgfarm/llm/client.py"]}]
    problems, _ = qc.validate([registered], foreign_claims=claims)
    assert not [p for p in problems if p.rule in ("foreign-claim", "duplicate-active")], problems


def test_two_foreign_owners_overlapping_each_other_are_duplicate_active() -> None:
    claims = [
        {"owner": _MISSIONS_OWNER, "paths": ["tgfarm/llm/reliability.py"]},
        {"owner": "another-session", "paths": ["tgfarm/llm/reliability.py"]},
    ]
    problems, _ = qc.validate([_task(id="T-0", status="BACKLOG", allowed_paths=[])],
                              foreign_claims=claims)
    hits = [p for p in problems if p.rule == "duplicate-active"]
    assert hits and _MISSIONS_OWNER in hits[0].detail and "another-session" in hits[0].detail


def test_porcelain_status_output_is_accepted_as_inline_claims() -> None:
    porcelain = (" M tgfarm/llm/client.py\n"
                 "?? tgfarm/llm/reliability.py\n"
                 "R  tgfarm/old.py -> tgfarm/new.py\n")
    claims = qc._load_foreign_claims(porcelain, _MISSIONS_OWNER)
    assert claims == [{"owner": _MISSIONS_OWNER,
                       "paths": ["tgfarm/llm/client.py", "tgfarm/llm/reliability.py",
                                 "tgfarm/new.py"]}], claims


def test_a_json_claim_file_is_accepted(tmp_path) -> None:
    import json

    claim_file = tmp_path / "claims.json"
    claim_file.write_text(json.dumps(
        [{"owner": _MISSIONS_OWNER, "paths": ["tgfarm/cli.py"]}]), encoding="utf-8")
    claims = qc._load_foreign_claims(str(claim_file), qc.DEFAULT_FOREIGN_OWNER)
    assert claims == [{"owner": _MISSIONS_OWNER, "paths": ["tgfarm/cli.py"]}]


def test_validate_without_foreign_claims_keeps_the_old_ruleset(tmp_path) -> None:
    head = _real_sha()
    task = _task(base_sha=head)
    old, _ = qc.validate([task])
    new, _ = qc.validate([task], foreign_claims=[])
    assert [str(p) for p in old] == [str(p) for p in new]


def test_main_reports_foreign_problems_with_exit_one(tmp_path) -> None:
    queue = _write(tmp_path, _task(id="W28-A", owner_agent=_W28A_OWNER,
                                   allowed_paths=["tgfarm/llm/client.py"],
                                   base_sha=_real_sha()))
    assert qc.main(["--queue", str(queue)]) == 0
    claims_file = tmp_path / "claims.yaml"
    claims_file.write_text(
        f"- owner: {_MISSIONS_OWNER}\n  paths: [tgfarm/llm/client.py]\n", encoding="utf-8")
    assert qc.main(["--queue", str(queue), "--foreign-claims", str(claims_file)]) == 1


# ---------------------------------------------------------------- sha honesty
def test_a_base_sha_that_the_repo_does_not_advertise_is_flagged() -> None:
    problems, _ = qc.validate([_task(base_sha="f" * 40)])
    assert "unknown-sha" in _rules(problems)


def test_a_real_sha_is_not_flagged() -> None:
    """The check must not be theatre: a commit that exists has to pass it."""
    problems, _ = qc.validate([_task(base_sha=_real_sha())])
    assert "unknown-sha" not in _rules(problems), problems


def test_known_shas_includes_objects_from_a_pack_index(tmp_path, monkeypatch) -> None:
    """Historical queue SHAs are often packed objects, not current ref tips."""
    git_dir = tmp_path / ".git"
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True)

    object_sha = "12" + "34567890abcdef" * 2 + "12345678"
    assert len(object_sha) == 40
    first_byte = int(object_sha[:2], 16)
    fanout = b"".join(
        (0 if index < first_byte else 1).to_bytes(4, "big")
        for index in range(256)
    )
    index_data = (
        b"\\xfftOc"
        + (2).to_bytes(4, "big")
        + fanout
        + bytes.fromhex(object_sha)
        + b"\\x00" * 4  # CRC32
        + (12).to_bytes(4, "big")  # 32-bit pack offset
        + b"\\x00" * 40  # pack checksum and index checksum
    )
    (pack_dir / "pack-test.idx").write_bytes(index_data)
    monkeypatch.setattr(qc, "REPO_ROOT", tmp_path)

    known = qc._known_shas()
    assert known is not None
    assert object_sha in known
    assert object_sha[:7] in known


# ---------------------------------------------------------------- entry point
def _real_sha() -> str:
    """Resolve HEAD the way git does: loose ref first, then packed-refs.

    A bare `refs/heads/main` lookup skips silently on any repository that has been
    packed, and a skipped guard is how the encoding mutant survived its first run:
    the test that was supposed to prove it never executed at all.

    HEAD is resolved through `qc._git_dirs`, so this works in the main checkout and
    in a linked executor worktree alike, where `.git` is a `gitdir:` pointer file;
    branch refs live under the common dir, HEAD lives under the worktree's own dir.
    """
    gitdir, common = qc._git_dirs(REPO_ROOT)
    assert gitdir is not None and common is not None, f"{REPO_ROOT} is not a git checkout"
    head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head[5:] if len(head) == 40 else head
    ref_name = head[5:]
    loose = common / ref_name
    if loose.exists():
        return loose.read_text(encoding="utf-8").strip()
    packed = common / "packed-refs"
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

# ------------------------------------------------------------- cross-package contract
def test_the_two_state_vocabularies_are_pinned_apart() -> None:
    """`state` in telemetry is an outcome; `status` in the queue is a lifecycle.

    They share three spellings (RUNNING, BLOCKED, FAILED), which is exactly how a
    reader starts treating two different axes as one fact: an executor recording
    `DONE` has said nothing at all about verification, and a queue row saying
    `RUNNING` has said nothing about cost. The mapping is total in one direction and
    deliberately lossy in the other, and that is asserted here rather than assumed.
    """
    import importlib.util

    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # @dataclass inside agent_record.py resolves its own module through
        # sys.modules during exec_module, so registering it is not optional.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    record = _load("agent_record_under_test", REPO_ROOT / "tools" / "agent_record.py")

    assert set(record.STATES).isdisjoint(set(qc.STATES) - {"RUNNING", "BLOCKED", "FAILED"}), (
        "the two vocabularies drifted from 'different axes, three shared words' into "
        "a partial copy, which is worse than either")
    assert set(record.STATES) != set(qc.STATES)
    assert {w for w in record.STATES if w in qc.STATES} == {"RUNNING", "BLOCKED", "FAILED"}

    # total on the outcome side, and honest about the one outcome that implies nothing
    assert set(record.OUTCOME_TO_LIFECYCLE) == set(record.STATES)
    assert record.OUTCOME_TO_LIFECYCLE["UNKNOWN"] is None
    assert record.OUTCOME_TO_LIFECYCLE["DONE"] == "ACCEPTED"
    assert record.OUTCOME_TO_LIFECYCLE["DONE"] != "VERIFIED", (
        "an executor self-declaring DONE must never map onto a verified queue row")

    mapped = {v for v in record.OUTCOME_TO_LIFECYCLE.values() if v is not None}
    assert mapped <= set(qc.STATES), f"the map points at states the queue does not know: {mapped}"
    assert set(record.FAILURE_STATES) == {
        key for key, value in record.OUTCOME_TO_LIFECYCLE.items()
        if value in {"BLOCKED", "FAILED", "CANCELLED"}
    }, "a failure outcome that maps to a non-failure lifecycle state would hide the reason"
    assert "DONE" not in record.FAILURE_STATES and "UNKNOWN" not in record.FAILURE_STATES

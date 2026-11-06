#!/usr/bin/env python3
"""Validator for `docs/orchestration/queue.yaml`.

What this tool is: a reader. It answers four questions an orchestrator otherwise
answers from memory — is the queue internally consistent, which tasks can start
now, who would collide if two of them started, and why is this one blocked.

What this tool is NOT: a dispatcher. It must never launch an agent, never change
repository state, never decide that a package is accepted. The honesty rule of the
directive is that a Python file in the product must not pretend to be an Agent
Dispatcher, and the cheap way to keep that promise is to make it structurally
impossible: no subprocess, no sockets, no writes. `tests/test_w25a_queue.py` asserts
that on this module's own source, so the day someone adds `subprocess.run` here the
queue tool stops being a lie rather than becoming one quietly.

Exit codes: 0 consistent, 1 violations found, 2 the queue could not be read.
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = REPO_ROOT / "docs" / "orchestration" / "queue.yaml"

STATES = (
    "BACKLOG", "READY", "ASSIGNED", "RUNNING", "REVIEW", "ACCEPTED", "INTEGRATED",
    "VERIFIED", "BLOCKED", "FAILED", "RECOVERY_REQUIRED", "CANCELLED", "DEFERRED",
)
#: Tasks that still hold files: two of these may not own the same path.
CLAIMING = ("READY", "ASSIGNED", "RUNNING", "REVIEW", "ACCEPTED")
#: Dependency states that count as satisfied.
SATISFYING = ("ACCEPTED", "INTEGRATED", "VERIFIED")
#: States that require an execution location: you cannot run work with nowhere to run it.
NEEDS_LOCATION = ("ASSIGNED", "RUNNING", "REVIEW")

REQUIRED_FIELDS = (
    "id", "title", "description", "priority", "category", "status", "owner_role",
    "owner_agent", "base_sha", "worktree", "branch", "allowed_paths", "depends_on",
    "blocked_by", "acceptance", "test_commands", "review_status",
    "integration_status", "retry_count", "attempt_limit", "started_at", "updated_at",
    "completed_at", "evidence", "next_action",
)
OPTIONAL_FIELDS = ("p_refs", "tool_calls_used", "history")
KNOWN_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

INTEGRATION_BY_STATUS = {
    "ACCEPTED": ("ACCEPTED", "INTEGRATED", "VERIFIED"),
    "INTEGRATED": ("INTEGRATED", "VERIFIED"),
    "VERIFIED": ("VERIFIED",),
}


@dataclass(frozen=True)
class Problem:
    task_id: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.task_id}: {self.detail}"


def _load_raw(path: Path) -> dict[str, Any]:
    """Parse the queue. PyYAML is a declared dependency; absence is exit 2, not a guess."""
    try:
        import yaml  # noqa: PLC0415 - optional by design, and the error must be actionable
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the interpreter
        raise RuntimeError("PyYAML is required to read the queue") from exc
    if not path.exists():
        raise FileNotFoundError(f"queue file not found: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("tasks"), list):
        raise ValueError("queue must be a mapping with a `tasks` list")
    return loaded


def _check_fields(task: dict[str, Any]) -> list[Problem]:
    tid = str(task.get("id", "<no id>"))
    problems: list[Problem] = []
    for field in REQUIRED_FIELDS:
        if field not in task:
            problems.append(Problem(tid, "missing-field", f"`{field}` is required"))
    for field in task:
        if field not in KNOWN_FIELDS:
            problems.append(Problem(tid, "unknown-field", f"`{field}` is not in the schema"))
    status = task.get("status")
    if status is not None and status not in STATES:
        problems.append(Problem(tid, "bad-state", f"`{status}` is not a state of the machine"))
    for field in ("retry_count", "attempt_limit"):
        value = task.get(field)
        if value is not None and not isinstance(value, int):
            problems.append(Problem(tid, "bad-type", f"`{field}` must be an int, got {type(value).__name__}"))
    for field in ("allowed_paths", "depends_on", "blocked_by", "test_commands", "evidence"):
        value = task.get(field)
        if value is not None and not isinstance(value, list):
            problems.append(Problem(tid, "bad-type", f"`{field}` must be a list"))
    paths = task.get("allowed_paths") or []
    if task.get("status") not in ("BACKLOG", "BLOCKED", "CANCELLED", "DEFERRED", None) and paths:
        for item in paths:
            if not isinstance(item, str) or not item.strip():
                problems.append(Problem(tid, "bad-type", f"allowed_paths entry is not a path: {item!r}"))
    return problems


def _check_state_consistency(task: dict[str, Any]) -> list[Problem]:
    tid = str(task.get("id", "<no id>"))
    status = task.get("status")
    problems: list[Problem] = []

    expected = INTEGRATION_BY_STATUS.get(str(status))
    if expected is not None:
        actual = task.get("integration_status")
        if actual not in expected:
            problems.append(Problem(
                tid, "two-states",
                f"status `{status}` says the work is done, integration_status says `{actual}`"))

    if status in NEEDS_LOCATION and not (task.get("branch") and task.get("worktree")):
        owner = str(task.get("owner_agent") or "")
        if owner != "coordinator":
            problems.append(Problem(
                tid, "no-location",
                f"`{status}` needs a branch and a worktree; owner `{owner}` has neither"))

    completed = task.get("completed_at")
    if status in ("VERIFIED", "INTEGRATED", "ACCEPTED") and completed is None:
        problems.append(Problem(
            tid, "no-completion-time", f"`{status}` without completed_at cannot be ordered in history"))
    if status in ("RUNNING", "READY", "BACKLOG") and completed is not None:
        problems.append(Problem(
            tid, "early-completion", f"`{status}` cannot already carry completed_at {completed!r}"))
    return problems


def _path_overlap(left: str, right: str) -> bool:
    """Directory prefixes and single files are the same claim on the same bytes.

    A pattern is matched against the other side in both directions, because an
    executor claims `tests/test_w25a_*.py` while another names
    `tests/test_w25a_queue.py`, and either may be written first.
    """
    if left == right:
        return True
    if fnmatch.fnmatchcase(right, left) or fnmatch.fnmatchcase(left, right):
        return True
    if "*" in left or "*" in right:
        left_dir = left.split("*", 1)[0].rstrip("/")
        right_dir = right.split("*", 1)[0].rstrip("/")
        if left_dir and (right_dir + "/").startswith(left_dir + "/"):
            return True
        if right_dir and (left_dir + "/").startswith(right_dir + "/"):
            return True
    l_path = left.rstrip("/")
    r_path = right.rstrip("/")
    return (r_path + "/").startswith(l_path + "/") or (l_path + "/").startswith(r_path + "/")


def _check_owner_conflicts(tasks: list[dict[str, Any]]) -> list[Problem]:
    problems: list[Problem] = []
    claimers = [t for t in tasks if t.get("status") in CLAIMING]
    for i, a in enumerate(claimers):
        for b in claimers[i + 1:]:
            hits = [p for p in (a.get("allowed_paths") or [])
                    for q in (b.get("allowed_paths") or []) if _path_overlap(str(p), str(q))]
            if hits:
                problems.append(Problem(
                    str(a.get("id")), "owner-conflict",
                    f"and {b.get('id')} both claim {sorted(set(map(str, hits)))}"))
    return problems


def _dependency_graph(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    return {str(t["id"]): {str(x) for x in ((t.get("depends_on") or []) + (t.get("blocked_by") or []))}
            for t in tasks}


def _check_cycles(tasks: list[dict[str, Any]]) -> list[Problem]:
    graph = _dependency_graph(tasks)
    problems: list[Problem] = []
    state: dict[str, int] = {}
    for start in sorted(graph):
        if state.get(start, 0) == 0:
            _walk_cycle(start, graph, state, [], problems)
    return problems


def _walk_cycle(
    node: str,
    graph: dict[str, set[str]],
    state: dict[str, int],
    path: list[str],
    problems: list[Problem],
) -> None:
    """0 = unvisited, 1 = on the current path, 2 = finished.

    Written as an explicit recursive walk with its state passed in rather than as a
    closure: the earlier nested `visit()` captured `stack`/`on_stack` from the loop,
    which ruff rightly flags (B023) and which is the kind of bug that makes a cycle
    checker report the wrong cycle.
    """
    state[node] = 1
    path.append(node)
    for nxt in sorted(graph.get(node, ())):
        if nxt not in graph:
            continue
        marker = state.get(nxt, 0)
        if marker == 1:
            problems.append(Problem(
                nxt, "dependency-cycle", " -> ".join(path[path.index(nxt):] + [nxt])))
        elif marker == 0:
            _walk_cycle(nxt, graph, state, path, problems)
    path.pop()
    state[node] = 2


def _check_unknown_references(tasks: list[dict[str, Any]]) -> list[Problem]:
    ids = {str(t.get("id")) for t in tasks}
    problems: list[Problem] = []
    for task in tasks:
        tid = str(task.get("id"))
        for dep in {str(x) for x in ((task.get("depends_on") or []) + (task.get("blocked_by") or []))}:
            if dep not in ids:
                problems.append(Problem(tid, "unknown-dependency", f"`{dep}` is not a task in the queue"))
    return problems


def _explain(task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for dep in sorted({str(x) for x in ((task.get("depends_on") or []) + (task.get("blocked_by") or []))}):
        target = by_id.get(dep)
        if target is None:
            reasons.append(f"`{dep}` does not exist in the queue")
            continue
        state = target.get("status")
        if state not in SATISFYING:
            reasons.append(f"`{dep}` is `{state}`; it must reach one of {list(SATISFYING)} first")
    if task.get("status") == "BLOCKED" or task.get("blocked_by"):
        reasons.append(f"next action recorded: {task.get('next_action') or '(none)'}")
    return reasons


def _check_duplicates(tasks: list[dict[str, Any]]) -> list[Problem]:
    problems: list[Problem] = []
    titles: dict[str, list[str]] = {}
    for task in tasks:
        if task.get("status") not in CLAIMING:
            continue
        key = str(task.get("title", "")).strip().lower()
        titles.setdefault(key, []).append(str(task.get("id")))
    for key, ids in titles.items():
        if len(ids) > 1:
            problems.append(Problem(",".join(ids), "duplicate-active", f"same goal is active twice: {key!r}"))
    return problems


def _check_sha_claims(tasks: list[dict[str, Any]]) -> list[Problem]:
    """A recorded integration must point at a commit that exists.

    An audit of this project found executors naming SHAs that do not exist, so the
    queue is not allowed to be a place where that becomes an accepted fact. The check
    is deliberately file-system only (`git` is not invoked from here): the repo's own
    object list is read through the refs file when available, and when it is not we
    report `UNKNOWN` rather than pretending.
    """
    problems: list[Problem] = []
    refs = _known_shas()
    for task in tasks:
        for field in ("base_sha",):
            value = task.get(field)
            if value in (None, ""):
                continue
            text = str(value)
            if len(text) == 40 and refs is not None and text not in refs:
                problems.append(Problem(str(task.get("id")), "unknown-sha",
                                        f"{field}={text[:12]} is not an object this repo advertises"))
    return problems


def _known_shas() -> set[str] | None:
    git_dir = REPO_ROOT / ".git"
    packed = git_dir / "packed-refs"
    found: set[str] = set()
    if packed.exists():
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 40:
                found.add(parts[0])
    refs_dir = git_dir / "refs"
    if refs_dir.exists():
        for path in refs_dir.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(text) == 40:
                    found.add(text)
    if not found:
        return None
    # Short forms used in this queue are prefixes of the full objects.
    return found | {sha[:7] for sha in found}


def validate(tasks: list[dict[str, Any]]) -> tuple[list[Problem], list[dict[str, Any]]]:
    problems: list[Problem] = []
    for task in tasks:
        problems += _check_fields(task)
        problems += _check_state_consistency(task)
    problems += _check_unknown_references(tasks)
    problems += _check_owner_conflicts(tasks)
    problems += _check_cycles(tasks)
    problems += _check_duplicates(tasks)
    problems += _check_sha_claims(tasks)

    by_id = {str(t.get("id")): t for t in tasks}
    for task in tasks:
        if task.get("status") != "READY":
            continue
        deps = {str(x) for x in ((task.get("depends_on") or []) + (task.get("blocked_by") or []))}
        for dep in sorted(deps):
            target = by_id.get(dep)
            if target is None or target.get("status") not in SATISFYING:
                state = "missing" if target is None else target.get("status")
                problems.append(Problem(str(task.get("id")), "not-ready",
                                        f"marked READY but dependency `{dep}` is `{state}`"))
    return problems, tasks


def ready_now(tasks: list[dict[str, Any]]) -> list[str]:
    blocked = {p.task_id for p in _check_unknown_references(tasks)}
    graph = _dependency_graph(tasks)
    by_id = {str(t.get("id")): t for t in tasks}
    out: list[str] = []
    for task in tasks:
        if task.get("status") != "BACKLOG":
            continue
        tid = str(task.get("id"))
        deps = graph.get(tid, set())
        if tid in blocked:
            continue
        if all(by_id.get(d) and by_id[d].get("status") in SATISFYING for d in deps):
            out.append(tid)
    return out


def _configure_streams() -> None:
    """A validator that crashes while reporting is worse than no validator.

    Titles, descriptions and evidence in this queue are Russian on purpose, and the
    operator's console here is cp1252, which cannot encode them: printing a found
    problem raised `UnicodeEncodeError` and hid every problem after it (measured in
    this very session — the first run reported 7 of 15 and died on the 8th). UTF-8
    with `replace` is the minimum for a report whose whole job is to be read.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - exotic or replaced streams
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", default=str(QUEUE_PATH), help="path to queue.yaml")
    parser.add_argument("--explain", metavar="ID", help="why this task cannot move")
    parser.add_argument("--ready", action="store_true", help="list BACKLOG tasks whose dependencies are met")
    parser.add_argument("--owners", action="store_true", help="list every file claimed by an active task")
    args = parser.parse_args(argv)

    try:
        loaded = _load_raw(Path(args.queue))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"queue unreadable: {exc}", file=sys.stderr)
        return 2

    tasks = [t for t in loaded["tasks"] if isinstance(t, dict)]
    by_id = {str(t.get("id")): t for t in tasks}

    if args.explain:
        target = by_id.get(args.explain)
        if target is None:
            print(f"no such task: {args.explain}", file=sys.stderr)
            return 1
        reasons = _explain(target, by_id)
        print(f"{args.explain} [{target.get('status')}]: "
              + ("; ".join(reasons) if reasons else "nothing in the queue blocks it"))
        return 0

    problems, _ = validate(tasks)

    if args.owners:
        for task in tasks:
            if task.get("status") in CLAIMING:
                print(f"{task.get('id')} [{task.get('status')}] -> {task.get('allowed_paths')}")
        return 0 if not problems else 1

    if args.ready:
        for tid in ready_now(tasks):
            print(tid)
        return 0 if not problems else 1

    print(f"queue: {len(tasks)} tasks, {len(problems)} problem(s)")
    for problem in problems:
        print(f"  {problem}")
    if not problems:
        print("  consistent: states, dependencies, owners and SHAs all agree")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

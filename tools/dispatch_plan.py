#!/usr/bin/env python3
"""Dispatch planner for `docs/orchestration/queue.yaml`.

What this tool is: a reader. It takes the queue records whose state makes them
dispatchable — the `DISPATCHABLE` subset of the `STATES` vocabulary of
`tools/queue_check.py` (defined below; the validator does not export it), not a
second state machine invented here — and prints the exact `git worktree add`
commands a coordinator must run to create one correctly-pinned worktree per task.

Why it exists (measured, this repo, wave 25): nine executors were dispatched and
all nine were raised on `cd21f29` — the stale tip of the second remote — instead
of the stated base `183daa4`, 253 commits apart. Half of them noticed; the other
half reported measurements of the wrong tree as findings about the project, and
their evidence was void even though their code merged. The rule that should have
stopped this lives in `docs/CONTINUATION_STATE.md` and was not applied, which is
precisely the kind of failure a tool must make impossible rather than inadvisable:

* the planner never guesses a base. It requires an explicit full 40-character
  SHA — a `--base` argument or a pinned `base_sha` on the record — and refuses
  to print a plan without one. "The SHA you were told" is the thing that went
  wrong; a short form like `cd21f29` is a summary, not a pin, so it is refused;
* every command is `git worktree add -b <branch> <path> <sha>`, never
  `git worktree add <path> <sha>`: without `-b` git reads the SHA as the branch
  name, a defect this project recorded once already;
* ownership overlaps are refused before any command exists, using the same path
  rules `queue_check.py` exports (`_path_overlap`, `_check_foreign_claims`) —
  imported, not re-derived;
* the planner checks overlaps against the other planned tasks, against the
  claiming and reserving records of the queue, and against foreign-claim paths
  handed in through `--foreign-claims` (the W25-B input style, reused).

What this tool is NOT: a dispatcher. It launches nothing — no subprocess, no
sockets, no writes; the AST honesty guard in `tests/test_w26qm_dispatch_plan.py`
checks this module's imports and call targets, and a behavioural test pins that
a run leaves the queue and the tree byte-identical. It prints commands and a
human or the coordinator runs them; real dispatch happens through the harness,
never through this script. An agent's report of its own base is not evidence, so
the plan closes with the one command that verifies each created worktree:
`git -C <path> rev-parse HEAD` must equal the pinned SHA, and the base itself
is a claim until `git cat-file -t <sha>` says `commit`. The planner can check
the refs files (like `queue_check._known_shas`) and annotates, but after `git
gc` that view is blind, so existence is never silently denied — it is named
and handed to `cat-file`.

Exit codes: 0 a plan was printed (or the queue is not asking for a dispatch),
1 a refusal: missing/short base, an overlap, a blocked or refused record,
2 the queue or the claims input could not be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:  # so the shared rules import works from any cwd
    sys.path.insert(0, str(_TOOLS_DIR))

import queue_check as qc  # noqa: E402  - the queue rules live there; import, never duplicate

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKTREE_ROOT = REPO_ROOT / ".qoder" / "worktrees"
WORKTREE_PREFIX = ".qoder/worktrees/"
_HEX = frozenset("0123456789abcdef")
_BRANCH_BANNED = set(" ~^:?*[\\")

#: States that mean "this belongs in a worktree": READY has its dependencies
#: closed but no owner bound yet; ASSIGNED has an owner and may still be waiting
#: for its tree to be created. A subset of the validator's STATES vocabulary,
#: not a second state machine. It lives HERE because the current queue_check.py
#: does not export it; the planner must not edit the validator to fit itself.
#: If the coordinator exports DISPATCHABLE there one day, this becomes an alias.
DISPATCHABLE = ("READY", "ASSIGNED")
if not set(DISPATCHABLE) <= set(qc.STATES):  # drift guard, not a second vocabulary
    raise RuntimeError("DISPATCHABLE must stay a subset of queue_check.STATES")


def _is_full_sha(text: str) -> bool:
    return len(text) == 40 and all(ch in _HEX for ch in text.lower())


def _slug(text: str) -> str:
    out: list[str] = []
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "task"


def _branch_for(task: dict[str, Any]) -> tuple[str, str]:
    branch = task.get("branch")
    if isinstance(branch, str) and branch.strip() and not (set(branch) & _BRANCH_BANNED):
        return branch.strip(), f"branch `{branch.strip()}` from the queue record"
    fallback = _slug(str(task.get("id", "task")))
    return fallback, f"branch `{fallback}` derived from the task id (record has branch={branch!r})"


def _worktree_path(task: dict[str, Any], branch: str, worktree_root: Path) -> tuple[Path, str]:
    """Absolute path under .qoder/worktrees/ — the one layout this project uses.

    The record's `worktree` field is honoured only while it stays under that
    prefix; anything else (`.`, an absolute foreign tree) is reported as the
    deviation it is and the default is planned instead.
    """
    root = Path(worktree_root)
    wt = task.get("worktree")
    if isinstance(wt, str) and wt.strip():
        posix = wt.strip().replace("\\", "/")
        if posix.startswith("./"):
            posix = posix[2:]
        if posix.startswith(WORKTREE_PREFIX):
            tail = posix[len(WORKTREE_PREFIX):].strip("/")
            if tail and ".." not in Path(tail).parts:
                return root / tail, f"worktree path from the queue record (`{wt.strip()}`)"
    return root / branch, f"default worktree path (record has worktree={wt!r})"


def _resolve_base(task: dict[str, Any], base: str | None) -> tuple[str | None, list[str]]:
    """The pinned SHA or a refusal. The planner never defaults a base and never trusts a short."""
    pinned = task.get("base_sha")
    pinned_s = str(pinned).strip() if pinned not in (None, "") else ""
    if base and pinned_s and base != pinned_s:
        return None, [
            f"two bases disagree and I refuse to pick: --base says {base}, "
            f"the record pins base_sha={pinned_s}"]
    candidate = base or pinned_s
    if not candidate:
        return None, [
            "no base given: pass --base <full-40-char-SHA> or pin a full base_sha on the "
            "record — the wave-25 dispatch used a base nobody had pinned"]
    if not _is_full_sha(candidate):
        return None, [
            f"`{candidate}` is not a full 40-character SHA: a short form is a summary, "
            "and summaries of the wrong remote pinned nine executors onto cd21f29"]
    return candidate.lower(), []


def _base_reality(sha: str, known: set[str] | None) -> str:
    if known is None:
        return (f"base existence is UNKNOWN from this tree (refs not enumerable): run "
                f"`git cat-file -t {sha}` and require `commit` before dispatching")
    if sha in known or sha[:7] in known:
        return f"base {sha[:12]}… is advertised by this repo's refs (still verify the " \
               "created tree with rev-parse; refs alone do not prove the checkout landed)"
    return (f"WARNING: {sha[:12]}… is not advertised by this repo's refs — a reported SHA "
            f"is a claim (a2e5c90, 131895c were once reported and did not exist); prove it "
            f"with `git cat-file -t {sha}` before running the command")


def _select(
    tasks: list[dict[str, Any]],
    ids: list[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {str(t.get("id")): t for t in tasks}
    refusals: list[dict[str, Any]] = []
    if ids is None:
        chosen = [t for t in tasks if t.get("status") in DISPATCHABLE]
        return chosen, refusals
    chosen = []
    for want in ids:
        task = by_id.get(str(want))
        if task is None:
            refusals.append({"id": str(want), "status": "-", "title": "",
                             "reasons": ["no such task in the queue"], "blockers": []})
            continue
        if task.get("status") not in DISPATCHABLE:
            refusals.append({"id": str(want), "status": str(task.get("status")),
                             "title": str(task.get("title") or ""),
                             "reasons": [f"status `{task.get('status')}` is not dispatchable "
                                         f"({'/'.join(DISPATCHABLE)}); I do not replan work "
                                         "that already has a tree, and I do not restate the "
                                         "state machine here"],
                             "blockers": []})
            continue
        chosen.append(task)
    return chosen, refusals


def _hits(left: list[Any], right: list[Any]) -> list[str]:
    return sorted({str(p) for p in left
                   for q in right if qc._path_overlap(str(p), str(q))})


def _pair_conflicts(chosen: list[dict[str, Any]]) -> list[qc.Problem]:
    """Overlap inside the plan itself: fatal, because the two commands together are wrong."""
    problems: list[qc.Problem] = []
    for i, a in enumerate(chosen):
        for b in chosen[i + 1:]:
            hits = _hits(a.get("allowed_paths") or [], b.get("allowed_paths") or [])
            if hits:
                problems.append(qc.Problem(
                    f"{a.get('id')}+{b.get('id')}", "plan-owner-conflict",
                    f"`{a.get('id')}` and `{b.get('id')}` both claim {hits} — "
                    "no commands are emitted until one owner gives ground"))
    return problems


def _plan_task(
    task: dict[str, Any],
    base: str | None,
    chosen_ids: set[str],
    tasks: list[dict[str, Any]],
    foreign_claims: list[dict[str, Any]] | None,
    worktree_root: Path,
    known: set[str] | None,
) -> dict[str, Any]:
    tid = str(task.get("id"))
    branch, branch_note = _branch_for(task)
    wpath, path_note = _worktree_path(task, branch, worktree_root)
    paths = [str(p) for p in (task.get("allowed_paths") or [])]
    sha, reasons = _resolve_base(task, base)
    notes: list[str] = [branch_note, path_note]
    blockers: list[qc.Problem] = []
    if sha:
        notes.append(_base_reality(sha, known))
        for other in tasks:
            oid = str(other.get("id"))
            if oid == tid or oid in chosen_ids:
                continue
            status = other.get("status")
            if status not in qc.CLAIMING and status not in qc.RESERVING:
                continue
            hits = _hits(paths, other.get("allowed_paths") or [])
            if hits:
                blockers.append(qc.Problem(
                    tid, "queue-ownership",
                    f"`{oid}` (`{status}`, owner `{other.get('owner_agent')}`) already holds "
                    f"{hits} — dispatching this task would put a second owner on those bytes"))
        if foreign_claims:
            blockers += qc._check_foreign_claims([task], foreign_claims)
    return {
        "id": tid, "status": str(task.get("status")), "title": str(task.get("title") or ""),
        "owner": str(task.get("owner_agent") or "(unassigned)"), "branch": branch,
        "path": wpath.as_posix(), "base": sha, "paths": paths, "notes": notes,
        "reasons": reasons, "blockers": blockers, "ok": not reasons and not blockers,
    }


def build_plan(
    tasks: list[dict[str, Any]],
    *,
    base: str | None = None,
    ids: list[str] | None = None,
    foreign_claims: list[dict[str, Any]] | None = None,
    worktree_root: Path = DEFAULT_WORKTREE_ROOT,
) -> dict[str, Any]:
    chosen, refused = _select(tasks, ids)
    fatal = _pair_conflicts(chosen)
    known = qc._known_shas()
    chosen_ids = {str(t.get("id")) for t in chosen}
    entries = [_plan_task(t, base, chosen_ids, tasks, foreign_claims, worktree_root, known)
               for t in chosen]
    planned = [e for e in entries if e["ok"]]
    refused += [e for e in entries if not e["ok"]]
    return {
        "tasks": tasks, "chosen": chosen, "planned": planned, "refused": refused,
        "fatal": fatal, "suppress_commands": bool(fatal),
        "state_summary": _state_summary(tasks),
    }


def _state_summary(tasks: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[str(task.get("status"))] = counts.get(str(task.get("status")), 0) + 1
    return ", ".join(f"{state}={count}" for state, count in sorted(counts.items())) or "empty"


def render(plan: dict[str, Any]) -> list[str]:
    lines = [
        "dispatch plan — read-only: the planner prints commands, a human or the coordinator runs them",
        f"run every command from: {REPO_ROOT.as_posix()}",
        "base rule: explicit full SHA only; never guessed, never a short form",
    ]
    if plan["fatal"]:
        lines.append("REFUSED: two planned tasks claim the same bytes; no commands are emitted.")
        lines += [f"  {problem}" for problem in plan["fatal"]]
    shown = [] if plan["suppress_commands"] else plan["planned"]
    for entry in shown:
        lines.append(f"== {entry['id']} [{entry['status']}] {entry['title']}")
        owns = ", ".join(entry["paths"]) or "(no allowed_paths — nothing overlaps, but nothing " \
                                           "protects the merge either)"
        lines.append(f"   owner: {entry['owner']}")
        lines.append(f"   owns: {owns}")
        for note in entry["notes"]:
            lines.append(f"   note: {note}")
        lines.append(f"   command: git worktree add -b {entry['branch']} {entry['path']} "
                     f"{entry['base']}")
        lines.append(f"   verify:  git -C {entry['path']} rev-parse HEAD  must print exactly "
                     f"{entry['base']}")
    for entry in plan["refused"]:
        reasons = "; ".join(entry["reasons"]) if entry.get("reasons") else ""
        for problem in entry.get("blockers") or []:
            reasons = f"{reasons}; {problem}" if reasons else str(problem)
        title = entry.get("title") or ""
        label = f"{entry['id']} [{entry.get('status', '-')}]" + (f" {title}" if title else "")
        lines.append(f"refused {label}: {reasons}")
    if not plan["chosen"] and not plan["refused"]:
        lines.append(f"no dispatchable records: queue states are {_state_counts(plan)} — "
                     f"dispatchable means {'/'.join(DISPATCHABLE)}")
        lines.append("nothing to plan is the honest output; an empty plan built on a guessed "
                     "base is how wave 25 happened")
    if shown:
        lines.append("self-check after creating each worktree — an agent's report of its own "
                     "base is not evidence:")
        for entry in shown:
            lines.append(f"   git -C {entry['path']} rev-parse HEAD == {entry['base']}")
        lines.append("and the base you pinned is itself a claim until `git cat-file -t <sha>` "
                     "says `commit`")
    lines.append(f"summary: {len(shown)} planned, {len(plan['refused'])} refused, "
                 f"{len(plan['fatal'])} fatal overlap(s); queue: {len(plan['tasks'])} records "
                 f"({_state_counts(plan)})")
    return lines


def _state_counts(plan: dict[str, Any]) -> str:
    return plan["state_summary"]


def _exit_code(plan: dict[str, Any]) -> int:
    return 1 if (plan["fatal"] or plan["refused"]) else 0


def main(argv: list[str] | None = None) -> int:
    qc._configure_streams()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", default=str(qc.QUEUE_PATH), help="path to queue.yaml")
    parser.add_argument("queue_path", nargs="?", default=None,
                        help="queue.yaml as a positional argument (alternative to --queue)")
    parser.add_argument("--base", metavar="SHA",
                        help="full 40-character SHA every planned worktree is created on; "
                             "required unless each record pins its own full base_sha")
    parser.add_argument("--id", action="append", dest="ids", default=None, metavar="ID",
                        help="plan only these tasks (still refused unless their state is "
                             "dispatchable)")
    parser.add_argument("--foreign-claims", metavar="SPEC", action="append", default=[],
                        help="claims from outside the queue, same input style as queue_check: "
                             "inline `git status --porcelain`, a path to such a dump, or a "
                             "JSON/YAML list of {owner, paths}")
    parser.add_argument("--foreign-owner", default=qc.DEFAULT_FOREIGN_OWNER,
                        help="owner name for unstructured foreign claims (default: %(default)s)")
    parser.add_argument("--worktree-root", default=str(DEFAULT_WORKTREE_ROOT),
                        help="absolute root the worktree paths are planned under "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)

    try:
        loaded = qc._load_raw(Path(args.queue_path or args.queue))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"queue unreadable: {exc}", file=sys.stderr)
        return 2

    base = None
    if args.base:
        base = str(args.base).strip().lower()
        if not _is_full_sha(base):
            print(f"refusing to plan: --base `{args.base}` is not a full 40-character SHA. "
                  "The planner exists because 'the SHA you were told' pinned nine executors "
                  "onto the wrong remote; bring evidence, not a prefix.")
            return 1

    try:
        foreign: list[dict[str, Any]] = []
        for spec in args.foreign_claims:
            foreign += qc._load_foreign_claims(spec, args.foreign_owner)
    except OSError as exc:
        print(f"foreign claims unreadable: {exc}", file=sys.stderr)
        return 2

    plan = build_plan(
        [t for t in loaded["tasks"] if isinstance(t, dict)],
        base=base,
        ids=args.ids,
        foreign_claims=foreign,
        worktree_root=Path(args.worktree_root),
    )
    for line in render(plan):
        print(line)
    return _exit_code(plan)


if __name__ == "__main__":
    sys.exit(main())
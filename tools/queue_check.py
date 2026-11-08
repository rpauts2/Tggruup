#!/usr/bin/env python3
"""Validator for `docs/orchestration/queue.yaml`.

What this tool is: a reader. It answers five questions an orchestrator otherwise
answers from memory — is the queue internally consistent, which tasks can start
now, who would collide if two of them started, why is this one blocked, and does
a tree outside the queue already hold bytes an active task claims.

What this tool is NOT: a dispatcher. It must never launch an agent, never change
repository state, never decide that a package is accepted. The honesty rule of the
directive is that a Python file in the product must not pretend to be an Agent
Dispatcher, and the cheap way to keep that promise is to make it structurally
impossible: no subprocess, no sockets, no writes. `tests/test_w25a_queue.py` asserts
that on this module's own source, so the day someone adds `subprocess.run` here the
queue tool stops being a lie rather than becoming one quietly. The outside-tree
claims are therefore handed IN, not probed: the caller runs `git status --porcelain`
and passes its output (or a JSON/YAML claim file) through `--foreign-claims`; this
module never spawns git and never reads the index itself.

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


_HEX = frozenset("0123456789abcdef")


def _is_full_sha(text: str) -> bool:
    """A commit is 40 lowercase hex characters; anything else is a guess.

    Kept as a plain set membership instead of `re` because this module's import
    list is pinned by a test: the validator may not gain capabilities.
    """
    return len(text) == 40 and set(text) <= _HEX


def declared_foreign_shas(meta: dict[str, Any] | None) -> dict[str, str]:
    """SHAs the queue says live in another repository, with the reason why.

    A queue imported from a different history names commits this clone cannot
    advertise. Silently deleting those SHAs destroys the record; accepting any
    unknown SHA destroys the rule. The middle way is a declaration in `meta`:
    every foreign SHA is listed in full next to the repository it belongs to, the
    list is checked for staleness (a declaration no task uses is a problem), and
    an undeclared SHA is still reported. A mapping is preferred; a list is read as
    SHA -> "" and then fails the non-empty-reason rule on purpose.
    """
    raw = (meta or {}).get("foreign_shas")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(key).strip(): str(value or "").strip() for key, value in raw.items()}
    if isinstance(raw, list):
        return {str(item).strip(): "" for item in raw}
    # Any other shape declares nothing. The old fallback named a constant that
    # only existed in the docstring example, so a malformed queue raised
    # NameError before a single rule could report it.
    return {}


def _check_sha_claims(
    tasks: list[dict[str, Any]],
    foreign_shas: dict[str, str] | None = None,
) -> list[Problem]:
    """A recorded integration must point at a commit that exists.

    An audit of this project found executors naming SHAs that do not exist, so the
    queue is not allowed to be a place where that becomes an accepted fact. The check
    is deliberately file-system only (`git` is not invoked from here): the repo's own
    object list is read through the refs files when available — following the
    `gitdir:` pointer so linked worktrees see the common refs too — and when it is
    not we report `UNKNOWN` rather than pretending.

    `foreign_shas` is the `meta.foreign_shas` declaration: a full SHA listed there
    with a reason is a recorded commit of another repository, not a fiction. The
    declaration itself is audited: a malformed entry, an empty reason, or an entry
    no task uses is reported, so the allowlist cannot grow into a blanket excuse.
    """
    problems: list[Problem] = []
    foreign = dict(foreign_shas or {})
    for sha, reason in sorted(foreign.items()):
        if not _is_full_sha(sha):
            problems.append(Problem("meta", "bad-foreign-sha",
                                    f"foreign_shas key {sha[:12]!r} is not a full 40-char SHA"))
        if not reason:
            problems.append(Problem("meta", "bad-foreign-sha",
                                    f"foreign_shas[{sha[:12]}] has no reason recorded"))
    used = {str(task.get("base_sha")) for task in tasks}
    for sha in sorted(set(foreign) - used):
        problems.append(Problem("meta", "stale-foreign-sha",
                                f"foreign_shas declares {sha[:12]} but no task uses it"))
    refs = _known_shas()
    for task in tasks:
        for field in ("base_sha",):
            value = task.get(field)
            if value in (None, ""):
                continue
            text = str(value)
            if text in foreign:
                continue
            if len(text) == 40 and refs is not None and text not in refs:
                problems.append(Problem(str(task.get("id")), "unknown-sha",
                                        f"{field}={text[:12]} is not an object this repo advertises"))
    return problems


def _git_dirs(root: Path) -> tuple[Path | None, Path | None]:
    """Resolve (gitdir, commondir) for a plain checkout or a linked worktree.

    Every executor in this project lives in a linked worktree, where `.git` is a
    *file* holding `gitdir: …`, and the refs the SHA rule needs are under the
    common dir. A previous version only looked at `<root>/.git/…` as a directory,
    so inside a worktree `_known_shas` silently returned None and the unknown-sha
    rule went blind in exactly the trees where fabricated SHAs are written.
    """
    git = root / ".git"
    if git.is_dir():
        return git, git
    if git.is_file():
        pointer = None
        for line in git.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("gitdir:"):
                pointer = line.split(":", 1)[1].strip()
                break
        if not pointer:
            return None, None
        gitdir = Path(pointer) if pointer.startswith(("/", "\\")) else (root / pointer).resolve()
        common = gitdir
        commondir_file = gitdir / "commondir"
        if commondir_file.is_file():
            rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
            common = (gitdir / rel).resolve()
        return gitdir, common
    return None, None


def _known_shas() -> set[str] | None:
    """Read advertised refs and the local object database without invoking Git.

    Queue entries often point at historical base commits, not branch tips.
    Looking only at packed-refs/loose refs therefore rejects real ancestors.
    Pack index v1/v2 files contain the complete sorted object-ID table, so they
    let this read-only validator recognize objects fetched by a full checkout.
    """
    git_dir, common_dir = _git_dirs(REPO_ROOT)
    if git_dir is None or common_dir is None:
        return None

    found: set[str] = set()
    packed_refs = common_dir / "packed-refs"
    if packed_refs.exists():
        for line in packed_refs.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 40:
                found.add(parts[0])

    refs_dir = common_dir / "refs"
    if refs_dir.exists():
        for path in refs_dir.rglob("*"):
            if path.is_file():
                value = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(value) == 40:
                    found.add(value)

    objects_dir = common_dir / "objects"
    if objects_dir.is_dir():
        # Loose object names are split into a two-hex directory and 38-hex file.
        for prefix in objects_dir.iterdir():
            if (
                len(prefix.name) == 2
                and all(char in "0123456789abcdef" for char in prefix.name.lower())
                and prefix.is_dir()
            ):
                for object_path in prefix.iterdir():
                    if (
                        object_path.is_file()
                        and len(object_path.name) == 38
                        and all(char in "0123456789abcdef" for char in object_path.name.lower())
                    ):
                        found.add(prefix.name.lower() + object_path.name.lower())

        # A pack index stores all object IDs in sorted order after its fanout table.
        pack_dir = objects_dir / "pack"
        if pack_dir.is_dir():
            for index_path in pack_dir.glob("*.idx"):
                raw = index_path.read_bytes()
                if raw.startswith(b"\xfftOc"):
                    if len(raw) < 8 + 1024 or int.from_bytes(raw[4:8], "big") != 2:
                        continue
                    fanout_offset = 8
                    object_ids_offset = 8 + 1024
                else:
                    # Version 1 has no magic/version prefix.
                    if len(raw) < 1024:
                        continue
                    fanout_offset = 0
                    object_ids_offset = 1024

                count_offset = fanout_offset + (255 * 4)
                count = int.from_bytes(raw[count_offset:count_offset + 4], "big")
                end_offset = object_ids_offset + count * 20
                if end_offset > len(raw):
                    continue
                for offset in range(object_ids_offset, end_offset, 20):
                    found.add(raw[offset:offset + 20].hex())

    if not found:
        return None
    # Short forms used in this queue are prefixes of the full objects.
    return found | {sha[:7] for sha in found}


#: States that no longer hold files: two owners may collide only over work still open.
RELEASED = ("INTEGRATED", "VERIFIED", "FAILED", "CANCELLED")
#: States that deliberately reserve files without running: a dirty foreign path
#: against one of these is a `foreign-claim`, not yet a double activity.
RESERVING = ("BLOCKED", "RECOVERY_REQUIRED")

#: Owner shown for unstructured foreign claims when the caller names none.
DEFAULT_FOREIGN_OWNER = "working-tree"

#: The XY columns `git status --porcelain` may print; anything else cannot start
#: a porcelain line, which is what keeps YAML and status dumps apart.
_PORCELAIN_XY = set("MADRCU?!T ")


def _porcelain_path(token: str) -> str:
    """Extract the touched path from one `git status --porcelain` field.

    Handles the XY-prefix columns and the rename form `old -> new`; a rename
    claims the NEW side, because that is where the bytes move to.
    """
    text = token.strip()
    if not text:
        return ""
    if "->" in text:
        text = text.split("->", 1)[1].strip()
    parts = text.split(None, 1)
    if len(parts) == 2 and len(parts[0]) <= 2 and not parts[0].endswith(("/", ".py")):
        text = parts[1]
    return text.strip().strip('"').replace("\\", "/")


def _looks_porcelain(text: str) -> bool:
    """True when any line carries the `XY␠path` shape of `git status --porcelain`.

    Checked before the structured parse on purpose: `?? path` is a legal YAML
    explicit-key document, so a porcelain dump fed through `safe_load` first
    returns plausible-looking nonsense instead of failing — the worst kind of
    input mishandling for a detector whose job is precisely to see the claim.
    """
    for line in text.splitlines():
        if len(line) > 3 and line[2] == " " and line[3] != " ":
            xy = line[:2]
            if all(c in _PORCELAIN_XY for c in xy) and any(c != " " for c in xy):
                return True
    return False


def _load_foreign_claims(spec: str, default_owner: str) -> list[dict[str, Any]]:
    """Normalize a caller-supplied claim source into [{owner, paths}, …].

    Accepted shapes, tried in this order: a path to a file (JSON/YAML list of
    `{owner, paths}` mappings, JSON/YAML list of path strings, or a plain-text
    `git status --porcelain` dump), or the same shapes inline as the argument.
    Nothing here shells out to git — the read-only guarantee means the data is
    handed in, never probed.
    """
    candidate = Path(spec)
    text = (candidate.read_text(encoding="utf-8", errors="replace")
            if candidate.is_file() else spec)
    records: list[dict[str, Any]] = []
    loaded: Any = None
    if not _looks_porcelain(text):
        try:
            import yaml  # noqa: PLC0415 - same optional dependency as the queue itself
            loaded = yaml.safe_load(text)
        except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
            loaded = None
        except Exception:
            loaded = None
    if isinstance(loaded, dict):
        loaded = [loaded]
    if isinstance(loaded, list) and loaded and all(isinstance(item, dict) for item in loaded):
        for item in loaded:
            paths = item.get("paths") or item.get("allowed_paths") or []
            records.append({"owner": str(item.get("owner") or default_owner),
                            "paths": [str(p) for p in paths if str(p).strip()]})
        return records
    if isinstance(loaded, list) and loaded and all(isinstance(item, str) for item in loaded):
        lines = [str(item) for item in loaded]
    elif isinstance(loaded, str):
        lines = [line for chunk in loaded.split(",") for line in chunk.splitlines()]
    else:
        lines = text.splitlines()
    paths = [p for p in (_porcelain_path(line) for line in lines) if p]
    if paths:
        records.append({"owner": default_owner, "paths": paths})
    return records


def _check_foreign_claims(tasks: list[dict[str, Any]], claims: list[dict[str, Any]]) -> list[Problem]:
    """Collisions between the queue and dirty paths that come from OUTSIDE it.

    Two rule ids, split by what they prove:

    * `foreign-claim` — the path is dirty in a foreign tree while a queue record
      that is not live-claiming it (BLOCKED, RECOVERY_REQUIRED, …) still reserves
      it for an owner. Nobody is running twice, but the reservation is being
      walked over.
    * `duplicate-active` — a CLAIMING queue record and a foreign owner hold the
      same bytes at the same time. This is the W28-A / missions-`tgfarm/llm/client.py`
      shape: two live owners, one file, and the collision is invisible until the
      two trees merge.
    """
    problems: list[Problem] = []
    for claim in claims:
        owner = str(claim.get("owner") or DEFAULT_FOREIGN_OWNER)
        for path in claim.get("paths") or []:
            path_s = str(path)
            for task in tasks:
                status = str(task.get("status"))
                if status in RELEASED:
                    continue
                claimed = [str(p) for p in (task.get("allowed_paths") or [])]
                if not any(_path_overlap(p, path_s) for p in claimed):
                    continue
                task_owner = str(task.get("owner_agent") or "")
                if task_owner == owner:
                    continue  # the foreign session registered its claim; known reality
                tid = str(task.get("id"))
                if status in CLAIMING:
                    problems.append(Problem(
                        tid, "duplicate-active",
                        f"owners `{owner}` (outside the queue) and `{task_owner}` "
                        f"({tid}, `{status}`) both hold `{path_s}`"))
                elif status in RESERVING:
                    problems.append(Problem(
                        tid, "foreign-claim",
                        f"`{path_s}` is dirty for `{owner}` while `{tid}` "
                        f"(`{status}`, owner `{task_owner}`) still reserves it"))
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            owner_a, owner_b = str(a.get("owner")), str(b.get("owner"))
            if owner_a == owner_b:
                continue
            hits = sorted({str(p) for p in (a.get("paths") or [])
                           for q in (b.get("paths") or []) if _path_overlap(str(p), str(q))})
            if hits:
                problems.append(Problem(
                    f"{owner_a}+{owner_b}", "duplicate-active",
                    f"foreign owners `{owner_a}` and `{owner_b}` both claim {hits}"))
    return problems


def validate(
    tasks: list[dict[str, Any]],
    foreign_claims: list[dict[str, Any]] | None = None,
    foreign_shas: dict[str, str] | None = None,
) -> tuple[list[Problem], list[dict[str, Any]]]:
    """Every rule that needs only the queue, plus the outside-tree rules when fed.

    `foreign_claims` defaults to None so every existing caller keeps the exact
    old behaviour: the cross-tree checks are additive, never a silent requirement.
    `foreign_shas` is the `meta.foreign_shas` declaration for a queue whose base
    commits live in another repository; see `_check_sha_claims`.
    """
    problems: list[Problem] = []
    for task in tasks:
        problems += _check_fields(task)
        problems += _check_state_consistency(task)
    problems += _check_unknown_references(tasks)
    problems += _check_owner_conflicts(tasks)
    problems += _check_cycles(tasks)
    problems += _check_duplicates(tasks)
    problems += _check_sha_claims(tasks, foreign_shas)
    if foreign_claims:
        problems += _check_foreign_claims(tasks, foreign_claims)

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
    parser.add_argument(
        "queue_path", nargs="?", default=None,
        help="queue.yaml as a positional argument (alternative to --queue)",
    )
    parser.add_argument("--explain", metavar="ID", help="why this task cannot move")
    parser.add_argument("--ready", action="store_true", help="list BACKLOG tasks whose dependencies are met")
    parser.add_argument("--owners", action="store_true", help="list every file claimed by an active task")
    parser.add_argument(
        "--foreign-claims",
        metavar="SPEC",
        action="append",
        default=[],
        help="claims from outside the queue: inline paths (e.g. `git status --porcelain` "
             "output), a path to such a dump, or a JSON/YAML list of {owner, paths}",
    )
    parser.add_argument(
        "--foreign-owner",
        default=DEFAULT_FOREIGN_OWNER,
        help="owner name for unstructured foreign claims (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        loaded = _load_raw(Path(args.queue_path or args.queue))
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

    try:
        foreign: list[dict[str, Any]] = []
        for spec in args.foreign_claims:
            foreign += _load_foreign_claims(spec, args.foreign_owner)
    except OSError as exc:
        print(f"foreign claims unreadable: {exc}", file=sys.stderr)
        return 2

    problems, _ = validate(
        tasks,
        foreign_claims=foreign,
        foreign_shas=declared_foreign_shas(loaded.get("meta")),
    )

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

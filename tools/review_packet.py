"""W26-T: assemble the acceptance evidence packet for one package — strictly read-only.

Six executors work in parallel branches and the coordinator re-derives the same
facts by hand for every package: do the SHAs the agent named exist at all, is the
base really an ancestor of the head, which files moved, were existing tests
weakened, what did ruff say before and after. The forensic audit found real
packages that named a SHA which does not exist, and several that arrived with a
test expectation quietly rewritten. This tool makes those checks repeatable and
cheap instead of memory-dependent, and it never accepts anything: the last line
is always `verdict: NEEDS_HUMAN`.

Design rules, in the order they bit:

* **every git call goes through `run_git`, which refuses any subcommand outside
  `GIT_READONLY_SUBCOMMANDS`** — the tool cannot add, commit, check out, reset,
  merge or stash, so running it next to five live executors is safe. The
  allowlist is asserted on this module's own source by
  `tests/test_w26t_review_packet.py`, so the guarantee is structural, not a
  promise in a docstring.
* **no app import, no database**. `tgfarm.db` is live and an app import can
  bootstrap or migrate it, so this file imports only the standard library and
  reads everything through `git show`/`git diff` blobs rather than the working
  tree. The head is never checked out.
* **absent evidence is reported as absent, never as zero**. A missing ruff, an
  unreadable blob, a ref that does not resolve each carry their own status
  (`NOT_MEASURED` / `FAIL`) instead of a plausible-looking 0. Same rule the rest
  of the project applies to a number it cannot point at.

What the packet CANNOT see is printed in its own section: an assertion rewritten
in place with a different expected literal still reads as one removed and one
added line; a test that was already wrong at base is not made visible by this
diff; and the AST parse checks syntax, not that an import resolves. It narrows
what a human must look at — it does not replace the look.

Usage:
    python tools/review_packet.py --base 91029a3 --head 7e58455 [--branch main]
                                  [--against main] [--out packet.txt] [--repo PATH]
                                  [--task W28-A] [--queue docs/orchestration/queue.yaml]

W25-B addition: the packet is owner-aware. A reviewed package can be perfectly
clean and still collide, because the queue holds a different live owner for the
same bytes (the W28-A / missions `tgfarm/llm/client.py` shape: two trees, one
file, neither visible to the other's diff). Section `3c` therefore lists every
queue record whose `allowed_paths` overlap a changed path, naming that record's
status and owner, so the reviewer sees the collision before approving — it is
information for the human, never an automatic veto, and the verdict line stays
`NEEDS_HUMAN` either way.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Loaded lazily; the collision rule must have ONE owner, and it lives in the
#: validator, not here. Copying `_path_overlap` into this file would let the two
#: tools disagree about what a collision is — the exact failure the queue exists
#: to prevent (W25-B design decision).
_queue_check_module: Any = None


def _queue_check() -> Any:
    global _queue_check_module
    if _queue_check_module is None:
        path = Path(__file__).with_name("queue_check.py")
        spec = importlib.util.spec_from_file_location("queue_check_for_packet", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load the queue validator from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(spec.name, module)
        spec.loader.exec_module(module)
        _queue_check_module = module
    return _queue_check_module


def _load_queue_tasks(queue_file: Path) -> list[dict[str, Any]]:
    """Read queue.yaml strictly as data; PyYAML missing is the caller's NOT_MEASURED."""
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("PyYAML is required to read the queue") from exc
    loaded = yaml.safe_load(queue_file.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("tasks"), list):
        raise ValueError("queue must be a mapping with a `tasks` list")
    return [task for task in loaded["tasks"] if isinstance(task, dict)]


def _claim_overlaps(
    tasks: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    exclude_task_id: str | None = None,
) -> list[dict[str, Any]]:
    """Queue records that hold some of the paths this diff touches."""
    qc = _queue_check()
    overlaps: list[dict[str, Any]] = []
    for task in tasks:
        tid = str(task.get("id"))
        if exclude_task_id is not None and tid == str(exclude_task_id):
            continue
        claimed = [str(p) for p in (task.get("allowed_paths") or [])]
        hits = sorted({path for path in changed_paths for q in claimed if qc._path_overlap(path, q)})
        if hits:
            overlaps.append({
                "id": tid,
                "status": str(task.get("status")),
                "owner": str(task.get("owner_agent") or "<no owner>"),
                "live": str(task.get("status")) in set(qc.CLAIMING) | set(qc.RESERVING),
                "paths": hits,
            })
    return overlaps


def _owner_header_lines(overlaps: list[dict[str, Any]], own: str) -> list[str]:
    """The owner-aware header lines, including the diff's own queue record.

    Returned, not printed, on purpose: `--out` writes wherever the caller says,
    so the only safe way to hand the same facts to a human without any file
    write is via the caller's own stream — `main` prints them before the packet.
    """
    live = [entry for entry in overlaps if entry["live"]]
    released = [entry for entry in overlaps if not entry["live"]]
    lines = [
        "OWNER HEADER (W25-B): who the queue says holds these bytes",
        f"  this package: {own}",
    ]
    if live:
        lines.append(f"  {len(live)} OTHER LIVE/RESERVED CLAIM(S) OVERLAP — reconcile before accepting:")
        for entry in live:
            lines.append(
                f"  - {entry['id']} [{entry['status']}] owner `{entry['owner']}` holds {entry['paths']}")
    else:
        lines.append("  no other queue record claims a changed path")
    for entry in released:
        lines.append(
            f"  - (released) {entry['id']} [{entry['status']}] owner `{entry['owner']}`"
            f" overlaps {entry['paths']}")
    return lines

#: The only git subcommands this module will ever spawn. Every one of them reads
#: and none of them move HEAD, the index or the working tree. Widening this set
#: would turn a review tool into a repo mutator, which is exactly what the
#: parallel-executor setup cannot tolerate.
GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "diff",
        "log",
        "show",
        "rev-parse",
        "merge-base",
        "cat-file",
        "status",
        "ls-tree",
    }
)

#: Ruff is invoked with the flags the project declares in pyproject.toml
#: (`line-length = 110`, `select = E,F,I,UP,B`, `ignore = B008`) plus
#: `--isolated`/`--target-version py311` so the count is reproducible from a git
#: blob in a temp directory instead of depending on whichever config the temp
#: path happens to inherit. CI runs 3.11, so py311 is the honest target.
RUFF_FLAGS = (
    "--line-length",
    "110",
    "--select",
    "E,F,I,UP,B",
    "--ignore",
    "B008",
    "--isolated",
    "--target-version",
    "py311",
)

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_NOT_MEASURED = "NOT_MEASURED"

VERDICT_LINE = "verdict: NEEDS_HUMAN"

#: Substrings that would mean this tool claims an acceptance decision for itself.
#: Spelled from fragments on purpose: a declaration that wrote either word out
#: in full would make the source-level test fail on its own definition instead
#: of on a real claim, which is the failure mode the test exists to catch.
FORBIDDEN_SOURCE_TOKENS = ("accept" + "_package", "APPRO" + "VED")

TEST_CATEGORY = "tests"
PRODUCTION_CATEGORY = "production"
DOCS_CATEGORY = "docs"
OTHER_CATEGORY = "other"

_ASSERT_LINE = re.compile(r"^\s*(?:assert)\b")
_RAISES_LINE = re.compile(r"(?:pytest\.raises|unittest\.expectedFailure|\braises\s*\()")
_DEF_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_LITERAL = re.compile(
    r"""("""
    r""""[^"]*"|'[^']*'"""
    r"""|\b\d+\.\d+\b|\b\d+\b"""
    r"""|\b(?:True|False|None)\b)"""
)
_E501_WIDTH = re.compile(r"E501.*?\((\d+)\s*>")

def force_utf8_streams() -> None:
    """Windows consoles default to cp1251/cp437 and turn every dash into a `?`.

    The packet and the telemetry summary are evidence documents; a mangled
    em dash in a quoted line is a transcription error the tool would cause
    itself, so stdout/stderr are re-opened as UTF-8 when the stream allows it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


class GitCommandNotAllowed(RuntimeError):
    """Raised before spawning anything when a git subcommand is not read-only."""


@dataclass
class GitResult:
    """One completed git invocation, kept with its argv so the packet can cite it."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()


@dataclass
class ChangedFile:
    path: str
    added: int
    removed: int
    category: str


@dataclass
class Weakening:
    """One piece of test-change evidence. No judgement, only the fact and where."""

    kind: str
    path: str
    line: int
    text: str
    #: every finding above is addressed in BASE coordinates
    in_base: bool = True


@dataclass
class Packet:
    """Everything the packet says, so a test can poke at facts instead of a blob."""

    lines: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"[{title}]")

    def item(self, label: str, status: str, detail: str = "") -> None:
        self.lines.append(f"  {label}: {status}{f'  {detail}' if detail else ''}")

    def raw(self, text: str = "") -> None:
        self.lines.extend(f"    {line}" if line else "" for line in text.splitlines())


def run_git(repo: Path, *args: str) -> GitResult:
    """Spawn one git command, refusing any subcommand that is not strictly read-only."""
    subcommand = args[0] if args else ""
    if subcommand not in GIT_READONLY_SUBCOMMANDS:
        raise GitCommandNotAllowed(
            f"refusing to run `git {subcommand or '(empty)'}`: only "
            f"{sorted(GIT_READONLY_SUBCOMMANDS)} are allowed in a review packet"
        )
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return GitResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)


def resolve_commit(repo: Path, ref: str) -> tuple[str | None, str]:
    """Return (full sha or None, subject of the commit) for a ref that may not exist."""
    result = run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if not result.ok or not result.out:
        return None, ""
    sha = result.out.splitlines()[0]
    subject = run_git(repo, "log", "-1", "--format=%s", sha).out
    return sha, subject


def blob(repo: Path, ref: str, path: str) -> str | None:
    """The bytes of `path` at `ref` as text, or None when it is not there."""
    result = run_git(repo, "show", f"{ref}:{path}")
    return result.stdout if result.ok else None


def classify(path: str) -> str:
    """Bucket a changed path. Production is what ships; everything else says so."""
    if path.startswith("tests/") or Path(path).name.startswith("test_"):
        return TEST_CATEGORY
    if path.startswith("docs/") or path.endswith(".md"):
        return DOCS_CATEGORY
    if path.startswith(("tgfarm/", "tools/", "config/", "src/")):
        return PRODUCTION_CATEGORY
    return OTHER_CATEGORY


def collect_changed_files(repo: Path, base: str, head: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    for result in run_git(repo, "diff", "--numstat", base, head).stdout.splitlines():
        parts = result.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[-1]
        # a rename arrives as `old => new` in the path column; keep the new name
        if "{" in path and " => " in path:
            path = path.split(" => ")[-1].rstrip("}").lstrip("{").strip()
        elif " => " in path:
            path = path.split(" => ")[-1]
        binary = added == "-" or removed == "-"
        files.append(
            ChangedFile(
                path=path,
                added=-1 if binary else int(added),
                removed=-1 if binary else int(removed),
                category=classify(path),
            )
        )
    return files


def _literals(text: str) -> list[str]:
    return _LITERAL.findall(text)


def _pair_score(old: str, new: str) -> float:
    return SequenceMatcher(None, old.strip(), new.strip()).ratio()


def _parse_hunks(diff: str) -> list[tuple[list[tuple[int, str]], list[tuple[int, str]]]]:
    """Split a unified diff into hunks of (removed base lines, added head lines).

    Removed lines are numbered in base coordinates, added lines in head
    coordinates, which is what a reviewer has to click on.
    """
    hunks: list[tuple[list[tuple[int, str]], list[tuple[int, str]]]] = []
    old_no = new_no = 0
    for line in diff.splitlines():
        header = _HUNK.match(line)
        if header:
            hunks.append(([], []))
            old_no, new_no = int(header.group(1)), int(header.group(2))
            continue
        if not hunks or line.startswith(("--- ", "+++ ", "index ", "diff ", "\\")):
            continue
        if line.startswith("-"):
            hunks[-1][0].append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            hunks[-1][1].append((new_no, line[1:]))
            new_no += 1
        else:
            old_no += 1
            new_no += 1
    return hunks


def _analyze_hunk(path: str, removed: list, added: list) -> list[Weakening]:
    """Pair each removed line with its closest added partner and report the evidence."""
    findings: list[Weakening] = []
    consumed: set[int] = set()
    for line_no, text in removed:
        best_index, best_score = -1, 0.0
        for index, (_added_line, added_text) in enumerate(added):
            if index in consumed:
                continue
            score = _pair_score(text, added_text)
            if score > best_score:
                best_index, best_score = index, score
        partner = added[best_index] if best_score >= 0.75 else None
        if partner is not None:
            consumed.add(best_index)
        stripped = text.strip()
        if _ASSERT_LINE.match(text):
            if partner is None or not _ASSERT_LINE.match(partner[1]):
                # the assertion stopped existing, or became a skip / pass / comment
                findings.append(Weakening("removed_assert", path, line_no, stripped))
            elif Counter(_literals(text)) != Counter(_literals(partner[1])):
                findings.append(
                    Weakening(
                        "changed_expected_literal",
                        path,
                        line_no,
                        f"-{line_no}: {stripped}  ->  +{partner[0]}: {partner[1].strip()}",
                    )
                )
        if _RAISES_LINE.search(text) and (partner is None or not _RAISES_LINE.search(partner[1])):
            findings.append(Weakening("removed_pytest_raises", path, line_no, stripped))
        if partner is not None and not _ASSERT_LINE.match(text):
            before, after = Counter(_literals(text)), Counter(_literals(partner[1]))
            if before and before != after:
                findings.append(
                    Weakening(
                        "changed_literal_in_non_assert_line",
                        path,
                        line_no,
                        f"-{line_no}: {stripped}  ->  +{partner[0]}: {partner[1].strip()}",
                    )
                )
    return findings


def find_weakenings(repo: Path, base: str, head: str, paths: list[str]) -> list[Weakening]:
    """Surface removed / re-written test evidence with file:line, without judging it.

    Two passes per test file: deleted `def test_…` names are resolved across the
    whole file (a moved function is not a deleted one), while assertion evidence
    is resolved inside each hunk, where a removed line and its replacement
    actually sit next to each other.
    """
    findings: list[Weakening] = []
    for path in paths:
        diff = run_git(repo, "diff", "-U3", base, head, "--", path).stdout
        if not diff:
            continue
        hunks = _parse_hunks(diff)
        removed_all = [item for hunk in hunks for item in hunk[0]]
        added_all = [item for hunk in hunks for item in hunk[1]]
        added_names = set()
        for _line, text in added_all:
            match = _DEF_TEST.match(text)
            if match:
                added_names.add(match.group(1))
        for line_no, text in removed_all:
            match = _DEF_TEST.match(text)
            if match and match.group(1) not in added_names:
                findings.append(
                    Weakening("deleted_test_function", path, line_no, text.strip())
                )
        for removed, added in hunks:
            findings.extend(_analyze_hunk(path, removed, added))
    return findings


def syntax_check(repo: Path, head: str, paths: list[str]) -> list[tuple[str, str]]:
    """AST-parse every changed Python file as it exists at head.

    Deliberately not `python -m compileall` on the working tree and deliberately
    not an import: the head is usually not checked out here, five other
    executors share this repo, and importing `tgfarm` can bootstrap or migrate
    the live database. A parse failure is a fact worth reporting; a successful
    parse says only that the file is valid Python.
    """
    results: list[tuple[str, str]] = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        source = blob(repo, head, path)
        if source is None:
            results.append((path, "deleted-at-head"))
            continue
        try:
            ast.parse(source, filename=path)
        except SyntaxError as exc:
            results.append((path, f"SyntaxError: {exc.msg} (line {exc.lineno})"))
        else:
            results.append((path, "parsed"))
    return results


def _ruff_counts(repo: Path, ref: str, path: str, workdir: Path) -> tuple[int | None, Counter]:
    """Findings and E501 widths for one file at one ref; None when ruff cannot run."""
    source = blob(repo, ref, path)
    if source is None:
        return 0, Counter()
    target = workdir / f"{ref}_{abs(hash(path))}_{Path(path).name}"
    target.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *RUFF_FLAGS, "--exit-zero",
         "--output-format", "concise", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode not in (0, 1) and not proc.stdout:
        return None, Counter()
    findings = 0
    widths: Counter = Counter()
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        findings += 1
        width = _E501_WIDTH.search(line)
        if width:
            widths[int(width.group(1))] += 1
    return findings, widths


def ruff_available() -> bool:
    """Whether `python -m ruff` actually runs here — a missing ruff is not a clean file."""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode == 0


def ruff_compare(repo: Path, base: str, head: str, paths: list[str]) -> dict[str, Any]:
    """Per changed Python file: findings at base versus head, plus the width set."""
    out: dict[str, Any] = {"available": ruff_available(), "files": {}}
    if not out["available"]:
        return out
    with tempfile.TemporaryDirectory(prefix="w26t-packet-") as tmp:
        workdir = Path(tmp)
        py_files = [p for p in paths if p.endswith(".py")]
        for path in py_files:
            base_count, base_widths = _ruff_counts(repo, base, path, workdir)
            head_count, head_widths = _ruff_counts(repo, head, path, workdir)
            out["files"][path] = {
                "base": base_count,
                "head": head_count,
                "base_e501_widths": dict(sorted(base_widths.items())),
                "head_e501_widths": dict(sorted(head_widths.items())),
            }
    return out


def _status_from(flag: bool | None, *, on_fail: str = STATUS_FAIL) -> str:
    """Map a tri-state check to a status label; None is never quietly OK."""
    if flag is None:
        return STATUS_NOT_MEASURED
    return STATUS_OK if flag else on_fail


def build_packet(
    repo: Path,
    base_ref: str,
    head_ref: str,
    *,
    branch: str | None = None,
    against: str | None = None,
    with_ruff: bool = True,
    queue_path: Path | None = None,
    task_id: str | None = None,
) -> Packet:
    """Run every read-only check and return the facts plus their rendering."""
    packet = Packet()
    packet.lines.append("REVIEW PACKET — read-only evidence, not an acceptance decision")
    packet.lines.append(f"generated: {datetime.now(UTC).isoformat(timespec='seconds')}")
    packet.lines.append(f"repo: {repo}")
    packet.lines.append(f"base: {base_ref}  head: {head_ref}  branch: {branch or 'not given'}")

    # -- 1. refs and ancestry -------------------------------------------------
    packet.section("1 refs and ancestry")
    base_sha, base_subject = resolve_commit(repo, base_ref)
    head_sha, head_subject = resolve_commit(repo, head_ref)
    packet.facts["base_sha"] = base_sha
    packet.facts["head_sha"] = head_sha
    packet.item(
        "base resolves to a commit",
        _status_from(base_sha is not None),
        f"{(base_sha or 'not measured')[:7]} {base_subject}".strip(),
    )
    packet.item(
        "head resolves to a commit",
        _status_from(head_sha is not None),
        f"{(head_sha or 'not measured')[:7]} {head_subject}".strip(),
    )
    if base_sha is None or head_sha is None:
        packet.item("base is an ancestor of head", STATUS_NOT_MEASURED, "a named ref does not exist")
        packet.facts["ancestry"] = None
        packet.facts["fatal_missing_ref"] = True
        packet.item(
            "stale-lineage check",
            STATUS_NOT_MEASURED,
            "unreachable while a ref is missing: an invented SHA is itself the finding",
        )
        packet.section("2 verdict")
        packet.raw(VERDICT_LINE)
        packet.facts["verdict"] = VERDICT_LINE
        return packet

    ancestor = run_git(repo, "merge-base", "--is-ancestor", base_sha, head_sha)
    if ancestor.returncode == 0:
        is_ancestor: bool | None = True
    elif ancestor.returncode == 1:
        is_ancestor = False
    else:
        is_ancestor = None
    packet.facts["ancestry"] = is_ancestor
    packet.item(
        "base is an ancestor of head",
        _status_from(is_ancestor),
        f"git merge-base --is-ancestor {base_sha[:7]} {head_sha[:7]}",
    )
    if is_ancestor is False:
        common = run_git(repo, "merge-base", base_sha, head_sha).out
        packet.raw("HEAD is NOT a descendant of base — the package sits on a side branch.")
        packet.raw(f"  merge-base(base, head) = {(common[:7] if common else 'none')}")
        packet.raw("  a non-descendant head means the agent branched somewhere else (often the")
        packet.raw("  stale origin lineage); a blind cherry-pick would carry that lineage in.")

    behind_ref = against or ("main" if resolve_commit(repo, "main")[0] else "HEAD")
    behind = run_git(repo, "log", "--format=%H", f"{base_sha}..{behind_ref}").stdout.splitlines()
    packet.facts["base_behind"] = len(behind)
    packet.facts["behind_ref"] = behind_ref
    packet.item(
        f"base is current against {behind_ref}",
        STATUS_OK if not behind else STATUS_WARN,
        f"base is behind {behind_ref} by {len(behind)} commit(s)"
        if behind
        else f"base is on the {behind_ref} lineage",
    )

    # -- 2. commits -----------------------------------------------------------
    packet.section("2 commits base..head")
    log = run_git(
        repo,
        "log",
        "--reverse",
        "--format=%h%x09%ad%x09%an%x09%s",
        "--date=iso-strict",
        f"{base_sha}..{head_sha}",
    ).stdout.strip()
    commits = log.splitlines() if log else []
    packet.facts["commits"] = commits
    packet.item("commit count", STATUS_OK if commits else STATUS_WARN, str(len(commits)))
    if commits:
        packet.raw(log)

    # -- 3. changed files -----------------------------------------------------
    packet.section("3 changed files by category")
    files = collect_changed_files(repo, base_sha, head_sha)
    packet.facts["files"] = files
    by_category: dict[str, list[ChangedFile]] = {}
    for entry in files:
        by_category.setdefault(entry.category, []).append(entry)
    for category in (PRODUCTION_CATEGORY, TEST_CATEGORY, DOCS_CATEGORY, OTHER_CATEGORY):
        members = by_category.get(category, [])
        inserted = sum(max(entry.added, 0) for entry in members)
        deleted = sum(max(entry.removed, 0) for entry in members)
        packet.item(
            category,
            STATUS_OK if members else STATUS_WARN,
            f"{len(members)} file(s), +{inserted}/-{deleted}"
            if members
            else "no files in this category",
        )
    packet.item("changed paths total", STATUS_OK, f"{len(files)}")
    stat = run_git(repo, "diff", "--stat", base_sha, head_sha).stdout.strip()
    if stat:
        packet.section("3a git diff --stat")
        packet.raw(stat)
    packet.section("3b git diff --numstat")
    for entry in files:
        marker = "binary" if entry.added < 0 else f"+{entry.added}/-{entry.removed}"
        packet.raw(f"{entry.category:<10} {marker:<14} {entry.path}")

    # -- 3c. claim owners (W25-B: the reviewer sees collisions before approving) --
    packet.section("3c claim owners from the queue")
    queue_file = queue_path or (repo / "docs" / "orchestration" / "queue.yaml")
    overlaps: list[dict[str, Any]] | None = None
    if not Path(queue_file).exists():
        packet.item(
            "queue.yaml",
            STATUS_NOT_MEASURED,
            f"not found at {queue_file} — other owners of these paths cannot be shown",
        )
    else:
        try:
            tasks = _load_queue_tasks(Path(queue_file))
        except (RuntimeError, ValueError) as exc:
            packet.item("queue.yaml", STATUS_NOT_MEASURED, f"unreadable: {exc}")
            tasks = None
        if tasks is not None:
            overlaps = _claim_overlaps(tasks, [entry.path for entry in files], exclude_task_id=task_id)
            if not overlaps:
                packet.item("other queue owners", STATUS_OK, "no other record claims a changed path")
            for other in overlaps:
                status = STATUS_WARN if other["live"] else STATUS_OK
                packet.item(
                    f"other owner {other['id']} [{other['status']}]",
                    status,
                    f"`{other['owner']}` claims {other['paths']}",
                )
            if any(other["live"] for other in overlaps):
                packet.raw(
                    "  at least one overlapping record is live or reserved in the queue: accepting"
                    " this packet without reconciling owners recreates the W28-A/missions shape —"
                    " two owners, one file, two trees."
                )
    packet.facts["claim_overlaps"] = overlaps
    header: list[str] | None = None
    if overlaps is not None:
        header = _owner_header_lines(overlaps, task_id or branch or "not given")
    packet.facts["owner_header_lines"] = header

    # -- 4. test-weakening detector ------------------------------------------
    packet.section("4 test-weakening detector")
    test_paths = [entry.path for entry in by_category.get(TEST_CATEGORY, [])]
    if not test_paths:
        packet.item(
            "test files touched",
            STATUS_WARN,
            "NO TEST FILE IN THIS DIFF — a behaviour package with no test change",
        )
        packet.facts["weakenings"] = []
    else:
        findings = find_weakenings(repo, base_sha, head_sha, test_paths)
        packet.facts["weakenings"] = findings
        counts = Counter(entry.kind for entry in findings)
        for kind in (
            "removed_assert",
            "removed_pytest_raises",
            "changed_expected_literal",
            "changed_literal_in_non_assert_line",
            "deleted_test_function",
        ):
            hits = counts.get(kind, 0)
            packet.item(kind, STATUS_OK if hits == 0 else STATUS_WARN, f"{hits} occurrence(s)")
        packet.item("test files inspected", STATUS_OK, ", ".join(test_paths))
        if findings:
            packet.section("4a evidence (file:line; removed lines are base coordinates)")
            for finding in findings:
                packet.raw(f"[{finding.kind}] {finding.path}:{finding.line}")
                packet.raw(f"    {finding.text}")

    # -- 5. parse check -------------------------------------------------------
    packet.section("5 head tree still parses")
    code_paths = [
        entry.path
        for entry in files
        if entry.path.endswith(".py") and entry.path.startswith(("tgfarm/", "tools/"))
    ]
    parse_results = syntax_check(repo, head_sha, code_paths)
    packet.facts["parse"] = parse_results
    broken = [entry for entry in parse_results if entry[1].startswith("SyntaxError")]
    packet.item(
        "AST parse of changed tgfarm/tools Python files at head",
        STATUS_FAIL if broken else STATUS_OK if parse_results else STATUS_WARN,
        f"{len(parse_results)} file(s) checked, {len(broken)} parse failure(s)"
        if parse_results
        else "no changed Python source under tgfarm/ or tools/ in this diff",
    )
    for path, note in parse_results:
        if note != "parsed":
            packet.raw(f"{path}: {note}")
    packet.raw(
        "note: the test suite is NOT run here — six executors are live and the"
        " coordinator runs it at integration."
    )

    # -- 6. ruff comparison ---------------------------------------------------
    packet.section("6 ruff comparison (per changed .py, base -> head)")
    if not with_ruff:
        packet.item("ruff", STATUS_NOT_MEASURED, "skipped with --no-ruff")
        packet.facts["ruff"] = {"available": None}
    elif not ruff_available():
        packet.item("ruff", STATUS_NOT_MEASURED, "`python -m ruff` is not runnable here")
        packet.facts["ruff"] = {"available": False}
        packet.raw("not measured is not zero: no count is printed for any file")
    else:
        all_py = [entry.path for entry in files if entry.path.endswith(".py")]
        comparison = ruff_compare(repo, base_sha, head_sha, all_py)
        packet.facts["ruff"] = comparison
        packet.raw(
            "flags: --line-length 110 --select E,F,I,UP,B --ignore B008"
            " --isolated --target-version py311"
        )
        for path, data in sorted(comparison["files"].items()):
            base_n, head_n = data["base"], data["head"]
            if base_n is None or head_n is None:
                trend = "not measured"
                status = STATUS_NOT_MEASURED
            else:
                trend = f"{head_n - base_n:+d}"
                status = STATUS_WARN if head_n - base_n > 0 else STATUS_OK
            packet.item(
                path,
                status,
                f"base/new = {base_n}/{head_n}  ({trend})  E501 widths"
                f" base={data['base_e501_widths']} head={data['head_e501_widths']}",
            )

    # -- 7. limits + verdict --------------------------------------------------
    packet.section("7 what this packet cannot see")
    packet.raw(
        "an assertion rewritten in place with a different expected literal is listed as"
        " changed_expected_literal, but a correct rewrite and a wrong one look identical"
        " here; a test that was already wrong at base stays wrong and invisible; the parse"
        " check proves syntax only, not that a name or import resolves; ruff is counted per"
        " file, so one new violation hiding a fixed one reads as unchanged; nothing here"
        " runs the suite, reads tgfarm.db, or writes to the repo."
    )
    packet.section("8 verdict")
    packet.raw(VERDICT_LINE)
    packet.facts["verdict"] = VERDICT_LINE
    return packet


def render(packet: Packet) -> str:
    """The packet as it is printed: last line is always the NEEDS_HUMAN verdict."""
    return "\n".join(packet.lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only acceptance evidence packet.")
    parser.add_argument("--base", required=True, help="the ref the package claims to sit on")
    parser.add_argument("--head", required=True, help="the ref the package claims to deliver")
    parser.add_argument("--branch", default=None, help="branch name, printed for the reviewer")
    parser.add_argument(
        "--against",
        default=None,
        help="lineage reference for the staleness check (default: main, else HEAD)",
    )
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository to inspect")
    parser.add_argument("--out", default=None, help="also write the packet text here")
    parser.add_argument("--no-ruff", action="store_true", help="skip the ruff comparison")
    parser.add_argument(
        "--queue",
        default=None,
        help="queue.yaml for the owner header (default: <repo>/docs/orchestration/queue.yaml)",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="queue id of THIS package, so the header excludes itself from 'other owners'",
    )
    args = parser.parse_args(argv)

    force_utf8_streams()
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 2
    try:
        packet = build_packet(
            repo,
            args.base,
            args.head,
            branch=args.branch,
            against=args.against,
            with_ruff=not args.no_ruff,
            queue_path=Path(args.queue) if args.queue else None,
            task_id=args.task,
        )
    except GitCommandNotAllowed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    header = packet.facts.get("owner_header_lines")
    if header:
        print("\n".join(header))
    text = render(packet)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

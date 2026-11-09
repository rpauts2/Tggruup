"""Scan the checkout for leaked API keys before anything leaves the machine.

Born from a real incident: a test run with real credentials in .env reached the
live VK wall. .env itself is gitignored and expected; anything else with a
sk-pattern is a finding.

The report names a path and a line, never the matched text. Printing even a
12-character prefix put live key material into a pytest log twice; locating a
leak does not require copying it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PATTERN = re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{16,}")

SKIP_DIRS = {
    ".venv", "venv", "dist", "build", "backup", ".git", ".zwork", "data",
    "__pycache__", ".serena", ".pytest_cache", ".ruff_cache", "node_modules",
    ".playwright-mcp", ".specstory",
}
SKIP_SUFFIX = {".db", ".db-wal", ".db-shm", ".png", ".jpg", ".jpeg", ".gif", ".exe", ".dll", ".log",
               ".session", ".session-journal", ".woff", ".woff2"}


def _is_linked_worktree(path: Path) -> bool:
    """A nested checkout at another commit, not part of this one.

    Stale agent worktrees still carry fixtures this checkout has already fixed,
    so scanning them reported the repository's history as a present leak — and
    walking 174 of them made the gate take minutes. A submodule also carries a
    `.git` file but its content does ship with this checkout, so the pointer has
    to name a worktree, not just exist.
    """
    marker = path / ".git"
    if not marker.is_file():
        return False
    try:
        pointer = marker.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return False
    if not pointer.startswith("gitdir:"):
        return False
    return "/worktrees/" in pointer[len("gitdir:"):].strip().replace("\\", "/")


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and not _is_linked_worktree(here / name)
        ]
        for name in filenames:
            path = here / name
            if path.suffix.lower() in SKIP_SUFFIX:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if PATTERN.search(line):
                    note = " (.env — допустимо, в гитигноре)" if name == ".env" else ""
                    hits.append(f"{path.relative_to(root)}:{number}: key-shaped literal{note}")
    return hits


def findings_outside_env(hits: list[str]) -> list[str]:
    """The one predicate the gate and its test share.

    `.env` exactly. The old test asked for `scan() == []`, which this function
    deliberately contradicts, and `main()` forgave any path *containing* ".env" —
    including `.env.example`, which is tracked and pushed.
    """
    return [hit for hit in hits if Path(hit.split(":", 1)[0]).name != ".env"]


def main() -> int:
    # The report is Russian and the Windows console is cp1252; without this the
    # gate dies in a UnicodeEncodeError instead of returning a verdict.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parent.parent
    hits = scan(root)
    if hits:
        print("\n".join(hits))
    else:
        print("утечек не найдено")
    return 1 if findings_outside_env(hits) else 0


if __name__ == "__main__":
    sys.exit(main())
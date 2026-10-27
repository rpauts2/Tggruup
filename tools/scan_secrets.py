"""Scan the checkout for leaked API keys before anything leaves the machine.

Born from a real incident: a test run with real credentials in .env reached the
live VK wall. .env itself is gitignored and expected; anything else with a
sk-pattern is a finding.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{16,}")

SKIP_DIRS = {
    ".venv", "venv", "dist", "build", "backup", ".git", ".zwork", "data",
    "__pycache__", ".serena", ".pytest_cache", ".ruff_cache", "node_modules",
    ".playwright-mcp", ".specstory",
}
SKIP_SUFFIX = {".db", ".png", ".jpg", ".jpeg", ".gif", ".exe", ".dll", ".log",
               ".session", ".session-journal", ".woff", ".woff2"}


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & SKIP_DIRS:
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = PATTERN.search(line)
            if match:
                note = " (.env — допустимо, в гитигноре)" if path.name == ".env" else ""
                hits.append(f"{path.relative_to(root)}:{number}: {match.group(0)[:12]}…{note}")
    return hits


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    hits = scan(root)
    if hits:
        print("\n".join(hits))
    else:
        print("утечек не найдено")
    leak_outside_env = any(".env" not in hit for hit in hits)
    return 1 if leak_outside_env else 0


if __name__ == "__main__":
    sys.exit(main())

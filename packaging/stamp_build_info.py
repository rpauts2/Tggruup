#!/usr/bin/env python3
"""Stamp build identity into the artifact. Build-time only — never called at runtime.

This is the one place in the project allowed to ask git anything, and it asks
while the tree is frozen for the build (CI checkout, immediately before
PyInstaller). Its output is a data file, ``tgfarm/build_identity.json``, that
``tgfarm.buildinfo`` reads. Git never runs in the running process: a SHA
obtained at call time describes the directory, not the process, and the whole
point of this file is to stop those two being confused.

    python packaging/stamp_build_info.py --packaging-mode pyinstaller \
        --artifact-name "TgFarm-Windows-$SHA" --artifact-path dist/TgFarm

Fails closed: outside a git work tree, or with a version it cannot read from
``pyproject.toml``, it exits non-zero instead of writing a file that would look
like a build fact.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgfarm.buildinfo import (  # noqa: E402
    METADATA_FILENAME,
    PACKAGING_MODES,
    SCHEMA,
    declared_version,
    metadata_is_current,
    render_metadata,
    utc_now_stamp,
)

DEFAULT_OUTPUT = ROOT / "tgfarm" / METADATA_FILENAME

_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def source_sha(root: Path = ROOT) -> str:
    """Full HEAD SHA of the tree being built, or raise.

    Legal here, forbidden at runtime: at stamp time the tree *is* the input, so
    its HEAD is a fact about the build. Later it stops being one.
    """
    done = _git(["rev-parse", "HEAD"], root)
    sha = (done.stdout or "").strip()
    if done.returncode != 0 or not _SHA_RE.fullmatch(sha):
        raise RuntimeError(f"cannot stamp: not a git work tree ({(done.stderr or '').strip()[:120]})")
    return sha


def source_branch(root: Path = ROOT) -> str:
    done = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = (done.stdout or "").strip()
    return branch if done.returncode == 0 and branch else ""


def is_dirty(root: Path = ROOT) -> bool:
    """True when the built tree was not clean.

    The metadata file itself is excluded: stamping twice must not make the
    second stamp dirty because of the first one's output.
    """
    done = _git(["status", "--porcelain"], root)
    if done.returncode != 0:
        # Unknown cleanliness is dirty. "Clean" is a claim, not a default.
        return True
    own = f"tgfarm/{METADATA_FILENAME}"
    for line in (done.stdout or "").splitlines():
        entry = line.strip()
        if not entry:
            continue
        path = entry[3:] if len(entry) > 3 else entry
        path = path.split(" -> ")[0].strip().replace("\\", "/")
        if path.lstrip("./") == own:
            continue
        return True
    return False


def collect(
    *,
    packaging_mode: str,
    artifact_name: str,
    artifact_path: str,
    app_version: str | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    """The metadata payload, validated by the schema's owner before it is returned."""
    if packaging_mode not in PACKAGING_MODES:
        raise ValueError(f"unknown packaging mode {packaging_mode!r}")
    sha = source_sha(root)
    declared = app_version
    if declared is None:
        # Honour ``root``: a stamp taken for another work tree must read that
        # tree's version, not the one this checkout happens to declare.
        declared = declared_version(root)
    if not declared:
        raise RuntimeError("cannot stamp: no readable version in pyproject.toml")

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "packaging_mode": packaging_mode,
        "app_version": declared,
        "source_sha": sha,
        "source_sha_short": sha[:12],
        "dirty": is_dirty(root),
        "build_timestamp": utc_now_stamp(),
        "build_python_version": platform.python_version(),
        "artifact_name": artifact_name,
    }
    branch = source_branch(root)
    if branch:
        payload["source_branch"] = branch
    if artifact_path:
        payload["artifact_path"] = artifact_path

    if not metadata_is_current(payload):
        raise RuntimeError("cannot stamp: payload does not satisfy the identity contract")
    return payload


def write(payload: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_metadata(payload), encoding="utf-8", newline="\n")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stamp TgFarm build identity metadata.")
    parser.add_argument("--packaging-mode", default="pyinstaller", choices=list(PACKAGING_MODES))
    parser.add_argument("--artifact-name", default="")
    parser.add_argument("--artifact-path", default="")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--print-json", action="store_true", help="emit the payload instead of writing it")
    args = parser.parse_args(argv)

    output = Path(args.output)
    try:
        payload = collect(
            packaging_mode=args.packaging_mode,
            artifact_name=args.artifact_name or f"tgfarm-{args.packaging_mode}-unstamped",
            artifact_path=args.artifact_path,
            root=ROOT,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"stamp_build_info: refusing to stamp: {exc}", file=sys.stderr)
        return 2

    if args.print_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    write(payload, output)
    print(f"stamped {output} at {payload['source_sha'][:12]} dirty={payload['dirty']}")
    return 0


if __name__ == "__main__":
    # Windows consoles are cp1252 here; the payload is ASCII but a path from the
    # shell may not be, so never let a print encode-crash a build.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())

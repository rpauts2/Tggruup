"""The checkout's own secret gate.

`tools/scan_secrets.py` exists because a test run once carried real credentials
into a live VK wall. A gate that is red on a clean checkout is worse than no
gate: every operator learns to ignore its exit code, which is exactly the state
a real leak needs.

Three separate defects kept it red or let it lie:

* The assertion below asked for `scan() == []`, but `scan()` deliberately
  reports the gitignored `.env` it is supposed to tolerate. The gate could never
  be green on a machine that has a working `.env` at all.
* 174 linked worktrees under `.qoder/` are separate checkouts pinned to older
  commits. 28 of them still carry the credential-shaped fixture that
  `test_w24b_buildinfo.py` planted before it was fixed, so the history of this
  repository was reported as a leak in its present.
* `main()` tolerated any hit whose *path contained* the substring `.env`, which
  also tolerates `.env.example` — a tracked file that is pushed.
"""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Assembled, never written as one literal: this file must not trip its own gate.
KEY = "sk-" + "live-" + "qwertyuiopasdfghjkl"


def _load_tool():
    spec = spec_from_file_location("scan_secrets_under_test", REPO_ROOT / "tools" / "scan_secrets.py")
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_shipped_checkout_has_no_secret_named_where_it_may_not_be() -> None:
    """The contract is `main()`'s, not `scan()`'s: `.env` is expected, anything
    else is a finding. Asserting the two share one predicate is what stops the
    gate and its test from drifting apart again."""
    scanner = _load_tool()

    hits = scanner.scan(REPO_ROOT)

    assert scanner.findings_outside_env(hits) == []
    assert all(Path(hit.split(":", 1)[0]).name == ".env" for hit in hits)


def test_a_planted_literal_is_still_reported(tmp_path: Path) -> None:
    """The gate must stay strict: silencing the fixture must not silence a leak."""
    scanner = _load_tool()
    (tmp_path / "leak.py").write_text(f'KEY = "{KEY}"\n', encoding="utf-8")

    hits = scanner.scan(tmp_path)

    assert len(hits) == 1
    assert hits[0].startswith("leak.py:1:")
    assert scanner.findings_outside_env(hits) == hits


def test_a_credential_assembled_from_parts_is_not_a_literal(tmp_path: Path) -> None:
    """How the fixture keeps its shape without writing a key into the tree."""
    scanner = _load_tool()
    (tmp_path / "fixture.py").write_text(
        'KEY = "sk-" + "live-" + "qwertyuiopasdfghjkl"\n', encoding="utf-8"
    )

    assert scanner.scan(tmp_path) == []


def test_skipped_directories_are_not_read(tmp_path: Path) -> None:
    scanner = _load_tool()
    cache = tmp_path / ".venv"
    cache.mkdir()
    (cache / "odd.py").write_text(f'KEY = "{KEY}"\n', encoding="utf-8")

    assert scanner.scan(tmp_path) == []


def test_env_example_is_not_env(tmp_path: Path) -> None:
    """`.env.example` is tracked and pushed. A real key there is a leak, and the
    old substring tolerance reported the gate green."""
    scanner = _load_tool()
    (tmp_path / ".env.example").write_text(f"API_KEY={KEY}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"API_KEY={KEY}\n", encoding="utf-8")

    hits = scanner.scan(tmp_path)
    outside = scanner.findings_outside_env(hits)

    assert len(hits) == 2, "both files are reported"
    assert len(outside) == 1, "only the gitignored one is tolerated"
    assert outside[0].startswith(".env.example:1:")


def test_a_nested_worktree_is_a_separate_checkout(tmp_path: Path) -> None:
    """A linked worktree is another commit's tree, not this one. Its findings are
    history; what this checkout pushes is what the gate has to answer for."""
    scanner = _load_tool()
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".git").write_text(
        "gitdir: /repo/.git/worktrees/elsewhere\n", encoding="utf-8"
    )
    (other / "old_fixture.py").write_text(f'KEY = "{KEY}"\n', encoding="utf-8")

    assert scanner.scan(tmp_path) == []


def test_a_submodule_is_not_mistaken_for_a_worktree(tmp_path: Path) -> None:
    """A submodule also carries a `.git` file, but its content ships with this
    checkout. Pruning every `.git` file would hide a leak inside one."""
    scanner = _load_tool()
    module = tmp_path / "vendor"
    module.mkdir()
    (module / ".git").write_text("gitdir: ../.git/modules/vendor\n", encoding="utf-8")
    (module / "leak.py").write_text(f'KEY = "{KEY}"\n', encoding="utf-8")

    hits = scanner.scan(tmp_path)

    assert len(hits) == 1
    assert hits[0].startswith(str(Path("vendor") / "leak.py") + ":1:")


def test_the_report_never_prints_secret_material(tmp_path: Path) -> None:
    """The gate's own output went into a pytest log twice, carrying a 12-char
    prefix of the live key. Path and line locate a leak; the value only spreads
    it. This applies to the tolerated `.env` line too."""
    scanner = _load_tool()
    (tmp_path / ".env").write_text(f"API_KEY={KEY}\n", encoding="utf-8")
    (tmp_path / "leak.py").write_text(f'KEY = "{KEY}"\n', encoding="utf-8")

    hits = scanner.scan(tmp_path)

    assert len(hits) == 2
    for hit in hits:
        assert KEY not in hit
        assert KEY[:12] not in hit, f"the report still carries key material: {hit!r}"


def test_the_gate_itself_returns_zero_on_this_checkout(capsys) -> None:
    """The exit code is what an operator and CI consume. Asserting on scan()
    alone missed that main() died in a UnicodeEncodeError printing its own
    Russian note on a cp1252 console — a traceback, not a verdict."""
    scanner = _load_tool()

    assert scanner.main() == 0

    out = capsys.readouterr().out
    assert "sk-" not in out, "the gate printed key material into the log again"

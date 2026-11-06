"""W26-T: the review packet must show its work, and must not be able to do anything else.

Every test below runs against a throwaway git repository built in `tmp_path` —
never this repo's own history, never `tgfarm.db`, never the app. The point is to
pin the *bite* of each check, not its wording:

* an invented SHA is reported as missing, and the packet still ends in NEEDS_HUMAN;
* a head that is not a descendant of base is flagged, not silently linearised;
* a base that sits on the stale lineage is flagged even when ancestry holds;
* a deleted `def test_…`, a removed `assert`, a removed `pytest.raises` and a
  changed expected literal each appear with a file:line, and the counts move;
* a package with no test file says so;
* ruff is reported base/new per file, and a missing ruff is NOT_MEASURED rather
  than a clean zero;
* and structurally: the module's own source must contain no acceptance word and
  no mutating git subcommand, because a review tool that can commit is the exact
  failure mode five live executors cannot survive.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import review_packet as rp  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch=main")
    git(work, "config", "user.email", "w26t@example.invalid")
    git(work, "config", "user.name", "W26-T fixture")
    (work / "tgfarm").mkdir()
    (work / "tests").mkdir()
    (work / "tgfarm" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (work / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 1\n", encoding="utf-8"
    )
    git(work, "add", "-A")
    git(work, "commit", "--quiet", "-m", "base commit")
    return work


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def facts(repo: Path, base: str, head: str, *, with_ruff: bool = False, **kwargs) -> rp.Packet:
    return rp.build_packet(repo, base, head, with_ruff=with_ruff, **kwargs)


# ---------------------------------------------------------------------------
# the tool itself is read-only: asserted on its own source, not on a promise
# ---------------------------------------------------------------------------


def test_module_source_carries_no_acceptance_vocabulary() -> None:
    source = (ROOT / "tools" / "review_packet.py").read_text(encoding="utf-8")
    for token in rp.FORBIDDEN_SOURCE_TOKENS:
        assert token not in source, f"review_packet.py must never claim {token}"


def test_verdict_line_is_a_needs_human_literal() -> None:
    assert rp.VERDICT_LINE == "verdict: NEEDS_HUMAN"


def test_rendered_packet_always_ends_with_the_verdict(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
    head = commit_all(repo, "feat: value 2")
    text = rp.render(facts(repo, base, head))
    assert text.rstrip().splitlines()[-1].strip() == "verdict: NEEDS_HUMAN"


def test_run_git_refuses_any_mutating_subcommand(tmp_path: Path) -> None:
    for forbidden in ("add", "commit", "checkout", "reset", "merge", "stash", "push", "rebase"):
        with pytest.raises(rp.GitCommandNotAllowed):
            rp.run_git(tmp_path, forbidden, "--help")


def test_run_git_allowlist_covers_every_subcommand_the_source_spawns() -> None:
    """Every literal git subcommand in this file must be a read-only one.

    Read off the AST, not off a string search: `run_git(repo, "diff", ...)` and
    `def run_git(repo: Path, ...)` look alike to a split on `"run_git("`.
    """
    tree = ast.parse((ROOT / "tools" / "review_packet.py").read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "run_git":
            continue
        for argument in node.args[1:]:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value.startswith(("-", "--")) or argument.value in {
                    "not given",
                }:
                    continue
                used.add(argument.value)
                break
    assert used, "no git subcommand found in the source: the extractor is broken"
    assert used <= set(rp.GIT_READONLY_SUBCOMMANDS), f"mutating subcommand in source: {used}"
    assert {"diff", "log", "show", "rev-parse", "merge-base"} <= used


def test_tool_never_imports_the_app_or_opens_the_database() -> None:
    source = (ROOT / "tools" / "review_packet.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in {"tgfarm", "sqlalchemy"}}, (
        f"the packet must not import the app: {imported}"
    )
    assert "sqlite3" not in imported
    assert "connect" not in source, "no database handle may be opened here"
    assert "TGFARM_" not in source, "no environment read: the config could be bootstrapped"


# ---------------------------------------------------------------------------
# 1. refs, ancestry, lineage
# ---------------------------------------------------------------------------


def test_invented_sha_is_reported_as_missing_and_still_returns_a_verdict(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    packet = facts(repo, base, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert packet.facts["head_sha"] is None
    assert packet.facts["fatal_missing_ref"] is True
    lines = rp.render(packet)
    assert "head resolves to a commit: FAIL" in lines
    assert "base is an ancestor of head: NOT_MEASURED" in lines
    assert lines.rstrip().endswith("verdict: NEEDS_HUMAN")


def test_non_descendant_head_is_flagged(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit_all(repo, "on main: value 2")
    git(repo, "checkout", "--quiet", "-b", "side", base)
    (repo / "tgfarm" / "side.py").write_text("SIDE = 1\n", encoding="utf-8")
    side_head = commit_all(repo, "side branch head")
    main_head = git(repo, "rev-parse", "main")

    packet = facts(repo, side_head, main_head)
    assert packet.facts["ancestry"] is False
    text = rp.render(packet)
    assert "base is an ancestor of head: FAIL" in text
    assert "HEAD is NOT a descendant of base" in text


def test_stale_lineage_base_is_flagged_even_when_ancestry_holds(repo: Path) -> None:
    """The cd21f29 case: the agent's base IS an ancestor of their head, and the
    base is 218+ commits off main. Ancestry alone passes; the lineage item bites."""
    stale_base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 7\n", encoding="utf-8")
    commit_all(repo, "main moves on 1")
    (repo / "tgfarm" / "more.py").write_text("MORE = 1\n", encoding="utf-8")
    commit_all(repo, "main moves on 2")
    git(repo, "checkout", "--quiet", "-b", "agent", stale_base)
    (repo / "tgfarm" / "agent.py").write_text("AGENT = 1\n", encoding="utf-8")
    agent_head = commit_all(repo, "agent work")

    packet = facts(repo, stale_base, agent_head, against="main")
    assert packet.facts["ancestry"] is True
    assert packet.facts["base_behind"] == 2
    assert "base is current against main: WARN" in rp.render(packet)


def test_ancestry_ok_when_head_is_a_plain_descendant(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 3\n", encoding="utf-8")
    head = commit_all(repo, "feat: value 3")
    packet = facts(repo, base, head)
    assert packet.facts["ancestry"] is True
    assert "base is an ancestor of head: OK" in rp.render(packet)
    assert len(packet.facts["commits"]) == 1


# ---------------------------------------------------------------------------
# 2 and 3. commits, file categories
# ---------------------------------------------------------------------------


def test_commit_list_carries_author_dates(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "a.py").write_text("A = 1\n", encoding="utf-8")
    commit_all(repo, "first")
    (repo / "tgfarm" / "b.py").write_text("B = 1\n", encoding="utf-8")
    head = commit_all(repo, "second")
    commits = facts(repo, base, head).facts["commits"]
    assert [c.split("\t")[3] for c in commits] == ["first", "second"]
    assert all(len(c.split("\t")[1]) >= 10 for c in commits)


def test_changed_files_are_split_into_categories(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 4\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 4\n", encoding="utf-8"
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("# note\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    head = commit_all(repo, "mixed package")
    files = {f.path: f.category for f in facts(repo, base, head).facts["files"]}
    assert files["tgfarm/core.py"] == rp.PRODUCTION_CATEGORY
    assert files["tests/test_core.py"] == rp.TEST_CATEGORY
    assert files["docs/note.md"] == rp.DOCS_CATEGORY
    assert files["pyproject.toml"] == rp.OTHER_CATEGORY


def test_package_without_a_test_change_says_so(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 5\nraise SystemExit\n", encoding="utf-8")
    head = commit_all(repo, "behaviour only")
    text = rp.render(facts(repo, base, head))
    assert "NO TEST FILE IN THIS DIFF" in text
    assert "test files touched: WARN" in text


# ---------------------------------------------------------------------------
# 4. test-weakening detector (the bites that matter)
# ---------------------------------------------------------------------------


def test_removed_assert_is_counted_and_located(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    pass\n", encoding="utf-8"
    )
    head = commit_all(repo, "test: drop the assertion")
    packet = facts(repo, base, head)
    findings = packet.facts["weakenings"]
    removed = [f for f in findings if f.kind == "removed_assert"]
    assert [f.text for f in removed] == ["assert VALUE == 1"]
    assert removed[0].path == "tests/test_core.py"
    assert removed[0].line == 2
    assert "removed_assert: WARN  1 occurrence(s)" in rp.render(packet)


def test_deleted_test_function_is_named(repo: Path) -> None:
    """A test that stopped existing is the loudest weakening there is: name it."""
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 1\n\n\ndef test_flaky():\n"
        "    assert VALUE == 1\n",
        encoding="utf-8",
    )
    commit_all(repo, "two tests")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 1\n", encoding="utf-8"
    )
    head = commit_all(repo, "test: remove test_flaky")
    findings = facts(repo, base, head).facts["weakenings"]
    deleted = [f for f in findings if f.kind == "deleted_test_function"]
    assert [f.text for f in deleted] == ["def test_flaky():"]
    assert deleted[0].path == "tests/test_core.py"
    assert "deleted_test_function: WARN  1 occurrence(s)" in rp.render(facts(repo, base, head))


def test_moved_test_function_is_not_reported_as_deleted(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_core.py").write_text(
        "class Group:\n    def test_value(self):\n        assert VALUE == 1\n", encoding="utf-8"
    )
    head = commit_all(repo, "test: restructure only")
    findings = facts(repo, base, head).facts["weakenings"]
    assert [f.kind for f in findings if f.kind == "deleted_test_function"] == []


def test_changed_expected_literal_is_surfaced(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 999\n", encoding="utf-8"
    )
    head = commit_all(repo, "test: expectation rewritten")
    findings = facts(repo, base, head).facts["weakenings"]
    literals = [f for f in findings if f.kind == "changed_expected_literal"]
    assert len(literals) == 1
    assert "assert VALUE == 1" in literals[0].text
    assert "assert VALUE == 999" in literals[0].text
    assert "changed_expected_literal: WARN  1 occurrence(s)" in rp.render(facts(repo, base, head))


def test_removed_pytest_raises_is_counted(repo: Path) -> None:
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    with pytest.raises(ValueError):\n        boom()\n",
        encoding="utf-8",
    )
    commit_all(repo, "a guard in place")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    boom()\n", encoding="utf-8"
    )
    head = commit_all(repo, "test: swallow the error instead")
    findings = facts(repo, base, head).facts["weakenings"]
    raises = [f for f in findings if f.kind == "removed_pytest_raises"]
    assert [f.text for f in raises] == ["with pytest.raises(ValueError):"]
    assert raises[0].line == 2
    assert "removed_pytest_raises: WARN  1 occurrence(s)" in rp.render(facts(repo, base, head))


def test_assertion_replaced_by_skip_counts_as_removed_assert(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    pytest.skip('flaky')\n", encoding="utf-8"
    )
    head = commit_all(repo, "test: skip it")
    findings = facts(repo, base, head).facts["weakenings"]
    assert [f.kind for f in findings if f.kind == "removed_assert"] == ["removed_assert"]


# ---------------------------------------------------------------------------
# 5. parse check
# ---------------------------------------------------------------------------


def test_syntax_error_at_head_is_a_fail(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = (1\n", encoding="utf-8")
    head = commit_all(repo, "broken: unbalanced paren")
    packet = facts(repo, base, head)
    assert any(note.startswith("SyntaxError") for _path, note in packet.facts["parse"])
    assert "AST parse of changed tgfarm/tools Python files at head: FAIL" in rp.render(packet)


def test_parse_check_reads_head_blobs_not_the_working_tree(repo: Path) -> None:
    """The head is usually not checked out here, so the bytes must come from git."""
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 6\n", encoding="utf-8")
    head = commit_all(repo, "feat: value 6")
    # dirty the working tree with something that would not parse at all
    (repo / "tgfarm" / "core.py").write_text("VALUE = (6\n", encoding="utf-8")
    assert [note for _p, note in facts(repo, base, head).facts["parse"]] == ["parsed"]


# ---------------------------------------------------------------------------
# 6. ruff comparison
# ---------------------------------------------------------------------------


def test_ruff_reports_base_new_and_the_e501_width_multiset(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "long.py").write_text(
        'X = "' + "wide" * 40 + '"  # ' + "tail" * 10 + "\n", encoding="utf-8"
    )
    head = commit_all(repo, "feat: an over-long line")
    packet = facts(repo, base, head, with_ruff=True)
    data = packet.facts["ruff"]["files"]["tgfarm/long.py"]
    assert data["base"] == 0
    assert data["head"] >= 1
    assert sum(data["head_e501_widths"].values()) >= 1
    assert max(data["head_e501_widths"]) > 110
    assert "base/new = 0/" in rp.render(packet)


def test_ruff_absence_is_not_measured_rather_than_zero(repo: Path, monkeypatch) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 8\n", encoding="utf-8")
    head = commit_all(repo, "feat: value 8")
    monkeypatch.setattr(rp, "ruff_available", lambda: False)
    packet = facts(repo, base, head, with_ruff=True)
    text = rp.render(packet)
    assert "ruff: NOT_MEASURED" in text
    assert "not measured is not zero" in text
    assert "base/new" not in text


def test_packet_is_serialisable_for_the_audit_trail(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 9\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 9\n", encoding="utf-8"
    )
    head = commit_all(repo, "feat: value 9 with its test")
    packet = facts(repo, base, head)
    dump = json.dumps(
        {
            "ancestry": packet.facts["ancestry"],
            "commits": packet.facts["commits"],
            "files": [[f.path, f.category, f.added, f.removed] for f in packet.facts["files"]],
            "weakenings": [
                [w.kind, w.path, w.line, w.text] for w in packet.facts["weakenings"]
            ],
            "verdict": packet.facts["verdict"],
        },
        ensure_ascii=False,
    )
    assert json.loads(dump)["verdict"] == "verdict: NEEDS_HUMAN"


def test_cli_writes_the_packet_to_a_file_and_exits_zero(repo: Path, tmp_path: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tgfarm" / "core.py").write_text("VALUE = 1\n\nVALUE = 1\n", encoding="utf-8")
    head = commit_all(repo, "feat: duplicate constant")
    out = tmp_path / "packet.txt"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "review_packet.py"),
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--branch",
            "w26t-fixture",
            "--no-ruff",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8").rstrip().endswith("verdict: NEEDS_HUMAN")
    assert "branch: w26t-fixture" in proc.stdout

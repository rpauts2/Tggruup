"""W25-B: the review packet is owner-aware — the collision is seen before approving.

These tests pin the three promises of the W25-B packet change and nothing else:

* section `3c` (plus the printed owner header) names every OTHER queue record
  whose `allowed_paths` overlap a path in base..head, with its status and owner;
* the packet's own queue record (`--task`) is excluded from "other owners",
  and a terminal record is shown as released, not as a live collision;
* an absent or unreadable queue.yaml is reported as NOT_MEASURED — the packet
  never invents "no conflicts" from data it could not read, and the verdict
  line `NEEDS_HUMAN` stays last in every shape.

The regression case is built from the real W25-B collision: while W28-A held
`tgfarm/llm/client.py` in the queue, a second owner rewrote the same file
uncommitted in the shared main tree. A packet for that branch must list both.
"""

from __future__ import annotations

import io
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
    git(work, "config", "user.email", "w25b@example.invalid")
    git(work, "config", "user.name", "W25-B fixture")
    (work / "tgfarm").mkdir()
    (work / "tests").mkdir()
    (work / "tgfarm" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (work / "tgfarm" / "llm").mkdir()
    (work / "tgfarm" / "llm" / "client.py").write_text("CLIENT = 1\n", encoding="utf-8")
    (work / "tests" / "test_core.py").write_text(
        "def test_value():\n    assert VALUE == 1\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "--quiet", "-m", "base commit")
    return work


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def _record(tid: str, status: str, owner: str, paths: list[str]) -> dict:
    return {"id": tid, "title": f"{tid} fixture record", "description": "why",
            "priority": "medium", "category": "product", "status": status,
            "owner_role": "Engineer", "owner_agent": owner, "base_sha": "0" * 40,
            "worktree": ".qoder/worktrees/x" if status != "BACKLOG" else None,
            "branch": "x" if status != "BACKLOG" else None, "allowed_paths": paths,
            "depends_on": [], "blocked_by": [], "acceptance": "measured",
            "test_commands": [], "review_status": "NOT_STARTED",
            "integration_status": "VERIFIED" if status == "VERIFIED" else "NOT_INTEGRATED",
            "retry_count": 0, "attempt_limit": 3, "tool_calls_used": None,
            "started_at": None, "updated_at": None,
            "completed_at": "2026-11-06T14:00:00+03:00" if status == "VERIFIED" else None,
            "evidence": [], "next_action": None}


def _write_queue(repo: Path, *records: dict) -> Path:
    import yaml

    queue = repo / "docs" / "orchestration"
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / "queue.yaml"
    path.write_text(yaml.safe_dump({"meta": {"schema_version": 1},
                                    "tasks": list(records)}, allow_unicode=True),
                    encoding="utf-8")
    return path


def _head_touching(repo: Path, *paths: str) -> str:
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(old + "# W25-B fixture change\n", encoding="utf-8")
    (repo / "tests" / "test_w25b_fixture.py").write_text(
        "def test_change_exists():\n    assert True\n", encoding="utf-8")
    return commit_all(repo, "feat: touched by the W25-B fixture package")


# ------------------------------------------------------------------ section 3c
def test_other_live_owners_of_changed_paths_are_listed(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    _write_queue(repo,
                 _record("T-9", "RUNNING", "ageneral-purpose-other", ["tgfarm/core.py"]),
                 _record("T-0", "RUNNING", "ageneral-purpose-stranger", ["tgfarm/elsewhere.py"]))
    packet = rp.build_packet(repo, base, head, with_ruff=False)
    text = rp.render(packet)
    overlaps = packet.facts["claim_overlaps"]
    assert overlaps is not None, text
    assert [o["id"] for o in overlaps] == ["T-9"], overlaps
    assert overlaps[0]["owner"] == "ageneral-purpose-other"
    assert overlaps[0]["live"] is True
    assert "3c claim owners" in text
    assert "tgfarm/core.py" in text
    assert text.rstrip().endswith(rp.VERDICT_LINE)


def test_the_packet_excludes_its_own_queue_record(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    own = _record("T-9", "REVIEW", "ageneral-purpose-mine", ["tgfarm/core.py"])
    _write_queue(repo, own)
    packet = rp.build_packet(repo, base, head, with_ruff=False, task_id="T-9")
    assert packet.facts["claim_overlaps"] == [], packet.facts["claim_overlaps"]
    assert "no other record claims a changed path" in rp.render(packet)


def test_a_terminal_record_is_released_not_live(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    _write_queue(repo, _record("T-7", "VERIFIED", "coordinator", ["tgfarm/core.py"]))
    packet = rp.build_packet(repo, base, head, with_ruff=False)
    overlaps = packet.facts["claim_overlaps"]
    assert overlaps and overlaps[0]["live"] is False
    header = packet.facts["owner_header_lines"]
    assert header and any("(released)" in line for line in header), header
    assert not any("reconcile before accepting" in line for line in header), header


def test_the_real_shape_two_records_one_client_file(repo: Path) -> None:
    """W28-A held the file in the queue; missions rewrote it in the shared tree.

    Both are registered as queue records here (that is how W25-B made the
    foreign session visible); a packet for the branch-side package must show
    the other owner as a live/reserved collision, not stay silent.
    """
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/llm/client.py")
    _write_queue(repo,
                 _record("W28-A", "REVIEW", "ageneral-purpose-43372317f891f137",
                         ["tgfarm/llm/client.py"]),
                 _record("MISSIONS-T1", "BLOCKED", "missions/t1-t4 session",
                         ["tgfarm/llm/client.py", "tgfarm/llm/reliability.py"]))
    packet = rp.build_packet(repo, base, head, with_ruff=False, task_id="W28-A")
    overlaps = packet.facts["claim_overlaps"]
    assert [o["id"] for o in overlaps] == ["MISSIONS-T1"], overlaps
    assert "tgfarm/llm/client.py" in overlaps[0]["paths"]
    header = "\n".join(packet.facts["owner_header_lines"])
    assert "reconcile before accepting" in header, header
    assert "missions/t1-t4 session" in header, header


# ------------------------------------------------------------------ honesty edges
def test_absent_queue_is_not_measured_and_never_a_fake_all_clear(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    # no queue.yaml anywhere in this fixture repo, and pointed at:
    packet = rp.build_packet(repo, base, head, with_ruff=False,
                             queue_path=repo / "docs" / "orchestration" / "queue.yaml")
    text = rp.render(packet)
    assert "NOT_MEASURED" in text
    assert "cannot be shown" in text
    assert packet.facts["claim_overlaps"] is None
    assert text.rstrip().endswith(rp.VERDICT_LINE)


def test_a_package_touching_nothing_in_the_queue_says_so(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    _write_queue(repo, _record("T-5", "BACKLOG", "nobody-yet", ["tgfarm/other.py"]))
    packet = rp.build_packet(repo, base, head, with_ruff=False)
    assert packet.facts["claim_overlaps"] == []


# ------------------------------------------------------------------ encoding
def test_owner_header_prints_a_russian_owner_without_replacement_chars(
    repo: Path, monkeypatch
) -> None:
    """The reviewer reads owners, and owners here are Russian; cp1252 must not eat them."""
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    _write_queue(repo, _record("T-9", "RUNNING", "координатор-носитель", ["tgfarm/core.py"]))
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    code = rp.main(["--base", base, "--head", head, "--repo", str(repo), "--no-ruff"])
    assert code == 0
    stream.flush()
    printed = buffer.getvalue().decode("utf-8", errors="replace")
    assert "координатор-носитель" in printed, printed[:400]
    assert "\ufffd" not in printed, "the owner header replaced Cyrillic with U+FFFD"
    assert printed.rstrip().endswith(rp.VERDICT_LINE)


# ------------------------------------------------------------------ promise reuse
def test_the_owner_check_never_claims_an_acceptance_decision(repo: Path) -> None:
    """W25-B added a section, not a verdict: NEEDS_HUMAN must remain the last line."""
    base = git(repo, "rev-parse", "HEAD")
    head = _head_touching(repo, "tgfarm/core.py")
    _write_queue(repo, _record("T-9", "RUNNING", "someone", ["tgfarm/core.py"]))
    packet = rp.build_packet(repo, base, head, with_ruff=False)
    text = rp.render(packet)
    assert text.count(rp.VERDICT_LINE) == 1, text
    assert text.rstrip().endswith(rp.VERDICT_LINE)

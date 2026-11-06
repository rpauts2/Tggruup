"""W26-T: append-only telemetry for an executor task — what it cost, or that we do not know.

Directive 8 wants per-task telemetry (task, agent, start/end, duration, calls,
commits, files, tests, defects found by the reviewer, failure reason) and is
explicit that an unavailable number must be recorded as unavailable, never
invented. Two real incidents shape this file: an executor that burned
2 164 461 subagent tokens / 42 tool calls / 40 minutes and produced ZERO commits,
and another that died with uncommitted work after 1 726 160 tokens. Before this
tool neither number was written down anywhere, so an audit had to mark durations
`UNKNOWN`.

Format decision — JSONL, one file per task, under `docs/orchestration/telemetry/`:

* **JSONL because null is data.** CSV has no null: an empty CSV cell cannot be
  told apart from a zero, which is the exact confusion this tool exists to
  prevent. JSON writes `null` and nothing else.
* **one file per task, because six executors append in parallel branches.** A
  single shared file would conflict on every merge; a file per task merges
  cleanly and one corrupted task cannot invalidate another task's hash chain.
* **append-only with a per-file hash chain** (`seq`, `prev_hash`, `hash`), so a
  later edit of a record is detectable by `--verify` rather than by memory.

Rules the code enforces, not just documents:

* unknown is not zero: every nullable field defaults to `None` and is written as
  JSON `null`; an absent flag never becomes a `0`;
* a rate with a `null` (or zero) denominator is `null`, never `0.0`;
* `--verify` runs before every append, so a record is never chained onto a file
  whose chain is already broken;
* this module imports only the standard library, reads no environment
  variables, opens no `.db` file, and will refuse a target whose suffix is not
  `.jsonl`/`.json` — `tgfarm.db` stays untouched and no app import can migrate it;
* a record is immutable once written: there is no update path in this file.

Usage:
    python tools/agent_record.py --task W24-A --agent ageneral-purpose-e0078278 \\
        --state FAILED --failure-reason harness_error --commits 0 \\
        --tool-calls 42 --subagent-tokens 2164461 --duration-s 2446
    python tools/agent_record.py --verify
    python tools/agent_record.py --summary
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TELEMETRY_DIR = REPO_ROOT / "docs" / "orchestration" / "telemetry"

SCHEMA_VERSION = 1
ANCHORS_FILE = "chain_anchors.json"
GENESIS = "GENESIS"
NULLABLE_FIELDS = (
    "role",
    "base_sha",
    "branch",
    "worktree",
    "started_at",
    "ended_at",
    "duration_s",
    "tool_calls",
    "subagent_tokens",
    "commits",
    "files_touched",
    "own_tests_passed",
    "defects_found_by_reviewer",
    "failure_reason",
)
#: The payload fields covered by the hash, in canonical order. `state` and the
#: two identifiers are mandatory; everything else may be null.
HASHED_KEYS = (
    "schema",
    "task_id",
    "agent_id",
    "role",
    "base_sha",
    "branch",
    "worktree",
    "started_at",
    "ended_at",
    "duration_s",
    "tool_calls",
    "subagent_tokens",
    "commits",
    "files_touched",
    "own_tests_passed",
    "defects_found_by_reviewer",
    "failure_reason",
    "state",
    "derived",
    "recorded_at",
    "recorded_by",
)
INTEGRITY_KEYS = ("seq", "prev_hash", "hash")

#: The lifecycle states a task record may carry. `UNKNOWN` is a state, not a
#: number: a task whose outcome nobody observed is recorded as UNKNOWN, and no
#: field is invented to make it look measured.
STATES = ("DONE", "PARTIAL", "BLOCKED", "FAILED", "SUPERSEDED", "RUNNING", "UNKNOWN")
#: States that only make sense with a stated reason.
FAILURE_STATES = ("FAILED", "BLOCKED", "SUPERSEDED")

SAFE_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DB_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"}
ALLOWED_SUFFIXES = {".jsonl", ".json"}


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


class RecordError(ValueError):
    """A record that would store a lie: a missing number, a bad state, a broken chain."""


@dataclass
class VerifyResult:
    """Outcome of a chain verification over one telemetry file."""

    file: str
    records: int
    ok: bool
    problem: str | None = None

    @property
    def status(self) -> str:
        return "OK" if self.ok else "FAIL"


def canonical_json(payload: dict[str, Any]) -> str:
    """The exact bytes that get hashed: sorted keys, no whitespace, non-ASCII kept."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """Chain step: sha256(prev_hash, payload). Any payload edit changes this."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()


def parse_timestamp(value: str | None, *, field_name: str) -> str | None:
    """Normalise an ISO-8601 stamp, or None meaning "not measured" (never a fake now)."""
    if value is None:
        return None
    if value == "null":
        return None
    if value == "now":
        return datetime.now(UTC).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(value).isoformat(timespec="seconds")
    except ValueError as exc:
        raise RecordError(f"{field_name} is not ISO-8601: {value!r} ({exc})") from exc


def parse_int(value: str | None, *, field_name: str) -> int | None:
    """A measured integer, or None for "not reported". `null` is a legal CLI value."""
    if value is None or value == "null":
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise RecordError(
            f"{field_name} must be an integer or the literal 'null'; got {value!r}. "
            "An unknown number is recorded as null, not guessed."
        ) from exc
    if number < 0:
        raise RecordError(f"{field_name} must be >= 0; got {number}")
    return number


def parse_number(value: str | None, *, field_name: str) -> float | int | None:
    if value is None or value == "null":
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise RecordError(f"{field_name} must be a number or 'null'; got {value!r}") from exc
    if number < 0:
        raise RecordError(f"{field_name} must be >= 0; got {number}")
    return int(number) if number.is_integer() else number


def parse_text(value: str | None) -> str | None:
    if value is None or value == "null":
        return None
    return value


def safe_target(telemetry_dir: Path, name: str) -> Path:
    """Refuse to write anywhere except a .jsonl/.json file inside the telemetry dir."""
    resolved_dir = telemetry_dir.resolve()
    target = (resolved_dir / name).resolve()
    if target.parent != resolved_dir:
        raise RecordError(f"refusing to write outside the telemetry dir: {target}")
    if target.suffix in DB_SUFFIXES:
        raise RecordError(f"refusing to touch a database file: {target}")
    if target.suffix not in ALLOWED_SUFFIXES:
        raise RecordError(f"refusing to write a {target.suffix or '(none)'} file: {target}")
    return target


def compute_derived(record: dict[str, Any]) -> dict[str, Any]:
    """Rates are null whenever their denominator is null or zero — never 0.0.

    This is the rule the audit needed: a `0` in a rate column reads like a cheap
    task, when the real story is that nobody measured the calls. `not_computed`
    says which of the two it was, because "denominator missing" and "denominator
    legitimately zero" are different facts and must not print the same way.
    """
    notes: dict[str, str] = {}
    tokens_per_call = _rate(
        record.get("subagent_tokens"),
        record.get("tool_calls"),
        notes,
        "subagent_tokens_per_tool_call",
        numerator_field="subagent_tokens",
        denominator_field="tool_calls",
    )
    seconds_per_commit = _rate(
        record.get("duration_s"),
        record.get("commits"),
        notes,
        "duration_s_per_commit",
        numerator_field="duration_s",
        denominator_field="commits",
    )
    tokens_per_second = _rate(
        record.get("subagent_tokens"),
        record.get("duration_s"),
        notes,
        "tokens_per_duration_s",
        numerator_field="subagent_tokens",
        denominator_field="duration_s",
    )
    return {
        "subagent_tokens_per_tool_call": tokens_per_call,
        "duration_s_per_commit": seconds_per_commit,
        "tokens_per_duration_s": tokens_per_second,
        "not_computed": notes,
    }


def _rate(
    numerator: Any,
    denominator: Any,
    notes: dict[str, str],
    name: str,
    *,
    numerator_field: str,
    denominator_field: str,
) -> float | None:
    """One safe division: null in, null out, and a reason instead of a fabricated 0."""
    if denominator is None:
        notes[name] = f"not computed: {denominator_field} is not measured"
        return None
    if numerator is None:
        notes[name] = f"not computed: {numerator_field} is not measured"
        return None
    if denominator == 0:
        notes[name] = f"not computed: {denominator_field} is 0 (measured, but no denominator)"
        return None
    return round(float(numerator) / float(denominator), 2)


def build_record(args: argparse.Namespace, *, recorded_by: str) -> dict[str, Any]:
    """Assemble one payload from CLI arguments, honouring null-for-absent."""
    if not SAFE_TASK.match(args.task):
        raise RecordError(f"task id is not a safe file name: {args.task!r}")
    if not args.agent:
        raise RecordError("--agent is required: an unattributed record cannot be reviewed")
    if args.state not in STATES:
        raise RecordError(f"state must be one of {STATES}; got {args.state!r}")
    if args.commits is None or args.commits == "null":
        raise RecordError(
            "--commits is required: the commit count is always countable from git, so a null "
            "here would mean the recorder did not look, not that the number does not exist"
        )
    started = parse_timestamp(args.started_at, field_name="started_at")
    ended = parse_timestamp(args.ended_at, field_name="ended_at")
    duration = parse_number(args.duration_s, field_name="duration_s")
    if duration is None and started and ended:
        try:
            delta = (
                datetime.fromisoformat(ended) - datetime.fromisoformat(started)
            ).total_seconds()
        except TypeError as exc:
            raise RecordError(
                f"cannot derive duration_s from {started!r} and {ended!r}: "
                "both stamps must carry the same UTC offset"
            ) from exc
        duration = int(delta) if delta.is_integer() else round(delta, 3)
    failure_reason = parse_text(args.failure_reason)
    if args.state in FAILURE_STATES and failure_reason is None:
        raise RecordError(
            f"state {args.state} requires --failure-reason: a failure with no stated "
            "reason is an unverifiable claim"
        )
    if args.state == "DONE" and failure_reason is not None:
        raise RecordError("state DONE with a failure_reason contradicts itself")

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "task_id": args.task,
        "agent_id": args.agent,
        "role": parse_text(args.role),
        "base_sha": parse_text(args.base_sha),
        "branch": parse_text(args.branch),
        "worktree": parse_text(args.worktree),
        "started_at": started,
        "ended_at": ended,
        "duration_s": duration,
        "tool_calls": parse_int(args.tool_calls, field_name="tool_calls"),
        "subagent_tokens": parse_int(args.subagent_tokens, field_name="subagent_tokens"),
        "commits": parse_int(args.commits, field_name="commits"),
        "files_touched": parse_int(args.files_touched, field_name="files_touched"),
        "own_tests_passed": parse_own_tests(args.own_tests_passed),
        "defects_found_by_reviewer": parse_int(
            args.defects_found, field_name="defects_found_by_reviewer"
        ),
        "failure_reason": failure_reason,
        "state": args.state,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "recorded_by": recorded_by,
    }
    record["derived"] = compute_derived(record)
    return record


def parse_own_tests(value: str | None) -> int | str | None:
    """A passed-test count when one was reported, else the verbatim string, else null."""
    if value is None or value == "null":
        return None
    return int(value) if value.isdigit() else value


def read_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path.name}:{number} is not JSON: {exc}") from exc
    return records


def verify_file(path: Path) -> VerifyResult:
    """Recompute the chain: every field edit, reordering, removal or graft shows up."""
    try:
        records = read_records(path)
    except (OSError, RecordError) as exc:
        return VerifyResult(path.name, 0, False, str(exc))
    if not records:
        return VerifyResult(path.name, 0, True, "file holds no records yet")
    prev = GENESIS
    for index, record in enumerate(records):
        seq = record.get("seq")
        if seq != index:
            return VerifyResult(path.name, len(records), False, f"record {index}: seq is {seq}")
        if record.get("prev_hash") != prev:
            return VerifyResult(
                path.name,
                len(records),
                False,
                f"record {index}: prev_hash does not follow the previous record",
            )
        payload = {k: record.get(k) for k in HASHED_KEYS}
        recomputed = record_hash(prev, payload)
        if record.get("hash") != recomputed:
            return VerifyResult(
                path.name,
                len(records),
                False,
                f"record {index} ({record.get('task_id')}): content hash mismatch — the "
                "record was edited after it was written",
            )
        prev = recomputed
    return VerifyResult(path.name, len(records), True)


def head_hash(path: Path) -> str:
    records = read_records(path)
    return records[-1]["hash"] if records else GENESIS


def anchor_path(telemetry_dir: Path) -> Path:
    return safe_target(telemetry_dir, ANCHORS_FILE)


def load_anchors(telemetry_dir: Path) -> dict[str, Any] | None:
    target = anchor_path(telemetry_dir)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def write_anchors(telemetry_dir: Path, anchors: dict[str, Any]) -> Path:
    target = anchor_path(telemetry_dir)
    target.write_text(
        json.dumps(anchors, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


def append_record(telemetry_dir: Path, payload: dict[str, Any]) -> Path:
    """Append exactly one line. There is no update, delete or rewrite path here."""
    file = safe_target(telemetry_dir, f"{payload['task_id']}.jsonl")
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if file.exists():
        check = verify_file(file)
        if not check.ok:
            raise RecordError(
                f"refusing to append to a broken chain ({file.name}: {check.problem})"
            )
        existing = read_records(file)
    prev = existing[-1]["hash"] if existing else GENESIS
    record = dict(payload)
    record["seq"] = len(existing)
    record["prev_hash"] = prev
    record["hash"] = record_hash(prev, {k: record.get(k) for k in HASHED_KEYS})
    with file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    refresh_anchor(telemetry_dir, file.name, record["seq"], record["hash"])
    return file


def refresh_anchor(telemetry_dir: Path, name: str, seq: int, digest: str) -> None:
    anchors = load_anchors(telemetry_dir) or {"schema": SCHEMA_VERSION, "files": {}}
    anchors["files"][name] = {"seq": seq, "head_hash": digest, "records": seq + 1}
    anchors["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    write_anchors(telemetry_dir, anchors)


def telemetry_files(telemetry_dir: Path) -> list[Path]:
    if not telemetry_dir.exists():
        return []
    return sorted(p for p in telemetry_dir.glob("*.jsonl") if p.name != ANCHORS_FILE)


def verify_dir(telemetry_dir: Path) -> tuple[list[VerifyResult], list[str]]:
    """Chain check plus the anchor cross-check that catches a truncated tail."""
    results: list[VerifyResult] = []
    problems: list[str] = []
    anchors = load_anchors(telemetry_dir)
    for file in telemetry_files(telemetry_dir):
        result = verify_file(file)
        results.append(result)
        if anchors is None:
            problems.append(f"{file.name}: no anchor file, tail edits are not detectable")
            continue
        entry = anchors.get("files", {}).get(file.name)
        if entry is None:
            problems.append(f"{file.name}: not listed in {ANCHORS_FILE}")
            continue
        try:
            actual_head = head_hash(file)
        except (RecordError, KeyError) as exc:
            problems.append(f"{file.name}: unreadable head ({exc})")
            continue
        if entry.get("head_hash") != actual_head:
            problems.append(
                f"{file.name}: anchor head {str(entry.get('head_hash'))[:12]} does not match "
                f"file head {str(actual_head)[:12]} — records added or removed outside the writer"
            )
        if entry.get("records") != result.records:
            problems.append(
                f"{file.name}: anchor says {entry.get('records')} record(s), file has "
                f"{result.records}"
            )
    return results, problems


def fmt(value: Any, *, unit: str = "") -> str:
    """Print null as `not measured`, so absence can never be read as zero."""
    if value is None:
        return "not measured"
    return f"{value}{unit}"


def summary_lines(telemetry_dir: Path) -> list[str]:
    lines = ["AGENT TASK TELEMETRY SUMMARY", f"dir: {telemetry_dir}"]
    total_duration = 0.0
    total_commits = 0
    duration_records = 0
    commit_records = 0
    unmeasured: dict[str, int] = {}
    for file in telemetry_files(telemetry_dir):
        result = verify_file(file)
        for record in read_records(file):
            task = record.get("task_id")
            state = record.get("state")
            duration = record.get("duration_s")
            commits = record.get("commits")
            derived = record.get("derived") or compute_derived(record)
            if duration is not None:
                total_duration += float(duration)
                duration_records += 1
            if commits is not None:
                total_commits += int(commits)
                commit_records += 1
            for key in NULLABLE_FIELDS:
                if record.get(key) is None:
                    unmeasured[key] = unmeasured.get(key, 0) + 1
            lines.append(
                f"  {task}  seq={record.get('seq')}  agent={fmt(record.get('agent_id'))}"
                f"  state={fmt(state)}"
            )
            lines.append(
                f"    duration_s={fmt(duration)}  commits={fmt(commits)}"
                f"  tool_calls={fmt(record.get('tool_calls'))}"
                f"  subagent_tokens={fmt(record.get('subagent_tokens'))}"
            )
            lines.append(
                f"    derived: tokens/call={fmt(derived.get('subagent_tokens_per_tool_call'))}"
                f"  s/commit={fmt(derived.get('duration_s_per_commit'))}"
                f"  tokens/s={fmt(derived.get('tokens_per_duration_s'))}"
            )
            for name, reason in sorted((derived.get("not_computed") or {}).items()):
                lines.append(f"    {name}: {reason}")
            lines.append(
                f"    failure_reason={fmt(record.get('failure_reason'))}"
                f"  defects_found_by_reviewer={fmt(record.get('defects_found_by_reviewer'))}"
                f"  chain={result.status}"
            )
    lines.append("TOTALS (only over records where the value was measured)")
    lines.append(
        f"  duration_s total = {round(total_duration, 3)} over {duration_records} measured"
        " record(s); unmeasured records contribute nothing to this sum"
    )
    lines.append(f"  commits total = {total_commits} over {commit_records} measured record(s)")
    per_file = ", ".join(f"{f.name}={len(read_records(f))}" for f in telemetry_files(telemetry_dir))
    lines.append(f"  records per task file: {per_file or 'none'}")
    lines.append("NOT MEASURED (absence, not zero — do not read these as no cost)")
    if unmeasured:
        for key, count in sorted(unmeasured.items()):
            lines.append(f"  {key}: null in {count} record(s)")
    else:
        lines.append("  nothing: every nullable field carries a measured value")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append-only executor task telemetry.")
    parser.add_argument("--task", help="task id, e.g. W24-A (also the record file name)")
    parser.add_argument("--agent", help="agent id of the executor that did the work")
    parser.add_argument("--role", default=None)
    parser.add_argument("--base-sha", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--started-at", default=None, help="ISO-8601, 'now', or 'null'")
    parser.add_argument("--ended-at", default=None, help="ISO-8601, 'now', or 'null'")
    parser.add_argument("--duration-s", default=None, help="seconds, or 'null'")
    parser.add_argument("--tool-calls", default=None, help="integer, or 'null' (absent != 0)")
    parser.add_argument("--subagent-tokens", default=None, help="integer, or 'null'")
    parser.add_argument("--commits", default=None, help="integer commit count, or 'null'")
    parser.add_argument("--files-touched", default=None, help="integer count, or 'null'")
    parser.add_argument("--own-tests-passed", default=None, help="count, verbatim text, or null")
    parser.add_argument("--defects-found", default=None, help="found by the reviewer, or null")
    parser.add_argument("--failure-reason", default=None)
    parser.add_argument("--state", default=None, help=f"one of {', '.join(STATES)}")
    parser.add_argument("--recorded-by", default="coordinator", help="who typed this in")
    parser.add_argument("--telemetry-dir", default=str(DEFAULT_TELEMETRY_DIR))
    parser.add_argument("--verify", action="store_true", help="recompute every hash chain")
    parser.add_argument("--summary", action="store_true", help="print per-task and total lines")
    parser.add_argument(
        "--show-schema",
        action="store_true",
        help="print the field list and the null rules, then exit",
    )
    args = parser.parse_args(argv)
    force_utf8_streams()
    telemetry_dir = Path(args.telemetry_dir)

    if args.show_schema:
        print("payload keys:", ", ".join(HASHED_KEYS))
        print("integrity keys:", ", ".join(INTEGRITY_KEYS))
        print("nullable fields (null = not measured, never 0):", ", ".join(NULLABLE_FIELDS))
        print("states:", ", ".join(STATES))
        return 0

    if args.verify:
        results, problems = verify_dir(telemetry_dir)
        print("CHAIN VERIFY")
        print(f"dir: {telemetry_dir}")
        if not results and not problems:
            print("  no telemetry files found — nothing to verify (status: NOT_MEASURED)")
            return 0
        for result in results:
            detail = f"  ({result.problem})" if result.problem else ""
            print(f"  {result.file}: {result.status}  records={result.records}{detail}")
        for problem in problems:
            print(f"  ANCHOR {problem}")
        ok = all(result.ok for result in results) and not problems
        print(f"verify: {'OK' if ok else 'FAIL'}")
        return 0 if ok else 1

    if args.summary:
        for line in summary_lines(telemetry_dir):
            print(line)
        return 0

    if not args.task or not args.state:
        parser.error(
            "appending a record needs --task and --state; use --verify or --summary"
            " to read what is already recorded"
        )
    try:
        payload = build_record(args, recorded_by=args.recorded_by)
        file = append_record(telemetry_dir, payload)
    except RecordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"appended 1 record to {file}")
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

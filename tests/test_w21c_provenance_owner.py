"""W21-C: one owner resolves «post row -> what the plan decided» (the P97 debt).

Measured at `a6c9b46`, not assumed. `tgfarm/distribution.py` had no provenance at
all: the queue preview is a list comprehension inside
`_build_primary_platform_plan` (line 651 of that file) whose rows carried exactly
`post_public_id`, `title`, `channel`, `status`, `scheduled_at` — while
`tgfarm/api.py` answered the same operator question for the same row from a
hand-written triple (`_plan_entry_reference` + `_iso_stamp` + `_plan_snapshot_labels`)
inside `_post_view`. Two services of one object, two answers, and the second one
untested against the first — the defect class this repo keeps paying for.

Now there is one owner, in `tgfarm/editorial_plan.py`, because the two columns
describe a plan decision and the resolution must not need the HTTP layer or the
publisher to run. This file pins the four things the owner is the only place
allowed to decide:

* which values count as a reference at all (`0`, a negative, a `bool`, a string
  name no row and so say nothing — P34's other side: an unusable value may not be
  promoted into a claim);
* one clock for the booked minute, second precision, and a value that is not a
  moment rendered as `None` rather than as 1970;
* the three keys as one vocabulary, and the fourth state — all keys *absent* —
  as a separate function no value can imitate;
* no second copy: the key literals live in the owner and in no other served file,
  checked against the source text, which is how a rename or a re-implemented rule
  gets caught in review rather than in production.

Offline by construction: a fake session counts statements instead of a database,
`resolve_plan_labels` is never given one that touches a file, and nothing here
imports `tgfarm.api` (importing the app would move the live store to WAL).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tgfarm.editorial_plan import (
    PROVENANCE_COLUMNS_MISSING,
    PROVENANCE_KEYS,
    plan_provenance,
    provenance_columns_missing,
    provenance_not_read,
    resolve_plan_labels,
)

ROOT = Path(__file__).resolve().parent.parent

#: Only the owner may write these key literals into a payload. `plan_slot_at` is
#: deliberately missing: the booking reader (`api._booking_row_view`) legitimately
#: serves it as one of its six keys, because *there* it is the reserved instant the
#: queue is measured against, not a provenance statement about a plan entry.
OWNER_ONLY_KEYS = ('"plan_entry_id":', '"plan_public_id":')


class _Row:
    """A post row as far as provenance is concerned: two columns and nothing else."""

    def __init__(self, entry_id: Any = None, slot_at: Any = None) -> None:
        self.plan_entry_id = entry_id
        self.plan_slot_at = slot_at


class _RecordingSession:
    """Counts reads, so «looked and found nothing» can be told from «did not look»."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.reads = 0

    def execute(self, _statement: Any) -> Any:
        self.reads += 1
        session = self

        class _Result:
            def all(self) -> list[Any]:
                return list(session.rows)

        return _Result()


# ------------------------------------------------------------------ the vocabulary
def test_the_owner_offers_exactly_the_three_keys_the_card_and_preview_use() -> None:
    assert PROVENANCE_KEYS == ("plan_entry_id", "plan_slot_at", "plan_public_id")
    assert set(plan_provenance(_Row(), {})) == set(PROVENANCE_KEYS)


def test_the_slot_of_a_named_entry_travels_even_when_the_answer_does_not() -> None:
    """A pruned entry is not the absence of a plan, and the payload must not blur them."""
    gone = plan_provenance(_Row(904, datetime(2026, 11, 5, 9, 0, tzinfo=UTC)), {})
    unplanned = plan_provenance(_Row(None, None), {})
    assert gone["plan_entry_id"] == 904
    assert gone["plan_public_id"] is None
    assert gone["plan_slot_at"] == "2026-11-05T09:00:00+00:00"
    assert unplanned == {
        "plan_entry_id": None,
        "plan_slot_at": None,
        "plan_public_id": None,
    }
    assert gone != unplanned, "an unread answer was folded into 'no plan'"


@pytest.mark.parametrize("junk", [0, -3, True, False, "7", 7.5, None, [], {}])
def test_a_reference_that_cannot_name_a_row_says_nothing(junk: Any) -> None:
    """`0` and a negative are not autoincrement ids; a float and a bool name none either.

    Serving any of them as a reference would let the panel print "по плану" about a
    row whose column was never written by the plan — the same invention `0` is for
    a count (P34), on the other side of the payload.
    """
    view = plan_provenance(_Row(junk, None), {})
    assert view["plan_entry_id"] is None, f"{junk!r} was promoted to a reference"
    assert view["plan_public_id"] is None


def test_a_float_whole_number_is_read_as_the_id_it_holds() -> None:
    """`7.0` names entry 7 exactly as `7` does; a usable id, not an int type."""
    assert plan_provenance(_Row(7.0, None), {7: "pl7"})["plan_entry_id"] == 7
    assert plan_provenance(_Row(7.5, None), {})["plan_entry_id"] is None


def test_the_snapshot_named_for_an_entry_is_served_verbatim() -> None:
    view = plan_provenance(_Row(7, None), {7: "pl0aaaabbbbccccdddd"})
    assert view["plan_public_id"] == "pl0aaaabbbbccccdddd"


# ---------------------------------------------------------------------- one clock
def test_a_naive_slot_is_served_in_one_clock_and_at_second_precision() -> None:
    """SQLite hands `DateTime(timezone=True)` back naive-UTC.

    Printed as it arrives, the browser reads it as local wall-clock time, so the
    booking lands hours from the minute the plan wrote. The owner repairs that once
    instead of at every call site.
    """
    naive = datetime(2026, 11, 5, 9, 0, 30, 500000)
    assert plan_provenance(_Row(None, naive), {})["plan_slot_at"] == (
        "2026-11-05T09:00:30+00:00"
    )


def test_an_aware_slot_is_moved_to_utc_rather_than_relabeled() -> None:
    east = datetime(2026, 11, 5, 12, 0, tzinfo=timezone(timedelta(hours=3)))
    assert plan_provenance(_Row(None, east), {})["plan_slot_at"] == (
        "2026-11-05T09:00:00+00:00"
    )


@pytest.mark.parametrize("junk", ["не дата", "", 0, 1765000000, None, object()])
def test_a_slot_that_is_not_a_moment_is_none_and_never_a_fabricated_date(junk: Any) -> None:
    stamp = plan_provenance(_Row(5, junk), {})["plan_slot_at"]
    assert stamp is None, f"{junk!r} became {stamp!r}"
    assert "1970" not in str(stamp)


def test_an_iso_string_in_the_column_is_parsed_then_normalised() -> None:
    """A row written outside the model can hold text; the owner reads it or refuses it."""
    assert plan_provenance(_Row(None, "2026-11-05T09:00:00"), {})[
        "plan_slot_at"
    ] == "2026-11-05T09:00:00+00:00"


# ------------------------------------------------------------------ the fourth state
def test_not_read_is_no_keys_and_no_value_can_imitate_it() -> None:
    """`{}` and three nulls are different answers, and only one of them is a silence."""
    unread = provenance_not_read()
    assert unread == {}
    for key in PROVENANCE_KEYS:
        assert key not in unread
        assert key in plan_provenance(_Row(), {}), key


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("(sqlite3.OperationalError) no such column: posts.plan_slot_at", True),
        ("(sqlite3.OperationalError) no such column: posts.plan_entry_id", True),
        ("(sqlite3.OperationalError) no such table: posts", False),
        ("(sqlite3.OperationalError) database is locked", False),
        ("", False),
    ],
)
def test_a_missing_column_is_not_a_query_that_failed(detail: str, expected: bool) -> None:
    assert provenance_columns_missing(RuntimeError(detail)) is expected


def test_the_drift_vocabulary_is_a_name_a_payload_can_carry() -> None:
    assert PROVENANCE_COLUMNS_MISSING == "plan_columns_missing"


# ------------------------------------------------------------------- the batch read
def test_nothing_named_touches_no_table() -> None:
    """The per-platform join is paid for only when a row actually names an entry."""
    session = _RecordingSession()
    assert resolve_plan_labels(session, [None, 0, -1, "x", True]) == {}
    assert session.reads == 0, "an empty answer was bought with a statement"


def test_a_batch_of_rows_is_resolved_in_one_read_not_one_per_row() -> None:
    session = _RecordingSession([(7, "pl7"), (9, "pl9")])
    labels = resolve_plan_labels(session, [7, 7, 9, None, 0])
    assert session.reads == 1
    assert labels == {7: "pl7", 9: "pl9"}
    assert plan_provenance(_Row(7, None), labels)["plan_public_id"] == "pl7"


def test_an_entry_row_that_is_gone_is_a_missing_key_not_a_placeholder() -> None:
    """Retention may prune a snapshot; the post still names the entry it came from."""
    session = _RecordingSession([(7, "pl7")])
    labels = resolve_plan_labels(session, [7, 8])
    assert 8 not in labels
    view = plan_provenance(_Row(8, None), labels)
    assert view["plan_public_id"] is None
    assert view["plan_entry_id"] == 8


def test_a_label_row_without_a_public_id_is_not_renamed_into_an_answer() -> None:
    session = _RecordingSession([(7, None)])
    assert resolve_plan_labels(session, [7]) == {}


# ----------------------------------------------------------------- no second copy
#: The services that answer «этот пост по плану или совпал?» about one post row.
#: Every one of them must reach the payload through the owner, and the list has to
#: name every one of them: a service that answers from its own dict is the drift
#: this wave was ordered to close.
PROVENANCE_SERVICES = ("api.py",)


def _provenance_dicts(root: Path) -> list[str]:
    """Every dict in `tgfarm/` assembling two of the provenance keys, by file.

    A dict needs two of the three keys (`PROVENANCE_KEYS`) to count as the
    provenance triple, which keeps `s4_generation`'s booking decision — a record
    about a cluster that shares one key name — out of the net. `plan_slot_at`
    alone cannot be the test: the booking reader (`api._booking_row_view`)
    legitimately serves it as one of its six keys, because *there* it is the
    reserved instant the queue is measured against, not a statement about an entry.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - a broken file fails elsewhere
            raise AssertionError(f"{path.name} does not parse: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            named = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if len(named & set(PROVENANCE_KEYS)) >= 2:
                found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_no_second_copy_of_the_provenance_keys_exists_anywhere() -> None:
    """The class of defect this wave exists to close, pinned on the whole package.

    Two hand-written dicts answering one question is how the post card and the
    distribution queue preview drifted apart while each kept its own green test,
    and no file-by-file list of suspects would catch the third one. So the rule is
    stated over all of `tgfarm/`: exactly one dict in the tree assembles these
    keys, and it is the owner's.

    `ast` rather than text, because a docstring may quote a key and a payload may
    not build one.
    """
    copies = _provenance_dicts(ROOT / "tgfarm")
    owner = "editorial_plan.py"
    assert all(owner in where for where in copies), f"a second copy: {copies}"
    assert len(copies) == 1, f"the owner itself has {len(copies)} copies of the keys"


@pytest.mark.parametrize("service", PROVENANCE_SERVICES)
def test_every_service_that_answers_provenance_asks_the_owner(service: str) -> None:
    """The same rule from the other side: a named service that stopped asking.

    Dropping the owner's name from a caller is how a file goes back to answering
    the question itself while its own tests stay green — they would be reading the
    same dict they write.
    """
    text = (ROOT / "tgfarm" / service).read_text(encoding="utf-8")
    assert "plan_provenance(" in text, f"{service} answers provenance without the owner"
    assert "resolve_plan_labels(" in text, f"{service} resolves the snapshot without the owner"


def test_the_owner_imports_neither_caller_module_at_module_level() -> None:
    """The import cycle the brief warns about, pinned rather than remembered.

    The owner must answer without the HTTP layer or the publisher present, and
    `tgfarm/plan_execution.py` imports this module at its own top level
    (`tgfarm/plan_execution.py:75`), so a module-level import back would close the
    cycle that breaks the whole test build. The existing reference to
    `plan_execution` is a function-local one and has to stay function-local.
    """
    source = (ROOT / "tgfarm" / "editorial_plan.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module)
        elif isinstance(node, ast.Import):
            module_level.update(alias.name for alias in node.names)
    for forbidden in ("tgfarm.api", "tgfarm.distribution", "tgfarm.plan_execution"):
        assert forbidden not in module_level, f"module-level import of {forbidden}"
    pointing = [line for line in source.splitlines() if line.startswith("from tgfarm.")]
    assert not [line for line in pointing if "plan_execution" in line], (
        "the plan_execution reference moved to module level"
    )

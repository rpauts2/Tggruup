"""One snapshot, one bound: the three readers may not each carry their own 40.

`editorial_plan.MAX_ENTRIES` is the builder's cap — a plan snapshot holds at most
that many entries — and three other modules each needed "read the whole ledger,
do not slice it": `editorial.MAX_OPEN_ORDERS`, `plan_execution.MAX_ENTRIES_PER_RUN`
and `s4_generation.MAX_BOOKINGS_PER_RUN`. Each stated the same fact and each held
its own literal `40`, so the number could be edited in one place and stay true in
two others — the drift class this registry has paid for since wave 14, and the
residual wave 21 named for wave 22 and wave 22 named for the coordinator.

Two guards, because the fix has two halves and each needs its own:

* the source guard, because an assignment evaluated at import time cannot be
  observed moving at runtime. `MAX_ENTRIES_PER_RUN = MAX_ENTRIES` and
  `MAX_ENTRIES_PER_RUN = 40` are indistinguishable to a test that patches the
  owner — both read 40 — so the only falsification is the text itself;
* the runtime guard, because `editorial` could not import its owner at all
  (`editorial_plan` imports `editorial` at top level, so the cycle closes the
  other way) and its bound is a live read through a local import. A live read is
  observable, and an operator raising the snapshot cap must see the queue's
  default follow it.

Database hygiene: nothing here writes. The fake session in the runtime test stops
`open_order_cluster_ids` before it reaches a real query, so the shared session
database (`tests/conftest.py:242`) is not touched and needs no teardown.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tgfarm import editorial, editorial_plan

ROOT = Path(__file__).resolve().parents[1]

#: Each derived bound and the file that must derive it. The owner is not in this
#: table: `editorial_plan.MAX_ENTRIES` legitimately is a bare number, and this
#: file would be useless if it could not tell the owner from a copy.
DERIVED_BOUNDS = {
    "tgfarm/plan_execution.py": ("MAX_ENTRIES_PER_RUN",),
    "tgfarm/s4_generation/__init__.py": ("MAX_BOOKINGS_PER_RUN",),
}

#: The name `editorial` used to carry its copy under. It cannot be an assignment
#: from the owner, so it must not come back as one either.
RETIRED_COPY = "MAX_OPEN_ORDERS"


def _module(relpath: str) -> ast.Module:
    return ast.parse((ROOT / relpath).read_text(encoding="utf-8"))


def _module_level_assigns(tree: ast.Module) -> dict[str, ast.expr]:
    """Top-of-file `NAME = value` only: a rebind inside a function is not a bound."""
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = node.value
    return found


def _int_literal(node: ast.expr) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    return None


# ---------------------------------------------------------------- source guards
def test_the_derived_bounds_are_reads_and_never_literals():
    """No reader of the snapshot may hard-code its size again.

    Each name must still exist (a guard that passes because the code it guards
    was deleted is not a guard), must not be an integer literal, and must name
    the owner.
    """
    assert isinstance(editorial_plan.MAX_ENTRIES, int)

    for relpath, names in DERIVED_BOUNDS.items():
        assigns = _module_level_assigns(_module(relpath))
        for name in names:
            assert name in assigns, f"{name} is gone from {relpath}"
            value = assigns[name]
            assert _int_literal(value) is None, (
                f"{relpath} copies the bound as a literal again: {name} = {_int_literal(value)}"
            )
            assert isinstance(value, ast.Name) and value.id == "MAX_ENTRIES", (
                f"{relpath} derives {name} from something other than MAX_ENTRIES: "
                f"{ast.dump(value)}"
            )


def test_the_import_cycle_copy_stays_retired():
    """`editorial` must not bring its own 40 back under the old name.

    The cycle is measured, not assumed: adding `from tgfarm.editorial_plan
    import MAX_ENTRIES` to `tgfarm/editorial.py`'s own import block makes the
    package unimportable — `ImportError: cannot import name 'queue' from
    partially initialized module 'tgfarm.editorial'`, because
    `editorial_plan.py:46` imports `editorial.queue` at its top level. So this
    file carries the bound as a call, and the literal it replaces has to stay
    replaced.
    """
    assigns = _module_level_assigns(_module("tgfarm/editorial.py"))
    assert RETIRED_COPY not in assigns, (
        f"{RETIRED_COPY} is back as a module-level binding; the bound belongs to "
        "editorial_plan.MAX_ENTRIES and is read through snapshot_bound()"
    )
    assert hasattr(editorial, "snapshot_bound"), "the live read has no owner"


# --------------------------------------------------------------- runtime guards
class _Captured(Exception):
    """Stops the fake session before it has to pretend to be a result set."""


class _RecordingSession:
    """Remembers the LIMIT the query was built with, then leaves the read."""

    def __init__(self, sink: list[int]) -> None:
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement):
        self.sink.append(int(statement._limit))
        raise _Captured


def _applied_limit(monkeypatch, args: tuple[int, ...], kwargs: dict) -> int:
    seen: list[int] = []
    monkeypatch.setattr(editorial, "session_scope", lambda: _RecordingSession(seen))
    try:
        editorial.open_order_cluster_ids(*args, **kwargs)
    except _Captured:
        pass
    assert len(seen) == 1, seen
    return seen[0]


def test_the_default_read_of_open_orders_follows_the_owner(monkeypatch):
    """Moving the snapshot cap moves the queue's default with it.

    This is the difference between a read and a copy, and it is only observable
    here: `editorial` reaches its owner at call time, so the bound it applies is
    the owner's number as it reads now.
    """
    monkeypatch.setattr(editorial_plan, "MAX_ENTRIES", 7)

    assert editorial.snapshot_bound() == 7
    assert _applied_limit(monkeypatch, (), {}) == 7, "the default sliced the ledger"


def test_a_caller_that_names_its_limit_still_wins(monkeypatch):
    """The owner is a default, not a second scheduler.

    `queue()` passes its own limit and `s4_generation.book_plan_slots` passes
    the pass's bound; neither may be overridden by a number the reader invented.
    """
    monkeypatch.setattr(editorial_plan, "MAX_ENTRIES", 7)

    assert _applied_limit(monkeypatch, (3,), {}) == 3
    assert _applied_limit(monkeypatch, (), {"limit": 0}) == 1, (
        "max(1, ...) is the floor the read already had; the owner does not lift it"
    )

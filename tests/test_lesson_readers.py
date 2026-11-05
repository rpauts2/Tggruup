"""No lesson the farm stores may go unread.

The defect class is the DEAD LESSON: S12 computes something, pays a database write
for it every cycle, and no code ever asks for it again. A lesson nobody reads is
not dormant documentation — it is a claim that the farm learned something, charged
in DB writes and reported as `lessons`, that changes no decision. It is the same
defect as a `Settings` knob with no call site (`tests/test_dead_knobs.py`) and the
same one as a coverage payload whose four fields no panel rendered (reverted at P51).

The rule, stated exactly:

* a lesson kind is WRITTEN when `tgfarm/**.py` contains `_upsert(channel, "<kind>",
  ...)` (the package's only storage entry point, `tgfarm/s12_learning/__init__.py`)
  or a `LearningMemory(kind="<kind>", ...)` construction;
* a kind is READ when some file compares the column (`LearningMemory.kind ==
  "<kind>"`) or calls a lesson reader with the keyword (`lessons(cid,
  kind="<kind>")`, `_lessons(...)`, `format_lessons(...)`);
* a read inside the same function that writes the kind is the upsert looking its
  own row up, not a consumer — `record_gate_failure` does exactly that, so it is
  excluded;
* a read inside `tgfarm/s12_learning/**` counts only when the reading function is
  imported or attribute-called from outside that package. One level of helper
  indirection is followed, not more, which is how `slot` (read by `best_hours`,
  consumed at `tgfarm/s7_publish/__init__.py:80`) and `format` (read by
  `format_lessons`, consumed at `tgfarm/s4_generation/__init__.py:1184`) pass;
* anything else must be listed in `UNREAD_KIND_ALLOWLIST` with a reason naming the
  reader, and `test_allowlist_entries_are_still_needed` fails an entry that stops
  being dead, so the list cannot become a hiding place.

Its own limits, on purpose, because a guard that overstates itself becomes the next
bug:

* this is an AST-presence check, so it proves ABSENCE, not presence: a `kind=`
  keyword inside a call nobody iterates satisfies it;
* reads under `tests/**` are deliberately not counted — a test that queries a kind
  is not a farm that uses the lesson;
* it works at KIND granularity. A kind consumed row by row (`mistake`, `tactic`)
  passes even when one `key` or `value` field inside it is never rendered; telling
  a rendered field from an inert one needs a consumer-side AST this file does not
  attempt. The format-lesson value fields that no production reader names today
  (`niche`, `window_days`, `baseline_engagement`, `relative_quality`, `quality`,
  `reliable`) are reported as a finding, not silently asserted safe;
* a kind written through a computed name (`kind=some_variable`) is invisible, just
  like a computed settings attribute is invisible to the knob guard;
* the check reads the source tree, so it cannot see a kind consumed through raw
  SQL text (`session.execute("select ... where kind = 'slot'")`). None exists in
  the package today; `test_lesson_rows_are_written_only_by_s12` is the closest
  tripwire for that class.

The last two tests are the proof of aim, not a rubber stamp: a synthetic tree with
one unread kind, and the real tree with a fake unread kind injected into the source
this package actually ships.
"""

from __future__ import annotations

import ast
import dataclasses
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete

from tgfarm.config import get_config
from tgfarm.core.settings import PACKAGE_DIR
from tgfarm.db.models import LearningMemory
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import _persona_block
from tgfarm.s12_learning import best_angle_weights, best_hours, format_lessons, lessons

#: Kinds this checker cannot see, one entry per kind with the reason it is a false
#: positive. A reason must name the reader; "trust me" is not a reason.
UNREAD_KIND_ALLOWLIST: dict[str, str] = {}

#: The kinds the current source really writes, asserted so the derivation cannot
#: pass by finding nothing. Measured from `tgfarm/s12_learning/__init__.py`.
#: `learning_memory.kind` is a free `String(32)` column with no database enum, so
#: this list is the only place the vocabulary is stated as data.
EXPECTED_WRITTEN_KINDS = frozenset({"format", "mistake", "slot", "strategy", "tactic"})

SEED_CHANNELS: list[str] = []


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    """Every row these tests write belongs to a channel they created."""
    yield
    if SEED_CHANNELS:
        with session_scope() as session:
            session.execute(
                delete(LearningMemory).where(LearningMemory.channel_id.in_(SEED_CHANNELS))
            )
        SEED_CHANNELS.clear()


# --------------------------------------------------------------- the derivation
def _site(path: Path, scope: str, line: int) -> tuple[Path, str, int]:
    return (path, scope, line)


@lru_cache(maxsize=1)
def _corpus() -> Mapping[Path, str]:
    """Every Python file of the package, keyed by its resolved path.

    Cached because the nine tests below read the same tree; a caller that wants to
    mutate it (`test_guard_reports_a_new_unread_kind_in_the_real_tree`) copies it
    first.
    """
    out: dict[Path, str] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        resolved = path.resolve()
        if "__pycache__" in resolved.parts:
            continue
        out[resolved] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _is_s12(path: Path) -> bool:
    return (PACKAGE_DIR / "s12_learning").resolve() in path.parents


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _keyword_str(node: ast.Call, name: str) -> str | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return _literal_str(keyword.value)
    return None


def _scopes(tree: ast.AST) -> list[tuple[str, list[ast.AST]]]:
    """Nodes grouped by the function that owns them, innermost wins, plus `<module>`.

    Without the innermost rule a write inside a nested function would also count
    as a write of the outer one, and a writer could then read itself back into
    existence.
    """
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    owner: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for func in functions:
        for node in ast.walk(func):
            current = owner.get(id(node))
            if current is None:
                owner[id(node)] = func
                continue
            current_span = (current.end_lineno or 0) - current.lineno
            func_span = (func.end_lineno or 0) - func.lineno
            if func_span < current_span:
                owner[id(node)] = func
    grouped: dict[str, list[ast.AST]] = {func.name: [] for func in functions}
    grouped["<module>"] = []
    for node in ast.walk(tree):
        func = owner.get(id(node))
        grouped[func.name if func is not None else "<module>"].append(node)
    return list(grouped.items())


def find_written_kinds(
    corpus: Mapping[Path, str],
) -> dict[str, list[tuple[Path, str, int]]]:
    """Kind -> write sites, from `_upsert(...)` calls and row constructions.

    Only the kind is a site field, because only the kind is what a reader can
    select on; key-level deadness inside a live kind is out of scope, said so in
    the module docstring instead of pretended to be covered.
    """
    writes: dict[str, list[tuple[Path, str, int]]] = defaultdict(list)
    for path, text in corpus.items():
        for scope, nodes in _scopes(ast.parse(text)):
            for node in nodes:
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node.func)
                if name == "_upsert" and len(node.args) >= 2:
                    kind = _literal_str(node.args[1])
                elif name == "LearningMemory":
                    kind = _keyword_str(node, "kind")
                else:
                    continue
                if kind:
                    writes[kind].append(_site(path, scope, node.lineno))
    return dict(writes)


def find_read_kinds(
    corpus: Mapping[Path, str],
) -> dict[str, list[tuple[Path, str, int]]]:
    """Kind -> read sites: a column comparison or a lesson reader called by name."""
    reads: dict[str, list[tuple[Path, str, int]]] = defaultdict(list)
    for path, text in corpus.items():
        for scope, nodes in _scopes(ast.parse(text)):
            for node in nodes:
                kind = None
                if isinstance(node, ast.Call) and _call_name(node.func).endswith("lessons"):
                    kind = _keyword_str(node, "kind")
                elif isinstance(node, ast.Compare) and len(node.ops) == 1:
                    kind = _compare_kind(node)
                if kind:
                    reads[kind].append(_site(path, scope, node.lineno))
    return dict(reads)


def _compare_kind(node: ast.Compare) -> str | None:
    """`<something>.kind == "value"` in either order — the column test the ORM uses."""
    if not isinstance(node.ops[0], ast.Eq):
        return None
    sides = [node.left, *node.comparators]
    attribute = next(
        (side for side in sides if isinstance(side, ast.Attribute) and side.attr == "kind"),
        None,
    )
    if attribute is None:
        return None
    for side in sides:
        if side is attribute:
            continue
        found = _literal_str(side)
        if found:
            return found
    return None


def _helper_reached_outside(
    path: Path, scope: str, corpus: Mapping[Path, str], s12_dir: Path
) -> bool:
    """True when a helper defined inside `s12_dir` is imported or used outside it."""
    if scope == "<module>":
        return False
    for other, text in corpus.items():
        if other == path or s12_dir in other.parents:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tgfarm.s12"):
                if any(alias.name == scope for alias in node.names):
                    return True
            elif isinstance(node, ast.Attribute) and node.attr == scope:
                if isinstance(node.value, ast.Name) and node.value.id.startswith("s12"):
                    return True
    return False


def find_unread_kinds(
    writes: Mapping[str, Iterable[tuple[Path, str, int]]],
    reads: Mapping[str, Iterable[tuple[Path, str, int]]],
    corpus: Mapping[Path, str],
    s12_dir: Path | None = None,
) -> list[str]:
    """Kinds the package stores and never asks for again.

    Pure and parameterised on purpose: the reverse-run tests hand it a synthetic
    tree, so the matcher itself is under test and not only its result. `s12_dir`
    is the package whose helpers need one level of indirection to be consumed;
    the synthetic tree passes its own, so the indirection rule is exercised and
    not only the outside-read rule.
    """
    lesson_dir = (PACKAGE_DIR / "s12_learning").resolve() if s12_dir is None else s12_dir
    dead: list[str] = []
    for kind, sites in writes.items():
        writer_scopes = {(path, scope) for path, scope, _line in sites}
        alive = False
        for path, scope, _line in reads.get(kind, ()):
            if (path, scope) in writer_scopes:
                # The write looking its own row up to update it is not a consumer.
                continue
            if lesson_dir not in path.parents:
                alive = True
                break
            if _helper_reached_outside(path, scope, corpus, lesson_dir):
                alive = True
                break
        if not alive:
            dead.append(kind)
    return sorted(dead)


@lru_cache(maxsize=1)
def _real_state() -> tuple[Mapping[str, list], Mapping[str, list], Mapping[Path, str], list[str]]:
    corpus = _corpus()
    writes = find_written_kinds(corpus)
    reads = find_read_kinds(corpus)
    return writes, reads, corpus, find_unread_kinds(writes, reads, corpus)


def _relative(path: Path) -> str:
    return str(path.relative_to(PACKAGE_DIR.parent)).replace("\\", "/")


def _describe(
    kinds: Iterable[str], reads: Mapping[str, Iterable[tuple[Path, str, int]]]
) -> str:
    parts = []
    for kind in kinds:
        sites = list(reads.get(kind, ()))
        tail = ", ".join(f"{_relative(path)}:{line} ({name})" for path, name, line in sites[:3])
        parts.append(f"{kind} -> {tail or 'no read site at all'}")
    return "; ".join(parts)


# ------------------------------------------------------------------- the guard
def test_lesson_kind_derivation_is_not_vacuous():
    """A guard that finds nothing because it looks at nothing is worse than none."""
    writes, reads, _, _ = _real_state()
    assert EXPECTED_WRITTEN_KINDS <= set(writes), (
        f"the derivation saw {sorted(writes)}, but the package writes "
        f"{sorted(EXPECTED_WRITTEN_KINDS)} — the checker is blind, not green"
    )
    assert set(reads) & set(EXPECTED_WRITTEN_KINDS), "no read site of any lesson kind was found"


def test_no_lesson_kind_is_written_without_a_reader():
    """The guard itself: a stored kind nobody queries is a failing test."""
    _writes, reads, _source, dead = _real_state()
    unexplained = [kind for kind in dead if kind not in UNREAD_KIND_ALLOWLIST]
    assert not unexplained, (
        "these lesson kinds are written to learning_memory every cycle and read by "
        "nothing in tgfarm/, so they change no decision: "
        + ", ".join(unexplained)
        + f" (read sites: {_describe(unexplained, reads)}). Either consume the kind "
        "where the decision is made and prove the decision moves, or delete the write."
    )


def test_lesson_rows_are_written_only_by_s12():
    """Learning is one stage: a second writer would dodge the reader audit by luck."""
    writes, _, _, _ = _real_state()
    outsiders = sorted(
        {
            f"{_relative(path)}:{line} ({kind})"
            for kind, sites in writes.items()
            for path, _scope, line in sites
            if not _is_s12(path)
        }
    )
    assert not outsiders, (
        "these places outside tgfarm/s12_learning write learning_memory rows: "
        + ", ".join(outsiders)
        + " — a lesson written anywhere else has no audit trail and no reader check."
    )


def test_allowlist_entries_are_still_needed():
    """An excuse for a kind the checker can now see through must be deleted."""
    _, _, _, dead = _real_state()
    stale = sorted(set(UNREAD_KIND_ALLOWLIST) - set(dead))
    assert not stale, (
        f"UNREAD_KIND_ALLOWLIST excuses {stale}, which the checker no longer finds dead"
    )


def test_the_shipped_lesson_kinds_are_the_expected_ones():
    """`angle` was deleted, not hidden: it must not come back as a written kind."""
    writes, _, _, _ = _real_state()
    assert "angle" not in writes, (
        "kind='angle' is written again. It was deleted because nothing in tgfarm/ "
        "read it: 5 rows and 10 statements (a SELECT and an INSERT each) per channel "
        "per cycle, 62% of the stage's DB writes, ~1.9 ms each. Re-adding it "
        "requires naming the line that reads it and the decision that moves."
    )
    assert EXPECTED_WRITTEN_KINDS == set(writes), (
        f"the written kinds changed from {sorted(EXPECTED_WRITTEN_KINDS)} to "
        f"{sorted(writes)}; update the expectation only with a reader line for each"
    )


# ------------------------------------------------ the dead lesson, measured live
def _seed_lesson(channel_id: str, kind: str, key: str, *, score: float = 0.99) -> None:
    """One row straight into the table, exactly as an old cycle would have left it."""
    with session_scope() as session:
        session.execute(
            delete(LearningMemory).where(
                LearningMemory.channel_id == channel_id,
                LearningMemory.kind == kind,
                LearningMemory.key == key,
            )
        )
        session.add(
            LearningMemory(
                channel_id=channel_id,
                kind=kind,
                key=key,
                value={"angle": key, "quality": score, "observed_quality": score, "n": 99},
                score=score,
                sample_size=99,
                confidence=0.99,
                note=f"seeded {kind}/{key}",
            )
        )


def _seed_channel() -> str:
    channel_id = f"lrdr_{uuid4().hex[:8]}"
    SEED_CHANNELS.append(channel_id)
    return channel_id


def _consumer_views(channel_id: str) -> dict[str, object]:
    """Every output a lesson row could move, read back as plain data."""
    personas = get_config().enabled_personas
    if not personas:
        pytest.skip("the shipped config declares no enabled persona")
    scoped = dataclasses.replace(personas[0], id=channel_id, angle_weights={})
    return {
        "angle_weights": best_angle_weights(channel_id),
        "hours": best_hours(channel_id),
        "formats": [row["key"] for row in format_lessons(channel_id)],
        "prompt": _persona_block(scoped, "news"),
    }


def test_a_stored_angle_lesson_moves_no_decision():
    """Deadness proven from code and data, not from an empty live table.

    A row of the deleted kind is written straight into `learning_memory` and every
    consumer of learning output is re-read: the angle weights S3 replaces the
    persona with, the posting hours S7 doubles, the format lessons S4 plans on and
    the S4 prompt itself. If any of them moved, the kind had a reader and deleting
    its writer was the wrong call.
    """
    channel_id = _seed_channel()
    before = _consumer_views(channel_id)
    _seed_lesson(channel_id, "angle", "analysis")
    assert len(lessons(channel_id, kind="angle")) == 1, (
        "the row is not even readable through the generic reader, so this proved "
        "nothing about consumers"
    )
    after = _consumer_views(channel_id)
    moved = [key for key in before if before[key] != after[key]]
    assert not moved, f"an `angle` row moved {moved}, so it does have a reader"


def test_the_learning_cycle_writes_no_unread_kind(monkeypatch):
    """Behavioural half of the guard: run the real writer, check what landed.

    `angle_performance` is faked with five eligible angles — the way a measured
    channel looks on a normal cycle — so the strategy branch of `_learn_channel`
    really runs against the real `_upsert` and the real table.
    """
    from tgfarm import s12_learning

    channel_id = _seed_channel()
    perf = {
        angle: {"n": 8, "er": 0.04, "ctr": 0.1, "quality": 0.5 + index / 10}
        for index, angle in enumerate(
            ("news", "analysis", "checklist", "why_it_matters", "history")
        )
    }
    monkeypatch.setattr(
        s12_learning, "angle_performance", lambda days, channel_id=None: dict(perf)
    )
    monkeypatch.setattr(s12_learning, "hook_style_performance", lambda days, channel_id=None: {})
    monkeypatch.setattr(s12_learning, "recent_metrics", lambda days: [])
    monkeypatch.setattr(s12_learning, "learn_format_lessons", lambda *a, **k: {"written": 0})

    s12_learning._learn_channel(channel_id)  # noqa: SLF001 - the stage's own entry point

    written = {row["kind"] for row in lessons(channel_id)}
    _writes, _reads, _source, dead = _real_state()
    assert written, "the learning cycle stored nothing, so this test measured nothing"
    assert written <= (EXPECTED_WRITTEN_KINDS - set(dead)), (
        f"the cycle wrote kinds with no reader: {sorted(written - EXPECTED_WRITTEN_KINDS)}"
    )
    assert "angle" not in written
    assert {"strategy", "tactic"} <= written, (
        "the angle evidence must still reach the kinds S3 and S4 read, or deleting "
        "the raw rows silently deleted the learning too"
    )


# ----------------------------------------------------- the guard proved on itself
def test_guard_reports_a_deliberately_unread_kind_in_a_synthetic_tree(tmp_path: Path):
    """A kind only written, never queried, must come back as dead — proof of aim.

    Two legitimate ways to be alive (an outside consumer, an in-package helper that
    the outside calls) plus one that is nothing but a write.
    """
    fake_dir = tmp_path / "s12_learning"
    fake_dir.mkdir()
    (fake_dir / "__init__.py").write_text(
        "def learn(channel_id):\n"
        "    _upsert(channel_id, 'strategy', 'angle_weights', {}, 0.5, 3, 0.2)\n"
        "    _upsert(channel_id, 'slot', '9', {}, 0.5, 3, 0.2)\n"
        "    _upsert(channel_id, 'moon_phase', 'full', {}, 0.5, 3, 0.2)\n"
        "\n"
        "def best_hours(channel_id):\n"
        "    return lessons(channel_id, kind='slot')\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from tgfarm.s12_learning import best_hours\n"
        "\n"
        "def plan(persona):\n"
        "    hours = best_hours(persona.id)\n"
        "    rows = lessons(persona.id, kind='strategy')\n"
        "    return hours, rows\n",
        encoding="utf-8",
    )
    corpus = {
        (fake_dir / "__init__.py").resolve(): (fake_dir / "__init__.py").read_text(
            encoding="utf-8"
        ),
        consumer.resolve(): consumer.read_text(encoding="utf-8"),
    }
    dead = find_unread_kinds(
        find_written_kinds(corpus), find_read_kinds(corpus), corpus, s12_dir=fake_dir.resolve()
    )
    assert dead == ["moon_phase"], (
        f"the guard reported {dead}; it has to catch exactly the kind nothing reads, "
        "while accepting both the direct outside read (`strategy`) and the "
        "in-package helper the outside imports (`slot` via `best_hours`)"
    )


def test_guard_reports_a_new_unread_kind_in_the_real_tree():
    """Same fake kind, real source: proves the matcher bites on this codebase."""
    corpus = dict(_corpus())
    s12_file = (PACKAGE_DIR / "s12_learning" / "__init__.py").resolve()
    fake = "moon_phase"
    corpus[s12_file] = (
        corpus[s12_file]
        + "\n\ndef _learn_moon(channel_id):\n"
        + "    _upsert(channel_id, 'moon_phase', 'full', {}, 0.5, 3, 0.2)\n"
    )
    writes = find_written_kinds(corpus)
    reads = find_read_kinds(corpus)
    dead = find_unread_kinds(writes, reads, corpus)
    assert fake in dead, (
        "writing an unread kind into the real source did not make this guard fail, "
        "so the guard itself is dead"
    )
    assert set(dead) - {fake} == set(UNREAD_KIND_ALLOWLIST), (
        f"the real tree reported {dead} dead, which nobody explained"
    )

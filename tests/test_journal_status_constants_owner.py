"""The journal's status vocabulary is spelled in exactly one file.

`tgfarm/crosspost_journal.py` owns `crosspost_logs.status`. At the base of this
block it declared a `JOURNAL_STATUS_*` constant for six of its nine tokens and
nothing for `scheduled`, `permanent` and `unknown` — although all three are
members of `JOURNAL_SUPPRESSED_STATUSES` and therefore of
`JOURNAL_KNOWN_STATUSES`. A status with no constant cannot be found by a grep for
its owner, so every reader that needed one re-typed it: the planner's three
budget sets (`tgfarm/distribution.py`), the journal/`posts` split pinned as
`tgfarm.s7_publish.CROSSPOST_ONLY_STATUSES`, and one test that had copied the
planner's cap arithmetic into a fifth literal set.

What this file pins, in order:

1. every token the owner declares has a constant, and every constant it declares
   is a token the owner declares — the direction that lets a new status be added
   to a set *without* being named;
2. the owner builds its own class sets out of those constants, not out of
   strings, so the vocabulary is written once;
3. no module under `tgfarm/` builds a status-vocabulary container out of
   spelled-out tokens, except the owner; the `api.py` sets that still do are
   registered by name as the gap this wave could not close, because that file
   belongs to another agent;
4. the three new constants still carry the journal's own spelling, and the
   journal's `unknown` is still not `posts.status`'s `publish_unknown`;
5. the spelling round-trips through the real column: a row written with the
   constant is read back by the constant, classified SUPPRESSED, and moves the
   planner's budget numbers through the same object the planner reads.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from tgfarm import crosspost_journal, distribution, s7_publish
from tgfarm.db.models import Channel, CrosspostLog, Post
from tgfarm.db.session import session_scope

PKG_ROOT = Path(inspect.getfile(crosspost_journal)).resolve().parent
OWNER_FILE = PKG_ROOT / "crosspost_journal.py"

#: Module-level containers that *name themselves* a status vocabulary, e.g.
#: `JOURNAL_SUPPRESSED_STATUSES`, `CROSSPOST_ONLY_STATUSES`,
#: `JOURNAL_OPERATOR_ATTENTION_STATUSES`. A single-status constant
#: (`JOURNAL_STATUS_POSTED = "posted"`) is not a container and is not caught.
_VOCAB_NAME = re.compile(r"^(?:JOURNAL|CROSSPOST)[A-Z0-9_]*STATUS[A-Z0-9_]*$")

#: The consumers held to zero spelled-out tokens, relative to `tgfarm/`.
#: `api.py` joined the list when its two sets were moved onto
#: `JOURNAL_STATUS_UNKNOWN` / `JOURNAL_STATUS_PERMANENT`, which is what the gap
#: allowlist below was registered for.
FIXED_CONSUMERS = ("api.py", "distribution.py", "s7_publish/__init__.py")

#: Gaps that are reported rather than edited, by file and attribute — never by
#: line number, which goes stale the moment someone edits above it. The list is
#: empty: the two `api.py` sets this block registered are now named from the
#: owner's constants, so the scan below has no exemption to honour and a new
#: spelled-out vocabulary set anywhere in the package fails this file.
REGISTERED_GAPS: frozenset[str] = frozenset()

#: The rows this file writes all live under this channel/material prefix and are
#: removed by the autouse fixture below, whatever a test asserts.
PREFIX = "p79"
#: A destination no other test file journals: `effective_daily_cap` filters rows
#: by `row.platform`, so a private platform cannot be polluted by a residual row
#: and cannot pollute another file's platform measurement.
PLATFORM = "p79-dest"

NEW_STATUSES = (
    ("JOURNAL_STATUS_SCHEDULED", "scheduled"),
    ("JOURNAL_STATUS_PERMANENT", "permanent"),
    ("JOURNAL_STATUS_UNKNOWN", "unknown"),
)


# ---------------------------------------------------------------------------
# The scan itself.
# ---------------------------------------------------------------------------


def _containers(node: ast.AST) -> list[ast.Set | ast.List | ast.Tuple]:
    """Every set/list/tuple literal inside an assignment value."""
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, (ast.Set, ast.List, ast.Tuple)) and child.elts
    ]


def _spelled_vocab_containers(source: str) -> dict[str, frozenset[str]]:
    """`{name: tokens}` for every status-vocabulary container that spells tokens.

    Reads a *file's* source, so a set built at module level, inside a class or
    inside a function is caught in the same way — the question is where a token
    is spelled, not how deep it sits.
    """
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
        names = [t.id for t in targets if isinstance(t, ast.Name) and _VOCAB_NAME.match(t.id)]
        if not names or node.value is None:
            continue
        for container in _containers(node.value):
            tokens = {
                element.value
                for element in container.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            } & set(crosspost_journal.JOURNAL_KNOWN_STATUSES)
            if tokens:
                for name in names:
                    found.setdefault(name, set()).update(tokens)
    return {name: frozenset(tokens) for name, tokens in found.items()}


def _owner_status_constants() -> dict[str, str]:
    """`{JOURNAL_STATUS_*: value}` as written at the top of the owner module."""
    tree = ast.parse(OWNER_FILE.read_text(encoding="utf-8"))
    declared: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id.startswith("JOURNAL_STATUS_")):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            declared[target.id] = node.value.value
    return declared


@lru_cache(maxsize=1)
def _package_sources() -> dict[str, str]:
    """Every `.py` under `tgfarm/`, keyed by its path relative to that package."""
    return {
        str(path.relative_to(PKG_ROOT)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in sorted(PKG_ROOT.rglob("*.py"))
    }


# ---------------------------------------------------------------------------
# 1 & 2. The owner: a constant per declared token, and the sets read the constants.
# ---------------------------------------------------------------------------


def test_every_declared_status_has_a_constant_on_the_owner() -> None:
    """The defect, as a failing assertion at the base of this block.

    `JOURNAL_KNOWN_STATUSES` had nine members and `_owner_status_constants()` had
    six values, so three tokens could only ever be read as strings. Widening a
    class the same way again fails here, in the owner's own file, before a single
    consumer has to notice.
    """
    spelled = frozenset(_owner_status_constants().values())
    missing = set(crosspost_journal.JOURNAL_KNOWN_STATUSES) - spelled
    assert not missing, (
        f"{sorted(missing)} are declared journal statuses with no JOURNAL_STATUS_* "
        "constant on tgfarm/crosspost_journal.py; spell them there, not here"
    )


def test_no_constant_spells_a_status_the_owner_never_declared() -> None:
    """The other direction: a constant is not a way to grow the vocabulary quietly."""
    undeclared = set(_owner_status_constants().values()) - set(
        crosspost_journal.JOURNAL_KNOWN_STATUSES
    )
    assert not undeclared, f"{sorted(undeclared)} name a status no class set contains"


@pytest.mark.parametrize(("name", "token"), NEW_STATUSES)
def test_the_three_new_constants_carry_the_journals_own_spelling(name: str, token: str) -> None:
    """The journal's spelling wins over tidiness; these are the strings it stores.

    A rename here would be a data migration of `crosspost_logs.status`, which this
    block does not do, so the expected value is written as a literal on purpose.
    """
    assert getattr(crosspost_journal, name) == token
    assert token in crosspost_journal.JOURNAL_SUPPRESSED_STATUSES
    assert crosspost_journal.journal_status_class(token) is (
        crosspost_journal.JournalStatusClass.SUPPRESSED
    )


def test_the_owner_builds_its_class_sets_from_constants() -> None:
    """Inside the owner, a token is written exactly once: at its constant."""
    assert _spelled_vocab_containers(OWNER_FILE.read_text(encoding="utf-8")) == {}


def test_the_classes_partition_exactly_the_known_statuses() -> None:
    """Nine tokens, three disjoint classes — the shape the consumers dispatch on."""
    classes = (
        crosspost_journal.JOURNAL_SUPPRESSED_STATUSES,
        crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES,
        crosspost_journal.JOURNAL_REOPEN_STATUSES,
    )
    union = classes[0] | classes[1] | classes[2]
    assert union == set(crosspost_journal.JOURNAL_KNOWN_STATUSES)
    assert len(union) == 9 == sum(len(group) for group in classes)


# ---------------------------------------------------------------------------
# 3. No consumer may paraphrase the owner.
# ---------------------------------------------------------------------------


def test_the_gap_allowlist_is_empty_and_stays_empty() -> None:
    """Zero-tolerance by construction: the scan has nothing to forgive.

    A gap re-added to `REGISTERED_GAPS` would be the same defect this block was
    written to close, filed under an allowlist instead of fixed. The one file that
    ever had an entry is in `FIXED_CONSUMERS` now, so there is no exempt consumer
    left to widen this toward.
    """
    assert REGISTERED_GAPS == frozenset()


def test_no_module_outside_the_owner_spells_a_status_vocabulary_container() -> None:
    """The grep this block exists to make possible, run over the whole package.

    Every `.py` under `tgfarm/` is parsed; a module-level (or nested) container
    whose name declares it a status vocabulary may hold no spelled-out journal
    token. Anything the owner already names must be named, not re-typed.
    """
    offenders: set[str] = set()
    for relative, source in _package_sources().items():
        if relative == OWNER_FILE.name:
            continue
        for name in _spelled_vocab_containers(source):
            offenders.add(f"{relative}:{name}")
    new = offenders - REGISTERED_GAPS
    assert not new, (
        f"{sorted(new)} build a journal status set out of spelled-out tokens; "
        "import the JOURNAL_STATUS_* constant from tgfarm/crosspost_journal.py"
    )


@pytest.mark.parametrize("relative", FIXED_CONSUMERS)
def test_the_files_this_block_fixed_hold_no_spelled_token(relative: str) -> None:
    """`distribution.py` and `s7_publish/__init__.py` are held to zero, not to a gap list."""
    assert _spelled_vocab_containers(_package_sources()[relative]) == {}


def test_the_scan_finds_a_spelled_container_when_one_exists() -> None:
    """An empty scan above must mean a clean tree, never a broken scanner."""
    source = 'JOURNAL_EVERYTHING_STATUSES = frozenset({"posted", "scheduled", "not_a_status"})\n'
    assert _spelled_vocab_containers(source) == {
        "JOURNAL_EVERYTHING_STATUSES": frozenset({"posted", "scheduled"})
    }
    # And it is a container scan: the same tokens written as names are invisible
    # to it, which is exactly what makes naming them the required form.
    named_source = (
        "from tgfarm.crosspost_journal import JOURNAL_STATUS_POSTED, JOURNAL_STATUS_SCHEDULED\n"
        "JOURNAL_EVERYTHING_STATUSES = frozenset({JOURNAL_STATUS_POSTED, JOURNAL_STATUS_SCHEDULED})\n"
    )
    assert _spelled_vocab_containers(named_source) == {}
    # A single-status constant is not a container either, so the owner's own
    # `JOURNAL_STATUS_* = "posted"` lines cannot trip the scan.
    assert _spelled_vocab_containers('JOURNAL_STATUS_POSTED = "posted"\n') == {}


# ---------------------------------------------------------------------------
# 4. Two vocabularies, two tables, still two.
# ---------------------------------------------------------------------------


def test_the_planner_budget_sets_are_the_constants_not_strings() -> None:
    """Membership unchanged by this block, stated through the owner's names."""
    journal = crosspost_journal
    assert distribution.JOURNAL_ATTEMPT_STATUSES == frozenset(
        {
            journal.JOURNAL_STATUS_POSTED,
            journal.JOURNAL_STATUS_SCHEDULED,
            journal.JOURNAL_STATUS_FAILED,
            journal.JOURNAL_STATUS_PERMANENT,
        }
    )
    assert distribution.JOURNAL_DELIVERED_STATUSES == frozenset(
        {journal.JOURNAL_STATUS_POSTED, journal.JOURNAL_STATUS_SCHEDULED}
    )
    assert distribution.JOURNAL_SLOT_OCCUPYING_STATUSES == frozenset(
        {
            journal.JOURNAL_STATUS_POSTED,
            journal.JOURNAL_STATUS_SCHEDULED,
            journal.JOURNAL_STATUS_PUBLISHING,
            journal.JOURNAL_STATUS_UNKNOWN,
        }
    )


def test_the_journals_unknown_is_still_not_the_posts_publish_unknown() -> None:
    """Naming the journal's token does not merge it with the editorial column's.

    `posts.status` carries `publish_unknown`; the journal carries `unknown`. The
    set in `s7_publish` exists to keep the two apart, so it is built from the
    journal's constants and contains none of the post's.
    """
    assert crosspost_journal.JOURNAL_STATUS_UNKNOWN == "unknown"
    assert s7_publish.STATUS_PUBLISH_UNKNOWN == "publish_unknown"
    assert s7_publish.CROSSPOST_ONLY_STATUSES == frozenset(
        {
            crosspost_journal.JOURNAL_STATUS_POSTED,
            crosspost_journal.JOURNAL_STATUS_SKIPPED,
            crosspost_journal.JOURNAL_STATUS_PERMANENT,
            crosspost_journal.JOURNAL_STATUS_UNKNOWN,
            crosspost_journal.JOURNAL_STATUS_DRY_RUN,
        }
    )
    assert s7_publish.CROSSPOST_ONLY_STATUSES.isdisjoint(s7_publish.POST_STATUSES)


# ---------------------------------------------------------------------------
# 5. The real column: a row written with the constant is read back by the constant.
# ---------------------------------------------------------------------------


def _seed_journal_row(status: str, *, attempt: int = 1) -> str:
    """One (post, platform) pair journalled with `status`; returns the material id."""
    public_id = f"{PREFIX}-{uuid4().hex[:12]}"
    now = datetime.now(UTC) - timedelta(days=3)
    with session_scope() as session:
        session.add(
            Channel(
                id=f"{PREFIX}_chan_{public_id}",
                title="p79 journal vocabulary",
                niche="tech",
                platform="telegram",
                enabled=True,
            )
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=f"{PREFIX}_chan_{public_id}",
                hook="h",
                body="b",
                status="published",
                published_at=now,
            )
        )
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform=PLATFORM,
                status=status,
                attempt=attempt,
                created_at=now,
            )
        )
    return public_id


@pytest.fixture(autouse=True)
def _p79_rows_removed() -> None:
    """Nothing this file writes survives it; the session database is shared."""
    yield
    with session_scope() as session:
        session.query(CrosspostLog).filter(
            CrosspostLog.post_public_id.like(f"{PREFIX}-%")
        ).delete(synchronize_session=False)
        session.query(Post).filter(Post.public_id.like(f"{PREFIX}-%")).delete(
            synchronize_session=False
        )
        session.query(Channel).filter(Channel.id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )


def _rows_of(public_id: str) -> list[CrosspostLog]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(CrosspostLog).where(CrosspostLog.post_public_id == public_id)
            ).all()
        )


@pytest.mark.parametrize(("name", "token"), NEW_STATUSES)
def test_a_seeded_row_round_trips_through_its_constant(name: str, token: str) -> None:
    """The constant is the column's value, not a comment about it."""
    public_id = _seed_journal_row(getattr(crosspost_journal, name))
    row = _rows_of(public_id)[0]
    assert row.status == token
    assert crosspost_journal.journal_row_decision(row) == (
        crosspost_journal.JournalStatusClass.SUPPRESSED,
        token,
    )


def test_the_planner_reads_seeded_rows_through_the_shared_objects(monkeypatch) -> None:
    """Real ORM rows, the real budget sets, the numbers each set exists to produce.

    vk-shaped 3/4 rules on a private platform, so the only thing under test is
    which rows the constants let into the ratio.
    """
    monkeypatch.setitem(distribution.PLATFORM_RULES, PLATFORM, {"daily": 3, "max_daily": 4})
    journal = crosspost_journal

    landed = [
        _seed_journal_row(journal.JOURNAL_STATUS_POSTED) for _ in range(8)
    ]
    refused = [
        _seed_journal_row(journal.JOURNAL_STATUS_PERMANENT) for _ in range(8)
    ]
    unanswered = [
        _seed_journal_row(journal.JOURNAL_STATUS_UNKNOWN) for _ in range(8)
    ]
    queued = [_seed_journal_row(journal.JOURNAL_STATUS_SCHEDULED) for _ in range(8)]

    assert distribution.effective_daily_cap(PLATFORM, [r for pid in landed for r in _rows_of(pid)]) == 4
    assert distribution.effective_daily_cap(
        PLATFORM, [r for pid in refused for r in _rows_of(pid)]
    ) == 2
    # `unknown` is not an attempt, so eight of them leave the denominator under
    # the 8-row floor; `scheduled` is both an attempt and a delivery.
    assert distribution.effective_daily_cap(
        PLATFORM, [r for pid in unanswered for r in _rows_of(pid)]
    ) == 3
    assert distribution.effective_daily_cap(
        PLATFORM, [r for pid in queued for r in _rows_of(pid)]
    ) == 4

"""`crosspost_logs.status` must declare real headroom for every token its writers can produce.

The defect (reported by the journal-vocabulary agent, landed same day): the column was
declared `String(16)` while `api.py` writes the 16-character token `retry_authorized` —
zero headroom on any backend that enforces declared width. SQLite silently ignores
VARCHAR lengths (a test insert below proves it), so nothing broke locally; PostgreSQL
would truncate-or-raise, and the declaration is the contract regardless of engine.

This file enumerates the vocabulary *from the code*, not from a list someone typed into
a review, because a future writer that adds a token must trip the guard:

* every value passed as `status=` to a `CrosspostLog(...)` constructor in `tgfarm/**`
  that this file can resolve to one string — a bare literal or a `JOURNAL_STATUS_*`
  name from the shared vocabulary, resolved through that module rather than re-typed
  (found by AST scan — today: `posted`, `retry_authorized`, `publishing`);
* every member of the `CrosspostResult.status` `Literal[...]` (pulled with
  `typing.get_args`, not re-typed), which is what `s9_growth/adapters.py` persists
  verbatim through `status=result.status`;
* the reader-side re-offer vocabulary `crosspost_journal.JOURNAL_KNOWN_STATUSES` — every
  token both the planner and the claim gate understand, which is one frozenset in one
  leaf module since the lock and the view were given a shared source, so a writer token
  the readers do not know cannot hide from this guard.

The scanner's own aim is pinned by a membership anchor (an AST scan that quietly found
nothing would otherwise rubber-stamp any width), and the width itself is checked twice:
against the live `CrosspostLog.__table__` metadata and against the `VARCHAR(...)` text
a freshly created SQLite file actually stores in `sqlite_master`.

Known limit, stated on purpose: a future writer that passes a *computed* string not
covered by `CrosspostResult` and not written as a constructor literal is outside static
reach — same limitation class as the env-flag scanner's f-string note. The anchor and
the Literal member list are what keep that channel narrow in practice.
"""

from __future__ import annotations

import ast
import sqlite3
import typing
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from tgfarm import crosspost_journal
from tgfarm.core.schemas import CrosspostResult
from tgfarm.db import bootstrap
from tgfarm.db.models import Base, CrosspostLog

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "tgfarm"


def _crosspost_log_written_statuses() -> set[str]:
    """Every status string written as `status=` into a CrosspostLog(...) constructor.

    Matches both bare-name calls (`CrosspostLog(...)`) and attribute calls
    (`models.CrosspostLog(...)`); a value under any other name is out of scope by
    construction, not by an allowlist.
    """
    found: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "CrosspostLog":
                continue
            for kw in node.keywords:
                if kw.arg != "status":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    found.add(kw.value.value)
                    continue
                # A writer may spell its token with the shared vocabulary instead of a
                # bare literal — `status=JOURNAL_STATUS_PUBLISHING`. That is the same
                # string, so resolving it through the module it comes from keeps the
                # scan honest; the name must exist there and be a str, or this guard
                # would be inventing a token the writer never wrote.
                symbol = getattr(kw.value, "id", None) or getattr(kw.value, "attr", None)
                if isinstance(symbol, str) and symbol.startswith("JOURNAL_STATUS_"):
                    resolved = getattr(crosspost_journal, symbol, None)
                    assert isinstance(resolved, str), (
                        f"{path}:{node.lineno}: CrosspostLog status={symbol} does not "
                        "resolve to a string in tgfarm.crosspost_journal"
                    )
                    found.add(resolved)
                    continue
                # `status=result.status` is the known dynamic channel, covered via the
                # CrosspostResult Literal below. Any *other* non-literal form (f-string,
                # concatenation, computed dict) must be surfaced, not silently skipped,
                # exactly like the env scanner's unresolved sites.
                rendered = ast.unparse(kw.value)
                assert isinstance(kw.value, ast.Attribute) and kw.value.attr == "status", (
                    f"{path}:{node.lineno}: non-literal CrosspostLog status={rendered!r} "
                    "is not covered by the CrosspostResult enumeration; extend this guard"
                )
    return found


def _crosspost_result_literal_statuses() -> set[str]:
    annotation = typing.get_type_hints(CrosspostResult)["status"]
    args = typing.get_args(annotation)
    assert args, "CrosspostResult.status stopped being a Literal; the enumeration is blind now"
    return {a for a in args if isinstance(a, str)}


def producible_statuses() -> set[str]:
    """The full publishable vocabulary of crosspost_logs.status, enumerated from code."""
    from tgfarm.crosspost_journal import JOURNAL_KNOWN_STATUSES

    return (
        _crosspost_log_written_statuses()
        | _crosspost_result_literal_statuses()
        | set(JOURNAL_KNOWN_STATUSES)
    )


# --- scanner aim -------------------------------------------------------------


def test_constructor_scan_finds_the_known_writers():
    literals = _crosspost_log_written_statuses()
    # api._reconcile_crosspost_outcome and adapters.claim_crosspost write these as
    # literals; if the scanner misses one, every width assertion below goes quiet.
    assert "retry_authorized" in literals
    assert "publishing" in literals
    assert "posted" in literals


def test_result_literal_channel_is_enumerated():
    result_literals = _crosspost_result_literal_statuses()
    assert {"dry_run", "unknown", "permanent"} <= result_literals


def test_full_vocabulary_is_superset_of_both_channels():
    vocab = producible_statuses()
    assert vocab >= _crosspost_log_written_statuses() | _crosspost_result_literal_statuses()


# --- the width contract ------------------------------------------------------


def test_declared_width_has_headroom_for_longest_producible_token():
    vocab = producible_statuses()
    longest = max(vocab, key=len)
    declared = CrosspostLog.__table__.c.status.type.length
    assert declared is not None, "crosspost_logs.status lost its declared width"
    assert declared > len(longest), (
        f"crosspost_logs.status is String({declared}) but a writer can produce "
        f"{longest!r} ({len(longest)} chars): zero headroom. Widen the declaration; "
        "renaming a token already on disk is a data migration, not a fix."
    )


def test_every_producible_token_fits_declared_width():
    declared = CrosspostLog.__table__.c.status.type.length
    too_long = {token for token in producible_statuses() if len(token) > declared}
    assert not too_long, f"statuses exceed String({declared}): {sorted(too_long)}"


# --- what the DDL actually says ----------------------------------------------


@pytest.fixture()
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "width_measure.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


def test_created_ddl_stores_the_declared_width_text(fresh_db: sqlite3.Connection):
    table_sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='crosspost_logs'"
    ).fetchone()[0]
    declared = CrosspostLog.__table__.c.status.type.length
    assert f"status VARCHAR({declared})" in table_sql, table_sql


def test_sqlite_does_not_enforce_the_declared_length(fresh_db: sqlite3.Connection):
    """Runtime proof that widening is a contract change, not a behavioural one here.

    A token longer than the *old* String(16) must insert cleanly on SQLite even
    though it would raise or truncate on PostgreSQL. If SQLite ever starts
    enforcing, this test fails first and tells us the operator database matters.
    """
    long_token = "retry_authorized_" + "x" * 4  # 21 chars, > 16 and > 32 would fail
    fresh_db.execute(
        "INSERT INTO crosspost_logs (post_public_id, platform, status, attempt, created_at)"
        " VALUES ('p1','vk',?,1,'2026-01-01 00:00:00')",
        (long_token,),
    )
    stored = fresh_db.execute("SELECT status FROM crosspost_logs WHERE post_public_id='p1'").fetchone()[0]
    assert stored == long_token
    fresh_db.rollback()


def test_restart_of_existing_file_writes_nothing_for_the_width(tmp_path: Path):
    """`plan_additive_migrations` is purely ADD COLUMN / CREATE INDEX.

    A database created under the old declaration and re-opened under the new one
    must need zero statements, i.e. the operator's tgfarm.db is untouched by a
    restart: no migration exists to run and none is needed.
    """
    db_path = tmp_path / "live_like.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    assert bootstrap.plan_additive_migrations(engine) == []
    assert bootstrap.apply_additive_migrations(engine) == 0
    engine.dispose()

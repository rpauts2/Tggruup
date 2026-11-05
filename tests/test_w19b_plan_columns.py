"""W19-B: the two columns that carry an editorial-plan decision onto a post row.

The plan already knows three things the post row could not store before this
wave: which entry ordered a post, and which slot that entry booked. Without a
column there is no handoff — `tgfarm/plan_execution.py` said so itself at base
("the shift this module is allowed to make *without touching the schema*"), and
`tgfarm/editorial_plan.py` that a `slot_at` "never reached `Post.scheduled_at`".

These tests pin the schema half only: both columns are additive and nullable,
they reach a database provisioned before they existed through the migration
entry the repo already uses (`apply_additive_migrations`, the same comparison
`tools/preview_schema_drift.py` reads), and a second boot writes nothing.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from tgfarm.db.bootstrap import apply_additive_migrations, plan_additive_migrations
from tgfarm.db.session import get_engine

#: The posts table as it looked at `main` before W19-B, narrowed to the columns a
#: stripped-down farm actually had. Written out rather than derived from the
#: models so the test cannot pass by describing the schema it is checking.
_OLD_POSTS_TABLE = """
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id VARCHAR(32) NOT NULL,
    channel_id VARCHAR(64) NOT NULL,
    cluster_public_id VARCHAR(32),
    status VARCHAR(16) NOT NULL,
    scheduled_at DATETIME,
    published_at DATETIME,
    attempts INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

#: `apply_additive_migrations` backfills model defaults on the tables a real farm
#: database always has, so the fixture database has to have them too — a boot
#: onto a partial schema is not what a restart does.
_OLD_SOURCES_TABLE = """
CREATE TABLE sources (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    url TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    min_interval_s INTEGER NOT NULL,
    created_at DATETIME NOT NULL
)
"""

NEW_COLUMNS = ("plan_entry_id", "plan_slot_at")


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_the_plan_decision_has_a_column_on_the_post_row():
    """The live schema of this run carries both, not just the model."""
    assert set(NEW_COLUMNS) <= _columns(get_engine(), "posts")


def test_both_columns_are_declared_null_safe():
    """A column a restart adds to a populated table must accept a null.

    `apply_additive_migrations` only ever handles additive, nullable-safe
    changes; a NOT NULL here would make the boot fail instead of migrate.
    """
    columns = {column["name"]: column for column in inspect(get_engine()).get_columns("posts")}
    for name in NEW_COLUMNS:
        assert columns[name]["nullable"] is True, f"{name} would break an existing database"
        assert columns[name]["default"] is None, f"{name} would backfill a value nobody booked"


def test_a_database_from_before_the_columns_gets_them_on_boot(tmp_path):
    """The drift preview and the boot agree, and the boot survives a restart.

    This is the "schema change must survive restart" proof for a production
    database provisioned before W19-B: the statements the next boot *would* run
    are the statements it does run, and after running them the preview is empty.
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'before.db').as_posix()}")
    with engine.begin() as connection:
        connection.execute(text(_OLD_POSTS_TABLE))
        connection.execute(text(_OLD_SOURCES_TABLE))
    assert not set(NEW_COLUMNS) & _columns(engine, "posts")

    preview = plan_additive_migrations(engine)
    for name in NEW_COLUMNS:
        assert any(f'ALTER TABLE "posts" ADD COLUMN "{name}"' in statement for statement in preview), (
            f"{name} is not in the drift preview: {preview}"
        )
    # Indexes are the other half of the drift, and the provenance link is read by
    # entry, so its index is part of what the boot must add.
    assert any("ix_posts_plan_entry_id" in statement for statement in preview), preview

    added = apply_additive_migrations(engine)
    assert added >= len(NEW_COLUMNS)
    assert set(NEW_COLUMNS) <= _columns(engine, "posts")

    # Second boot: nothing left to write, which is what makes the restart safe to
    # repeat rather than a migration that has to be tracked by hand.
    assert apply_additive_migrations(engine) == 0
    assert plan_additive_migrations(engine) == []

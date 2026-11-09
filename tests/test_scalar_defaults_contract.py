"""The scalar-default reader must see `server_default`, not only the Python default.

A column added through `ALTER TABLE` never sees a server default — SQLite puts
NULL in every existing row — so the boot's backfill is the only thing that can
give those rows the value the model promises. In this schema `channels.platform`
is the one column with a `server_default`, and it also carries a Python
`default=`, so the `server_default` branch is only reachable through a table
like the one defined here: a column whose fixed value the DDL knows but the ORM
does not.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from tgfarm.db.bootstrap import _scalar_defaults, apply_additive_migrations
from tgfarm.db.models import Base

#: A table that exists in no module of the product: it is registered into the
#: real `Base.metadata` for the duration of one test and removed afterwards, so
#: the boot under test sees a `server_default`-only column it can otherwise
#: never meet.
_SERVER_ONLY = Table(
    "server_default_only",
    MetaData(),  # detached until the test attaches it
    Column("id", Integer, primary_key=True),
    Column("flag", String(16), server_default="on"),
    Column("py_default", String(16), default="off"),
)


def test_server_default_reaches_scalar_defaults():
    """A column whose only fixed value is the DDL default must still be seeded."""
    defaults = _scalar_defaults(_SERVER_ONLY)
    assert defaults.get("flag") == "on"


def test_python_default_reaches_scalar_defaults():
    defaults = _scalar_defaults(_SERVER_ONLY)
    assert defaults.get("py_default") == "off"


def test_a_callable_default_is_not_seeded():
    """`utcnow`-style defaults must stay out: seeding them would invent history.

    A row written last year seeded with today's timestamp is a fabricated fact,
    which is worse than the NULL the analytics layer already knows to skip.
    """
    from sqlalchemy.sql import func

    table = Table(
        "callable_default_only",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("made_up_at", String(32), server_default=func.now()),
    )
    assert "made_up_at" not in _scalar_defaults(table)


def test_a_server_default_only_column_is_backfilled_end_to_end():
    """The branch is not decoration: an added column with only a DDL default
    reaches existing rows through the boot."""
    metadata_table = _SERVER_ONLY
    Base.metadata._add_table(metadata_table.name, metadata_table.schema, metadata_table)
    try:
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE server_default_only (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO server_default_only (id) VALUES (1)"))

        assert "flag" not in {c["name"] for c in inspect(engine).get_columns("server_default_only")}
        apply_additive_migrations(engine)

        with engine.begin() as conn:
            flag = conn.execute(
                text("SELECT flag FROM server_default_only WHERE id = 1")
            ).scalar()
        assert flag == "on"
    finally:
        Base.metadata._remove_table(metadata_table.name, metadata_table.schema)

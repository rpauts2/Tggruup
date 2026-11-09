"""The additive boot must survive a partial schema and seed only what it added.

Two defects this file pins down.

1. `apply_additive_migrations` used to run a fixed list of `UPDATE`s for
   `posts`/`sources`/`post_stats`/`channels` whenever *any* column was added. A
   database provisioned before the `sources` table existed — an externally
   imported corpus, a test fixture, a partially migrated deployment — then died
   with `sqlite3.OperationalError: no such table: sources` during boot, which is
   the one moment the farm is not allowed to crash.

2. The seed list was handwritten, so it drifted from the models: `last_yield`
   and `idle_streak` were backfilled, the other nine `sources` defaults were
   not, and a column added to a model gained an unseeded NULL silently.
   The seed is now derived from the same model defaults the drift comparison
   reads, and it is restricted to the columns the boot actually added — which
   also means a routine boot with no drift writes no rows at all.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from tgfarm.db.bootstrap import apply_additive_migrations, plan_additive_migrations


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _engine_with(tmp_path, ddl: list[str]):
    engine = create_engine(f"sqlite:///{(tmp_path / 'partial.db').as_posix()}")
    with engine.begin() as connection:
        for statement in ddl:
            connection.execute(text(statement))
    return engine


#: `posts` narrowed to the columns a stripped-down deployment actually had. The
#: nullable columns the model gives a default are absent on purpose.
_OLD_POSTS = """
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id VARCHAR(32) NOT NULL,
    channel_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL
)
"""

_OLD_RAW_ITEMS = """
CREATE TABLE raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    fetched_at DATETIME NOT NULL
)
"""


def test_a_database_without_the_sources_table_boots(tmp_path):
    """The regression: `posts` + `raw_items`, no `sources`, no `channels`.

    Before the fix this raised `no such table: sources`, because the seed block
    ran unconditionally once a single column had been added.
    """
    engine = _engine_with(tmp_path, [_OLD_POSTS, _OLD_RAW_ITEMS])
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO posts (public_id, channel_id, status, created_at) "
                 "VALUES ('p-1', 'ch-1', 'draft', '2026-01-01 00:00:00')")
        )

    assert "post_format" not in _columns(engine, "posts")
    assert apply_additive_migrations(engine) > 0
    assert {"post_format", "generation_provider"} <= _columns(engine, "posts")


def test_a_seeded_default_reaches_the_rows_written_before_the_column(tmp_path):
    """A column added by `ALTER TABLE` is NULL for every existing row.

    The model promises a value, so the boot seeds it; a null `post_format`
    reaching the renderer is the failure this guards.
    """
    engine = _engine_with(tmp_path, [_OLD_POSTS])
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO posts (public_id, channel_id, status, created_at) "
                 "VALUES ('p-1', 'ch-1', 'draft', '2026-01-01 00:00:00')")
        )

    apply_additive_migrations(engine)

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT post_format, generation_provider, attempts, char_len FROM posts "
                 "WHERE public_id = 'p-1'")
        ).one()
    assert row == ("news", "offline", 0, 0)


def test_the_boot_never_overwrites_a_column_it_did_not_add(tmp_path):
    """The seed is a backfill of the columns the boot added, not of the table.

    `post_format` here already exists and already carries editorial intent; the
    boot adds `generation_provider` next to it. Seeding the whole table would
    relabel published content, so the write is restricted to the added columns.
    """
    engine = _engine_with(
        tmp_path,
        [_OLD_POSTS.replace("    status VARCHAR(16) NOT NULL,",
                            "    status VARCHAR(16) NOT NULL,\n"
                            "    post_format VARCHAR(16) NOT NULL,")],
    )
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO posts (public_id, channel_id, status, post_format, created_at) "
                 "VALUES ('p-1', 'ch-1', 'draft', 'explainer', '2026-01-01 00:00:00')")
        )

    assert "generation_provider" not in _columns(engine, "posts")
    apply_additive_migrations(engine)

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT post_format, generation_provider FROM posts WHERE public_id = 'p-1'")
        ).one()
    assert row == ("explainer", "offline")


def test_a_column_the_model_leaves_null_stays_null(tmp_path):
    """`generation_provider` is nullable *on purpose* in the read path.

    `tests/test_imported_history_distribution.py` requires a NULL provider to
    stay NULL, so a boot that seeds every null it finds would break a live
    contract. Only columns the boot itself added are seeded.
    """
    engine = _engine_with(
        tmp_path,
        [_OLD_POSTS.replace("    status VARCHAR(16) NOT NULL,",
                            "    status VARCHAR(16) NOT NULL,\n"
                            "    generation_provider VARCHAR(32),")],
    )
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO posts (public_id, channel_id, status, generation_provider, created_at) "
                 "VALUES ('p-1', 'ch-1', 'draft', NULL, '2026-01-01 00:00:00')")
        )

    apply_additive_migrations(engine)

    with engine.begin() as connection:
        provider = connection.execute(
            text("SELECT generation_provider FROM posts WHERE public_id = 'p-1'")
        ).scalar()
    assert provider is None, "the boot seeded a column it did not add"


def test_a_platform_seeded_before_the_column_existed_reads_as_telegram(tmp_path):
    """`channels.platform` is the one column with a `server_default`.

    `ALTER TABLE ... ADD COLUMN` never applies a server default, so a row seeded
    before the column existed would read as NULL and be mistaken for a
    non-Telegram surface. The value comes from the model, not from a literal in
    this module.
    """
    engine = _engine_with(
        tmp_path,
        ["""
        CREATE TABLE channels (
            id VARCHAR(64) PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            created_at DATETIME NOT NULL
        )
        """],
    )
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO channels (id, title, created_at) "
                 "VALUES ('c-1', 'old channel', '2026-01-01 00:00:00')")
        )

    apply_additive_migrations(engine)

    with engine.begin() as connection:
        platform = connection.execute(
            text("SELECT platform FROM channels WHERE id = 'c-1'")
        ).scalar()
    assert platform == "telegram"


def test_a_boot_with_no_drift_writes_no_rows(tmp_path):
    """Idempotence, measured by row identity rather than by the return value.

    `updated_at` is the column the age analytics read; if a routine restart
    touched rows that needed nothing, every post would look freshly edited.
    """
    engine = _engine_with(tmp_path, [_OLD_POSTS])
    apply_additive_migrations(engine)
    assert plan_additive_migrations(engine) == []

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO posts (public_id, channel_id, status, created_at) "
                 "VALUES ('p-1', 'ch-1', 'draft', '2026-01-01 00:00:00')")
        )
        before = connection.execute(
            text("SELECT post_format, generation_provider, updated_at FROM posts")
        ).all()

    assert apply_additive_migrations(engine) == 0

    with engine.begin() as connection:
        after = connection.execute(
            text("SELECT post_format, generation_provider, updated_at FROM posts")
        ).all()
    assert after == before

"""Database bootstrap: create schema, apply additive migrations, seed reference data."""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateIndex

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Base
from tgfarm.db.session import get_engine, session_scope

log = get_logger(__name__)


def _scalar_defaults(table) -> dict[str, object]:
    """`{column: value}` for every column whose model default is a fixed value.

    A Python `default=` is a fixed value; a callable (``utcnow``) is not, because
    seeding a row written last year with *today's* timestamp would invent history.
    A `server_default` that is a plain string literal counts as well — it is the
    value the column would have been created with, so it is what rows added
    through `ALTER TABLE` (which never sees the server default) must be seeded
    with. `CURRENT_TIMESTAMP`-style clause defaults are excluded for the same
    reason `utcnow` is.
    """
    out: dict[str, object] = {}
    for column in table.columns:
        if column.default is not None:
            if column.default.is_scalar:
                out[column.name] = column.default.arg
            continue
        server = column.server_default
        if server is not None and isinstance(server.arg, str):
            out[column.name] = server.arg
    return out


def init_db(drop: bool = False) -> None:
    engine = get_engine()
    if drop:
        log.warning("drop_all on %s", engine.url)
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    added = apply_additive_migrations(engine)
    log.info(
        "schema ready: %d tables%s",
        len(Base.metadata.tables),
        f", {added} column(s) added" if added else "",
    )


def plan_additive_migrations(engine: Engine) -> list[str]:
    """The statements the next boot would run, computed without running them.

    An operator deciding whether to restart needs to know what the restart
    *writes*. Deriving the answer from the same comparison `apply_additive_migrations`
    uses is the point: a preview with its own logic can disagree with the boot.
    """
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    columns: list[str] = []
    indexes: list[str] = []

    for table_name, table in Base.metadata.tables.items():
        if table_name not in live_tables:
            continue
        live_columns = {col["name"] for col in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in live_columns:
                continue
            type_sql = column.type.compile(dialect=engine.dialect)
            columns.append(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {type_sql}')

    for table_name, table in Base.metadata.tables.items():
        if table_name not in live_tables:
            continue
        try:
            existing_indexes = {ix["name"] for ix in inspector.get_indexes(table_name)}
        except Exception:  # noqa: BLE001 - a read-only inspector is not fatal
            continue
        for index in table.indexes:
            if index.name in existing_indexes:
                continue
            indexes.append(str(CreateIndex(index).compile(dialect=engine.dialect)))

    return columns + indexes


def apply_additive_migrations(engine: Engine) -> int:
    """Add columns and seed defaults for rows written before the column existed.

    `create_all` only ever creates *tables*, so a model that gains a column is
    invisible to an already-provisioned database: every read of that column then
    fails with "no such column". Comparing metadata against the live schema and
    issuing `ALTER TABLE ... ADD COLUMN` closes that gap.

    Only additive, nullable-safe changes are handled. Anything destructive
    (drops, renames, type changes, non-null constraints on populated tables)
    belongs in a real migration tool, not here.

    Indexes are the other half of the drift: one added to the models never
    reaches a database provisioned before it existed, and the slowdown is
    invisible until the tables are large.
    """
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    live_columns: dict[str, set[str]] = {}
    for tbl in live_tables:
        live_columns[tbl] = {c["name"] for c in inspector.get_columns(tbl)}

    # Capture which columns the models expect but the database lacks, before
    # any DDL runs.  This lets us backfill only the tables that exist (the
    # database may have posts/raw_items but not sources, and the boot must not
    # crash).
    pending: dict[str, dict[str, object]] = {}
    for table_name, table in Base.metadata.tables.items():
        if table_name not in live_tables:
            continue
        defaults = _scalar_defaults(table)
        missing = {
            col: defaults[col] for col in defaults if col not in live_columns.get(table_name, frozenset())
        }
        if missing:
            pending[table_name] = missing

    # Run all DDL (columns + indexes).
    statements = plan_additive_migrations(engine)
    added = 0
    for statement in statements:
        with engine.begin() as conn:
            conn.execute(text(statement))
        if statement.startswith("ALTER TABLE"):
            log.info("migrated %s", statement)
            added += 1
        else:
            log.info("migrated index: %s", statement)
    created_indexes = len(statements) - added
    if created_indexes:
        log.info("migrated %d missing index(es)", created_indexes)

    # Seed model-level defaults for rows written before the column existed,
    # so a null format does not reach the renderer. `pending` is the single
    # owner of "which tables exist and which columns this boot added": the loop
    # below must not re-derive either, or a guard here can rot unnoticed.
    for table_name, values in pending.items():
        assignments = ", ".join(f'"{col}" = :{col}' for col in values)
        condition = " OR ".join(f'"{col}" IS NULL' for col in values)
        sql = f'UPDATE "{table_name}" SET {assignments} WHERE {condition}'
        with engine.begin() as conn:
            conn.execute(text(sql), dict(values))
        log.info("backfilled %s: %s", table_name, ", ".join(values))

    return added


def ensure_bootstrapped() -> None:
    """Create the schema and load config-derived rows. Idempotent — every entry
    point (CLI, API, scheduler, tests) calls this first."""
    from tgfarm.config import get_config

    init_db()
    config = get_config()
    seed_channels([p.as_dict() for p in config.personas])
    seed_sources([s.as_dict() for s in config.sources])


def seed_channels(channels: list[dict]) -> int:
    """Upsert channel rows from persona definitions. Idempotent."""
    from tgfarm.db.models import Channel

    written = 0
    with session_scope() as session:
        for spec in channels:
            existing = session.get(Channel, spec["id"])
            if existing:
                existing.persona = spec
                existing.platform = str(spec.get("platform") or "telegram").lower()
                existing.title = spec.get("title", "")
                existing.niche = spec.get("niche", "")
                existing.timezone = spec.get("timezone", "Europe/Moscow")
                existing.posts_per_day = int(spec.get("posts_per_day", 3))
                existing.enabled = bool(spec.get("enabled", True))
            else:
                session.add(
                    Channel(
                        id=spec["id"],
                        platform=str(spec.get("platform") or "telegram").lower(),
                        title=spec.get("title", ""),
                        niche=spec.get("niche", ""),
                        persona=spec,
                        timezone=spec.get("timezone", "Europe/Moscow"),
                        posts_per_day=int(spec.get("posts_per_day", 3)),
                        enabled=bool(spec.get("enabled", True)),
                    )
                )
            written += 1
    log.info("channels seeded: %d", written)
    return written


def seed_sources(sources: list[dict], *, prune: bool = True) -> int:
    """Upsert sources from config and disable rows that left the registry.

    Without the prune, a registry that lost 200 duplicate entries keeps fetching
    them forever: the rows stay enabled in the database and only config changes
    are supposed to decide what the farm collects.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Source

    written = 0
    with session_scope() as session:
        for spec in sources:
            existing = session.get(Source, spec["id"])
            if existing:
                existing.name = spec.get("name", "")
                existing.kind = spec.get("kind", "rss")
                existing.url = spec.get("url", "")
                existing.niches = spec.get("niches", [])
                existing.weight = float(spec.get("weight", 0.5))
                existing.enabled = bool(spec.get("enabled", True))
                existing.min_interval_s = int(spec.get("min_interval_s", 600))
            else:
                session.add(
                    Source(
                        id=spec["id"],
                        name=spec.get("name", ""),
                        kind=spec.get("kind", "rss"),
                        url=spec.get("url", ""),
                        niches=spec.get("niches", []),
                        weight=float(spec.get("weight", 0.5)),
                        enabled=bool(spec.get("enabled", True)),
                        min_interval_s=int(spec.get("min_interval_s", 600)),
                    )
                )
            written += 1

        if prune:
            known = {spec["id"] for spec in sources}
            stale = session.execute(
                select(Source).where(Source.id.notin_(known), Source.enabled.is_(True))
            ).scalars().all()
            for row in stale:
                row.enabled = False
            if stale:
                log.info("sources disabled (no longer in config): %d", len(stale))

    log.info("sources seeded: %d", written)
    return written
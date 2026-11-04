"""Database bootstrap: create schema, apply additive migrations, seed reference data."""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateIndex

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Base
from tgfarm.db.session import get_engine, session_scope

log = get_logger(__name__)


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
    """Add columns that exist in the models but not yet in the database.

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
    statements = plan_additive_migrations(engine)
    live_tables = set(inspect(engine).get_table_names())
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

    if added:
        # Seed model-level defaults for rows written before the column existed,
        # so a null format does not reach the renderer.
        with engine.begin() as conn:
            conn.execute(text("UPDATE posts SET post_format = 'news' WHERE post_format IS NULL"))
            conn.execute(
                text("UPDATE posts SET generation_provider = 'offline' WHERE generation_provider IS NULL")
            )
            conn.execute(text("UPDATE sources SET last_yield = 0 WHERE last_yield IS NULL"))
            conn.execute(text("UPDATE sources SET idle_streak = 0 WHERE idle_streak IS NULL"))
            if "post_stats" in live_tables:
                for column in ("impressions", "opens"):
                    conn.execute(
                        text(f"UPDATE post_stats SET {column} = 0 WHERE {column} IS NULL")
                    )
    if "channels" in live_tables:
        # `platform` is newer than most existing databases; rows seeded before
        # it existed must not be mistaken for a non-Telegram surface.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE channels SET platform = 'telegram' WHERE platform IS NULL")
            )
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
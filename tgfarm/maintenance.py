"""Maintenance: cluster TTL, row retention, WAL checkpoint.

The farm accumulates faster than it reads: forming clusters nobody consumed,
raw items with their embedding blobs, run journals. Left alone, the database
grows into tens of gigabytes and every full scan gets slower. This module is
wired into the cycle as the `maintenance` stage and deletes exactly two things:

* clusters that never became ready within the TTL window;
* raw items / run rows older than the retention window.

Posts, media and crosspost logs are never touched here: they are the history
the operator and the analytics depend on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text, update

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Cluster, CrosspostLog, PipelineRun, RawItem
from tgfarm.db.session import get_engine, session_scope

log = get_logger(__name__)


def expire_stale_clusters(hours: int | None = None) -> int:
    """Clusters that never got hot enough age out of the forming queue.

    Without a TTL the `forming` pool grows forever: every cycle rescans it, and
    a cluster that sat cold for three days will never be news again.
    """
    settings = get_settings()
    window = int(hours if hours is not None else settings.stale_cluster_hours)
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, window))
    with session_scope() as session:
        result = session.execute(
            update(Cluster)
            .where(
                Cluster.status == "forming",
                Cluster.last_seen_at.isnot(None),
                Cluster.last_seen_at < cutoff,
            )
            .values(status="stale")
        )
        return result.rowcount or 0


def prune_raw_items(days: int | None = None) -> int:
    """Drop raw items older than the retention window.

    Each row carries a 512-dim embedding blob (~2 KB), which is most of the
    database volume. Children pointing at deleted duplicates lose the pointer
    first, or the self-referencing foreign key blocks the delete.
    """
    settings = get_settings()
    window = int(days if days is not None else settings.retention_raw_days)
    cutoff = datetime.now(UTC) - timedelta(days=max(1, window))
    with session_scope() as session:
        session.execute(
            text(
                "UPDATE raw_items SET duplicate_of = NULL WHERE duplicate_of IN "
                "(SELECT id FROM raw_items WHERE fetched_at < :cutoff)"
            ),
            {"cutoff": cutoff},
        )
        result = session.execute(delete(RawItem).where(RawItem.fetched_at < cutoff))
        return result.rowcount or 0


def prune_pipeline_runs(days: int | None = None) -> int:
    settings = get_settings()
    window = int(days if days is not None else settings.retention_runs_days)
    cutoff = datetime.now(UTC) - timedelta(days=max(1, window))
    with session_scope() as session:
        result = session.execute(delete(PipelineRun).where(PipelineRun.created_at < cutoff))
        return result.rowcount or 0


def prune_crosspost_logs(days: int | None = None) -> int:
    """Old crosspost attempts are history; mapped Dzen metrics live elsewhere."""
    cutoff = datetime.now(UTC) - timedelta(days=180)
    with session_scope() as session:
        result = session.execute(
            delete(CrosspostLog).where(
                CrosspostLog.created_at < cutoff,
                CrosspostLog.status.in_(("failed", "skipped")),
            )
        )
        return result.rowcount or 0


def checkpoint() -> bool:
    """Checkpoint the SQLite WAL so the -wal file does not grow unbounded."""
    engine = get_engine()
    if not str(engine.url).startswith("sqlite"):
        return False
    with engine.connect() as connection:
        connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    return True


def run_maintenance() -> dict[str, int | bool]:
    """One maintenance pass; every step fails separately, not the stage."""
    out: dict[str, int | bool] = {}
    for name, fn in (
        ("stale_clusters", expire_stale_clusters),
        ("raw_items_pruned", prune_raw_items),
        ("runs_pruned", prune_pipeline_runs),
        ("crosspost_logs_pruned", prune_crosspost_logs),
    ):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - hygiene must not kill the cycle
            log.warning("maintenance: %s failed: %s", name, exc)
            out[name] = -1
    try:
        out["checkpoint"] = checkpoint()
    except Exception as exc:  # noqa: BLE001
        log.debug("maintenance: checkpoint failed: %s", exc)
        out["checkpoint"] = False
    log.info("maintenance: %s", out)
    return out


__all__ = [
    "checkpoint",
    "expire_stale_clusters",
    "prune_crosspost_logs",
    "prune_pipeline_runs",
    "prune_raw_items",
    "run_maintenance",
]

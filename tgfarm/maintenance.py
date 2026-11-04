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

from sqlalchemy import delete, select, text, update

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


def checkpoint(mode: str = "PASSIVE") -> bool:
    """Checkpoint the SQLite WAL so the -wal file does not grow unbounded.

    PASSIVE never blocks writers — it checkpoints what it can and returns.
    TRUNCATE (which compacts the -wal file to zero) waits for every reader and
    writer to finish and used to hold the database against the publisher for
    seconds; it now belongs only to the weekly backup job.
    """
    engine = get_engine()
    if not str(engine.url).startswith("sqlite"):
        return False
    with engine.connect() as connection:
        connection.execute(text(f"PRAGMA wal_checkpoint({mode})"))
    return True


def expire_unpublishable_drafts(hours: int | None = None) -> int:
    """Reject drafts that can never publish under the current policy.

    Today that is the offline-composed Dzen draft: the publisher holds template
    text back (``DZEN_REQUIRES_LLM``), so one sitting in the backlog only rots
    there and hides the real outage behind a wall of «готово к публикации».
    Only formal criteria are used and nothing is published — the post can be
    regenerated once a model answers again.
    """
    from tgfarm.db.models import Channel, Post

    settings = get_settings()
    if not getattr(settings, "dzen_requires_llm", False):
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, hours or 24))
    reason = (
        "formal: offline draft for Dzen while DZEN_REQUIRES_LLM is on; "
        "regenerate when an online model is available"
    )
    with session_scope() as session:
        dzen_channels = session.execute(
            select(Channel.id).where(Channel.platform == "dzen")
        ).scalars().all()
        if not dzen_channels:
            return 0
        result = session.execute(
            update(Post)
            .where(
                Post.status.in_(("gated", "draft")),
                Post.channel_id.in_(dzen_channels),
                Post.generation_provider == "offline",
                Post.created_at <= cutoff,
            )
            .values(status="rejected", reject_reason=reason)
        )
        return result.rowcount or 0


def _reprint_backfill() -> int:
    """Link historical reprints to the document they copy, in one bounded slice.

    Ingest only recognises a copy against rows it has already stored, so the
    corpus written before that rule still reads as independent documents to
    trends, niches, heat and `item_count`. The pass is time-boxed and reports its
    own remainder; a maintenance step must never become the reason a cycle dies.
    """
    from tgfarm.s1_sources.backfill_reprints import backfill_reprints

    report = backfill_reprints(budget_seconds=60.0)
    log.info(
        "maintenance: reprint backfill scanned %d, linked %d, %d left (%s)",
        report.rows_scanned,
        report.links_written,
        report.candidates_remaining,
        report.stopped_by,
    )
    return report.links_written


def _close_stale_ai_reservations() -> int:
    """Close AI budget reservations whose process died mid-call.

    They stay charged on purpose: the provider may have been called, so refunding
    would let the farm exceed its daily cap. Closing only stops the panel from
    flagging the same dead reservation forever.
    """
    from tgfarm.llm.policy import close_stale_reservations

    return close_stale_reservations(30)


def run_maintenance() -> dict[str, int | bool]:
    """One maintenance pass; every step fails separately, not the stage."""
    out: dict[str, int | bool] = {}
    for name, fn in (
        ("stale_clusters", expire_stale_clusters),
        ("raw_items_pruned", prune_raw_items),
        ("runs_pruned", prune_pipeline_runs),
        ("crosspost_logs_pruned", prune_crosspost_logs),
        ("unpublishable_drafts", expire_unpublishable_drafts),
        ("reprint_backfill", _reprint_backfill),
        ("stale_ai_reservations", _close_stale_ai_reservations),
    ):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - hygiene must not kill the cycle
            log.warning("maintenance: %s failed: %s", name, exc)
            out[name] = -1
    try:
        out["checkpoint"] = checkpoint("PASSIVE")
    except Exception as exc:  # noqa: BLE001
        log.debug("maintenance: checkpoint failed: %s", exc)
        out["checkpoint"] = False
    log.info("maintenance: %s", out)
    return out


__all__ = [
    "checkpoint",
    "expire_stale_clusters",
    "expire_unpublishable_drafts",
    "prune_crosspost_logs",
    "prune_pipeline_runs",
    "prune_raw_items",
    "run_maintenance",
]

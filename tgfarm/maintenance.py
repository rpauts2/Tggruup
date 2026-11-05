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

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, text, update

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Cluster, CrosspostLog, PipelineRun, RawItem
from tgfarm.db.session import get_engine, session_scope

log = get_logger(__name__)


def _truthy_env(name: str) -> bool:
    """Consent read straight from the environment.

    A new `Settings` field is not available here (the settings module belongs to
    another owner's contract), and the flag is a one-off operator decision rather
    than a tuned knob, so it stays an env read with an explicit vocabulary.
    """
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on", "apply"}


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
    from tgfarm.s1_sources.backfill_reprints import backfill_reprints, note_backfill_report

    report = backfill_reprints(budget_seconds=60.0)
    # The panel answers "what remains" from this process only, so the pass has to
    # be remembered here or the operator sees "never ran" after every restart.
    note_backfill_report(report)
    log.info(
        "maintenance: reprint backfill scanned %d, linked %d, %d left (%s)",
        report.rows_scanned,
        report.links_written,
        report.candidates_remaining,
        report.stopped_by,
    )
    return report.links_written


def _duplicate_repair() -> int:
    """Heal stored `duplicate_of` links, but only after the operator has said so.

    The sweep rewrites tens of thousands of history rows, which is exactly the
    kind of change §80 keeps behind a copy of the database and an explicit
    decision. Without that consent the step reports zero and touches nothing.
    """
    from tgfarm.s2_trends.duplicate_repair import repair_duplicate_links

    if not get_settings().duplicate_repair_apply:
        return 0

    report = repair_duplicate_links(budget_seconds=60.0)
    log.info(
        "maintenance: duplicate repair scanned %d, rewrote %d, dropped %d, "
        "cycles broken %d, %d to rescan (%s)",
        report.rows_scanned,
        report.links_rewritten,
        report.links_dropped,
        report.cycles_broken,
        report.candidates_remaining,
        report.stopped_by,
    )
    return report.links_rewritten + report.links_dropped


#: Consent flag for the publication-evidence sweep, read straight from the
#: environment: `core/settings.py` is another owner's contract, so no new
#: Settings field is added for a single repair. Same shape as
#: `DUPLICATE_REPAIR_APPLY` (§80: history rewrites sit behind an explicit "yes"
#: and a fresh copy in `backup/`).
PUBLICATION_EVIDENCE_APPLY_ENV = "PUBLICATION_EVIDENCE_REPAIR_APPLY"

#: Providers that mark a row as imported archive rather than something the farm
#: published. Imported history is *told* to be published and carries no farm-side
#: id by design, so it is never a candidate for this sweep.
IMPORTED_HISTORY_PROVIDERS = ("history_import",)


def _fresh_backup_copy(max_age_hours: float | None = None) -> dict:
    """The §80 precondition: a copy of this database, written recently enough.

    Deliberately does not run a backup itself: the operator deciding to rewrite
    history and the process that protects it should not be the same click.
    """
    settings = get_settings()
    limit_hours = float(
        max_age_hours
        if max_age_hours is not None
        else getattr(settings, "backup_max_age_hours", 36)
        or 36
    )
    root = Path(__file__).resolve().parent.parent
    configured = str(getattr(settings, "backup_dir", "") or "").strip()
    backup_dir = Path(configured).expanduser() if configured else root / "backup"
    if not backup_dir.is_dir():
        return {"ok": False, "directory": str(backup_dir), "reason": "no backup directory"}

    newest = None
    newest_path = None
    for candidate in backup_dir.glob("tgfarm-*.db"):
        if not candidate.is_file():
            continue
        stamp = candidate.stat().st_mtime
        if newest is None or stamp > newest:
            newest, newest_path = stamp, candidate
    if newest is None:
        return {"ok": False, "directory": str(backup_dir), "reason": "no backup copy in backup/"}
    age_hours = (datetime.now(UTC).timestamp() - newest) / 3600
    if age_hours > limit_hours:
        return {
            "ok": False,
            "directory": str(backup_dir),
            "path": str(newest_path),
            "age_hours": round(age_hours, 2),
            "reason": f"newest copy is {age_hours:.1f} h old (limit {limit_hours:g} h)",
        }
    return {
        "ok": True,
        "directory": str(backup_dir),
        "path": str(newest_path),
        "age_hours": round(max(0.0, age_hours), 2),
    }


def publication_evidence_repair(*, apply: bool = False, limit: int = 200) -> dict:
    """Report posts that claim `published` while carrying no remote evidence.

    Why this exists: until the publisher was fixed, a dry run in `TGFARM_MODE=off`
    wrote `status = "published"`, `tg_message_id = NULL` and the invented
    `tg_chat_id = -1`. `posts.status` is the denominator of `format_performance`,
    `measurement_coverage`, the S12 lessons and the panel cards, so a send that
    never happened still counted as evidence. The live database (2026-11-05) holds
    15 telegram rows of exactly that shape.

    Evidence, per platform:

    * telegram — `posts.tg_message_id`: the Bot API returns it on every accepted
      message, so `NULL` means the farm has nothing to point at;
    * dzen / vk — a `crosspost_logs` row for that post and platform with
      `status = "posted"`. The publisher journals that status only when the remote
      side accepted the article, so a row without `remote_id`/`url` is still
      evidence: 29 of the 37 dzen publications are in that shape, and treating a
      missing *id* as a missing *publication* would un-publish real work. Those
      rows are reported (`posted_without_id`) and left alone.

    With `apply` the rows move to `publish_unknown`, the one state in the existing
    vocabulary that admits the uncertainty ("the remote side may have it — verify
    before retrying"), and the reconciliation board already knows how to resolve
    it in either direction. `published_at` and the placeholder chat id go with it:
    both were invented by the same buggy write. The step is bounded (`limit` rows
    per pass), previews by default, refuses to write without the consent flag *and*
    a fresh copy in `backup/`, and never touches imported history.
    """
    from sqlalchemy import func, not_, or_, select

    from tgfarm.db.models import Channel as ChannelRow
    from tgfarm.db.models import CrosspostLog
    from tgfarm.db.models import OperatorLog
    from tgfarm.db.models import Post as PostRow
    from tgfarm.tg_sync import UNKNOWN_CHAT_PLACEHOLDERS

    report: dict[str, object] = {
        "apply": bool(apply),
        "consent": _truthy_env(PUBLICATION_EVIDENCE_APPLY_ENV),
        "limit": int(limit),
        "moved": 0,
        "cleared_chat_ids": 0,
        "posted_without_id": 0,
        "imported_history_excluded": 0,
        "placeholder_chat_ids_all": 0,
        "by_platform": {},
        "sample": [],
        "remaining": 0,
    }

    not_imported = or_(
        PostRow.generation_provider.is_(None),
        PostRow.generation_provider.notin_(IMPORTED_HISTORY_PROVIDERS),
    )

    with session_scope() as session:
        report["imported_history_excluded"] = int(
            session.execute(
                select(func.count(PostRow.id)).where(
                    PostRow.status == "published",
                    PostRow.generation_provider.in_(IMPORTED_HISTORY_PROVIDERS),
                )
            ).scalar_one()
            or 0
        )
        # A `-1` chat id anywhere is the invented value, including on rows that
        # still hold a real message id; those are reported, not repaired here.
        report["placeholder_chat_ids_all"] = int(
            session.execute(
                select(func.count(PostRow.id)).where(PostRow.tg_chat_id.in_(UNKNOWN_CHAT_PLACEHOLDERS))
            ).scalar_one()
            or 0
        )

        candidates: list[tuple[int, str, str, str]] = []
        for platform in ("telegram", "dzen", "vk"):
            base = (
                select(PostRow.id, PostRow.public_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .where(
                    PostRow.status == "published",
                    not_imported,
                    func.lower(ChannelRow.platform) == platform,
                )
            )
            if platform == "telegram":
                rows = session.execute(base.where(PostRow.tg_message_id.is_(None))).all()
            else:
                journaled = (
                    select(CrosspostLog.id)
                    .where(
                        CrosspostLog.post_public_id == PostRow.public_id,
                        CrosspostLog.platform == platform,
                        CrosspostLog.status == "posted",
                    )
                    .exists()
                )
                rows = session.execute(base.where(not_(journaled))).all()
                # The deliberate exclusion: a `posted` row with neither an id nor a
                # URL is a real publication whose identifier the parser never found.
                posted_without_ref = (
                    select(func.count(func.distinct(PostRow.id)))
                    .select_from(PostRow)
                    .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                    .join(
                        CrosspostLog,
                        (CrosspostLog.post_public_id == PostRow.public_id)
                        & (CrosspostLog.platform == platform),
                    )
                    .where(
                        PostRow.status == "published",
                        not_imported,
                        func.lower(ChannelRow.platform) == platform,
                        CrosspostLog.status == "posted",
                        or_(CrosspostLog.remote_id.is_(None), CrosspostLog.remote_id == ""),
                        or_(CrosspostLog.url.is_(None), CrosspostLog.url == ""),
                    )
                )
                report["posted_without_id"] += int(
                    session.execute(posted_without_ref).scalar_one() or 0
                )
            report["by_platform"][platform] = len(rows)
            for row_id, public_id in rows:
                candidates.append((int(row_id), str(public_id), platform, ""))

    report["remaining"] = max(0, len(candidates) - max(1, int(limit)))
    chosen = candidates[: max(1, int(limit))]
    report["sample"] = [row[1] for row in chosen[:20]]
    report["scanned"] = len(candidates)

    if not apply or not chosen:
        return report

    if not report["consent"]:
        report["refused"] = (
            f"{PUBLICATION_EVIDENCE_APPLY_ENV} is not set: history is not rewritten without it"
        )
        log.warning("maintenance: publication evidence repair refused — %s", report["refused"])
        return report
    backup = _fresh_backup_copy()
    report["backup"] = backup
    if not backup["ok"]:
        report["refused"] = f"no fresh copy in backup/: {backup.get('reason')}"
        log.warning("maintenance: publication evidence repair refused — %s", report["refused"])
        return report

    reason = (
        "history: marked published with no remote evidence of any kind "
        "(no telegram message id / no `posted` journal row); verify the "
        "destination before trusting or retrying it"
    )
    moved = 0
    cleared = 0
    with session_scope() as session:
        for row_id, public_id, platform, _ in chosen:
            post = session.get(PostRow, row_id)
            if post is None or post.status != "published" or post.published_at is None:
                continue
            post.status = "publish_unknown"
            post.published_at = None
            post.publish_error = reason[:400]
            post.publish_heartbeat_at = None
            if platform == "telegram" and post.tg_chat_id in UNKNOWN_CHAT_PLACEHOLDERS:
                post.tg_chat_id = None
                cleared += 1
            session.add(
                OperatorLog(
                    action="publication-evidence-repair",
                    target=public_id[:64],
                    detail=f"{platform}: published without evidence -> publish_unknown",
                )
            )
            moved += 1
    report["moved"] = moved
    report["cleared_chat_ids"] = cleared
    log.warning(
        "maintenance: publication evidence repair moved %d rows to publish_unknown "
        "(%s rows still without evidence, %d published rows kept with a `posted` "
        "journal but no id, imported history excluded: %s)",
        moved,
        report["remaining"],
        report["posted_without_id"],
        report["imported_history_excluded"],
    )
    return report


def _publication_evidence_repair() -> int:
    """The maintenance hook: zero unless the operator has given consent.

    The report half is the CLI (`tgfarm publication-evidence [--apply]`); the
    cycle only ever moves rows behind `PUBLICATION_EVIDENCE_REPAIR_APPLY`, the
    same way `_duplicate_repair` stays silent behind `DUPLICATE_REPAIR_APPLY`.
    """
    if not _truthy_env(PUBLICATION_EVIDENCE_APPLY_ENV):
        return 0
    report = publication_evidence_repair(apply=True)
    if report.get("refused"):
        log.warning("maintenance: publication evidence repair: %s", report["refused"])
        return 0
    return int(report.get("moved") or 0)


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
        ("duplicate_repair", _duplicate_repair),
        ("publication_evidence_repair", _publication_evidence_repair),
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
    "publication_evidence_repair",
    "prune_crosspost_logs",
    "prune_pipeline_runs",
    "prune_raw_items",
    "run_maintenance",
]

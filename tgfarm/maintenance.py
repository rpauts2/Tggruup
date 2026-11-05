"""Maintenance: cluster TTL, row retention, WAL checkpoint.

The farm accumulates faster than it reads: forming clusters nobody consumed,
raw items with their embedding blobs, run journals. Left alone, the database
grows into tens of gigabytes and every full scan gets slower. This module is
wired into the cycle as the `maintenance` stage and deletes exactly two things:

* clusters that never became ready within the TTL window;
* raw items / run rows older than the retention window.

Posts, media and crosspost logs are never deleted here: they are the history
the operator and the analytics depend on. The one exception is the pair of
consent-gated repairs at the bottom of this file
(`publication_evidence_repair`, `requeue_failed_publishes`), which *rewrite*
publication states without deleting anything — and only behind an `*_APPLY`
environment flag, an explicit `--apply` and a fresh copy in `backup/`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text, update

from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
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


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; ages must not depend on that quirk."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


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


# ------------------------------------------------------------------ requeue path
#: §80 consent for the bulk requeue of terminal publish failures. Same shape as
#: `PUBLICATION_EVIDENCE_APPLY_ENV`: read straight from the environment (not a
#: `Settings` field — `core/settings.py` is another owner's contract, and this is
#: an operator decision rather than a tuned knob) and documented in `.env.example`
#: with the default off. Without it the sweep previews and writes nothing.
PUBLISH_FAILURE_REQUEUE_APPLY_ENV = "PUBLISH_FAILURE_REQUEUE_APPLY"

#: Rows one apply pass may move, whatever the caller asked for. The live queue
#: carried 365 terminal failures after the 2026-10-29 Dzen DNS outage; an
#: operator who means "start working through these" is one click away from
#: handing the publisher 365 six-day-old drafts at once, so a pass is capped.
REQUEUE_BUDGET_MAX = 200

#: Named refusals, so a "no" always says which rule said it. `requeue_eligibility`
#: is the only place that produces them; both operator paths ask the same question.
REQUEUE_REFUSALS: dict[str, str] = {
    "not_failed": "the row is not a terminal failure; only `failed` posts are requeued",
    "publish_outcome_unknown": (
        "§80: an unknown publication outcome is never requeued — the platform may "
        "already hold the post, so a repeat is a blind duplicate. Resolve it on the "
        "reconciliation board instead."
    ),
    "already_published": "the row carries a Telegram message id or a published_at stamp",
    "journaled_posted": "the journal already records this post as accepted by that platform",
    "outcome_uncertain": (
        "the stored error is a driver death, which does not prove the material never "
        "reached the platform; park it as publish_unknown instead"
    ),
    "platform_disabled": "the destination platform is switched off, so the row would "
    "sit in the queue and never be picked up",
}


def requeue_eligibility(
    *,
    status: str | None,
    publish_error: object = None,
    tg_message_id: object = None,
    published_at: object = None,
    journaled_posted: bool = False,
    platform: str = "",
    enabled: bool = True,
) -> tuple[bool, str]:
    """May this row go back into the queue? One answer for both operator paths.

    The panel's single-post `POST /api/posts/{id}/retry` and the bulk sweep below
    ask this one question, so a guard written for one of them cannot be missing
    from the other — which is exactly how a second, looser requeue path would be
    born.

    `status` is checked first and `publish_unknown` gets its own refusal code:
    §80 forbids requeueing it, and a rule that shares the generic `not_failed`
    label with every other state cannot be audited later. The remaining guards all
    ask the same thing from a different angle — *is there any evidence the
    platform received this?* — because `failed` is what our own classifier decided,
    not what the remote side proved.
    """
    name = str(status or "")
    if name == "publish_unknown":
        return False, "publish_outcome_unknown"
    if name != "failed":
        return False, "not_failed"
    if tg_message_id is not None or published_at is not None:
        return False, "already_published"
    if journaled_posted:
        return False, "journaled_posted"
    from tgfarm.s7_publish import _uncertain_driver_error

    if _uncertain_driver_error(str(publish_error or "")):
        return False, "outcome_uncertain"
    if not enabled:
        return False, "platform_disabled"
    return True, ""


def requeue_failed_publishes(
    *,
    apply: bool = False,
    limit: int = 50,
    platform: str | None = None,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move terminal publish failures back into the queue — bounded, consented, journalled.

    Why this exists: `tgfarm review` and the new `/api/backlog` card show the
    operator that 365 `failed` posts carry 1890 retries between them, and the
    rows written by `requeue_unsubmitted` literally end with "Requeue manually if
    it is still relevant" — but the only manual path was one click per post on a
    board that lists 25 of them per bucket. Showing the backlog without a way to
    work through it leaves the operator with SQL.

    The write follows the `duplicate_of` and `publication_evidence_repair`
    vocabulary rather than inventing one: it previews by default, an apply pass is
    bounded (`REQUEUE_BUDGET_MAX`), it refuses to write without
    `PUBLISH_FAILURE_REQUEUE_APPLY` *and* a fresh copy in `backup/`, the operator
    has to type the reason (the rows are stale by construction — that is what
    `expired:` means — so "still relevant" is a human judgement and belongs in the
    journal), and every moved row gets its own `OperatorLog` entry carrying the
    redacted error it failed with.

    What it will not do: touch `publish_unknown` (§80 — a repeat there is a blind
    duplicate), requeue a row that carries any evidence of publication, requeue a
    row whose stored error is a driver death, or requeue onto a platform the
    operator has switched off. Those refusals are counted and named in the report,
    never silently skipped.

    Nothing here publishes anything: it sets `status = "scheduled"`, which is the
    state the publisher already reads, with the same fields the single-post retry
    resets. Whether the farm then sends is the publisher's existing policy.
    """
    from sqlalchemy import func

    from tgfarm.db.models import Channel as ChannelRow
    from tgfarm.db.models import OperatorLog
    from tgfarm.db.models import Post as PostRow
    from tgfarm.platforms import is_platform_enabled

    current = _as_utc(now) or datetime.now(UTC)
    budget = max(1, min(int(limit), REQUEUE_BUDGET_MAX))
    target_platform = str(platform or "").strip().lower() or None
    operator_reason = str(reason or "").strip()

    report: dict[str, Any] = {
        "apply": bool(apply),
        "consent": _truthy_env(PUBLISH_FAILURE_REQUEUE_APPLY_ENV),
        "limit": budget,
        "requested_limit": int(limit),
        "platform": target_platform,
        "reason": operator_reason,
        "scanned": 0,
        "eligible": 0,
        "parked_unknown": 0,
        "parked_unknown_reason": REQUEUE_REFUSALS["publish_outcome_unknown"],
        "by_platform": {},
        "refusals": {},
        "refused_rows": [],
        "refused": None,
        "sample": [],
        "changed": 0,
        "remaining": 0,
    }

    with session_scope() as session:
        stmt = (
            select(PostRow, ChannelRow.platform)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id, isouter=True)
            .where(PostRow.status == "failed")
            # Freshest material first: a pass budget spent on the oldest rows
            # requeues the content least likely to still be news, and the
            # operator has to be able to stop after one pass and look at what
            # came back. The reconciliation board sorts its `terminal` bucket the
            # same way, so the two views agree about what a "next hundred" means.
            .order_by(PostRow.created_at.desc(), PostRow.id.desc())
        )
        # Counted, never selected: the operator reading "365 failed, 51 unknown"
        # needs this button to say out loud that the second number is not its
        # business. A silent skip is how a §80 rule gets forgotten.
        parked_stmt = select(func.count()).select_from(PostRow).join(
            ChannelRow, ChannelRow.id == PostRow.channel_id, isouter=True
        ).where(PostRow.status == "publish_unknown")
        if target_platform:
            stmt = stmt.where(func.lower(ChannelRow.platform) == target_platform)
            parked_stmt = parked_stmt.where(func.lower(ChannelRow.platform) == target_platform)
        scanned: list[dict[str, Any]] = []
        for post, row_platform in session.execute(stmt).all():
            # The publisher's own fallback, not the display one: `due_posts` treats
            # a post whose channel row is gone as telegram, so a requeue that
            # called it `unknown` and refused it would disagree with what happens
            # to the same row one tick later. `/api/backlog` may report it as
            # `unknown`, because that card only has to describe it.
            name = str(row_platform or "").lower() or "telegram"
            age = _as_utc(post.created_at)
            scanned.append(
                {
                    "id": int(post.id),
                    "public_id": str(post.public_id),
                    "platform": name,
                    "status": str(post.status or ""),
                    "error": post.publish_error,
                    "tg_message_id": post.tg_message_id,
                    "published_at": post.published_at,
                    "attempts": int(post.attempts or 0),
                    # Content age, not "since it last failed": `requeue_unsubmitted`
                    # closed these rows out for being older than `max_age_hours` of
                    # *material*, so the number the operator needs in order to judge
                    # relevance is the one that decision used.
                    "age_hours": (
                        round((current - age).total_seconds() / 3600, 1) if age else None
                    ),
                }
            )
        report["parked_unknown"] = int(session.execute(parked_stmt).scalar_one() or 0)

    report["scanned"] = len(scanned)
    if not scanned:
        return report

    public_ids = [row["public_id"] for row in scanned]
    journal: set[tuple[str, str]] = set()
    with session_scope() as session:
        accepted = (
            session.execute(
                select(CrosspostLog.post_public_id, func.lower(CrosspostLog.platform))
                .where(
                    CrosspostLog.status == "posted",
                    CrosspostLog.post_public_id.in_(public_ids),
                )
                .distinct()
            )
            .all()
        )
        journal = {(str(row[0]), str(row[1] or "")) for row in accepted}

    enabled: dict[str, bool] = {}
    for row in scanned:
        name = str(row["platform"])
        if name not in enabled:
            enabled[name] = bool(is_platform_enabled(name))

    chosen: list[dict[str, Any]] = []
    for row in scanned:
        name = str(row["platform"])
        ok, refusal = requeue_eligibility(
            status=row["status"],
            publish_error=row["error"],
            tg_message_id=row["tg_message_id"],
            published_at=row["published_at"],
            journaled_posted=(str(row["public_id"]), name) in journal,
            platform=name,
            enabled=enabled.get(name, False),
        )
        if not ok:
            report["refusals"][refusal] = int(report["refusals"].get(refusal, 0)) + 1
            if len(report["refused_rows"]) < 20:
                report["refused_rows"].append(
                    {
                        "public_id": row["public_id"],
                        "platform": name,
                        "reason": refusal,
                        "detail": REQUEUE_REFUSALS.get(refusal, refusal),
                    }
                )
            continue
        report["by_platform"][name] = int(report["by_platform"].get(name, 0)) + 1
        chosen.append(row)

    report["eligible"] = len(chosen)
    report["remaining"] = max(0, len(chosen) - budget)
    moving = chosen[:budget]
    report["sample"] = [
        {
            "public_id": row["public_id"],
            "platform": row["platform"],
            "attempts": row["attempts"],
            "age_hours": row["age_hours"],
            "error": redact_sensitive_text(row["error"], limit=120),
        }
        for row in moving[:20]
    ]

    if not apply:
        return report
    if not report["consent"]:
        report["refused"] = (
            f"{PUBLISH_FAILURE_REQUEUE_APPLY_ENV} is not set: "
            "terminal failures are not requeued without it"
        )
        log.warning("maintenance: failed requeue refused — %s", report["refused"])
        return report
    if not operator_reason:
        report["refused"] = (
            "no reason given: these rows were closed out as stale, so putting them "
            "back in the queue is a claim that the content is still relevant, and "
            "the journal has to say who claimed it and why"
        )
        log.warning("maintenance: failed requeue refused — %s", report["refused"])
        return report
    backup = _fresh_backup_copy()
    report["backup"] = backup
    if not backup["ok"]:
        report["refused"] = f"no fresh copy in backup/: {backup.get('reason')}"
        log.warning("maintenance: failed requeue refused — %s", report["refused"])
        return report
    if not moving:
        return report

    changed = 0
    with session_scope() as session:
        for row in moving:
            name = str(row["platform"])
            fresh = session.get(PostRow, int(row["id"]))
            if fresh is None:
                continue
            ok, refusal = requeue_eligibility(
                status=fresh.status,
                publish_error=fresh.publish_error,
                tg_message_id=fresh.tg_message_id,
                published_at=fresh.published_at,
                journaled_posted=(str(fresh.public_id), name) in journal,
                platform=name,
                enabled=enabled.get(name, False),
            )
            if not ok:
                # The row moved under us between the scan and the write — most
                # likely a publisher tick picked it up. Counting the change here
                # would credit this pass with a requeue it did not perform.
                report["refusals"][refusal] = int(report["refusals"].get(refusal, 0)) + 1
                continue
            previous = redact_sensitive_text(fresh.publish_error, limit=240)
            reset_from = int(fresh.attempts or 0)
            fresh.status = "scheduled"
            fresh.scheduled_at = current
            fresh.attempts = 0
            fresh.publish_error = None
            fresh.publish_heartbeat_at = None
            session.add(
                OperatorLog(
                    action="requeue-failed",
                    target=str(fresh.public_id)[:64],
                    detail=(
                        f"{name}: failed -> scheduled; budget reset from "
                        f"{reset_from} attempts; reason: {operator_reason}"
                        f" | last error: {previous}"
                    )[:500],
                )
            )
            changed += 1

    report["changed"] = changed
    log.warning(
        "maintenance: requeued %d terminal publish failures into the queue "
        "(%d still eligible beyond this pass, refusals: %s, reason: %s)",
        changed,
        report["remaining"],
        report["refusals"],
        operator_reason,
    )
    return report


def _close_stale_ai_reservations() -> int:
    """Close AI budget reservations whose process died mid-call.

    They stay charged on purpose: the provider may have been called, so refunding
    would let the farm exceed its daily cap. Closing only stops the panel from
    flagging the same dead reservation forever.
    """
    from tgfarm.llm.policy import close_stale_reservations

    return close_stale_reservations(30)


def run_maintenance() -> dict[str, int | bool]:
    """One maintenance pass; every step fails separately, not the stage.

    `requeue_failed_publishes` is deliberately NOT in this list. The two
    consent-gated repairs above can run from the cycle because they only ever move
    a row into a state that is more honest than the one it sat in; a requeue is the
    opposite — it restarts a publication the farm already gave up on, and it does
    so for rows the system closed out precisely because the content had gone stale.
    That judgement is an operator action (`tgfarm requeue-failed`, the panel, the
    API), not something a scheduled stage should be able to do because an
    environment variable happened to be set.
    """
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
    "requeue_eligibility",
    "requeue_failed_publishes",
    "run_maintenance",
]

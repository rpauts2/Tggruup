"""W24-M: can collected statistics actually be tied back to publications?

Two measured facts motivate this module (live DB, read-only, 2026-11-0x):
``post_stats`` stopped growing while publishing continued, and every Telegram
publish row has ``tg_message_id IS NULL`` — so whatever is collected cannot be
joined back to whatever was published. This module makes that gap *computable*
instead of papering over it.

The collector reality this must match (do not invent a schema):

* Telegram — ``tgfarm.tg_sync.collect_post_stats`` only pairs
  ``posts.status == "published"`` rows that have ``tg_message_id`` with a
  ``channels.tg_chat_id``, and writes ``post_stats`` keyed by ``posts.id``
  (``post_id``). A published post without ``tg_message_id`` is therefore
  *unknowable*: no measurement could ever be attributed to it.
* Crosspost platforms — ``crosspost_logs`` with ``status == "posted"`` carry
  the external id in ``remote_id``; that is the key the Dzen collector
  (``dzen_web.record_publication_metrics``) matches on, and Reddit/Telegraph
  snapshot tables reference ``crosspost_log_id`` directly.

Honesty rule (the pattern the project already applies elsewhere): a missing
measurement is reported as ``None``/absent, never as ``0``. Coverage is
``None`` when the denominator is empty — "we have no publications here" is not
the same claim as "none of them were measured".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tgfarm.db.models import (
    CrosspostLog,
    Post,
    PostStat,
    RedditMetricSnapshot,
    TelegraphMetricSnapshot,
)
from tgfarm.db.session import session_scope

TELEGRAM = "telegram"

# Crosspost platforms whose collectors we know how to join back to a
# crosspost_logs row. Anything else can only ever report with_stats == 0.
_CROSSPOST_MEASUREMENTS: dict[str, str] = {
    "dzen": "post_stats_via_post",
    "reddit": "crosspost_log_id",
    "telegraph": "crosspost_log_id",
}


@dataclass(frozen=True)
class PublicationRef:
    """One publication as the attribution problem sees it.

    ``key`` is the join target a collector would have to hit (``"tg:<post_id>"``
    or ``"cp:<log_id>"``); ``has_external_id`` says whether the external
    identifier that collector needs exists at all.
    """

    platform: str
    key: str
    has_external_id: bool


@dataclass(frozen=True)
class Measurement:
    """One collected stat row, addressed by the publication key it matches."""

    key: str
    collected_at: datetime | None


def summarize_attribution(
    refs: Iterable[PublicationRef],
    measurements: Iterable[Measurement],
    *,
    platforms: Iterable[str] = (),
) -> dict:
    """Pure, offline aggregation of attribution coverage per platform.

    Returns ``{"platforms": {...}, "orphan_measurements": N}``. Per platform:

    * ``publications`` — rows that were published on it,
    * ``with_external_id`` — the subset a collector could address at all,
    * ``unknowable`` — publications without that id (never folded into 0),
    * ``with_stats`` — publications a measurement actually matched,
    * ``coverage`` — ``with_stats / publications`` or ``None`` when there are
      no publications (empty denominator is unknown, not zero),
    * ``share_unknowable`` — same None rule,
    * ``last_measurement_at`` — ISO timestamp or ``None`` when nothing was
      ever collected for the platform.

    ``platforms`` forces extra buckets into the output even when they have no
    publications; such a bucket carries ``coverage = None``, which is exactly
    the honesty rule: "we have nothing here" is not "0 % measured".
    Measurements whose key matches no publication are counted once, globally,
    as ``orphan_measurements`` — they inflate nobody's coverage.
    """
    refs = list(refs)
    by_platform: dict[str, list[PublicationRef]] = defaultdict(list)
    key_to_platform: dict[str, str] = {}
    for ref in refs:
        by_platform[ref.platform].append(ref)
        key_to_platform[ref.key] = ref.platform
    for extra in platforms:
        by_platform.setdefault(extra, [])

    measured_keys: dict[str, datetime | None] = {}
    orphans = 0
    for m in measurements:
        if m.key in key_to_platform:
            prior = measured_keys.get(m.key)
            if m.collected_at is not None and (prior is None or m.collected_at > prior):
                measured_keys[m.key] = m.collected_at
        else:
            orphans += 1

    platforms: dict[str, dict] = {}
    for platform, rows in sorted(by_platform.items()):
        publications = len(rows)
        with_external_id = sum(1 for r in rows if r.has_external_id)
        with_stats = sum(1 for r in rows if r.key in measured_keys)
        stamps = [measured_keys[r.key] for r in rows if measured_keys.get(r.key)]
        coverage = round(with_stats / publications, 4) if publications else None
        share_unknowable = (
            round((publications - with_external_id) / publications, 4) if publications else None
        )
        platforms[platform] = {
            "publications": publications,
            "with_external_id": with_external_id,
            "with_stats": with_stats,
            "unknowable": publications - with_external_id,
            "coverage": coverage,
            "share_unknowable": share_unknowable,
            "last_measurement_at": max(stamps).isoformat() if stamps else None,
        }

    return {"platforms": platforms, "orphan_measurements": orphans}


def _tg_key(post_id: int) -> str:
    return f"tg:{post_id}"


def _cp_key(log_id: int) -> str:
    return f"cp:{log_id}"


def collect_refs_and_measurements(
    *,
    days: int = 30,
    channel_ids: Sequence[str] | None = None,
    cutoff: datetime | None = None,
) -> tuple[list[PublicationRef], list[Measurement]]:
    """Read-only load of publications and measurements from the farm database.

    ``channel_ids`` scopes the Telegram side so an operator (or a test) can
    look at one fleet slice without importing the whole history.
    """
    if cutoff is None:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, days))

    refs: list[PublicationRef] = []
    measurements: list[Measurement] = []
    with session_scope() as session:
        tg_stmt = select(Post).where(
            Post.status == "published", Post.published_at.isnot(None), Post.published_at >= cutoff
        )
        if channel_ids is not None:
            tg_stmt = tg_stmt.where(Post.channel_id.in_(list(channel_ids)))
        tg_posts = list(session.execute(tg_stmt).scalars().all())
        for p in tg_posts:
            refs.append(
                PublicationRef(
                    platform=TELEGRAM,
                    key=_tg_key(p.id),
                    has_external_id=p.tg_message_id is not None,
                )
            )

        # Every stat row is loaded, not only those matching an in-window post:
        # a stat that addresses no current publication is the fact this module
        # exists to report (the W24-M measured case: collection stopped while
        # publishing continued), so it must land in orphan_measurements.
        stats = session.execute(select(PostStat)).scalars().all()
        for stat in stats:
            measurements.append(Measurement(_tg_key(stat.post_id), stat.collected_at))

        cp_logs = list(
            session.execute(
                select(CrosspostLog).where(
                    CrosspostLog.status == "posted",
                    CrosspostLog.created_at >= cutoff,
                )
            ).scalars().all()
        )
        for log in cp_logs:
            refs.append(
                PublicationRef(
                    platform=log.platform or "unknown",
                    key=_cp_key(log.id),
                    has_external_id=bool(log.remote_id),
                )
            )

        # Dzen measures land in post_stats via the post's public id; a posted
        # dzen log counts as measured only when its post has any stat row.
        dzen_public_ids = {log.post_public_id: log.id for log in cp_logs if log.platform == "dzen"}
        if dzen_public_ids:
            rows = session.execute(
                select(Post.public_id, PostStat.collected_at)
                .join(PostStat, PostStat.post_id == Post.id)
                .where(Post.public_id.in_(list(dzen_public_ids)))
            ).all()
            for public_id, collected_at in rows:
                log_id = dzen_public_ids.get(public_id)
                if log_id is not None:
                    measurements.append(Measurement(_cp_key(log_id), collected_at))

        for snapshot in session.execute(select(RedditMetricSnapshot)).scalars():
            measurements.append(Measurement(_cp_key(snapshot.crosspost_log_id), snapshot.collected_at))
        for snapshot in session.execute(select(TelegraphMetricSnapshot)).scalars():
            measurements.append(Measurement(_cp_key(snapshot.crosspost_log_id), snapshot.collected_at))

    return refs, measurements


def attribution_report(
    *, days: int = 30, channel_ids: Sequence[str] | None = None, platforms: Sequence[str] = ()
) -> dict:
    """The panel-facing view: per-platform attribution of stats to publications.

    ``platforms`` forces buckets for platforms that may have published nothing
    in the window — they come back with ``coverage = None``, not 0.
    Seam for the coordinator: ``api.py`` can expose this as e.g.
    ``GET /api/analytics/attribution`` and the dashboard can render
    ``report["platforms"]`` directly; nothing here writes or requires network.
    """
    refs, measurements = collect_refs_and_measurements(days=days, channel_ids=channel_ids)
    report = summarize_attribution(refs, measurements, platforms=platforms)
    report["window_days"] = max(1, days)
    report["generated_at"] = datetime.now(UTC).isoformat()
    return report


__all__ = [
    "Measurement",
    "PublicationRef",
    "attribution_report",
    "collect_refs_and_measurements",
    "summarize_attribution",
]

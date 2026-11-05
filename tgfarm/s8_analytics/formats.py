"""Format performance: which shapes of post actually earned attention.

The planner can only prefer a format when the farm knows how formats performed,
and until now nothing measured that: `hook_style_performance` covers hooks,
`angle_performance` covers angles, but `post_format` was never aggregated. This
module answers one question with real rows: for each format, how many posts were
published and what engagement did the measured ones earn.

Honest limits, stated up front:

* engagement comes from ``post_stats`` (reactions + comments + forwards). A post
  without a snapshot is counted as published but not as measured, so a format
  cannot look good on the strength of unmeasured posts;
* ``measured`` answers «was it ever measured», while the new ``measurement``
  block answers the second question — *is that measurement still live?* A
  snapshot older than ``FORMAT_EVIDENCE_STALE_AFTER_DAYS`` is history: it stays
  in the report and it never moves a planner decision;
* rows are per channel owner, not per crosspost destination;
* a window with fewer than ``MIN_SAMPLE`` measured posts is reported with
  ``reliable: False`` instead of a confident number;
* ``platform`` scopes the whole report to one destination family, because a
  format that works on Reddit has not earned the right to decide a Дзен post.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope

log = get_logger(__name__)

#: Measured posts a format needs before its average may drive a decision.
MIN_SAMPLE = 3

#: Ids per `IN` list. SQLite's smallest historical bound is 999 variables per
#: statement, so a chunk well under it stays safe on any build.
_IN_CHUNK = 500

#: Fallback for an unreadable setting, deliberately equal to the code default.
DEFAULT_EVIDENCE_STALE_AFTER_DAYS = 7

#: Evidence states, in the vocabulary `s8_analytics.freshness` already uses for
#: snapshots. ``mixed`` is the one addition: inside a single format a week of
#: live snapshots and a month of dead ones are not the same evidence, and
#: averaging them into one number would hide that.
EVIDENCE_OK = "ok"
EVIDENCE_MIXED = "mixed"
EVIDENCE_STALE = "stale"
EVIDENCE_NEVER = "never"
#: States that carry no live evidence at all, so no decision may rest on them.
#: `blocked` is the fourth `freshness` state: nothing produced it here yet, but a
#: caller that knows collection is blocked may declare it and be believed.
EVIDENCE_UNTRUSTED: frozenset[str] = frozenset({EVIDENCE_STALE, EVIDENCE_NEVER, "blocked"})


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def evidence_stale_after_days(override: int | None = None) -> int:
    """How old a snapshot may be and still count as live evidence.

    A threshold nobody can tune is how this repo got its dead knobs, so the value
    comes from ``FORMAT_EVIDENCE_STALE_AFTER_DAYS``; an unreadable setting falls
    back to the documented code default instead of inventing one here.
    """
    for candidate in (override, _configured_stale_after_days()):
        if candidate is None:
            continue
        try:
            return max(1, min(int(candidate), 365))
        except (TypeError, ValueError):
            continue
    return DEFAULT_EVIDENCE_STALE_AFTER_DAYS


def _configured_stale_after_days() -> int:
    try:
        from tgfarm.core.settings import get_settings

        return int(get_settings().format_evidence_stale_after_days)
    except Exception as exc:  # noqa: BLE001 - an unreadable setting is not a verdict
        log.debug("s8: format evidence staleness setting unreadable: %s", exc)
        return DEFAULT_EVIDENCE_STALE_AFTER_DAYS


def _evidence_status(fresh: int, stale: int) -> str:
    """One word for «how live is this evidence», from its two counts alone."""
    if fresh <= 0 and stale <= 0:
        return EVIDENCE_NEVER
    if stale <= 0:
        return EVIDENCE_OK
    if fresh <= 0:
        return EVIDENCE_STALE
    return EVIDENCE_MIXED


#: Public spelling of the same rule, so a reader of a report can name a split the
#: same way S8 does instead of re-deriving it.
evidence_status_for = _evidence_status


def format_performance(
    days: int = 30,
    *,
    platform: str | None = None,
    now: datetime | None = None,
    stale_after_days: int | None = None,
) -> dict[str, Any]:
    """Published posts and measured engagement per post format inside the window.

    ``platform`` restricts the report to channels of that platform; without it the
    report mixes every destination and must not drive a per-platform choice.

    ``measured`` counts every post that ever got a snapshot, exactly as before,
    because that is what the panel and the sample threshold have always read. The
    parallel ``measurement`` block splits the same posts by *when* they were
    snapshotted: ``fresh`` is evidence a collector produced within
    ``stale_after_days`` (default: the settings knob), ``stale`` is a measurement
    nobody refreshed since. The split is additive on purpose — a report that
    silently redefines ``measured`` would change what the panel shows even with
    the planner switched off.
    """
    current = now or datetime.now(UTC)
    current = _aware(current) or current
    days = max(1, min(int(days), 365))
    since = current - timedelta(days=days)
    stale_after = evidence_stale_after_days(stale_after_days)
    evidence_since = current - timedelta(days=stale_after)
    platform_key = str(platform or "").strip().lower() or None

    rows: dict[str, dict[str, Any]] = {}
    with session_scope() as session:
        statement = select(Post.id, Post.post_format, Post.published_at).where(
            Post.status == "published",
            Post.published_at.isnot(None),
            Post.published_at >= since,
        )
        if platform_key is not None:
            statement = statement.join(Channel, Channel.id == Post.channel_id).where(
                func.lower(Channel.platform) == platform_key
            )
        posts = session.execute(statement).all()
        if not posts:
            return {
                "window_days": days,
                "since": since.isoformat(),
                "platform": platform_key,
                "formats": {},
                "measurement": {},
                "measured_total": 0,
                "published_total": 0,
                "fresh_measured_total": 0,
                "stale_measured_total": 0,
                "evidence_status": EVIDENCE_NEVER,
                "min_sample": MIN_SAMPLE,
                "stale_after_days": stale_after,
            }
        post_ids = [row.id for row in posts]
        stats: dict[int, tuple[int, int, int, datetime | None]] = {}
        # Grouped reads instead of a query per post: the table can hold tens of
        # thousands of snapshots and this runs inside the analytics stage. Chunked
        # because an `IN` list binds one variable per id and SQLite refuses the
        # statement past its variable limit — measured at 40 015 published posts,
        # where the read died and S12 quietly stopped learning formats.
        for start in range(0, len(post_ids), _IN_CHUNK):
            chunk = post_ids[start : start + _IN_CHUNK]
            for post_id, reactions, comments, forwards, latest in session.execute(
                select(
                    PostStat.post_id,
                    func.max(PostStat.reactions),
                    func.max(PostStat.comments),
                    func.max(PostStat.forwards),
                    func.max(PostStat.collected_at),
                )
                .where(PostStat.post_id.in_(chunk))
                .group_by(PostStat.post_id)
            ).all():
                stats[int(post_id)] = (
                    int(reactions or 0),
                    int(comments or 0),
                    int(forwards or 0),
                    _aware(latest),
                )

    for row in posts:
        fmt = str(row.post_format or "news")
        item = rows.setdefault(
            fmt,
            {
                "format": fmt, "published": 0, "measured": 0, "engagement_sum": 0.0,
                "fresh": 0, "fresh_engagement_sum": 0.0, "stale": 0,
                "last_measured_at": None, "fresh_last_measured_at": None,
            },
        )
        item["published"] += 1
        stats_row = stats.get(int(row.id))
        if stats_row is None:
            continue
        reactions, comments, forwards, measured_at = stats_row
        engagement = reactions + comments + forwards
        item["measured"] += 1
        item["engagement_sum"] += float(engagement)
        # A snapshot with no timestamp cannot be proven live, so it is stale: the
        # conservative reading, the same one `freshness` gives an unknown state.
        if measured_at is not None and measured_at >= evidence_since:
            item["fresh"] += 1
            item["fresh_engagement_sum"] += float(engagement)
            if item["fresh_last_measured_at"] is None or measured_at > item["fresh_last_measured_at"]:
                item["fresh_last_measured_at"] = measured_at
        else:
            item["stale"] += 1
        if item["last_measured_at"] is None or (
            measured_at is not None and measured_at > item["last_measured_at"]
        ):
            item["last_measured_at"] = measured_at

    formats: dict[str, dict[str, Any]] = {}
    for fmt, item in rows.items():
        measured = int(item["measured"])
        average = round(item["engagement_sum"] / measured, 4) if measured else 0.0
        formats[fmt] = {
            "format": fmt,
            "published": int(item["published"]),
            "measured": measured,
            "avg_engagement": average,
            "reliable": measured >= MIN_SAMPLE,
            "n": measured,
        }
    ordered = dict(sorted(formats.items(), key=lambda pair: (-pair[1]["avg_engagement"], pair[0])))
    measurement: dict[str, dict[str, Any]] = {}
    # Same order as `formats`, so a reader can walk the two side by side.
    for fmt in ordered:
        item = rows[fmt]
        fresh = int(item["fresh"])
        stale = int(item["stale"])
        measurement[fmt] = {
            "format": fmt,
            "measured": int(item["measured"]),
            "published": int(item["published"]),
            "fresh": fresh,
            "stale": stale,
            "avg_engagement": ordered[fmt]["avg_engagement"],
            # The live-only average: a mixed format's `avg_engagement` blends in
            # dead snapshots, and a decision must not be steered by that blend.
            "fresh_avg_engagement": round(item["fresh_engagement_sum"] / fresh, 4) if fresh else 0.0,
            "fresh_reliable": fresh >= MIN_SAMPLE,
            "evidence_status": _evidence_status(fresh, stale),
            "last_measured_at": (
                item["last_measured_at"].isoformat() if item["last_measured_at"] else None
            ),
            "fresh_last_measured_at": (
                item["fresh_last_measured_at"].isoformat() if item["fresh_last_measured_at"] else None
            ),
        }
    fresh_total = sum(int(item["fresh"]) for item in rows.values())
    stale_total = sum(int(item["stale"]) for item in rows.values())
    return {
        "window_days": days,
        "since": since.isoformat(),
        "platform": platform_key,
        "formats": ordered,
        "measurement": measurement,
        "measured_total": sum(item["measured"] for item in formats.values()),
        "published_total": sum(item["published"] for item in formats.values()),
        "fresh_measured_total": fresh_total,
        "stale_measured_total": stale_total,
        "evidence_status": _evidence_status(fresh_total, stale_total),
        "min_sample": MIN_SAMPLE,
        "stale_after_days": stale_after,
    }


def planner_history(
    platform: str,
    *,
    days: int = 30,
    now: datetime | None = None,
    stale_after_days: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Rows shaped for `s10_format.plan_with_history`, scoped to one platform.

    `measured` is what the planner gates its sample on: a published post nobody
    ever measured is activity, not evidence. `published` travels alongside it so
    the reason string can say how much of the window stayed unmeasured.

    This is where staleness binds hardest, because the rows it returns are the
    only evidence the planner is allowed to act on: `measured` and
    `avg_engagement` are the **live** counts, so a format measured only by dead
    snapshots arrives with `measured: 0` and the planner falls back to the rules.
    The dead count is not dropped, it is named in `stale_measured`, so a reason
    can say «measured, but weeks old» instead of «never measured». With every
    snapshot inside the threshold the numbers are identical to `formats`, which is
    what keeps a fresh window behaving exactly as it did before.
    """
    report = format_performance(days, platform=platform, now=now, stale_after_days=stale_after_days)
    history: dict[str, dict[str, Any]] = {}
    for fmt, item in report["formats"].items():
        evidence = report["measurement"].get(fmt) or {}
        fresh = int(evidence.get("fresh") or 0)
        history[fmt] = {
            "measured": fresh,
            "published": int(item["published"]),
            "avg_engagement": float(evidence.get("fresh_avg_engagement") or 0.0),
            "reliable": bool(evidence.get("fresh_reliable")),
            "fresh_measured": fresh,
            "stale_measured": int(evidence.get("stale") or 0),
            "evidence_status": str(evidence.get("evidence_status") or EVIDENCE_NEVER),
            "stale_after_days": int(report["stale_after_days"]),
        }
    return history


def best_format(days: int = 30, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The format with the best measured engagement, or ``None`` without evidence.

    Reports the whole window, live and dead together — it is a readout, not a
    planner input; the gated feed is `planner_history`.
    """
    report = format_performance(days, now=now)
    for item in report["formats"].values():
        if item["reliable"]:
            return item
    return None


__all__ = [
    "DEFAULT_EVIDENCE_STALE_AFTER_DAYS",
    "EVIDENCE_MIXED",
    "EVIDENCE_NEVER",
    "EVIDENCE_OK",
    "EVIDENCE_STALE",
    "EVIDENCE_UNTRUSTED",
    "MIN_SAMPLE",
    "best_format",
    "evidence_stale_after_days",
    "evidence_status_for",
    "format_performance",
    "planner_history",
]

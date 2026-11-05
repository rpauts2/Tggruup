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
  format that works on Reddit has not earned the right to decide a Дзен post;
* `status = "published"` is the farm's own claim, not the platform's. The
  publisher used to stamp it on sends that never happened (fixed in the
  publisher, but the historic rows remain), so a second question is asked of
  every row: *can anything point at that publication?* The answer is the
  ``publication`` block, in the same additive shape as ``measurement`` —
  ``published`` and ``measured`` keep counting what they always counted, and
  the evidenced subset travels beside them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
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

#: The three answers to «is this row backed by proof that it really happened».
#: `unevidenced` is not «unmeasured»: the farm wrote the row down as published
#: and nothing on the platform side can point at it.
EVIDENCED = "evidenced"
UNEVIDENCED = "unevidenced"
#: No rule exists for this platform yet, so nothing disproves its rows. They stay
#: in the sample — a row nobody could disprove is not a row proven false — and are
#: counted separately so a reader sees how much of the "evidenced" set rests on
#: the absence of a rule rather than on proof.
UNRULLED = "unrulled"

#: Proof, per platform, that a `published` row is a real publication. These are
#: the rules `maintenance.publication_evidence_repair` already applies when it
#: moves such rows to `publish_unknown`; naming them here is what lets a report
#: show the phantom rows instead of waiting for the sweep to remove them.
#: * telegram — the Bot API returns a message id for every accepted message;
#: * dzen / vk — the publisher journals `posted` only once the remote side
#:   accepted the article. A `posted` row without an id or url still counts: that
#:   is a real publication whose identifier the parser never found.
RULE_MESSAGE_ID = "posts.tg_message_id"
RULE_JOURNAL = "crosspost_logs.status = 'posted'"
EVIDENCE_RULES: dict[str, str] = {
    "telegram": RULE_MESSAGE_ID,
    "dzen": RULE_JOURNAL,
    "vk": RULE_JOURNAL,
}
#: `crosspost_logs` spelling of the platform, matched case-sensitively exactly as
#: the maintenance sweep matches it.
_JOURNAL_STATUS = "posted"

#: The fifth word of the evidence vocabulary, and the only one this dimension
#: adds: `never` would be a lie about a row nobody checked, and `ok` would be a
#: lie about a row nobody could check.
EVIDENCE_UNRULLED = "unknown"


def _rate(total: float, size: int) -> float | None:
    """A mean over `size`, or ``None`` when there is nothing to mean over.

    The old `... if size else 0.0` wrote a zero where no measurement existed,
    which is the one reading the operator rules out: an unmeasured format and a
    format that earned nothing are different facts, and a consumer that sorts on
    this number must not be handed a fabricated one.
    """
    if size <= 0:
        return None
    return round(total / size, 4)


def publication_evidence_status(evidenced: int, unevidenced: int, unrulled: int) -> str:
    """One word for how much of a set is backed by proof it really happened.

    Same four-word vocabulary as the freshness dimension, same verdict in the
    same word: `never` here means «not one row proven», which is why it already
    sits in `EVIDENCE_UNTRUSTED` and needs no new veto in a reader.
    """
    if evidenced <= 0 and unevidenced <= 0 and unrulled <= 0:
        return EVIDENCE_NEVER
    if unevidenced > 0:
        return EVIDENCE_MIXED if evidenced > 0 or unrulled > 0 else EVIDENCE_NEVER
    if evidenced > 0:
        return EVIDENCE_OK if unrulled <= 0 else EVIDENCE_MIXED
    return EVIDENCE_UNRULLED


def _publication_buckets(session: Any, posts: Sequence[Any]) -> dict[int, str]:
    """post id -> `evidenced` / `unevidenced` / `unrulled`, from the rows alone.

    Grouped reads, chunked like the snapshot read below: one `EXISTS` per post
    would be a query per publication.
    """
    wanted: dict[str, set[str]] = {}
    for row in posts:
        platform = str(row.platform or "").strip().lower()
        if EVIDENCE_RULES.get(platform) == RULE_JOURNAL:
            wanted.setdefault(platform, set()).add(str(row.public_id))

    journaled: dict[str, set[str]] = {}
    for platform, public_ids in wanted.items():
        ordered = sorted(public_ids)
        found: set[str] = set()
        for start in range(0, len(ordered), _IN_CHUNK):
            chunk = ordered[start : start + _IN_CHUNK]
            found |= {
                str(value)
                for (value,) in session.execute(
                    select(CrosspostLog.post_public_id)
                    .where(
                        CrosspostLog.platform == platform,
                        CrosspostLog.status == _JOURNAL_STATUS,
                        CrosspostLog.post_public_id.in_(chunk),
                    )
                    .distinct()
                ).all()
            }
        journaled[platform] = found

    buckets: dict[int, str] = {}
    for row in posts:
        platform = str(row.platform or "").strip().lower()
        rule = EVIDENCE_RULES.get(platform)
        if rule is None:
            bucket = UNRULLED
        elif rule == RULE_MESSAGE_ID:
            bucket = EVIDENCED if row.tg_message_id is not None else UNEVIDENCED
        elif str(row.public_id) in journaled.get(platform, set()):
            bucket = EVIDENCED
        else:
            bucket = UNEVIDENCED
        buckets[int(row.id)] = bucket
    return buckets


def _publication_block(
    *,
    published: int,
    evidenced: int,
    unevidenced: int,
    unrulled: int,
    measured: int,
    measured_unevidenced: int,
    measured_unrulled: int,
    sample_engagement_sum: float,
    fresh: int,
    fresh_unevidenced: int,
    fresh_sample_engagement_sum: float,
) -> dict[str, Any]:
    """The evidenced subset of a set that was already counted in full.

    `sample_size` is the number the reported rate actually divides by — every
    measurement the farm cannot disprove — and it is deliberately a different
    number from `measured`, which keeps its old meaning for the panel. The two
    averages are ``None`` when their own denominator is empty: no evidence yields
    no rate, never a rate of zero.
    """
    sample = max(0, measured - measured_unevidenced)
    fresh_sample = max(0, fresh - fresh_unevidenced)
    return {
        "published": int(published),
        "published_evidenced": int(evidenced),
        "published_unevidenced": int(unevidenced),
        "published_unrulled": int(unrulled),
        "measured": int(measured),
        "measured_evidenced": max(0, sample - measured_unrulled),
        "measured_unrulled": int(measured_unrulled),
        "measured_unevidenced": int(measured_unevidenced),
        "sample_size": sample,
        "avg_engagement": _rate(sample_engagement_sum, sample),
        "reliable": sample >= MIN_SAMPLE,
        "fresh_sample_size": fresh_sample,
        # The live count the freshness dimension reports, minus the rows this
        # dimension disproves: a reader must be able to get from `fresh` to
        # `fresh_sample_size` without guessing what was taken away.
        "fresh_unevidenced": int(fresh_unevidenced),
        "fresh_avg_engagement": _rate(fresh_sample_engagement_sum, fresh_sample),
        "evidence_status": publication_evidence_status(evidenced, unevidenced, unrulled),
    }


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

    The ``publication`` block asks the third question of the same rows: *did the
    publication happen at all*. Nothing is removed from ``published`` or
    ``measured``; instead each of them is joined by the evidenced subset and by
    ``sample_size``, the count the evidenced rate divides by. A row is
    ``unevidenced`` only when the platform has a rule and the row fails it; on a
    platform with no rule at all the row is ``unrulled`` and stays in the sample,
    because nobody has disproved it. ``publish_unknown`` travels alongside as its
    own count: those rows are in no denominator here, and a report that did not
    name them would leave the reader to guess how much of the output is in that
    state.
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
        statement = (
            select(
                Post.id,
                Post.post_format,
                Post.published_at,
                Post.public_id,
                Post.tg_message_id,
                Channel.platform,
            )
            .outerjoin(Channel, Channel.id == Post.channel_id)
            .where(
                Post.status == "published",
                Post.published_at.isnot(None),
                Post.published_at >= since,
            )
        )
        if platform_key is not None:
            statement = statement.where(func.lower(Channel.platform) == platform_key)
        posts = session.execute(statement).all()
        # No window on these: a row in this state has no `published_at` to put in
        # one, because the sweep that creates it clears the stamp it invented.
        unknown_statement = select(func.count(Post.id)).where(Post.status == "publish_unknown")
        if platform_key is not None:
            unknown_statement = unknown_statement.outerjoin(
                Channel, Channel.id == Post.channel_id
            ).where(func.lower(Channel.platform) == platform_key)
        publish_unknown = int(session.execute(unknown_statement).scalar_one() or 0)
        if not posts:
            return {
                "window_days": days,
                "since": since.isoformat(),
                "platform": platform_key,
                "formats": {},
                "measurement": {},
                "publication": {
                    **_publication_block(
                        published=0,
                        evidenced=0,
                        unevidenced=0,
                        unrulled=0,
                        measured=0,
                        measured_unevidenced=0,
                        measured_unrulled=0,
                        sample_engagement_sum=0.0,
                        fresh=0,
                        fresh_unevidenced=0,
                        fresh_sample_engagement_sum=0.0,
                    ),
                    "publish_unknown": publish_unknown,
                    "rules": dict(EVIDENCE_RULES),
                },
                "measured_total": 0,
                "published_total": 0,
                "fresh_measured_total": 0,
                "stale_measured_total": 0,
                "evidence_status": EVIDENCE_NEVER,
                "min_sample": MIN_SAMPLE,
                "stale_after_days": stale_after,
            }
        buckets = _publication_buckets(session, posts)
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
                # Publication evidence: a subset of the two counts above, never a
                # fourth count, so `evidenced + unevidenced + unrulled` is exactly
                # `published` and the same identity holds for `measured`.
                "published_evidenced": 0, "published_unevidenced": 0, "published_unrulled": 0,
                "measured_unevidenced": 0, "measured_unrulled": 0, "sample_engagement_sum": 0.0,
                "fresh_unevidenced": 0, "fresh_sample_engagement_sum": 0.0,
            },
        )
        item["published"] += 1
        bucket = buckets.get(int(row.id), UNRULLED)
        if bucket == EVIDENCED:
            item["published_evidenced"] += 1
        elif bucket == UNEVIDENCED:
            item["published_unevidenced"] += 1
        else:
            item["published_unrulled"] += 1
        stats_row = stats.get(int(row.id))
        if stats_row is None:
            continue
        reactions, comments, forwards, measured_at = stats_row
        engagement = reactions + comments + forwards
        item["measured"] += 1
        item["engagement_sum"] += float(engagement)
        if bucket == UNEVIDENCED:
            item["measured_unevidenced"] += 1
        else:
            if bucket == UNRULLED:
                item["measured_unrulled"] += 1
            item["sample_engagement_sum"] += float(engagement)
        # A snapshot with no timestamp cannot be proven live, so it is stale: the
        # conservative reading, the same one `freshness` gives an unknown state.
        if measured_at is not None and measured_at >= evidence_since:
            item["fresh"] += 1
            item["fresh_engagement_sum"] += float(engagement)
            if bucket != UNEVIDENCED:
                item["fresh_sample_engagement_sum"] += float(engagement)
            else:
                item["fresh_unevidenced"] += 1
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
        # The evidenced subset of the same two counts, beside them rather than in
        # place of them: `published`/`measured` above keep their old meaning.
        measurement[fmt]["publication"] = _publication_block(
            published=int(item["published"]),
            evidenced=int(item["published_evidenced"]),
            unevidenced=int(item["published_unevidenced"]),
            unrulled=int(item["published_unrulled"]),
            measured=int(item["measured"]),
            measured_unevidenced=int(item["measured_unevidenced"]),
            measured_unrulled=int(item["measured_unrulled"]),
            sample_engagement_sum=float(item["sample_engagement_sum"]),
            fresh=fresh,
            fresh_unevidenced=int(item["fresh_unevidenced"]),
            fresh_sample_engagement_sum=float(item["fresh_sample_engagement_sum"]),
        )
    fresh_total = sum(int(item["fresh"]) for item in rows.values())
    stale_total = sum(int(item["stale"]) for item in rows.values())
    return {
        "window_days": days,
        "since": since.isoformat(),
        "platform": platform_key,
        "formats": ordered,
        "measurement": measurement,
        "publication": {
            **_publication_block(
                published=sum(item["published"] for item in formats.values()),
                evidenced=sum(int(item["published_evidenced"]) for item in rows.values()),
                unevidenced=sum(int(item["published_unevidenced"]) for item in rows.values()),
                unrulled=sum(int(item["published_unrulled"]) for item in rows.values()),
                measured=sum(item["measured"] for item in formats.values()),
                measured_unevidenced=sum(int(item["measured_unevidenced"]) for item in rows.values()),
                measured_unrulled=sum(int(item["measured_unrulled"]) for item in rows.values()),
                sample_engagement_sum=sum(
                    float(item["sample_engagement_sum"]) for item in rows.values()
                ),
                fresh=fresh_total,
                fresh_unevidenced=sum(int(item["fresh_unevidenced"]) for item in rows.values()),
                fresh_sample_engagement_sum=sum(
                    float(item["fresh_sample_engagement_sum"]) for item in rows.values()
                ),
            ),
            # Report-level only: these two are properties of the destination
            # family as a whole, not of one format, and a per-format copy of a
            # report-wide number would read as a count that belongs to it.
            "publish_unknown": publish_unknown,
            "rules": dict(EVIDENCE_RULES),
        },
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

    Publication evidence binds here the same way and with the same courtesy:
    `measured` is the live count **minus the rows the platform's own rule
    disproves** — a snapshot of a send that never happened is not evidence about a
    format — and what was dropped is named in `unevidenced_measured` rather than
    vanishing. `sample_size` states plainly what `avg_engagement` divides by, and
    `avg_engagement` is ``None`` when that sample is empty: no proof, no rate, and
    never a zero that a planner could rank. On a platform with no evidence rule
    nothing is disproved, so the numbers are what they have always been and
    `unrulled_published` says how much of that rests on the missing rule.
    """
    report = format_performance(days, platform=platform, now=now, stale_after_days=stale_after_days)
    history: dict[str, dict[str, Any]] = {}
    for fmt, item in report["formats"].items():
        evidence = report["measurement"].get(fmt) or {}
        proven = evidence.get("publication") or {}
        fresh = int(evidence.get("fresh") or 0)
        unevidenced_fresh = int(proven.get("fresh_unevidenced") or 0)
        sample = max(0, fresh - unevidenced_fresh)
        history[fmt] = {
            "measured": sample,
            "published": int(item["published"]),
            # The mean over `sample_size`, and None when there is nothing to mean
            # over — the one value here a reader may not read as «earned nothing».
            "avg_engagement": proven.get("fresh_avg_engagement"),
            "reliable": sample >= MIN_SAMPLE,
            "fresh_measured": fresh,
            "stale_measured": int(evidence.get("stale") or 0),
            "evidence_status": str(evidence.get("evidence_status") or EVIDENCE_NEVER),
            "stale_after_days": int(report["stale_after_days"]),
            # What the publication veto took off the live count, named rather than
            # dropped, in the same shape as `stale_measured` above.
            "unevidenced_measured": unevidenced_fresh,
            "unevidenced_published": int(proven.get("published_unevidenced") or 0),
            "unrulled_published": int(proven.get("published_unrulled") or 0),
            "sample_size": sample,
            "publication_evidence": str(proven.get("evidence_status") or EVIDENCE_NEVER),
        }
    return history


def best_format(days: int = 30, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The format with the best measured engagement, or ``None`` without evidence.

    Reports the whole window, live and dead together, evidenced publications and
    disproved ones together — it is a readout, not a planner input; the gated feed
    is `planner_history`, and `format_performance`'s ``publication`` block holds
    the evidenced sample size and rate for anyone reading this number.
    """
    report = format_performance(days, now=now)
    for item in report["formats"].values():
        if item["reliable"]:
            return item
    return None


__all__ = [
    "DEFAULT_EVIDENCE_STALE_AFTER_DAYS",
    "EVIDENCED",
    "EVIDENCE_MIXED",
    "EVIDENCE_NEVER",
    "EVIDENCE_OK",
    "EVIDENCE_RULES",
    "EVIDENCE_STALE",
    "EVIDENCE_UNRULLED",
    "EVIDENCE_UNTRUSTED",
    "MIN_SAMPLE",
    "RULE_JOURNAL",
    "RULE_MESSAGE_ID",
    "UNEVIDENCED",
    "UNRULLED",
    "best_format",
    "evidence_stale_after_days",
    "evidence_status_for",
    "format_performance",
    "planner_history",
    "publication_evidence_status",
]

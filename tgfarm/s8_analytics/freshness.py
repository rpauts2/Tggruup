"""How fresh are the metrics we actually have, platform by platform.

``post_stats`` is an append-only set of snapshots: a platform either has rows
that were really collected or it has nothing. This module reports that
difference instead of printing zeroes. Absent data becomes ``never`` (nothing
collected yet) or ``blocked`` (collection is impossible for a named reason),
never a fabricated ``0``.

Attribution goes through ``PostStat.post_id -> Post.channel_id ->
Channel.platform``: a snapshot row says nothing about its platform on its own.
That is the only attribution the schema stores, so a snapshot taken for a
crossposted VK/Dzen publication is counted under the platform of the channel
that owns the post, not under the destination it was copied to.

Timestamps are returned as ISO 8601 strings in UTC so the result is directly
JSON-serialisable for the API layer.

``stale`` is a purely factual age test ("the newest snapshot is older than
24 hours"). ``status`` is the classification: a platform can be ``blocked``
while its last zero-only snapshot is already old, and then ``stale`` is still
true because the row really is old.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, union
from sqlalchemy.exc import SQLAlchemyError

from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import CrosspostLog, PostStat
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.platforms import CATALOG

log = get_logger(__name__)

STALE_AFTER_HOURS = 24.0

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_NEVER = "never"
STATUS_BLOCKED = "blocked"
STATUSES: tuple[str, ...] = (STATUS_OK, STATUS_STALE, STATUS_NEVER, STATUS_BLOCKED)

# Operator-facing texts. The VK wording names the exact API answer, so an
# operator does not spend an evening retrying a group token.
VK_TOKEN_REASON = "нужен VK_USER_TOKEN (групповой токен отклоняется с error 27)"
TELEGRAM_NO_COLLECTOR_REASON = "сборщика метрик Telegram нет"
DZEN_NO_PUBLICATIONS_REASON = "опубликованных статей нет"
NO_SNAPSHOTS_REASON = "снимков метрик нет"

# A snapshot of all zeroes is not a measurement: VK returns zeros for posts it
# could not read, and publishing one as "fresh metrics" would be a lie.
_NON_ZERO_COUNTERS = tuple(
    column > 0
    for column in (
        PostStat.impressions,
        PostStat.opens,
        PostStat.views,
        PostStat.reactions,
        PostStat.comments,
        PostStat.forwards,
    )
)


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo, so naive values are read as UTC, never as local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_hours(latest: datetime | None, now: datetime) -> float | None:
    if latest is None:
        return None
    # A clock skew must not produce a negative age that reads like a bug.
    return max(0.0, (now - latest).total_seconds() / 3600.0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _vk_token_present() -> bool:
    """Treat an unreadable settings object as "no token", never as "maybe"."""
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - freshness must not break the dashboard
        log.warning("s8: settings unavailable for freshness: %s", redact_sensitive_text(exc, limit=120))
        return False
    return bool(str(getattr(settings, "vk_user_token", "") or "").strip())


def _load_facts() -> tuple[dict[str, dict[str, Any]], int, int, int, str | None]:
    """One session, read-only: per-platform index, totals, and blocker evidence.

    Returns ``(index, snapshots_total, vk_meaningful, dzen_publications, error)``.
    On a database that was never bootstrapped the read fails; a dashboard should
    show that failure instead of a 500 or invented ``never`` statuses.
    """
    try:
        with session_scope() as session:
            grouped = session.execute(
                select(
                    ChannelRow.platform,
                    func.count(PostStat.id),
                    func.max(PostStat.collected_at),
                )
                .select_from(PostStat)
                .join(PostRow, PostRow.id == PostStat.post_id)
                .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                .group_by(ChannelRow.platform)
            ).all()
            snapshots_total = int(
                session.execute(select(func.count(PostStat.id))).scalar_one() or 0
            )
            vk_meaningful = int(
                session.execute(
                    select(func.count(PostStat.id))
                    .join(PostRow, PostRow.id == PostStat.post_id)
                    .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
                    .where(ChannelRow.platform == "vk", or_(*_NON_ZERO_COUNTERS))
                ).scalar_one()
                or 0
            )
            dzen_publications = int(
                session.execute(_dzen_publications_query()).scalar_one() or 0
            )
    except SQLAlchemyError as exc:
        detail = redact_sensitive_text(exc, limit=200)
        log.warning("s8: freshness read failed: %s", detail)
        return {}, 0, 0, 0, f"post_stats недоступна: {detail}"

    index: dict[str, dict[str, Any]] = {}
    for platform, count, latest in grouped:
        key = str(platform or "").strip().lower() or "unknown"
        bucket = index.setdefault(key, {"snapshots": 0, "last": None})
        bucket["snapshots"] += int(count or 0)
        newest = _utc(latest)
        if newest is not None and (bucket["last"] is None or newest > bucket["last"]):
            bucket["last"] = newest
    return index, snapshots_total, vk_meaningful, dzen_publications, None


def _dzen_publications_query():
    """Count distinct Dzen publications from both places that record them.

    A Dzen article can be a published post on a Dzen channel, a ``posted``
    crosspost log row, or both; a union deduplicates the pair.
    """
    published = (
        select(PostRow.public_id.label("public_id"))
        .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
        .where(
            ChannelRow.platform == "dzen",
            PostRow.status == "published",
            PostRow.public_id != "",
        )
    )
    posted = select(CrosspostLog.post_public_id.label("public_id")).where(
        CrosspostLog.platform == "dzen",
        CrosspostLog.status == "posted",
        CrosspostLog.post_public_id != "",
    )
    combined = union(published, posted).subquery()
    return select(func.count()).select_from(combined)


def _classify(
    platform_id: str,
    *,
    snapshots: int,
    age: float | None,
    vk_token: bool,
    vk_meaningful: int,
    dzen_publications: int,
) -> tuple[str, str | None]:
    """Return ``(status, reason)`` for one platform.

    Order matters: a hard blocker wins over "no data yet", because the operator
    needs the requirement, not a vague "never".
    """
    if platform_id == "vk" and vk_meaningful == 0 and not vk_token:
        return STATUS_BLOCKED, VK_TOKEN_REASON
    if platform_id == "telegram" and snapshots == 0:
        return STATUS_BLOCKED, TELEGRAM_NO_COLLECTOR_REASON
    if platform_id == "dzen" and snapshots == 0 and dzen_publications == 0:
        return STATUS_BLOCKED, DZEN_NO_PUBLICATIONS_REASON

    if snapshots == 0 or age is None:
        reason = (
            "публикации есть, но снимков метрик нет"
            if platform_id == "dzen" and dzen_publications
            else NO_SNAPSHOTS_REASON
        )
        return STATUS_NEVER, reason

    if platform_id == "vk" and vk_meaningful == 0:
        # The token is present here (otherwise the first rule fired), but every
        # snapshot is all zeroes. Reporting "ok" would dress up an empty read.
        return STATUS_NEVER, "снимки ВК есть, но все счётчики нулевые: измерять нечего"

    if age > STALE_AFTER_HOURS:
        reason = f"последний снимок {age:.1f} ч назад (порог {STALE_AFTER_HOURS:.0f} ч)"
        if platform_id == "vk" and not vk_token:
            reason = f"{reason}; обновление недоступно: {VK_TOKEN_REASON}"
        return STATUS_STALE, reason

    return STATUS_OK, None


def metrics_freshness(now: datetime | None = None) -> dict[str, Any]:
    """Per-platform snapshot count, newest timestamp, age, staleness and status.

    Every platform from ``tgfarm.platforms.CATALOG`` is present in the result,
    including platforms that were never measured. The returned mapping is::

        {
          "generated_at": "2026-11-04T12:00:00+00:00",
          "stale_after_hours": 24.0,
          "snapshots_total": 123,
          "platforms": {
            "vk": {
              "id": "vk", "name": "ВКонтакте", "symbol": "VK",
              "category": "Российские", "mode": "built_in",
              "snapshots": 40,
              "last_snapshot_at": "2026-11-04T10:00:00+00:00" | None,
              "age_hours": 2.0 | None,
              "stale": False,
              "status": "ok" | "stale" | "never" | "blocked",
              "reason": None | "нужен VK_USER_TOKEN (...)",
            },
            ...
          },
          "error": None,
        }

    ``snapshots_total`` counts every ``post_stats`` row, including rows whose
    channel platform is not in the catalog; per-platform counts only count what
    the catalog knows about.
    """
    current = _utc(now) or datetime.now(UTC)
    index, snapshots_total, vk_meaningful, dzen_publications, error = _load_facts()
    vk_token = _vk_token_present()

    platforms: dict[str, dict[str, Any]] = {}
    for item in CATALOG:
        platform_id = str(item.get("id") or "").strip().lower()
        if not platform_id:
            continue
        bucket = index.get(platform_id) or {}
        snapshots = int(bucket.get("snapshots", 0) or 0)
        latest = bucket.get("last")
        age = _age_hours(latest, current)
        if error is not None:
            # The read itself failed: no data is knowable, and the reason must
            # point at the read, not at a platform requirement.
            status, reason = STATUS_BLOCKED, error
        else:
            status, reason = _classify(
                platform_id,
                snapshots=snapshots,
                age=age,
                vk_token=vk_token,
                vk_meaningful=vk_meaningful,
                dzen_publications=dzen_publications,
            )
        platforms[platform_id] = {
            "id": platform_id,
            "name": str(item.get("name") or platform_id),
            "symbol": str(item.get("symbol") or ""),
            "category": str(item.get("category") or ""),
            "mode": str(item.get("mode") or ""),
            "snapshots": snapshots,
            "last_snapshot_at": _iso(latest),
            "age_hours": round(age, 2) if age is not None else None,
            "stale": bool(age is not None and age > STALE_AFTER_HOURS),
            "status": status,
            "reason": reason,
        }

    return {
        "generated_at": current.isoformat(),
        "stale_after_hours": STALE_AFTER_HOURS,
        "snapshots_total": snapshots_total,
        "platforms": platforms,
        "error": error,
    }


def freshness_summary(now: datetime | None = None) -> dict[str, Any]:
    """Count platforms per status, total snapshots, and the newest snapshot.

    ``freshest`` is the newest ``post_stats`` row across all catalog platforms,
    regardless of whether its counters are non-zero; ``None`` means the table is
    empty. Counts always add up to ``platforms_total``.
    """
    report = metrics_freshness(now)
    counts = {status: 0 for status in STATUSES}
    freshest: dict[str, Any] | None = None

    for row in report["platforms"].values():
        counts[row["status"]] += 1
        if row["last_snapshot_at"] is None or row["age_hours"] is None:
            continue
        if freshest is None or row["age_hours"] < freshest["age_hours"]:
            freshest = {
                "platform": row["id"],
                "name": row["name"],
                "status": row["status"],
                "collected_at": row["last_snapshot_at"],
                "age_hours": row["age_hours"],
            }

    return {
        "generated_at": report["generated_at"],
        "stale_after_hours": report["stale_after_hours"],
        "platforms_total": len(report["platforms"]),
        "ok": counts[STATUS_OK],
        "stale": counts[STATUS_STALE],
        "never": counts[STATUS_NEVER],
        "blocked": counts[STATUS_BLOCKED],
        "snapshots_total": report["snapshots_total"],
        "freshest": freshest,
        "error": report["error"],
    }


__all__ = [
    "STALE_AFTER_HOURS",
    "STATUSES",
    "freshness_summary",
    "metrics_freshness",
]

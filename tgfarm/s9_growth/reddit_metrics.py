"""Reddit post-metric snapshots and conservative community learning.

The public Data API exposes post score, comment count and upvote ratio. This
module deliberately does not pretend that it can fetch creator-only impressions
or views; it learns only from metrics actually returned by the API.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.db.models import CrosspostLog, RedditMetricSnapshot
from tgfarm.db.session import session_scope
from tgfarm.s9_growth.adapters import ADAPTERS, RedditAdapter

log = get_logger(__name__)
_COMMUNITY_RE = re.compile(r"/r/([A-Za-z0-9_]{2,21})/", re.IGNORECASE)
_REDDIT_POST_RE = re.compile(r"/comments/([a-z0-9]+)/", re.IGNORECASE)


class RedditMetricsError(RuntimeError):
    """Safe operator-facing error from Reddit metric collection."""


def _community_from_url(value: str | None) -> str:
    match = _COMMUNITY_RE.search(urlparse(value or "").path)
    return match.group(1).lower() if match else ""


def _post_id(remote_id: str | None, url: str | None) -> str:
    value = str(remote_id or "").strip()
    if value.startswith("t3_"):
        return value
    if value and re.fullmatch(r"[a-z0-9]{4,16}", value, re.IGNORECASE):
        return "t3_" + value
    match = _REDDIT_POST_RE.search(urlparse(url or "").path)
    return "t3_" + match.group(1) if match else ""


def _clamp(value: float, lower: float, upper: float) -> float:
    return round(max(lower, min(upper, value)), 2)


def _row_metrics(
    logs: list[dict[str, Any]], snapshots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build one latest metric per successful submission from plain dictionaries."""
    latest: dict[int, dict[str, Any]] = {}
    for snapshot in sorted(snapshots, key=lambda row: (row.get("collected_at") or "", row.get("id", 0)), reverse=True):
        latest.setdefault(int(snapshot["crosspost_log_id"]), snapshot)

    records: list[dict[str, Any]] = []
    for log_row in logs:
        if log_row.get("status") != "posted":
            continue
        subreddit = str(log_row.get("subreddit") or _community_from_url(log_row.get("url"))).lower()
        if not subreddit:
            continue
        metric = latest.get(int(log_row["id"]))
        records.append({
            "log_id": int(log_row["id"]),
            "subreddit": subreddit,
            "post_public_id": str(log_row.get("post_public_id") or ""),
            "url": str(log_row.get("url") or f"https://www.reddit.com/r/{subreddit}/"),
            "status": "posted",
            "score": metric.get("score") if metric else None,
            "comments": metric.get("num_comments") if metric else None,
            "upvote_ratio": metric.get("upvote_ratio") if metric else None,
            "collected_at": metric.get("collected_at") if metric else None,
        })
    return records


def _aggregate_communities(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["subreddit"], []).append(record)

    result: list[dict[str, Any]] = []
    for subreddit, posts in grouped.items():
        metrics = [row for row in posts if row.get("score") is not None]
        avg_score = round(mean(float(row["score"]) for row in metrics), 2) if metrics else None
        avg_comments = round(mean(float(row["comments"] or 0) for row in metrics), 2) if metrics else None
        ratios = [float(row["upvote_ratio"]) for row in metrics if row.get("upvote_ratio") is not None]
        avg_ratio = round(mean(ratios), 3) if ratios else None
        success_count = len(posts)

        # A transparent, intentionally capped signal. The sample size is shown
        # separately, so a single lucky post cannot dominate the topic match.
        if metrics:
            performance = (float(avg_score or 0) / 25.0)
            performance += float(avg_comments or 0) * 0.45
            if avg_ratio is not None:
                performance += (avg_ratio - 0.5) * 8.0
            bonus = _clamp(min(4.0, success_count * 0.5) + performance, -5.0, 15.0)
        else:
            bonus = _clamp(min(3.0, success_count * 0.4), 0.0, 3.0)

        latest_times = [str(row["collected_at"]) for row in metrics if row.get("collected_at")]
        result.append({
            "subreddit": subreddit,
            "url": f"https://www.reddit.com/r/{subreddit}/",
            "published_posts": success_count,
            "metric_samples": len(metrics),
            "avg_score": avg_score,
            "avg_comments": avg_comments,
            "avg_upvote_ratio": avg_ratio,
            "learning_bonus": bonus,
            "latest_metric_at": max(latest_times) if latest_times else None,
            "small_sample": len(metrics) < 3,
        })
    result.sort(
        key=lambda row: (
            row["learning_bonus"],
            row["published_posts"],
            row["metric_samples"],
        ),
        reverse=True,
    )
    return result


def get_reddit_metrics_report() -> dict[str, Any]:
    """Read-only community report for the dashboard and the scout."""
    with session_scope() as session:
        logs_query = (
            select(CrosspostLog)
            .where(CrosspostLog.platform == "reddit")
            .order_by(CrosspostLog.id.desc())
            .limit(1000)
        )
        log_rows = session.execute(logs_query).scalars().all()
        ids = [int(row.id) for row in log_rows]
        snapshot_rows = []
        if ids:
            snapshot_rows = session.execute(
                select(RedditMetricSnapshot)
                .where(RedditMetricSnapshot.crosspost_log_id.in_(ids))
                .order_by(RedditMetricSnapshot.collected_at.desc(), RedditMetricSnapshot.id.desc())
            ).scalars().all()

        logs = [{
            "id": int(row.id),
            "post_public_id": row.post_public_id,
            "status": row.status,
            "remote_id": row.remote_id,
            "url": row.url,
            "subreddit": _community_from_url(row.url),
        } for row in log_rows]
        snapshots = [{
            "id": int(row.id),
            "crosspost_log_id": int(row.crosspost_log_id),
            "subreddit": row.subreddit,
            "score": row.score,
            "num_comments": row.num_comments,
            "upvote_ratio": row.upvote_ratio,
            "collected_at": row.collected_at.isoformat() if row.collected_at else "",
        } for row in snapshot_rows]

    records = _row_metrics(logs, snapshots)
    communities = _aggregate_communities(records)
    return {
        "ok": True,
        "communities": communities[:50],
        "published_posts": sum(item["published_posts"] for item in communities),
        "metric_samples": sum(item["metric_samples"] for item in communities),
        "note": (
            "Рейтинг постепенно учитывает фактически опубликованные посты, score, комментарии и upvote ratio. "
            "Просмотры/показы здесь не выдумываются: стандартный Data API не отдаёт их в этом запросе."
        ),
    }


def community_learning_signals() -> dict[str, dict[str, Any]]:
    """Return small capped subreddit bonuses for the topic/relevance scout."""
    report = get_reddit_metrics_report()
    return {
        str(row["subreddit"]).lower(): {
            "learning_bonus": float(row["learning_bonus"]),
            "published_posts": int(row["published_posts"]),
            "metric_samples": int(row["metric_samples"]),
            "avg_score": row["avg_score"],
            "avg_comments": row["avg_comments"],
        }
        for row in report.get("communities", [])
    }


async def refresh_reddit_metrics(limit: int = 50) -> dict[str, Any]:
    """Refresh a bounded batch of known successful submissions, never publish."""
    limit = max(1, min(int(limit or 50), 100))
    adapter = ADAPTERS.get("reddit")
    if not isinstance(adapter, RedditAdapter) or not adapter.configured():
        raise RedditMetricsError("Сначала настрой Reddit OAuth с правом read и проверь доступ.")

    try:
        token = await adapter._access_token()
    except Exception as exc:  # noqa: BLE001
        raise RedditMetricsError("Reddit OAuth не прошёл. Проверь client secret и refresh token.") from exc

    with session_scope() as session:
        source_rows = session.execute(
            select(CrosspostLog)
            .where(
                CrosspostLog.platform == "reddit",
                CrosspostLog.status == "posted",
            )
            .order_by(CrosspostLog.id.desc())
            .limit(1000)
        ).scalars().all()
        targets = []
        for row in source_rows:
            post_id = _post_id(row.remote_id, row.url)
            subreddit = _community_from_url(row.url)
            if post_id and subreddit:
                targets.append({
                    "log_id": int(row.id),
                    "post_id": post_id,
                    "subreddit": subreddit,
                    "url": row.url or "",
                })
            if len(targets) >= limit:
                break

    if not targets:
        return {
            "ok": True,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "message": "Нет подтверждённых Reddit-публикаций с идентификатором и ссылкой для сбора метрик.",
            "report": get_reddit_metrics_report(),
        }

    updated = 0
    skipped = 0
    errors = 0
    now = datetime.now(UTC)
    headers = {
        "User-Agent": adapter._credentials()[-1],
        "Authorization": f"Bearer {token}",
    }

    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        for target in targets:
            try:
                response = await client.get(
                    "https://oauth.reddit.com/api/info",
                    params={"id": target["post_id"]},
                )
            except httpx.RequestError:
                errors += 1
                continue
            if response.status_code in (401, 403):
                raise RedditMetricsError("Reddit отклонил чтение метрик. Проверь OAuth scope read и доступ приложения.")
            if response.status_code == 429:
                errors += 1
                break
            if response.status_code >= 400:
                errors += 1
                continue
            try:
                payload = response.json() or {}
                children = ((payload.get("data") or {}).get("children") or [])
                post = children[0].get("data") if children else None
                if not isinstance(post, dict):
                    skipped += 1
                    continue
                score = int(post.get("score") or 0)
                num_comments = int(post.get("num_comments") or 0)
                ratio_raw = post.get("upvote_ratio")
                ratio = float(ratio_raw) if ratio_raw is not None else None
                ratio = max(0.0, min(1.0, ratio)) if ratio is not None else None
                subreddit = str(post.get("subreddit") or target["subreddit"]).lower()
                created_raw = post.get("created_utc")
                created_utc = float(created_raw) if created_raw is not None else None
            except (ValueError, TypeError, IndexError, AttributeError):
                errors += 1
                continue

            with session_scope() as session:
                previous = session.execute(
                    select(RedditMetricSnapshot)
                    .where(RedditMetricSnapshot.crosspost_log_id == target["log_id"])
                    .order_by(RedditMetricSnapshot.collected_at.desc(), RedditMetricSnapshot.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                # Keep a time series without letting repeated clicks flood the DB:
                # refresh the latest row inside a 15-minute window, otherwise append.
                if previous and previous.collected_at:
                    previous_time = previous.collected_at
                    if previous_time.tzinfo is None:
                        previous_time = previous_time.replace(tzinfo=UTC)
                    if now - previous_time < timedelta(minutes=15):
                        previous.score = score
                        previous.num_comments = num_comments
                        previous.upvote_ratio = ratio
                        previous.created_utc = created_utc
                        previous.subreddit = subreddit
                        previous.collected_at = now
                        updated += 1
                        continue
                session.add(RedditMetricSnapshot(
                    crosspost_log_id=target["log_id"],
                    subreddit=subreddit,
                    score=score,
                    num_comments=num_comments,
                    upvote_ratio=ratio,
                    created_utc=created_utc,
                    collected_at=now,
                ))
                updated += 1

    log.info("reddit metrics refreshed updated=%s skipped=%s errors=%s", updated, skipped, errors)
    return {
        "ok": True,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "message": (
            f"Обновлено: {updated}; пропущено: {skipped}; ошибок: {errors}. "
            "Неопределённые API-ответы не меняют статус публикации."
        ),
        "report": get_reddit_metrics_report(),
    }

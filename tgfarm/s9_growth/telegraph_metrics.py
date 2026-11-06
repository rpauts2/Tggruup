"""Telegraph page-view collection and learning snapshots.

The official getViews endpoint returns page views only. TgFarm keeps those
counts separate from Reddit votes/comments and Dzen impressions/opens.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.db.models import CrosspostLog, TelegraphMetricSnapshot
from tgfarm.db.session import session_scope

log = get_logger(__name__)
_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{3,100}$")
_REFRESH_COOLDOWN = timedelta(minutes=15)


class TelegraphMetricsError(RuntimeError):
    """Safe operator-facing error from Telegraph view collection."""


def _page_path(remote_id: str | None, url: str | None) -> str:
    """Resolve a Telegraph page path only from a known ID or canonical URL."""
    candidate = str(remote_id or "").strip()
    if candidate.startswith("https://"):
        url = candidate
        candidate = ""
    if candidate and _PATH_RE.fullmatch(candidate):
        return candidate
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in {"telegra.ph", "www.telegra.ph"}:
        return ""
    path = parsed.path.strip("/")
    if "/" in path or not _PATH_RE.fullmatch(path):
        return ""
    return path


def parse_views_response(payload: Any) -> int:
    """Validate an official Telegraph getViews response."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        reason = str((payload or {}).get("error") or "TELEGRAPH_API_ERROR")[:100] if isinstance(payload, dict) else "invalid response"
        raise TelegraphMetricsError(f"Telegraph getViews did not succeed ({reason}).")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TelegraphMetricsError("Telegraph getViews returned no result object.")
    try:
        views = int(result.get("views"))
    except (TypeError, ValueError) as exc:
        raise TelegraphMetricsError("Telegraph getViews returned an invalid views count.") from exc
    if views < 0:
        raise TelegraphMetricsError("Telegraph getViews returned a negative views count.")
    return views


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def build_article_metrics(logs: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join published delivery logs with the latest two official view snapshots."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        try:
            log_id = int(snapshot["crosspost_log_id"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(log_id, []).append(snapshot)

    articles: list[dict[str, Any]] = []
    for row in logs:
        if str(row.get("status") or "") != "posted":
            continue
        try:
            log_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        page_path = _page_path(row.get("remote_id"), row.get("url"))
        if not page_path:
            continue
        history = sorted(
            grouped.get(log_id, []),
            key=lambda item: (_timestamp(item.get("collected_at")), int(item.get("id") or 0)),
            reverse=True,
        )
        latest = history[0] if history else None
        previous = history[1] if len(history) > 1 else None
        current_views = int(latest["views"]) if latest is not None else None
        previous_views = int(previous["views"]) if previous is not None else None
        articles.append({
            "crosspost_log_id": log_id,
            "post_public_id": str(row.get("post_public_id") or ""),
            "page_path": page_path,
            "url": str(row.get("url") or f"https://telegra.ph/{page_path}"),
            "published_at": _timestamp(row.get("created_at")) or None,
            "current_views": current_views,
            "previous_views": previous_views,
            "views_delta": current_views - previous_views if current_views is not None and previous_views is not None else None,
            "collected_at": _timestamp(latest.get("collected_at")) if latest else None,
            "metric_samples": len(history),
        })
    articles.sort(key=lambda item: item["published_at"] or "", reverse=True)
    return articles


def _load_metric_rows(limit: int = 1000) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with session_scope() as session:
        logs = session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.platform == "telegraph", CrosspostLog.status == "posted")
            .order_by(CrosspostLog.id.desc())
            .limit(max(1, min(int(limit), 2000)))
        ).scalars().all()
        ids = [int(row.id) for row in logs]
        snapshots = []
        if ids:
            snapshots = session.execute(
                select(TelegraphMetricSnapshot)
                .where(TelegraphMetricSnapshot.crosspost_log_id.in_(ids))
                .order_by(TelegraphMetricSnapshot.collected_at.desc(), TelegraphMetricSnapshot.id.desc())
            ).scalars().all()
        log_records = [{
            "id": int(row.id), "post_public_id": row.post_public_id, "status": row.status,
            "remote_id": row.remote_id, "url": row.url, "created_at": row.created_at,
        } for row in logs]
        metric_records = [{
            "id": int(row.id), "crosspost_log_id": int(row.crosspost_log_id),
            "views": int(row.views or 0), "collected_at": row.collected_at,
        } for row in snapshots]
    return log_records, metric_records


def get_telegraph_metrics_report() -> dict[str, Any]:
    """Return actual page views and deltas, without inventing engagement."""
    logs, snapshots = _load_metric_rows()
    articles = build_article_metrics(logs, snapshots)
    sampled = [row for row in articles if row["current_views"] is not None]
    return {
        "ok": True,
        "articles": articles[:200],
        "article_count": len(articles),
        "metric_samples": len(sampled),
        "views_total": sum(int(row["current_views"] or 0) for row in sampled),
        "views_delta_total": sum(int(row["views_delta"] or 0) for row in articles if row["views_delta"] is not None),
        "note": "Telegraph API возвращает только просмотры страницы. Здесь нет выдуманных лайков, комментариев, дочитываний или показов.",
    }


async def refresh_telegraph_metrics(limit: int = 50) -> dict[str, Any]:
    """Refresh a bounded batch of published Telegraph pages; never creates posts."""
    limit = max(1, min(int(limit or 50), 100))
    if not str(get_settings().telegraph_access_token or "").strip():
        raise TelegraphMetricsError("Сначала подключи TELEGRAPH_ACCESS_TOKEN в карточке Telegraph.")

    logs, snapshots = _load_metric_rows(limit=2000)
    articles = build_article_metrics(logs, snapshots)
    latest_by_log = {
        int(row["crosspost_log_id"]): row
        # Walk oldest first so repeated log IDs retain the newest observation.
        for row in sorted(
            snapshots, key=lambda item: (_timestamp(item.get("collected_at")), int(item.get("id") or 0))
        )
    }
    now = datetime.now(UTC)
    targets: list[dict[str, Any]] = []
    skipped = 0
    for row in articles:
        previous = latest_by_log.get(int(row["crosspost_log_id"]))
        collected = previous.get("collected_at") if previous else None
        if isinstance(collected, str):
            try:
                collected = datetime.fromisoformat(collected.replace("Z", "+00:00"))
            except ValueError:
                collected = None
        if collected is not None:
            collected = collected.replace(tzinfo=UTC) if collected.tzinfo is None else collected.astimezone(UTC)
            if now - collected < _REFRESH_COOLDOWN:
                skipped += 1
                continue
        targets.append(row)
        if len(targets) >= limit:
            break

    if not targets:
        return {
            "ok": True, "updated": 0, "skipped": skipped, "errors": 0,
            "message": "Нет Telegraph-страниц, которым сейчас нужен новый снимок просмотров.",
            "report": get_telegraph_metrics_report(),
        }

    updated = errors = 0
    headers = {"User-Agent": "TgFarm/1.0 (+https://github.com/rpauts2/Tggruup)"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        for target in targets:
            path = str(target["page_path"])
            try:
                response = await client.get(f"https://api.telegra.ph/getViews/{quote(path, safe='-_')}")
                response.raise_for_status()
                views = parse_views_response(response.json())
            except TelegraphMetricsError:
                errors += 1
                continue
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    errors += 1
                    break
                if exc.response.status_code in {401, 403}:
                    raise TelegraphMetricsError("Telegraph отклонил запрос getViews.") from exc
                errors += 1
                continue
            except (httpx.RequestError, ValueError, TypeError):
                errors += 1
                continue

            with session_scope() as session:
                previous = session.execute(
                    select(TelegraphMetricSnapshot)
                    .where(TelegraphMetricSnapshot.crosspost_log_id == target["crosspost_log_id"])
                    .order_by(TelegraphMetricSnapshot.collected_at.desc(), TelegraphMetricSnapshot.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if previous and previous.collected_at:
                    stamp = previous.collected_at
                    stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
                    if now - stamp < _REFRESH_COOLDOWN:
                        skipped += 1
                        continue
                session.add(TelegraphMetricSnapshot(
                    crosspost_log_id=target["crosspost_log_id"], views=views, collected_at=now,
                ))
                updated += 1

    log.info("telegraph metrics refreshed updated=%s skipped=%s errors=%s", updated, skipped, errors)
    return {
        "ok": True, "updated": updated, "skipped": skipped, "errors": errors,
        "message": f"Обновлено: {updated}; пропущено: {skipped}; ошибок: {errors}.",
        "report": get_telegraph_metrics_report(),
    }

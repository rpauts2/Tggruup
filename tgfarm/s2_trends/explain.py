"""Explainable trend signals for S2 — read-only and ranking-neutral.

`Cluster.heat` is a single number produced by `_score_pass`. An operator looking
at a hot topic cannot tell whether it is hot because four outlets picked it up
in the last hour or because one high-authority source published it yesterday.
This module answers that question with named, individually checkable signals:

    signals = cluster_signals("a1b2c3d4e5f6")   # numbers, machine readable
    human = explain_cluster("a1b2c3d4e5f6")     # 2-4 sentences with numbers

Both functions only read the database. They never call the network, never
write, and are not imported by `_score_pass`, `ready_clusters` or
`promote_ready` — the ranking path stays exactly as it was. The derived
`priority` value is a *shadow* score for the operator's eye, not a queue
decision; `affects_ranking` is always False in the result for that reason.

Signal definitions
------------------
items        how many raw items belong to the cluster, how many of those are
             not duplicates, how many distinct sources, and the time window.
velocity     arrival rate, elements per hour since the first element. Uses
             non-duplicate items so one wire story repeated five times does not
             look like a trend.
freshness    hours since the newest element, plus an exponential decay in 0..1.
saturation   how many posts have already been published for this cluster, in
             how many channels. Answers "are we about to repeat ourselves?".
recurrence   how many *other* clusters in the last 7 days cover a similar topic.
             Computed from cluster centroids of `RawItem.embedding` via
             `tgfarm.db.vector`; when the blobs are missing, empty or on a
             different dimension, it degrades to normalised headline overlap and
             says so (`method`, `fallback`, `note`).

Why the process-local vector index is not used: `get_index()` is empty in a
fresh process (a CLI call or a UI request), and it stores item ids, not cluster
centroids. Reading the blobs and normalising them with the same
`tgfarm.db.vector` helpers keeps this function deterministic and independent of
process state, which is also what makes it testable.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select

from tgfarm.core.text import content_tokens
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.models import Post
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import l2_normalize, unpack

# Mirrors `s2_trends.CLUSTER_WINDOW_HOURS`, duplicated on purpose: the package
# `__init__` re-exports this module, so importing it back would be circular.
SIGNAL_WINDOW_HOURS = 24
STALE_AFTER_HOURS = 12
RECENT_WINDOW_HOURS = 6

RECURRENCE_WINDOW_DAYS = 7
# Topic-level similarity, deliberately far below the dedup floor (0.95): two
# clusters covering the same recurring theme are not the same story.
RECURRENCE_COSINE_FLOOR = 0.50
TITLE_JACCARD_FLOOR = 0.34
MIN_SHARED_TITLE_TOKENS = 2
MAX_RECURRENCE_CLUSTERS = 500

# A cluster this fast is treated as "saturated speed" for the shadow score.
VELOCITY_FULL_ITEMS_PER_HOUR = 2.0
# Four items in five minutes must not divide by ~0 hours and report 400/h; the
# floor keeps a burst readable without pretending the cluster has been running
# for a quarter of an hour.
MIN_VELOCITY_SPAN_HOURS = 0.25
SPREAD_FULL_SOURCES = 4.0
# Same 4h constant as heat's recency term, so the two numbers are comparable.
RECENCY_HALF_LIFE_HOURS = 4.0

# Shadow priority weights. Documented rather than tuned: the point of the number
# is that an operator can add it up by hand from the components we return.
W_PRIORITY_VELOCITY = 0.30
W_PRIORITY_FRESHNESS = 0.35
W_PRIORITY_SPREAD = 0.15
W_PRIORITY_NOVELTY = 0.10
W_PRIORITY_SATURATION = 0.10

PRIORITY_HIGH = 0.60
PRIORITY_MEDIUM = 0.35

PUBLISHED_STATUSES = ("published",)
IN_FLIGHT_STATUSES = ("scheduled", "publishing", "publish_unknown")


# --------------------------------------------------------------------- helpers
def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the rest of S2 normalises the same way."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return _as_utc(now) or datetime.now(UTC)


def _hours_since(moment: datetime | None, now: datetime) -> float | None:
    moment = _as_utc(moment)
    if moment is None:
        return None
    return round(max(0.0, (now - moment).total_seconds() / 3600.0), 4)


def _iso(value: datetime | None) -> str | None:
    moment = _as_utc(value)
    return moment.isoformat() if moment else None


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _safe_vector(blob) -> np.ndarray | None:
    """Decode a stored embedding, tolerating legacy or corrupt blobs."""
    try:
        vec = unpack(blob)
    except (TypeError, ValueError):
        return None
    if vec is None or vec.size == 0:
        return None
    if not np.isfinite(vec).all():
        return None
    return vec


def _mean_vector(vectors: list[np.ndarray]) -> np.ndarray | None:
    if not vectors:
        return None
    if any(vec.shape != vectors[0].shape for vec in vectors):
        return None
    stack = np.vstack(vectors)
    return l2_normalize(stack.sum(axis=0) / len(stack))


def _cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None or a.shape != b.shape:
        return None
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return None
    return float(np.dot(a, b) / (norm_a * norm_b))


def _title_tokens(title: str | None) -> set[str]:
    return {token.lower() for token in content_tokens(title or "") if len(token) > 1}


def _title_similarity(a_tokens: set[str], b_tokens: set[str]) -> float:
    """Jaccard over content words. Symmetric, unlike the IDF share used for
    clustering, because recurrence has no anchor document."""
    if not a_tokens or not b_tokens:
        return 0.0
    shared = a_tokens & b_tokens
    if len(shared) < MIN_SHARED_TITLE_TOKENS:
        return 0.0
    return len(shared) / len(a_tokens | b_tokens)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ----------------------------------------------------------------- cache lookups
def _resolve(session, ref) -> ClusterRow | None:
    """Accept either the numeric id or the public id — the UI has both."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return session.get(ClusterRow, ref)
    if not isinstance(ref, str):
        return None
    candidate = ref.strip()
    if not candidate:
        return None
    row = session.execute(
        select(ClusterRow).where(ClusterRow.public_id == candidate)
    ).scalar_one_or_none()
    if row is not None:
        return row
    if candidate.isdigit():
        return session.get(ClusterRow, int(candidate))
    return None


def _member_rows(session, cluster_id: int):
    return session.execute(
        select(
            RawItemRow.source_id,
            RawItemRow.published_at,
            RawItemRow.fetched_at,
            RawItemRow.duplicate_of,
            RawItemRow.embedding,
        ).where(RawItemRow.cluster_id == cluster_id)
    ).all()


def _item_moment(published_at, fetched_at) -> datetime | None:
    return _as_utc(published_at) or _as_utc(fetched_at)


def _post_rows(session, public_id: str):
    return session.execute(
        select(Post.status, Post.channel_id, Post.published_at, Post.created_at).where(
            Post.cluster_public_id == public_id
        )
    ).all()


def _cluster_centroids(session, cluster_ids: list[int]) -> dict[int, np.ndarray]:
    """One query for every candidate cluster instead of one query per cluster."""
    if not cluster_ids:
        return {}
    grouped: dict[int, list[np.ndarray]] = {}
    rows = session.execute(
        select(RawItemRow.cluster_id, RawItemRow.embedding).where(
            RawItemRow.cluster_id.in_(cluster_ids), RawItemRow.embedding.isnot(None)
        )
    ).all()
    for cluster_id, blob in rows:
        vec = _safe_vector(blob)
        if vec is not None and cluster_id is not None:
            grouped.setdefault(int(cluster_id), []).append(vec)
    out: dict[int, np.ndarray] = {}
    for cluster_id, vectors in grouped.items():
        centroid = _mean_vector(vectors)
        if centroid is not None:
            out[cluster_id] = centroid
    return out


# ---------------------------------------------------------------- recurrence
def _recurrence(session, cluster: ClusterRow, centroid: np.ndarray | None, now: datetime) -> dict:
    """Clusters with a similar topic in the last `RECURRENCE_WINDOW_DAYS` days."""
    cutoff = now - timedelta(days=RECURRENCE_WINDOW_DAYS)
    rows = session.execute(
        select(
            ClusterRow.id,
            ClusterRow.public_id,
            ClusterRow.label,
            ClusterRow.created_at,
            ClusterRow.last_seen_at,
        )
        .where(
            ClusterRow.id != cluster.id,
            # Legacy rows can carry a NULL created_at; last_seen_at is the fallback.
            func.coalesce(ClusterRow.created_at, ClusterRow.last_seen_at) >= cutoff,
        )
        .order_by(ClusterRow.last_seen_at.desc())
        .limit(MAX_RECURRENCE_CLUSTERS)
    ).all()

    target_tokens = _title_tokens(cluster.label)
    candidates = [row for row in rows if row[1] != cluster.public_id]
    centroids = _cluster_centroids(session, [int(row[0]) for row in candidates])

    similar: list[dict] = []
    embedding_checks = 0
    title_checks = 0
    for row in candidates:
        cluster_id, public_id, label, _created_at, last_seen_at = row
        score = _cosine(centroid, centroids.get(int(cluster_id)))
        method = "embedding"
        if score is None:
            # No usable vector on either side — fall back to the headline.
            score = _title_similarity(target_tokens, _title_tokens(label))
            method = "title"

        # Count comparisons, not hits: a run where every candidate lacked a
        # vector must report `title`, even if nothing crossed the threshold.
        if method == "embedding":
            embedding_checks += 1
            if score < RECURRENCE_COSINE_FLOOR:
                continue
        else:
            title_checks += 1
            if score < TITLE_JACCARD_FLOOR:
                continue

        similar.append(
            {
                "public_id": public_id,
                "label": _truncate(label or "", 90),
                "similarity": round(score, 4),
                "age_hours": _hours_since(last_seen_at, now),
                "method": method,
            }
        )

    similar.sort(key=lambda item: item["similarity"], reverse=True)

    if centroid is None:
        method = "title"
        fallback = True
        note = (
            "Эмбеддинги кластера недоступны или пусты: повторяемость посчитана "
            "по нормализованным заголовкам."
        )
    elif title_checks and not embedding_checks:
        method = "title"
        fallback = True
        note = "У кластеров-кандидатов нет эмбеддингов: сравнение по заголовкам."
    elif title_checks:
        method = "mixed"
        fallback = True
        note = (
            f"Эмбеддинги есть у части кандидатов: {embedding_checks} сравнений по векторам, "
            f"{title_checks} по заголовкам."
        )
    else:
        method = "embedding"
        fallback = False
        note = "Сравнение по центроидам эмбеддингов элементов кластеров."

    return {
        "window_days": RECURRENCE_WINDOW_DAYS,
        "count": len(similar),
        "method": method,
        "fallback": fallback,
        "note": note,
        "thresholds": {
            "embedding": RECURRENCE_COSINE_FLOOR,
            "title": TITLE_JACCARD_FLOOR,
        },
        "candidates_checked": len(candidates),
        "similar": similar[:5],
    }


# ------------------------------------------------------------------- signals
def _empty_signals(ref, reason: str) -> dict:
    return {
        "ok": False,
        "reason": reason,
        "ref": ref if isinstance(ref, (int, str)) else repr(ref),
        "cluster": None,
        "items": {"total": 0, "unique": 0, "duplicates": 0, "sources": 0},
        "window": {
            "span_hours": 0.0,
            "first_at": None,
            "last_at": None,
            "items_in_window": 0,
            "declared_window_hours": SIGNAL_WINDOW_HOURS,
        },
        "velocity": {
            "items_per_hour": 0.0,
            "recent_items": 0,
            "recent_sources": 0,
            "recent_window_hours": RECENT_WINDOW_HOURS,
            "basis": "deduplicated",
        },
        "freshness": {"hours_since_last": None, "decay": 0.0, "stale": True},
        "saturation": {
            "published_posts": 0,
            "posts_total": 0,
            "in_flight": 0,
            "channels": 0,
            "last_published_at": None,
            "hours_since_last_published": None,
            "level": "none",
        },
        "recurrence": {
            "window_days": RECURRENCE_WINDOW_DAYS,
            "count": 0,
            "method": "none",
            "fallback": False,
            "note": "Кластер не найден, сравнивать нечего.",
            "thresholds": None,
            "candidates_checked": 0,
            "similar": [],
        },
        "priority": 0.0,
        "priority_band": "low",
        "priority_components": {},
        "affects_ranking": False,
    }


def cluster_signals(cluster_id_or_public_id: int | str, *, now: datetime | None = None) -> dict:
    """Named signals for one cluster. Never raises on a missing cluster.

    Returns a dict with `ok` and, when the cluster exists, the groups `items`,
    `window`, `velocity`, `freshness`, `saturation`, `recurrence`, plus a shadow
    `priority` in 0..1 with its components spelled out. `affects_ranking` is
    always False: nothing here feeds `Cluster.heat`.
    """
    moment = _now(now)

    with session_scope() as session:
        cluster = _resolve(session, cluster_id_or_public_id)
        if cluster is None:
            return _empty_signals(cluster_id_or_public_id, "not_found")

        members = _member_rows(session, cluster.id)
        posts = _post_rows(session, cluster.public_id)

        public_id = cluster.public_id
        cluster_id = int(cluster.id)
        label = cluster.label or ""
        stored_heat = cluster.heat
        status = cluster.status
        cluster_created = cluster.created_at
        cluster_last_seen = cluster.last_seen_at
        item_count_column = cluster.item_count
        niche = cluster.niche

        # --- items and window ------------------------------------------------
        moments = [
            value
            for value in (_item_moment(row[1], row[2]) for row in members)
            if value is not None
        ]
        unique_rows = [row for row in members if row[3] is None]
        unique_moments = [
            value
            for value in (_item_moment(row[1], row[2]) for row in unique_rows)
            if value is not None
        ]

        first_at = min(moments) if moments else _as_utc(cluster_created)
        last_at = max(moments) if moments else _as_utc(cluster_last_seen)
        span_hours = 0.0
        if first_at is not None and last_at is not None:
            span_hours = max(0.0, (last_at - first_at).total_seconds() / 3600.0)

        sources = {row[0] for row in members}
        window_start = moment - timedelta(hours=SIGNAL_WINDOW_HOURS)
        items_in_window = sum(1 for value in moments if value >= window_start)

        # --- velocity: elements per hour since the first element -------------
        if len(unique_moments) >= 2:
            items_per_hour = len(unique_moments) / max(span_hours, MIN_VELOCITY_SPAN_HOURS)
        else:
            items_per_hour = 0.0

        recent_start = moment - timedelta(hours=RECENT_WINDOW_HOURS)
        recent_rows = [row for row in unique_rows if (_item_moment(row[1], row[2]) or moment) >= recent_start]

        # --- freshness --------------------------------------------------------
        hours_since_last = _hours_since(last_at, moment)
        decay = 0.0 if hours_since_last is None else math.exp(-hours_since_last / RECENCY_HALF_LIFE_HOURS)

        # --- saturation: posts already published for this cluster -------------
        published = [row for row in posts if row[0] in PUBLISHED_STATUSES]
        in_flight = [row for row in posts if row[0] in IN_FLIGHT_STATUSES]
        published_channels = {row[1] for row in published}
        published_moments = [
            value for value in (_as_utc(row[2]) or _as_utc(row[3]) for row in published) if value
        ]
        last_published_at = max(published_moments) if published_moments else None
        if not published:
            level = "none"
        elif len(published) == 1:
            level = "light"
        else:
            level = "saturated"

        # --- recurrence (embedding-first, headline fallback) ------------------
        centroid = _mean_vector(
            [vec for vec in (_safe_vector(row[4]) for row in members) if vec is not None]
        )
        recurrence = _recurrence(session, cluster, centroid, moment)

    signals = {
        "ok": True,
        "reason": None,
        "ref": public_id,
        "cluster": {
            "id": cluster_id,
            "public_id": public_id,
            "label": label,
            "status": status,
            "niche": niche,
            "stored_heat": round(float(stored_heat or 0.0), 4),
            "item_count_column": int(item_count_column or 0),
            "created_at": _iso(cluster_created),
            "last_seen_at": _iso(cluster_last_seen),
        },
        "items": {
            "total": len(members),
            "unique": len(unique_rows),
            "duplicates": len(members) - len(unique_rows),
            "sources": len(sources),
        },
        "window": {
            "span_hours": round(span_hours, 4),
            "first_at": _iso(first_at),
            "last_at": _iso(last_at),
            "items_in_window": items_in_window,
            "declared_window_hours": SIGNAL_WINDOW_HOURS,
        },
        "velocity": {
            "items_per_hour": round(items_per_hour, 4),
            "recent_items": len(recent_rows),
            "recent_sources": len({row[0] for row in recent_rows}),
            "recent_window_hours": RECENT_WINDOW_HOURS,
            "basis": "deduplicated",
        },
        "freshness": {
            "hours_since_last": hours_since_last,
            "decay": round(decay, 4),
            "stale": hours_since_last is None or hours_since_last > STALE_AFTER_HOURS,
        },
        "saturation": {
            "published_posts": len(published),
            "posts_total": len(posts),
            "in_flight": len(in_flight),
            "channels": len(published_channels),
            "last_published_at": _iso(last_published_at),
            "hours_since_last_published": _hours_since(last_published_at, moment),
            "level": level,
        },
        "recurrence": recurrence,
    }
    priority, components = _shadow_priority(signals)
    signals["priority"] = priority
    signals["priority_band"] = _band(priority)
    signals["priority_components"] = components
    signals["affects_ranking"] = False
    return signals


def _shadow_priority(signals: dict) -> tuple[float, dict]:
    """Add up the documented weights so the operator can audit the number."""
    items = signals["items"]
    velocity_per_hour = signals["velocity"]["items_per_hour"]
    hours = signals["freshness"]["hours_since_last"]

    velocity_component = min(1.0, velocity_per_hour / VELOCITY_FULL_ITEMS_PER_HOUR)
    recency_component = 0.0 if hours is None else math.exp(-hours / RECENCY_HALF_LIFE_HOURS)
    spread_component = min(1.0, items["sources"] / SPREAD_FULL_SOURCES)
    novelty_component = 1.0 / (1.0 + 0.5 * signals["recurrence"]["count"])
    saturation_component = 1.0 / (1.0 + signals["saturation"]["published_posts"])

    score = (
        W_PRIORITY_VELOCITY * velocity_component
        + W_PRIORITY_FRESHNESS * recency_component
        + W_PRIORITY_SPREAD * spread_component
        + W_PRIORITY_NOVELTY * novelty_component
        + W_PRIORITY_SATURATION * saturation_component
    )
    components = {
        "velocity": round(velocity_component, 4),
        "freshness": round(recency_component, 4),
        "spread": round(spread_component, 4),
        "novelty": round(novelty_component, 4),
        "saturation": round(saturation_component, 4),
        "weights": {
            "velocity": W_PRIORITY_VELOCITY,
            "freshness": W_PRIORITY_FRESHNESS,
            "spread": W_PRIORITY_SPREAD,
            "novelty": W_PRIORITY_NOVELTY,
            "saturation": W_PRIORITY_SATURATION,
        },
    }
    return round(min(1.0, max(0.0, score)), 4), components


def _band(priority: float) -> str:
    if priority >= PRIORITY_HIGH:
        return "high"
    if priority >= PRIORITY_MEDIUM:
        return "medium"
    return "low"


# ----------------------------------------------------------------- explanation
def _freshness_reason(signals: dict) -> dict:
    freshness = signals["freshness"]
    hours = freshness["hours_since_last"]
    if hours is None:
        text = "Свежесть: время публикации неизвестно — 0 элементов с датой."
        value = None
    else:
        text = (
            f"Свежесть: последний элемент {hours:.1f} ч назад, "
            f"затухание {freshness['decay']:.2f} из 1.00."
        )
        value = hours
    return {"code": "freshness", "text": text, "value": value, "unit": "hours"}


def _saturation_reason(signals: dict) -> dict:
    saturation = signals["saturation"]
    text = (
        f"Насыщенность: опубликовано {saturation['published_posts']} "
        f"{_plural(saturation['published_posts'], 'пост', 'поста', 'постов')}, "
        f"задействовано каналов: {saturation['channels']} "
        f"(в очереди ещё {saturation['in_flight']})."
    )
    return {
        "code": "saturation",
        "text": text,
        "value": saturation["published_posts"],
        "unit": "posts",
    }


def _velocity_reason(signals: dict) -> dict | None:
    items = signals["items"]
    window = signals["window"]
    velocity = signals["velocity"]
    if items["unique"] < 2:
        return None
    text = (
        f"Скорость: {items['unique']} "
        f"{_plural(items['unique'], 'элемент', 'элемента', 'элементов')} "
        f"за {window['span_hours']:.1f} ч — {velocity['items_per_hour']:.2f} эл./ч, "
        f"из них {velocity['recent_items']} за последние {velocity['recent_window_hours']} ч."
    )
    return {
        "code": "velocity",
        "text": text,
        "value": velocity["items_per_hour"],
        "unit": "items/hour",
    }


def _spread_reason(signals: dict) -> dict | None:
    items = signals["items"]
    if items["sources"] < 2:
        return None
    total = items["total"]
    sources_word = _plural(
        items["sources"], "независимый источник", "независимых источника", "независимых источников"
    )
    text = (
        f"Охват: {items['sources']} {sources_word} "
        f"на {total} {_plural(total, 'элемент', 'элемента', 'элементов')}, "
        f"дублей отброшено: {items['duplicates']}."
    )
    return {"code": "spread", "text": text, "value": items["sources"], "unit": "sources"}


def _recurrence_reason(signals: dict) -> dict | None:
    recurrence = signals["recurrence"]
    if recurrence["count"] < 1:
        return None
    nearest = recurrence["similar"][0]
    method = "по эмбеддингам" if recurrence["method"] == "embedding" else "по заголовкам"
    text = (
        f"Повторяемость: {recurrence['count']} "
        f"{_plural(recurrence['count'], 'похожая тема', 'похожие темы', 'похожих тем')} "
        f"за {recurrence['window_days']} дней ({method}), "
        f"ближайшая — «{nearest['label']}» со сходством {nearest['similarity']:.2f}."
    )
    return {"code": "recurrence", "text": text, "value": recurrence["count"], "unit": "clusters"}


# Reasons are picked in this order and rendered in `_DISPLAY_ORDER`. Repetition
# outranks source spread: "we covered this three times this week" changes the
# operator's decision, while the source count is already in the summary.
_SELECTION_ORDER = ("velocity", "recurrence", "spread")
_DISPLAY_ORDER = {"velocity": 0, "spread": 1, "freshness": 2, "saturation": 3, "recurrence": 4}


def explain_cluster(cluster_public_id: str, *, now: datetime | None = None) -> dict:
    """Why this cluster is considered hot: 2–4 reasons, each with numbers.

    Same data as `cluster_signals`, rendered for a human. A missing cluster is a
    normal answer (`ok=False`, `reason="not_found"`), never an exception — the UI
    asks for clusters that S3 may have consumed and reaped in the meantime.
    """
    signals = cluster_signals(cluster_public_id, now=now)

    if not signals["ok"]:
        ref = signals["ref"]
        text = f"Кластер «{ref}» не найден в базе: 0 элементов, 0 опубликованных постов."
        return {
            "ok": False,
            "reason": signals["reason"],
            "public_id": ref if isinstance(ref, str) else None,
            "label": "",
            "summary": text,
            "reasons": [{"code": "missing", "text": text, "value": 0, "unit": "items"}],
            "signals": signals,
            "notes": ["Проверьте public_id: кластер мог быть удалён или ещё не создан."],
            "affects_ranking": False,
        }

    cluster = signals["cluster"]
    text_reasons = [
        reason
        for reason in (
            _velocity_reason(signals),
            _spread_reason(signals),
            _freshness_reason(signals),
            _saturation_reason(signals),
            _recurrence_reason(signals),
        )
        if reason is not None
    ]

    # Always keep freshness and saturation: they answer the two questions an
    # operator asks first ("is it still fresh?" and "did we already cover it?").
    always = {"freshness", "saturation"}
    chosen = [reason for reason in text_reasons if reason["code"] in always]
    for code in _SELECTION_ORDER:
        for reason in text_reasons:
            if reason["code"] == code and len(chosen) < 4:
                chosen.append(reason)
    chosen.sort(key=lambda reason: _DISPLAY_ORDER.get(reason["code"], 99))

    items = signals["items"]
    window = signals["window"]
    freshness = signals["freshness"]
    saturation = signals["saturation"]
    last_seen = (
        f"{freshness['hours_since_last']:.1f} ч назад"
        if freshness["hours_since_last"] is not None
        else "время неизвестно"
    )
    summary = (
        f"«{_truncate(cluster['label'], 80)}»: {items['unique']} "
        f"{_plural(items['unique'], 'элемент', 'элемента', 'элементов')} "
        f"из {items['sources']} {_plural(items['sources'], 'источника', 'источников', 'источников')} "
        f"за {window['span_hours']:.1f} ч, последний — {last_seen}; "
        f"опубликовано {saturation['published_posts']} "
        f"{_plural(saturation['published_posts'], 'пост', 'поста', 'постов')}. "
        f"Теневой приоритет {signals['priority']:.2f} ({signals['priority_band']})."
    )

    notes = [signals["recurrence"]["note"]]
    if signals["freshness"]["stale"]:
        notes.append(
            f"Кластер старше {STALE_AFTER_HOURS} ч без новых элементов: "
            f"приоритет понижен, heat не пересчитывается."
        )

    return {
        "ok": True,
        "reason": None,
        "public_id": cluster["public_id"],
        "label": cluster["label"],
        "summary": summary,
        "reasons": chosen,
        "signals": signals,
        "notes": [note for note in notes if note],
        "affects_ranking": False,
    }

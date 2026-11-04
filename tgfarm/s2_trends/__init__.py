"""S2 — trend detection.

Three steps, each cheap enough to run on every cycle:

1. `deduplicate` — near-identical items from different outlets collapse via
   simhash (cheap) and embedding cosine (accurate).
2. `cluster`     — surviving items are greedily assigned to the nearest existing
   cluster above a similarity floor, otherwise they start one. A cluster is the
   farm's unit of "a story".
3. `score_heat`  — every cluster gets a 0..1 heat value from five signals. Only
   clusters above `HEAT_THRESHOLD` are allowed into content generation.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.settings import get_settings
from tgfarm.core.text import (
    SIMHASH_MAX_DISTANCE,
    content_tokens,
    hamming,
    is_near_duplicate,
    title_key,
)
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.models import Post
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import get_index, unpack

log = get_logger(__name__)

CLUSTER_WINDOW_HOURS = 24
STALE_AFTER_HOURS = 12

# "Same story" for news headlines is a lexical question: two outlets covering one
# event share the rare tokens (proper nouns, figures, version numbers) and almost
# nothing else. Measured on the local hash embedder, cosine for genuinely related
# headlines lands at 0.22–0.36 while random pairs average 0.36 — cosine alone
# cannot separate them at any usable threshold.
#
# So the clusterer scores IDF-weighted lexical overlap, using cosine only as a
# supporting signal. IDF is what makes it work: "OpenAI" and "19%" carry the
# weight, "выпустила" and "новости" carry none.
MIN_IDF_OVERLAP = 0.50
MIN_COSINE_SUPPORT = 0.22
IDF_FLOOR = 1.15


def _build_idf(rows) -> dict[str, float]:
    """Inverse document frequency over the titles in the current window."""
    from collections import Counter

    from tgfarm.core.text import content_tokens

    total = max(1, len(rows))
    document_freq: Counter[str] = Counter()
    for row in rows:
        document_freq.update(set(content_tokens(row.title or "")))

    idf: dict[str, float] = {}
    for token, freq in document_freq.items():
        idf[token] = max(IDF_FLOOR, (total / (1 + freq)) ** 0.5)
    return idf


def idf_weighted_overlap(
    a_tokens: set[str],
    b_tokens: set[str],
    idf: dict[str, float],
) -> float:
    """Share of A's total token weight that B also carries."""
    if not a_tokens or not b_tokens:
        return 0.0
    total = sum(idf.get(t, IDF_FLOOR) for t in a_tokens)
    if total <= 0:
        return 0.0
    shared = sum(idf.get(t, IDF_FLOOR) for t in (a_tokens & b_tokens))
    return shared / total


def same_story_score(
    a_vec,
    b_vec,
    a_tokens: set[str],
    b_tokens: set[str],
    idf: dict[str, float],
) -> float:
    """Combined 0..1 similarity. Returns 0 when neither signal is convincing."""
    cosine = float(np.dot(a_vec, b_vec)) if a_vec is not None and b_vec is not None else 0.0
    lexical = idf_weighted_overlap(a_tokens, b_tokens, idf)
    if lexical < MIN_IDF_OVERLAP:
        return 0.0
    if cosine < MIN_COSINE_SUPPORT:
        return 0.0
    return lexical

# heat weights — sum to 1.0. Tuned so that a single-source, fresh story needs a
# strong source weight, while a multi-source story with velocity passes easily.
W_SOURCE = 0.35
W_VELOCITY = 0.25
W_RECENCY = 0.20
W_CROSS = 0.10
W_DEFICIT = 0.10


# ----------------------------------------------------------------- deduplicate
def _dedup_pass() -> int:
    """Mark exact/near duplicates among recent items. Returns rows flagged."""
    with session_scope() as session:
        cutoff = datetime.now(UTC) - timedelta(hours=CLUSTER_WINDOW_HOURS)
        rows = session.execute(
            select(RawItemRow).where(
                RawItemRow.fetched_at >= cutoff, RawItemRow.duplicate_of.is_(None)
            ).order_by(RawItemRow.fetched_at.desc())
        ).scalars().all()

        seen_fp: dict[int, int] = {}
        seen_titles: dict[str, int] = {}
        index = get_index()
        flagged = 0

        for row in rows:
            # cheap pass: simhash first
            match_id = None
            if row.simhash:
                for fp, other_id in seen_fp.items():
                    if hamming(row.simhash, fp) <= SIMHASH_MAX_DISTANCE:
                        match_id = other_id
                        break

            if match_id is None:
                key = title_key(row.title)
                match_id = seen_titles.get(key)

            if match_id is None and row.embedding:
                vec = unpack(row.embedding)
                hits = index.search(vec, top_k=3, min_score=0.95, exclude={row.id})
                if hits:
                    match_id = hits[0][0]

            if match_id is not None:
                row.duplicate_of = match_id
                flagged += 1
            else:
                seen_titles[title_key(row.title)] = row.id
                if row.simhash:
                    seen_fp[row.simhash] = row.id

        return flagged


# -------------------------------------------------------------------- cluster
def _cluster_pass() -> int:
    cutoff = datetime.now(UTC) - timedelta(hours=CLUSTER_WINDOW_HOURS)
    created = 0

    with session_scope() as session:
        get_index()
        rows = session.execute(
            select(RawItemRow)
            .where(RawItemRow.fetched_at >= cutoff, RawItemRow.cluster_id.is_(None))
            .order_by(RawItemRow.fetched_at.asc())
        ).scalars().all()

        idf = _build_idf(rows)

        # cluster_id -> centroid (running mean), token set and niche set
        centroids: dict[int, np.ndarray] = {}
        cluster_sizes: dict[int, int] = {}
        cluster_tokens: dict[int, set[str]] = {}
        cluster_niches: dict[int, set[str]] = {}
        existing = session.execute(
            select(ClusterRow).where(ClusterRow.status.in_(("forming", "ready")))
        ).scalars().all()
        for cluster in existing:
            members = session.execute(
                select(
                    RawItemRow.embedding, RawItemRow.title, RawItemRow.niche_hint
                ).where(RawItemRow.cluster_id == cluster.id)
            ).all()
            vectors = [unpack(row[0]) for row in members if row[0]]
            tokens: set[str] = set()
            niches: set[str] = set()
            for _, title, hints in members:
                tokens |= set(content_tokens(title or ""))
                niches.update(hints or [])
            if vectors:
                centroids[cluster.id] = _mean(vectors)
                cluster_sizes[cluster.id] = len(vectors)
                cluster_tokens[cluster.id] = tokens
                cluster_niches[cluster.id] = niches

        for row in rows:
            vec = unpack(row.embedding) if row.embedding else None
            if vec is None:
                continue
            row_tokens = set(content_tokens(row.title or ""))
            row_niches = set(row.niche_hint or [])

            best_cluster: int | None = None
            best_score = 0.0
            for cid, centroid in centroids.items():
                # never merge across declared niches — that is exactly how a ЦБ
                # rate story ends up with an OpenAI headline attached to it
                known = cluster_niches.get(cid, set())
                if row_niches and known and not (row_niches & known):
                    continue
                score = same_story_score(
                    vec, centroid, row_tokens, cluster_tokens.get(cid, set()), idf
                )
                if score > best_score:
                    best_cluster, best_score = cid, score

            if best_cluster is None:
                cluster = ClusterRow(
                    public_id=uuid.uuid4().hex[:12],
                    label=row.title[:220],
                    status="forming",
                    item_count=1,
                    last_seen_at=row.published_at or row.fetched_at,
                )
                session.add(cluster)
                session.flush()
                best_cluster = cluster.id
                centroids[cluster.id] = vec
                cluster_sizes[cluster.id] = 1
                cluster_tokens[cluster.id] = set(row_tokens)
                cluster_niches[cluster.id] = set(row_niches)
                created += 1
            else:
                size = cluster_sizes.get(best_cluster, 1)
                centroids[best_cluster] = _running_mean(centroids[best_cluster], vec, size)
                cluster_sizes[best_cluster] = size + 1
                cluster_tokens[best_cluster] = cluster_tokens.get(best_cluster, set()) | row_tokens
                cluster_niches[best_cluster] = cluster_niches.get(best_cluster, set()) | row_niches
                cluster = session.get(ClusterRow, best_cluster)
                if cluster:
                    cluster.item_count = cluster_sizes[best_cluster]
                    cluster.last_seen_at = max(
                        cluster.last_seen_at or datetime.now(UTC),
                        row.published_at or row.fetched_at,
                    )
                    if best_score > 0.6 and len(cluster.label or "") < 60:
                        cluster.label = row.title[:220]

            row.cluster_id = best_cluster

    return created


def _niche_compatible(session, row: RawItemRow, cluster_id: int) -> bool:
    """A cluster with an explicit niche only accepts items from that niche's
    sources. Keeps crypto chatter out of a health channel."""
    cluster = session.get(ClusterRow, cluster_id)
    if cluster is None or not cluster.niche:
        return True
    return cluster.niche in (row.niche_hint or [])


def _mean(vectors: list[np.ndarray]) -> np.ndarray:
    if not vectors:
        return np.zeros(0, dtype=np.float32)
    stack = np.vstack(vectors)
    return (stack.sum(axis=0) / len(stack)).astype(np.float32)


def _running_mean(centroid: np.ndarray, vec: np.ndarray, size: int) -> np.ndarray:
    """Incremental centroid update without re-stacking the whole cluster."""
    return ((centroid * size + vec) / (size + 1)).astype(np.float32)


# ----------------------------------------------------------------- heat score
def _niche_coverage() -> dict[str, int]:
    """How many posts each niche published in the last 48h. Feeds `niche_deficit`:
    topics in a channel we just posted about are worth less than fresh ground."""
    cutoff = datetime.now(UTC) - timedelta(hours=48)
    counts: dict[str, int] = defaultdict(int)
    with session_scope() as session:
        rows = session.execute(
            select(Post.channel_id, Post.created_at).where(Post.created_at >= cutoff)
        ).all()
        from tgfarm.config import get_config

        niche_by_channel = {p.id: p.niche for p in get_config().personas}
        for channel_id, _ in rows:
            niche = niche_by_channel.get(channel_id)
            if niche:
                counts[niche] += 1
    return dict(counts)


def assign_niches() -> int:
    """Set `cluster.niche` from the dominant source-niche of its members."""
    from tgfarm.config import get_config

    valid = set(get_config().niches)
    # A cluster's niche describes the story, not the channel. With a catch-all
    # only fleet there are no niche personas, so the source vocabulary is the
    # only taxonomy left — dropping it would leave every cluster niche-less.
    for source in get_config().sources:
        valid.update(source.niches or [])
    updated = 0
    with session_scope() as session:
        clusters = session.execute(
            select(ClusterRow).where(ClusterRow.niche.is_(None))
        ).scalars().all()
        for cluster in clusters:
            members = (
                session.execute(
                    select(RawItemRow.niche_hint).where(RawItemRow.cluster_id == cluster.id)
                )
                .scalars()
                .all()
            )
            tally: dict[str, int] = defaultdict(int)
            for hints in members:
                for niche in hints or []:
                    if niche in valid:
                        tally[niche] += 1
            if tally:
                cluster.niche = max(tally.items(), key=lambda kv: kv[1])[0]
                updated += 1
    return updated


def _score_pass() -> int:
    get_settings()
    now = datetime.now(UTC)
    coverage = _niche_coverage()
    max_coverage = max(coverage.values(), default=0) or 1

    scored = 0
    with session_scope() as session:
        clusters = session.execute(
            select(ClusterRow).where(ClusterRow.status == "forming")
        ).scalars().all()

        for cluster in clusters:
            members = session.execute(
                select(RawItemRow).where(RawItemRow.cluster_id == cluster.id)
            ).scalars().all()
            if not members:
                continue

            # -- signal 1: source authority (best source in the cluster counts)
            source_component = max((m.source_weight for m in members), default=0.3)

            # -- signal 2: velocity, distinct outlets inside a 6h window
            window_start = now - timedelta(hours=6)
            recent = [m for m in members if _aware(m.published_at) >= window_start]
            recent_sources = {m.source_id for m in recent}
            velocity = min(1.0, len(recent_sources) / 4.0)
            if len(recent_sources) >= 2:
                velocity = min(1.0, velocity + 0.15)

            # -- signal 3: recency, 4h half-life-ish
            newest = max(_aware(m.published_at) for m in members)
            hours = max(0.0, (now - newest).total_seconds() / 3600.0)
            recency = math.exp(-hours / 4.0)

            # -- signal 4: cross-platform, different source *kinds* agreeing
            kinds = {"rss", "gnews", "gdelt", "api"}
            distinct_kinds = len({m.source_id.split("_")[0] for m in members}) or 1
            cross_platform = min(1.0, distinct_kinds / max(len(kinds), 1) + 0.0)
            cross_platform = max(cross_platform, min(1.0, len({m.source_id for m in members}) / 6.0))

            # -- signal 5: niche deficit (documented approximation)
            niche_posts = coverage.get(cluster.niche or "", 0)
            niche_deficit = 1.0 - min(1.0, niche_posts / max_coverage) if cluster.niche else 0.5

            heat = (
                W_SOURCE * source_component
                + W_VELOCITY * velocity
                + W_RECENCY * recency
                + W_CROSS * cross_platform
                + W_DEFICIT * niche_deficit
            )

            cluster.heat = round(min(1.0, heat), 4)
            cluster.item_count = len(members)
            last_seen = max(_aware(m.published_at) for m in members)
            cluster.last_seen_at = last_seen
            if now - last_seen > timedelta(hours=STALE_AFTER_HOURS):
                cluster.status = "ready"
            scored += 1

            for member in members:
                member.heat = cluster.heat

    return scored


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def promote_ready(threshold: float | None = None) -> int:
    """Clusters above the heat threshold become `ready` and are visible to S3."""
    settings = get_settings()
    threshold = settings.heat_threshold if threshold is None else threshold
    promoted = 0
    with session_scope() as session:
        clusters = session.execute(
            select(ClusterRow).where(ClusterRow.status == "forming", ClusterRow.heat >= threshold)
        ).scalars().all()
        for cluster in clusters:
            cluster.status = "ready"
            promoted += 1
    if promoted:
        log.info("promoted %d clusters above heat>=%.2f", promoted, threshold)
    return promoted


def ready_clusters(min_items: int = 2) -> list[tuple[str, str, float, list]]:
    settings = get_settings()
    with session_scope() as session:
        rows = session.execute(
            select(ClusterRow)
            .where(ClusterRow.status == "ready", ClusterRow.heat >= settings.heat_threshold)
            .order_by(ClusterRow.heat.desc())
            .limit(100)
        ).scalars().all()
        out = []
        for cluster in rows:
            members = session.execute(
                select(RawItemRow).where(RawItemRow.cluster_id == cluster.id)
            ).scalars().all()
            if len(members) < min_items:
                continue
            if max((m.source_weight for m in members), default=0.0) < 0.45:
                continue
            if not any(len(m.text or "") >= 120 for m in members):
                continue
            out.append((cluster.public_id, cluster.label or "", cluster.heat, members))
        return out


def mark_consumed(public_id: str) -> None:
    with session_scope() as session:
        row = session.execute(
            select(ClusterRow).where(ClusterRow.public_id == public_id)
        ).scalar_one_or_none()
        if row:
            row.status = "consumed"
            row.consumed_at = datetime.now(UTC)


async def run() -> dict[str, int]:
    result: dict[str, int] = {}
    with StageTimer("s2_dedup") as timer:
        result["duplicates"] = await to_thread(_dedup_pass)
        timer.items_out = result["duplicates"]

    with StageTimer("s2_cluster") as timer:
        result["clusters"] = await to_thread(_cluster_pass)
        timer.items_out = result["clusters"]

    result["niches"] = await to_thread(assign_niches)

    with StageTimer("s2_score") as timer:
        result["scored"] = await to_thread(_score_pass)
        timer.items_out = result["scored"]

    result["promoted"] = await to_thread(promote_ready)
    return result


def is_near_dup(fp_a: int, fp_b: int, threshold: int = 3) -> bool:
    return is_near_duplicate(fp_a, fp_b, threshold)


# Explainability lives in its own module and is re-exported here so callers can
# keep using `s2_trends.cluster_signals`. The import sits at the bottom on
# purpose: `explain` must never be pulled in by the scoring path, and it does not
# import anything back from this package.
from tgfarm.s2_trends.explain import cluster_signals, explain_cluster  # noqa: E402


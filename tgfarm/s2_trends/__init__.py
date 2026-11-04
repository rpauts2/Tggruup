"""S2 — trend detection.

Four steps, each cheap enough to run on every cycle:

1. `deduplicate` — near-identical items from different outlets collapse via
   simhash (cheap) and embedding cosine (accurate), and stay collapsed: a row
   with `duplicate_of` set is evidence of *copying*, never of independent
   interest, so every count below is taken over the rows that survived it.
2. `cluster`     — surviving items are greedily assigned to the nearest existing
   cluster above a similarity floor, otherwise they start one. A cluster is the
   farm's unit of "a story".
3. `measure`     — the independence signals (velocity, recency, cross-platform)
   and the window-over-window acceleration are measured and persisted, so the
   number a cluster was promoted on is auditable per item afterwards.
4. `score_heat`  — every cluster gets a 0..1 heat value from five signals, read
   back from the measurements above. Only clusters above `HEAT_THRESHOLD` — and
   whose shape is not a single-window burst — reach content generation.
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

# The interest window: "is this story accelerating?" is answered by comparing the
# current window with the one immediately before it. Six hours is the horizon the
# velocity signal already used, so a burst and its baseline are the same length.
VELOCITY_WINDOW_HOURS = 6.0
ACCELERATION_WINDOW_HOURS = 6.0

# A window-over-window change of +25% is the point where "more outlets are
# picking this up" stops being plausibly noise. Two items becoming three is not a
# trend no matter what the ratio says, hence the floor on the current window.
RISE_RATE = 0.25
DECAY_RATE = -0.25
RISING_MIN_ITEMS = 3

# Source kinds the farm polls, used by the cross-platform signal.
SOURCE_KINDS = frozenset({"rss", "gnews", "gdelt", "api"})

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


# ------------------------------------------------------------- acceleration
TREND_PHASES = ("rising", "steady", "spike", "decaying", "unknown")
# Phases a cluster may be promoted on. A `spike` is interest that exists in one
# window and in none before it — that is a burst, not a trend, and promoting it
# is how one reprint storm turns into six posts. `decaying` still earns a post
# while its heat is above the threshold: recency already penalises it.
PROMOTABLE_PHASES = ("rising", "steady", "unknown")


def classify_acceleration(
    current_items: int,
    previous_items: int | None,
    *,
    rise_rate: float = RISE_RATE,
    decay_rate: float = DECAY_RATE,
    rising_min_items: int = RISING_MIN_ITEMS,
) -> tuple[str, float | None]:
    """Shape of interest: the window now against the comparable window before it.

    Returns `(phase, rate)`.

    * `previous_items is None` — there is no preceding window at all (the store,
      or this story, is younger than one window). The rate is reported as unknown
      instead of being invented from nothing.
    * `previous_items == 0` with items now — a measured empty baseline plus a full
      window is a `spike`. No rate: dividing by zero would be a made-up number.
    * both windows non-empty — the relative change decides `rising`, `steady` or
      `decaying`. Steady-high volume is *steady*: five outlets covering a story on
      two windows in a row is not an acceleration, however loud it looks in one.

    A jump that clears the ratio but not `rising_min_items` (1 item becoming 2) is
    reported as steady — the rate is measured and returned, the verdict is not.
    """
    current = max(0, int(current_items))
    if previous_items is None:
        return "unknown", None
    previous = max(0, int(previous_items))

    if previous == 0:
        if current == 0:
            # Nothing in either window: no change to report, and no burst.
            return "steady", 0.0
        return "spike", None

    rate = (current - previous) / previous
    if current == 0:
        return "decaying", rate
    if rate >= rise_rate and current >= rising_min_items:
        return "rising", rate
    if rate <= decay_rate:
        return "decaying", rate
    return "steady", rate


def _item_time(item: RawItemRow) -> datetime:
    """When this document counts as having appeared.

    `published_at` is the honest clock for interest, but feeds lie about it and
    legacy rows omit it; `fetched_at` is always set, so it is the fallback.
    """
    return _aware(item.published_at or item.fetched_at)


def _window_counts(
    items: list[RawItemRow],
    *,
    now: datetime,
    window_hours: float = ACCELERATION_WINDOW_HOURS,
) -> tuple[int, int | None]:
    """(current window, preceding window) item counts — or None for the preceding.

    None means "there is nothing here that predates the current window", so the
    farm cannot claim the previous window was empty: it never observed one.
    """
    window = timedelta(hours=max(0.1, float(window_hours)))
    current_start = now - window
    previous_start = now - 2 * window

    times = [_item_time(item) for item in items]
    current = sum(1 for moment in times if moment >= current_start)
    if not any(moment < current_start for moment in times):
        return current, None
    previous = sum(1 for moment in times if previous_start <= moment < current_start)
    return current, previous


def acceleration_of(
    items: list[RawItemRow],
    *,
    now: datetime | None = None,
    window_hours: float = ACCELERATION_WINDOW_HOURS,
) -> tuple[str, float | None]:
    """Public shape read over the items that survived dedup."""
    now = now or datetime.now(UTC)
    current, previous = _window_counts(items, now=now, window_hours=window_hours)
    return classify_acceleration(current, previous)


# ----------------------------------------------------------------- deduplicate
def _dedup_pass() -> int:
    """Mark exact/near duplicates among recent items. Returns rows flagged.

    The match always points at a surviving item, so a reprint storm has exactly
    one original at the bottom of it. A row whose `simhash` is absent (0 or NULL,
    written before the column existed) is never treated as identical to anything:
    a missing fingerprint carries no information, and `hamming(0, x)` is a
    meaningless number.
    """
    with session_scope() as session:
        cutoff = datetime.now(UTC) - timedelta(hours=CLUSTER_WINDOW_HOURS)
        rows = session.execute(
            select(RawItemRow).where(
                RawItemRow.fetched_at >= cutoff, RawItemRow.duplicate_of.is_(None)
            ).order_by(RawItemRow.fetched_at.desc())
        ).scalars().all()

        seen_fp: dict[int, int] = {}
        seen_titles: dict[str, int] = {}
        eligible = {row.id for row in rows}
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
                match_id = next(
                    (hit_id for hit_id, _ in hits if hit_id in eligible), None
                )

            if match_id is not None:
                row.duplicate_of = match_id
                eligible.discard(row.id)
                flagged += 1
            else:
                seen_titles[title_key(row.title)] = row.id
                if row.simhash:
                    seen_fp[row.simhash] = row.id

        return flagged


# -------------------------------------------------------------------- cluster
def _cluster_pass(*, include_duplicates: bool = False) -> int:
    """Group surviving items into stories.

    A flagged reprint is not an argument for a story being bigger; letting it
    into the clustering pass would move centroids and inflate `item_count`, so by
    default it never reaches this query. `include_duplicates=True` keeps the old
    behaviour available for backfills and diagnostics.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=CLUSTER_WINDOW_HOURS)
    created = 0

    with session_scope() as session:
        get_index()
        conditions = [
            RawItemRow.fetched_at >= cutoff,
            RawItemRow.cluster_id.is_(None),
        ]
        if not include_duplicates:
            conditions.append(RawItemRow.duplicate_of.is_(None))
        rows = session.execute(
            select(RawItemRow).where(*conditions).order_by(RawItemRow.fetched_at.asc())
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
            member_conditions = [RawItemRow.cluster_id == cluster.id]
            if not include_duplicates:
                member_conditions.append(RawItemRow.duplicate_of.is_(None))
            members = session.execute(
                select(
                    RawItemRow.embedding, RawItemRow.title, RawItemRow.niche_hint
                ).where(*member_conditions)
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
                    select(RawItemRow.niche_hint).where(
                        RawItemRow.cluster_id == cluster.id,
                        RawItemRow.duplicate_of.is_(None),
                    )
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


def _recency_signal(moment: datetime, *, now: datetime, half_life_hours: float = 4.0) -> float:
    """Exponential age decay of one document. Measured from its own timestamp."""
    hours = max(0.0, (now - _aware(moment)).total_seconds() / 3600.0)
    return math.exp(-hours / max(0.1, half_life_hours))


def _velocity_signal(
    survivors: list[RawItemRow],
    *,
    now: datetime,
    window_hours: float = VELOCITY_WINDOW_HOURS,
) -> float:
    """Independent outlets that published inside the current window, 0..1.

    `survivors` is already the dedup-pass output: one document reprinted by five
    outlets is one outlet here, not five. The same number used to be a local
    variable that nobody could audit; it is persisted per item by `_measure_pass`.
    """
    window_start = now - timedelta(hours=max(0.1, float(window_hours)))
    recent_sources = {m.source_id for m in survivors if _item_time(m) >= window_start}
    velocity = min(1.0, len(recent_sources) / 4.0)
    if len(recent_sources) >= 2:
        velocity = min(1.0, velocity + 0.15)
    return round(velocity, 4)


def _cross_platform_signal(survivors: list[RawItemRow]) -> float:
    """How many different source *kinds* agree, 0..1 — over surviving items only.

    Kept formula-for-formula with the old local computation (prefix families, with
    a spread fallback on distinct outlets); the only change is which rows feed it.
    """
    distinct_kinds = len({m.source_id.split("_")[0] for m in survivors}) or 1
    cross_platform = min(1.0, distinct_kinds / max(len(SOURCE_KINDS), 1))
    outlets = len({m.source_id for m in survivors})
    return round(max(cross_platform, min(1.0, outlets / 6.0)), 4)


def _measure_pass(
    *,
    now: datetime | None = None,
    window_hours: float = ACCELERATION_WINDOW_HOURS,
) -> int:
    """Persist the measured S2 signals, then and only then let heat read them.

    Every number written here is a measurement over the items that survived the
    dedup pass:
      * `recency`       — this document's own age decay.
      * `velocity`      — independent outlets of its story inside the window.
      * `cross_platform`— source kinds agreeing on its story.
      * `trend_phase` / `acceleration` on the cluster — this window against the
        one before it, or unknown when there is no preceding window.

    A flagged duplicate is zeroed rather than left holding a value it earned
    before it was recognised as a copy: zero independent signal *is* the measured
    statement about a reprint. Returns the number of items written.
    """
    now = now or datetime.now(UTC)
    measured = 0
    with session_scope() as session:
        clusters = session.execute(
            select(ClusterRow).where(ClusterRow.status.in_(("forming", "ready")))
        ).scalars().all()

        for cluster in clusters:
            members = session.execute(
                select(RawItemRow).where(RawItemRow.cluster_id == cluster.id)
            ).scalars().all()
            survivors = [m for m in members if m.duplicate_of is None]
            copies = [m for m in members if m.duplicate_of is not None]

            if not survivors:
                # Nothing independent left in this cluster: no rate to invent.
                cluster.trend_phase = "unknown"
                cluster.acceleration = None
                for member in copies:
                    member.recency = 0.0
                    member.velocity = 0.0
                    member.cross_platform = 0.0
                continue

            current, previous = _window_counts(survivors, now=now, window_hours=window_hours)
            phase, rate = classify_acceleration(current, previous)
            cluster.trend_phase = phase
            cluster.acceleration = rate

            velocity = _velocity_signal(survivors, now=now)
            cross_platform = _cross_platform_signal(survivors)
            for member in survivors:
                member.recency = round(_recency_signal(_item_time(member), now=now), 4)
                member.velocity = velocity
                member.cross_platform = cross_platform
                measured += 1
            for member in copies:
                member.recency = 0.0
                member.velocity = 0.0
                member.cross_platform = 0.0

    return measured


def _score_pass(*, measure: bool = True) -> int:
    """Heat from the persisted measurements, over surviving evidence only.

    `measure=True` (the default, and what `run()` uses) refreshes the per-item
    signals first, so heat is always computed from this cycle's measurements
    rather than from a score written days ago. Reading the columns back instead of
    recomputing them locally is deliberate: it makes the stored numbers
    load-bearing — the heat a cluster was promoted on is reproducible from the
    rows that produced it.
    """
    get_settings()
    now = datetime.now(UTC)
    if measure:
        _measure_pass(now=now)
    coverage = _niche_coverage()
    max_coverage = max(coverage.values(), default=0) or 1

    scored = 0
    with session_scope() as session:
        clusters = session.execute(
            select(ClusterRow).where(ClusterRow.status == "forming")
        ).scalars().all()

        for cluster in clusters:
            # -- only items that survived the dedup pass are independence evidence
            members = session.execute(
                select(RawItemRow).where(
                    RawItemRow.cluster_id == cluster.id,
                    RawItemRow.duplicate_of.is_(None),
                )
            ).scalars().all()
            if not members:
                continue

            # -- signal 1: source authority (best source in the cluster counts)
            source_component = max((m.source_weight for m in members), default=0.3)

            # -- signals 2-4: read the measurements `_measure_pass` persisted
            velocity = max((m.velocity for m in members), default=0.0)
            recency = max((m.recency for m in members), default=0.0)
            cross_platform = max((m.cross_platform for m in members), default=0.0)

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
            # `item_count` is a claim about independent coverage, so it counts the
            # documents that survived dedup — not the reprints stacked on top.
            cluster.item_count = len(members)
            last_seen = max(_item_time(m) for m in members)
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


def promote_ready(threshold: float | None = None, *, allow_spikes: bool = False) -> int:
    """Clusters above the heat threshold become `ready` and are visible to S3.

    Heat alone cannot tell a rising story from a burst, so the measured shape
    gates promotion too: a cluster whose interest exists in one window and in none
    before it (`trend_phase == "spike"`) is held for the next cycle, where the
    preceding window either confirms it or exposes it. Clusters with no measured
    shape yet — legacy rows, or a store younger than one window — are judged on
    heat alone, because refusing to promote what cannot be measured would be
    inventing a verdict in the other direction. `allow_spikes=True` restores the
    old heat-only behaviour for operators who want a burst to publish now.
    """
    settings = get_settings()
    threshold = settings.heat_threshold if threshold is None else threshold
    promoted = 0
    held = 0
    with session_scope() as session:
        clusters = session.execute(
            select(ClusterRow).where(ClusterRow.status == "forming", ClusterRow.heat >= threshold)
        ).scalars().all()
        for cluster in clusters:
            if not allow_spikes and cluster.trend_phase == "spike":
                held += 1
                continue
            cluster.status = "ready"
            promoted += 1
    if promoted:
        log.info("promoted %d clusters above heat>=%.2f", promoted, threshold)
    if held:
        log.info(
            "held back %d spike-shaped clusters (heat>=%.2f but interest has no"
            " preceding window to compare against)",
            held,
            threshold,
        )
    return promoted


def ready_clusters(
    min_items: int = 2, *, include_duplicates: bool = False
) -> list[tuple[str, str, float, list]]:
    """Clusters S3 may turn into posts.

    `min_items` is a claim about independent coverage, so it counts the documents
    that survived dedup, and the members handed downstream are those same
    documents — a post that cites five outlets for one reprinted wire story cites
    one outlet five times.
    """
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
            conditions = [RawItemRow.cluster_id == cluster.id]
            if not include_duplicates:
                conditions.append(RawItemRow.duplicate_of.is_(None))
            members = session.execute(select(RawItemRow).where(*conditions)).scalars().all()
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


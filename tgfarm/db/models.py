"""SQLAlchemy ORM schema.

Design notes
------------
* Embeddings are stored as raw float32 blobs, not pgvector. At farm scale
  (<=1M items) an in-memory numpy index rebuilt on boot is faster than a vector
  extension round-trip and removes a service from the stack. RUNBOOK documents
  the pgvector migration threshold.
* Every table carries created_at/updated_at so the analytics layer can do
  time-series work without extra tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(32), default="rss")  # rss|gdelt|api
    url: Mapped[str] = mapped_column(Text, default="")
    niches: Mapped[list] = mapped_column(JSON, default=list)
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_interval_s: Mapped[int] = mapped_column(Integer, default=600)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(32), default="new")
    last_error: Mapped[str | None] = mapped_column(Text)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_yield: Mapped[int] = mapped_column(Integer, default=0)
    idle_streak: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sources_enabled", "enabled", "kind"),)


class RawItem(Base):
    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str] = mapped_column(String(8), default="unknown")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    niche_hint: Mapped[list] = mapped_column(JSON, default=list)
    source_weight: Mapped[float] = mapped_column(Float, default=0.5)
    media_links: Mapped[list] = mapped_column(JSON, default=list)
    media_kind: Mapped[str | None] = mapped_column(String(16))

    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    simhash: Mapped[int] = mapped_column(Integer, default=0)
    heat: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("raw_items.id"))
    # Measured in S2's `_measure_pass`, which reads only the items that survived
    # the dedup pass: `recency` is this document's own age decay, `velocity` and
    # `cross_platform` are the independence signals of the story it is evidence
    # for. A flagged duplicate keeps 0.0 — it measures no independent interest.
    recency: Mapped[float] = mapped_column(Float, default=0.0)
    velocity: Mapped[float] = mapped_column(Float, default=0.0)
    cross_platform: Mapped[float] = mapped_column(Float, default=0.0)

    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), index=True)
    cluster: Mapped[Cluster | None] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_source_external"),
        Index("ix_raw_items_fetched", "fetched_at"),
        Index("ix_raw_items_heat", "heat"),
    )


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    label: Mapped[str] = mapped_column(Text, default="")
    niche: Mapped[str | None] = mapped_column(String(64), index=True)
    heat: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="forming", index=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    # Window-over-window interest, measured by S2 over the items that survived
    # dedup. `trend_phase` is rising|steady|spike|decaying|unknown;
    # `acceleration` is the relative change and stays NULL when there was no
    # preceding window to compare against — an unknown rate is reported as
    # unknown, never as zero.
    trend_phase: Mapped[str | None] = mapped_column(String(16), index=True)
    acceleration: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[RawItem]] = relationship(back_populates="cluster")

    __table_args__ = (Index("ix_clusters_status_heat", "status", "heat"),)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # persona id
    platform: Mapped[str] = mapped_column(
        String(32), default="telegram", server_default="telegram", index=True
    )
    tg_chat_id: Mapped[int | None] = mapped_column(Integer, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200), default="")
    niche: Mapped[str] = mapped_column(String(64), default="")
    persona: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    posts_per_day: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Reality, as opposed to what config/personas.yaml claims. Filled by
    # `tgfarm telegram sync`; empty until an account session is authorised.
    tg_participants: Mapped[int | None] = mapped_column(Integer)
    tg_verified: Mapped[bool | None] = mapped_column(Boolean)
    tg_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tg_bot_admin: Mapped[bool | None] = mapped_column(Boolean)

    posts: Mapped[list[Post]] = relationship(back_populates="channel")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    cluster_public_id: Mapped[str | None] = mapped_column(String(32), index=True)

    angle: Mapped[str] = mapped_column(String(32), default="news")
    post_format: Mapped[str] = mapped_column(String(16), default="news", index=True)
    format_reason: Mapped[str | None] = mapped_column(Text)
    # Which hook pattern the draft used (fact | stat | question). S12 learns
    # which style earns reactions and S4 samples from the learned weights.
    hook_style: Mapped[str | None] = mapped_column(String(16))
    hook: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    claims: Mapped[list] = mapped_column(JSON, default=list)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str] = mapped_column(String(8), default="ru")
    generation_provider: Mapped[str] = mapped_column(String(32), default="offline")

    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    rewrites_used: Mapped[int] = mapped_column(Integer, default=0)

    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    char_len: Mapped[int] = mapped_column(Integer, default=0)
    variant: Mapped[str] = mapped_column(String(8), default="a")

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Refreshed while a worker is actively waiting on a platform/browser. The
    # stuck detector uses this heartbeat so slow but live uploads are not
    # incorrectly marked outcome-unknown just because they exceed the timeout.
    publish_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tg_message_id: Mapped[int | None] = mapped_column(Integer)
    tg_chat_id: Mapped[int | None] = mapped_column(Integer)
    publish_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    channel: Mapped[Channel] = relationship(back_populates="posts")
    media: Mapped[list[Media]] = relationship(back_populates="post", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_posts_channel_status", "channel_id", "status"),)


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="template")
    path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    sha: Mapped[str | None] = mapped_column(String(64))
    caption: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    post: Mapped[Post] = relationship(back_populates="media")


class PostStat(Base):
    __tablename__ = "post_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    # Dzen distinguishes impressions and openings from completed reads; keeping
    # all three makes CTR and read-through measurable instead of guessing from
    # views alone.
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    opens: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    forwards: Mapped[int] = mapped_column(Integer, default=0)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_post_stats_post_time", "post_id", "collected_at"),)


class ChannelStat(Base):
    __tablename__ = "channel_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    subscribers: Mapped[int | None] = mapped_column(Integer)
    posts: Mapped[int] = mapped_column(Integer, default=0)
    views_sum: Mapped[int] = mapped_column(Integer, default=0)
    reactions_sum: Mapped[int] = mapped_column(Integer, default=0)
    err: Mapped[float | None] = mapped_column(Float)
    er: Mapped[float | None] = mapped_column(Float)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("channel_id", "day", name="uq_channel_day"),)


class CrosspostLog(Base):
    __tablename__ = "crosspost_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_public_id: Mapped[str] = mapped_column(String(32), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    remote_id: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crosspost_post_platform", "post_public_id", "platform"),
    )


class CrosspostClaim(Base):
    """One durable in-flight claim per material/platform pair.

    CrosspostLog is append-only history; its historical 'publishing' rows cannot
    uniquely lock a pair. This separate row provides a database-enforced lock
    across multiple TgFarm processes and survives a crash for manual reconciliation.
    """
    __tablename__ = "crosspost_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_public_id: Mapped[str] = mapped_column(String(32), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("post_public_id", "platform", name="uq_crosspost_claim_pair"),
    )


class RedditMetricSnapshot(Base):
    """A bounded history of publicly available Reddit post-level feedback."""

    __tablename__ = "reddit_metric_snapshots"
    __table_args__ = (
        Index("ix_reddit_metrics_subreddit_time", "subreddit", "collected_at"),
        Index("ix_reddit_metrics_log_time", "crosspost_log_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crosspost_log_id: Mapped[int] = mapped_column(
        ForeignKey("crosspost_logs.id", ondelete="CASCADE"), index=True
    )
    subreddit: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    num_comments: Mapped[int] = mapped_column(Integer, default=0)
    upvote_ratio: Mapped[float | None] = mapped_column(Float)
    created_utc: Mapped[float | None] = mapped_column(Float)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegraphMetricSnapshot(Base):
    """A time series of official Telegraph page-view counts."""

    __tablename__ = "telegraph_metric_snapshots"
    __table_args__ = (
        Index("ix_telegraph_metrics_log_time", "crosspost_log_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crosspost_log_id: Mapped[int] = mapped_column(
        ForeignKey("crosspost_logs.id", ondelete="CASCADE"), index=True
    )
    views: Mapped[int] = mapped_column(Integer, default=0)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineRun(Base):
    """One row per stage per cycle.

    `run_id` is deliberately NOT unique — a cycle touches ten stages, so the
    constraint that actually expresses intent is (run_id, stage).
    """

    __tablename__ = "pipeline_runs"
    __table_args__ = (UniqueConstraint("run_id", "stage", name="uq_run_stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    items_in: Mapped[int] = mapped_column(Integer, default=0)
    items_out: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIUsage(Base):
    """One row per LLM attempt, including the ones policy refused.

    A reservation is written before the call and finalised afterwards, so a
    crash cannot hide spend. `charged` is true only when an online provider
    actually answered; offline fallbacks release their reservation.
    """

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    purpose: Mapped[str] = mapped_column(String(32), default="")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    charged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    post_public_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    cluster_public_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_ai_usage_day_charged", "created_at", "charged"),)


class ImageUsage(Base):
    """One persisted row per image-provider request attempt.

    Attempts count against the daily cap even when the provider returns an
    error, because a timeout does not prove the remote service did not bill it.
    """

    __tablename__ = "image_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    charged: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    reason: Mapped[str] = mapped_column(String(80), default="")
    error_kind: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_image_usage_day_charged", "created_at", "charged"),)


class BlocklistEntry(Base):
    """Domain / channel level negative memory.

    The farm never talks to anything in here again. Used both for content
    quality (blocked claim patterns) and for growth (channels that deleted us).
    """

    __tablename__ = "blocklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)  # domain|source|phrase|pattern
    value: Mapped[str] = mapped_column(Text, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    hits: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningMemory(Base):
    __tablename__ = "learning_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    kind: Mapped[str] = mapped_column(String(32), index=True)  # angle|slot|tactic|mistake|strategy
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("channel_id", "kind", "key", name="uq_learning_channel_kind_key"),
        Index("ix_learning_channel_kind", "channel_id", "kind"),
    )


class OperatorLog(Base):
    """Actions a human took from the panel: publish, reject, approve, polish."""

    __tablename__ = "operator_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    target: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str | None] = mapped_column(Text)


class EditorialPlan(Base):
    """Snapshot of the global editorial plan.

    Every refresh creates a new snapshot and supersedes the previous active
    one; superseded plans are kept as decision history, never overwritten.
    """

    __tablename__ = "editorial_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(64), unique=True, default=lambda: uuid.uuid4().hex
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    horizon_days: Mapped[int] = mapped_column(Integer, default=7)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("editorial_plans.id"))
    summary: Mapped[dict | None] = mapped_column(JSON)


class EditorialPlanEntry(Base):
    """One planned step for one topic: produce, schedule or hold."""

    __tablename__ = "editorial_plan_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("editorial_plans.id"), index=True)
    cluster_public_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    niche: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(16), index=True)  # produce|schedule|hold
    platform: Mapped[str | None] = mapped_column(String(32))
    channel_id: Mapped[str | None] = mapped_column(String(64), index=True)
    format: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=2)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    reasons: Mapped[list | None] = mapped_column(JSON)
    slot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class CommunityPolicySnapshot(Base):
    """Append-only history of a destination's publication rules.

    One row per *rules change or gate outcome change* (identical consecutive
    checks are collapsed by :func:`tgfarm.community_policy.record_policy_snapshot`).
    The raw rule text is kept in ``rules_excerpt`` as inert data — it is never
    parsed into instructions — and ``rules_hash`` makes a change detectable even
    when the excerpt is truncated.
    """

    __tablename__ = "community_policy_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(200), index=True)
    target_name: Mapped[str] = mapped_column(String(200), default="")
    rules_source: Mapped[str] = mapped_column(String(500), default="")
    rules_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    rules_available: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_excerpt: Mapped[str] = mapped_column(Text, default="")
    verdict_status: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    requirements_json: Mapped[list | None] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (
        Index("ix_policy_target", "platform", "target_id"),
    )
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

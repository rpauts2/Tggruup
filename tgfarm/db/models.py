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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crosspost_post_platform", "post_public_id", "platform"),
    )


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
    target: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

"""Pydantic domain schemas passed between pipeline stages.

Stage contracts are enforced here — an item that cannot become a `RawItem` never
reaches the database, and a post that cannot satisfy `DraftPost` is never
rendered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Language codes the farm can target. Kept open-ended on purpose — adding a
# market must not require a code change, and `unknown` is always safe.
Lang = str
Angle = Literal["news", "analysis", "why_it_matters", "comparison", "checklist", "history",
    "listicle"]
PostStatus = Literal[
    "draft", "gated", "scheduled", "publishing", "publish_unknown", "published", "rejected", "failed"
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RawItem(Strict):
    """One fetched document. The atomic unit of S1."""

    source_id: str
    external_id: str
    url: str
    title: str = Field(min_length=3)
    text: str = ""
    lang: Lang = "unknown"
    published_at: datetime | None = None
    niche_hint: list[str] = Field(default_factory=list)
    source_weight: float = 0.5
    # Media carried by the feed entry itself. Recorded at collection time because
    # the S11 licensing gate has to judge what the publisher attached, and the
    # feed is the only evidence that survives without re-fetching the article.
    media_links: list[str] = Field(default_factory=list)
    media_kind: str | None = None

    @property
    def blend(self) -> str:
        return f"{self.title}\n\n{self.text[:1500]}"


class ItemScored(RawItem):
    """RawItem + S2 analysis."""

    embedding: list[float] = Field(default_factory=list)
    simhash: int = 0
    recency: float = 0.0
    velocity: float = 0.0
    cross_platform: float = 0.0
    source_component: float = 0.0
    niche_deficit: float = 0.0
    heat: float = 0.0
    duplicate_of: str | None = None


class Cluster(Strict):
    id: str
    label: str
    items: list[ItemScored] = Field(default_factory=list)
    niche: str | None = None
    heat: float = 0.0
    status: Literal["forming", "ready", "consumed"] = "forming"
    created_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def lead_item(self) -> ItemScored | None:
        return self.items[0] if self.items else None

    def sources(self) -> list[dict[str, str]]:
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for item in self.items:
            if item.url in seen:
                continue
            seen.add(item.url)
            out.append({"title": item.title, "url": item.url, "source": item.source_id})
        return out

    def blend(self, limit: int = 6000) -> str:
        parts = []
        for item in self.items[:6]:
            parts.append(f"### {item.title}\n{item.text[:1200]}\nИсточник: {item.url}")
        return "\n\n".join(parts)[:limit]


class DraftPost(Strict):
    """S4 output: text ready for the quality gates."""

    hook: str
    body: str
    sources: list[dict[str, str]]
    hashtags: list[str] = Field(default_factory=list)
    cta: str | None = None
    angle: Angle = "news"
    language: Lang = "ru"
    claims: list[Claim] = Field(default_factory=list)
    generation_provider: str = "offline"
    # Learned hook pattern and A/B bucket ("a" = treatment, "holdout" = baseline
    # without few-shot examples). Both feed the self-measurement loop.
    hook_style: str | None = None
    variant: str = "a"
    # The S10 format decision travels with the draft: the structure gate tunes
    # emoji/length rules per format, not per channel.
    post_format: str | None = None

    @field_validator("language", mode="before")
    @classmethod
    def _normalise_lang(cls, value: Any) -> str:
        return str(value or "unknown").strip().lower()[:8] or "unknown"

    @model_validator(mode="after")
    def _trim(self) -> Self:
        self.hashtags = [h.lstrip("#") for h in self.hashtags if h.strip()][:6]
        self.sources = self.sources[:3]
        return self

    @property
    def text(self) -> str:
        parts = [self.hook.strip(), self.body.strip()]
        if self.sources:
            links = "\n".join(f"— {s['title']}\n  {s['url']}" for s in self.sources)
            parts.append(f"Источники:\n{links}")
        if self.cta:
            parts.append(self.cta.strip())
        if self.hashtags:
            parts.append(" ".join(f"#{h}" for h in self.hashtags))
        return "\n\n".join(p for p in parts if p)

    @property
    def editorial_len(self) -> int:
        """Length of the words a human wrote.

        The persona length budget applies here, not to `text`. The sources block
        and hashtag row are metadata: a 600-character budget that included three
        source URLs would leave ~200 characters of actual prose, which is why
        measuring the full string is the wrong gate.
        """
        parts = [self.hook.strip(), self.body.strip()]
        if self.cta:
            parts.append(self.cta.strip())
        return len(" ".join(p for p in parts if p))

    @property
    def char_len(self) -> int:
        """Full rendered length including citations and hashtags."""
        return len(self.text)


class Claim(Strict):
    claim: str
    source_url: str
    verified: bool = False
    note: str | None = None


class QualityReport(Strict):
    passed: bool
    gate: str | None = None
    reason: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    rewrites_used: int = 0
    # The critic's itemized notes; the panel shows them verbatim so the
    # operator sees *why*, not just a number.
    issues: list[str] = Field(default_factory=list)

    def fail(self, gate: str, reason: str) -> QualityReport:
        return QualityReport(passed=False, gate=gate, reason=reason, scores=self.scores)


class MediaAsset(Strict):
    kind: Literal[
        "template", "infographic", "quote", "ai_art", "article_image", "video", "none"
    ] = "template"
    path: str | None = None
    width: int = 0
    height: int = 0
    sha: str | None = None
    caption: str | None = None


class RoutedDraft(Strict):
    draft: DraftPost
    channel_id: str
    persona_id: str
    format: str = "news"
    min_chars: int = 400
    max_chars: int = 700
    outline: list[str] = Field(default_factory=list)
    needs_video: bool = False
    simenness_max: float = 0.0
    variant: str = "a"


class VideoRef(Strict):
    """An allowed, downloaded video attached to a source item."""

    url: str
    local_path: str
    bytes: int = 0
    mime: str = "video/mp4"
    licence_hint: str = ""
    title: str = ""


class CrosspostPayload(Strict):
    platform: str
    title: str
    text: str
    link: str | None = None
    image_path: str | None = None
    video_path: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class CrosspostResult(Strict):
    platform: str
    status: Literal["posted", "skipped", "failed", "permanent", "unknown", "dry_run"]
    remote_id: str | None = None
    url: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None


class ChannelHealth(Strict):
    channel_id: str
    posts_today: int = 0
    quota: int = 0
    last_post_at: datetime | None = None
    next_slot_at: datetime | None = None
    avg_err_7d: float | None = None


DraftPost.model_rebuild()
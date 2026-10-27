"""S-style — lightweight style learning from published posts."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.text import tokens
from tgfarm.db.models import Post, PostStat
from tgfarm.db.session import session_scope

log = get_logger(__name__)


@dataclass
class StyleFeatures:
    chars: int = 0
    paragraphs: int = 0
    sentences: int = 0
    questions: int = 0
    numbers: int = 0
    emoji: int = 0
    bullets: int = 0
    avg_sentence_len: float = 0.0
    hashtag_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chars": self.chars,
            "paragraphs": self.paragraphs,
            "sentences": self.sentences,
            "questions": self.questions,
            "numbers": self.numbers,
            "emoji": self.emoji,
            "bullets": self.bullets,
            "avg_sentence_len": self.avg_sentence_len,
            "hashtag_count": self.hashtag_count,
        }


_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]")
_BULLET_RE = re.compile(r"(^|\n)\s*[-•*]\s", re.M)


def extract_style_features(text: str) -> StyleFeatures:
    text = (text or "").strip()
    if not text:
        return StyleFeatures()
    sentences = [s for s in re.split(r"[.!?…]+", text) if s.strip()]
    avg = mean(len(s.split()) for s in sentences) if sentences else 0.0
    return StyleFeatures(
        chars=len(text),
        paragraphs=max(1, text.count("\n\n") + 1),
        sentences=len(sentences),
        questions=text.count("?"),
        numbers=len(re.findall(r"\d", text)),
        emoji=len(_EMOJI_RE.findall(text)),
        bullets=len(_BULLET_RE.findall(text)),
        avg_sentence_len=round(avg, 2),
        hashtag_count=len(re.findall(r"#\w+", text)),
    )


@dataclass
class StyleProfile:
    channel_id: str
    samples: int = 0
    features: StyleFeatures = field(default_factory=StyleFeatures)
    top_tokens: list[str] = field(default_factory=list)
    quality: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "samples": self.samples,
            "features": self.features.as_dict(),
            "top_tokens": self.top_tokens,
            "quality": self.quality,
        }

def _post_quality(post: Post, stat: PostStat | None) -> float:
    if stat is None:
        return 0.0
    views = stat.views or 0
    if views <= 0:
        return 0.0
    engagement = (stat.reactions or 0) + (stat.comments or 0) + (stat.forwards or 0)
    er = engagement / views
    if stat.impressions and stat.impressions >= 100:
        ctr = min(1.0, (stat.opens or 0) / stat.impressions)
        read_rate = min(1.0, views / (stat.opens or 1)) if stat.opens else 0.0
        return round(ctr * read_rate, 4)
    return round(er, 4)


def style_profile(channel_id: str, days: int = 14, min_quality: float = 0.0) -> StyleProfile:
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    with session_scope() as session:
        rows = session.execute(
            select(Post).where(
                Post.channel_id == channel_id,
                Post.status == "published",
                Post.published_at >= cutoff,
            )
        ).scalars().all()
        stats: dict[int, PostStat] = {}
        for row in rows:
            stat = session.execute(
                select(PostStat).where(PostStat.post_id == row.id).order_by(PostStat.collected_at.desc())
            ).scalars().first()
            if stat is not None:
                stats[row.id] = stat

    if not rows:
        return StyleProfile(channel_id=channel_id)

    scored = [(p, _post_quality(p, stats.get(p.id))) for p in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    keep = [p for p, q in scored if q > min_quality] or [p for p, _ in scored]
    keep = keep[: max(1, len(keep) // 2)] or keep

    feats = [extract_style_features(f"{p.hook}\n{p.body}") for p in keep]
    token_counter: Counter[str] = Counter()
    for p in keep:
        token_counter.update(tokens(f"{p.hook} {p.body}"))

    avg_features = StyleFeatures(
        chars=int(mean(f.chars for f in feats)),
        paragraphs=int(mean(f.paragraphs for f in feats)),
        sentences=int(mean(f.sentences for f in feats)),
        questions=int(mean(f.questions for f in feats)),
        numbers=int(mean(f.numbers for f in feats)),
        emoji=int(mean(f.emoji for f in feats)),
        bullets=int(mean(f.bullets for f in feats)),
        avg_sentence_len=round(mean(f.avg_sentence_len for f in feats), 2),
        hashtag_count=int(mean(f.hashtag_count for f in feats)),
    )
    qualities = [q for _, q in scored if q > 0]
    return StyleProfile(
        channel_id=channel_id,
        samples=len(keep),
        features=avg_features,
        top_tokens=[t for t, _ in token_counter.most_common(12)],
        quality=round(mean(qualities), 4) if qualities else 0.0,
    )


def style_score(text: str, profile: StyleProfile) -> float:
    if profile.samples == 0:
        return 0.5
    f = extract_style_features(text)
    p = profile.features
    def closeness(a, b, scale):
        if scale <= 0:
            return 1.0
        return max(0.0, 1.0 - abs(a - b) / scale)
    score = (
        closeness(f.chars, p.chars, max(200, p.chars)) * 0.25
        + closeness(f.paragraphs, p.paragraphs, max(1, p.paragraphs)) * 0.15
        + closeness(f.avg_sentence_len, p.avg_sentence_len, max(3, p.avg_sentence_len)) * 0.25
        + closeness(f.questions, p.questions, max(1, p.questions + 1)) * 0.1
        + closeness(f.emoji, p.emoji, max(1, p.emoji + 1)) * 0.1
        + closeness(f.bullets, p.bullets, max(1, p.bullets + 1)) * 0.15
    )
    return round(min(1.0, score), 4)


__all__ = ["StyleFeatures", "StyleProfile", "extract_style_features", "style_profile", "style_score"]

"""Unified CommunityPolicy contract for platform community/section targets.

The farm selects a concrete destination (Pikabu community, vc.ru topic,
subreddit) before publishing.  Existing live selectors
(:mod:`tgfarm.community_targets`, :mod:`tgfarm.s9_growth.reddit_scout`) return
platform-specific candidate dicts.  This module normalises the chosen
destination into one auditable contract that the publication pipeline (P6) can
re-check right before sending.

Rules fetched from external sites are untrusted data: the contract stores only
short inert excerpts and metadata, never executes them, and never turns them
into instructions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lines(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Mapping):
        return tuple(f"{k}: {v}" for k, v in value.items() if str(v).strip())
    return tuple(str(item) for item in value if str(item).strip())


def _combined_rules_available(candidate: Mapping[str, Any]) -> bool:
    if "rules_available" in candidate:
        return bool(candidate["rules_available"])
    if "topic_rules_available" in candidate:
        global_ok = candidate.get("global_rules_available", True)
        return bool(candidate["topic_rules_available"]) and bool(global_ok)
    return False


@dataclass(frozen=True)
class CommunityPolicy:
    """Normalised publication policy of one concrete destination."""

    platform: str
    target_type: str  # "community" | "topic" | "subreddit"
    target_id: str
    name: str
    url: str = ""
    audience: str = ""
    allowed_topics: tuple[str, ...] = ()
    forbidden_topics: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    media_limits: tuple[str, ...] = ()
    link_rules: str = ""
    tag_requirements: tuple[str, ...] = ()
    checked_at: str = field(default_factory=_utcnow_iso)
    rules_source: str = ""
    rules_available: bool = False
    auto_publish_supported: bool = False
    confidence: float = 0.0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for spec in fields(self):
            if spec.type.startswith("tuple"):
                data[spec.name] = list(data[spec.name])
        return data


def _clamp01(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(1.0, round(score, 3)))


def policy_from_candidate(
    platform: str,
    candidate: Mapping[str, Any],
    *,
    rules_source: str = "",
) -> CommunityPolicy:
    """Build the contract from a Pikabu/vc.ru selector candidate."""
    notes = [str(x) for x in (candidate.get("risks") or ())]
    notes += [str(x) for x in (candidate.get("blockers") or ())]
    return CommunityPolicy(
        platform=platform,
        target_type=str(candidate.get("target_type") or "community"),
        target_id=str(
            candidate.get("slug") or candidate.get("path") or candidate.get("name") or ""
        ),
        name=str(candidate.get("name") or candidate.get("title") or ""),
        url=str(candidate.get("url") or ""),
        audience=str(candidate.get("description") or ""),
        allowed_topics=_lines(candidate.get("matches")),
        requirements=_lines(candidate.get("editorial_brief")),
        media_limits=_lines(candidate.get("media_limits")),
        link_rules=str(candidate.get("link_rules") or ""),
        tag_requirements=_lines(candidate.get("suggested_tags")),
        checked_at=_utcnow_iso(),
        rules_source=str(candidate.get("rules_source") or rules_source),
        rules_available=_combined_rules_available(candidate),
        auto_publish_supported=bool(candidate.get("auto_publish_supported", False)),
        confidence=_clamp01(candidate.get("score")),
        notes=tuple(notes),
    )


def policy_from_reddit(
    community: Mapping[str, Any],
    *,
    subreddit: str,
    rules: Any = None,
    requirements: Any = None,
    rules_source: str = "https://www.reddit.com/r/{}/about/rules",
) -> CommunityPolicy:
    """Build the contract from a Reddit scout selection."""
    return CommunityPolicy(
        platform="reddit",
        target_type="subreddit",
        target_id=subreddit,
        name=str(community.get("title") or subreddit),
        url=str(community.get("url") or f"https://www.reddit.com/r/{subreddit}/"),
        audience=str(community.get("description") or ""),
        requirements=_lines(rules) + _lines(requirements),
        checked_at=_utcnow_iso(),
        rules_source=rules_source.format(subreddit),
        rules_available=bool(rules),
        confidence=_clamp01(community.get("subscribers", 0) and 0.5),
        notes=(
            f"subscribers: {community.get('subscribers', 0)}",
            f"active: {community.get('active_user_count', 0)}",
        ),
    )


def assess_policy(
    policy: CommunityPolicy,
    *,
    max_age_hours: float = 24.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publication gate: refuse on unknown or stale rules.

    Returns ``{"status": "ok"|"stale"|"unknown", "reason": str,
    "blockers": [...]}``.  ``unknown`` and ``stale`` forbid automatic
    publishing; a stale policy must be re-validated by re-running the live
    selector for the same destination.
    """
    if not policy.rules_available or not policy.target_id:
        return {
            "status": "unknown",
            "reason": "rules of the destination are unknown — publishing is forbidden",
            "blockers": ["rules_unavailable"],
        }
    try:
        checked = datetime.fromisoformat(policy.checked_at)
    except ValueError:
        return {
            "status": "unknown",
            "reason": "rules check timestamp is unreadable — publishing is forbidden",
            "blockers": ["checked_at_invalid"],
        }
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    age = moment - checked
    if age > timedelta(hours=max_age_hours):
        return {
            "status": "stale",
            "reason": (
                f"rules were checked {age.total_seconds() / 3600:.1f} h ago "
                f"(limit {max_age_hours:g} h) — re-run the selector before publishing"
            ),
            "blockers": ["rules_stale"],
        }
    return {"status": "ok", "reason": "rules are fresh enough", "blockers": []}

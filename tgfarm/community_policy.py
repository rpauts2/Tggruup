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

Contract decisions (2026-11-04)
-------------------------------
* ``requirements`` keeps the destination's own rules **first** (verbatim, inert)
  followed by the selector's editorial brief.  Dropping the raw rule text would
  leave the gate and the operator without the only evidence of what the
  community actually demands; test ``test_to_dict_round_trip_and_inert_rules``
  encodes that safety property.
* ``rules_source`` is never empty for a selector-produced policy: provenance is
  part of the contract, so an audit can tell where the rules came from even when
  the candidate dict omits it.
* ``assess_policy`` returns ``allow_publish`` explicitly; callers must gate on
  that boolean instead of re-deriving the status themselves.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: Provenance marker used when the selector dict does not carry its own source.
SELECTOR_RULES_SOURCE = "selector:tgfarm.community_targets"

#: Rules older than this must be re-read before an automatic publication.
DEFAULT_MAX_AGE_HOURS = 24.0

#: How much raw rule text is kept in the contract for audit/UI purposes.
RULES_EXCERPT_LIMIT = 400


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lines(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Mapping):
        return tuple(f"{k}: {v}" for k, v in value.items() if str(v).strip())
    return tuple(str(item).strip() for item in value if str(item).strip())


def _rule_line(item: Any) -> str:
    """Render one Reddit rule object (or plain string) as an inert line."""
    if isinstance(item, Mapping):
        name = str(
            item.get("short_name") or item.get("name") or item.get("title") or ""
        ).strip()
        body = str(item.get("description") or item.get("violation_reason") or "").strip()
        if name and body:
            return f"{name} — {body}"
        return name or body
    return str(item).strip()


def _rules_lines(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return _lines(value)
    if isinstance(value, Mapping):
        return _lines(value)
    return tuple(line for line in (_rule_line(item) for item in value) if line)


def _combined_rules_available(candidate: Mapping[str, Any]) -> bool:
    if "rules_available" in candidate:
        return bool(candidate["rules_available"])
    if "topic_rules_available" in candidate:
        global_ok = candidate.get("global_rules_available", True)
        return bool(candidate["topic_rules_available"]) and bool(global_ok)
    return False


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    rules_excerpt: str = ""
    rules_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for spec in fields(self):
            if str(spec.type).startswith("tuple"):
                data[spec.name] = list(data[spec.name])
        return data

    def target_key(self) -> str:
        """Stable key of the destination, used by the rules-history table."""
        return f"{self.platform}:{self.target_type}:{self.target_id}"


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
    auto_publish_supported: bool | None = None,
) -> CommunityPolicy:
    """Build the contract from a Pikabu/vc.ru selector candidate."""
    candidate = candidate or {}
    notes = [str(x) for x in (candidate.get("risks") or ())]
    notes += [str(x) for x in (candidate.get("blockers") or ())]
    raw_rules = str(candidate.get("rules") or "").strip()
    requirements = _rules_lines(raw_rules) + _lines(candidate.get("editorial_brief"))
    available = _combined_rules_available(candidate)
    source = str(candidate.get("rules_source") or rules_source or "").strip()
    if not source:
        source = SELECTOR_RULES_SOURCE
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
        requirements=requirements,
        media_limits=_lines(candidate.get("media_limits")),
        link_rules=str(candidate.get("link_rules") or ""),
        tag_requirements=_lines(candidate.get("suggested_tags")),
        checked_at=_utcnow_iso(),
        rules_source=source,
        rules_available=available,
        auto_publish_supported=bool(
            candidate.get("auto_publish_supported", False)
            if auto_publish_supported is None
            else auto_publish_supported
        ),
        confidence=_clamp01(candidate.get("score")),
        notes=tuple(notes),
        rules_excerpt=raw_rules[:RULES_EXCERPT_LIMIT],
        rules_hash=_digest(raw_rules) if raw_rules else "",
    )


def _reddit_about(community: Mapping[str, Any]) -> Mapping[str, Any]:
    about = community.get("about")
    return about if isinstance(about, Mapping) else community


def policy_from_reddit(
    community: Mapping[str, Any],
    *,
    subreddit: str | None = None,
    rules: Any = None,
    requirements: Any = None,
    rules_source: str = "https://www.reddit.com/r/{subreddit}/about/rules",
) -> CommunityPolicy:
    """Build the contract from a Reddit scout selection.

    ``community`` may be a discovery candidate (``title``/``url``/
    ``description``) or the ``inspect_community`` payload (``about``/``rules``).
    When ``rules`` is omitted the rules of an inspect payload are used.
    """
    community = community or {}
    about = _reddit_about(community)
    name = (
        str(subreddit or community.get("name") or "").strip()
        or str(community.get("title") or "").strip().removeprefix("r/")
        or str(about.get("display_name") or "").strip()
    )
    if not name:
        url = str(community.get("url") or about.get("url") or "")
        name = url.rstrip("/").rsplit("/", 1)[-1]
    rules_value = rules if rules is not None else community.get("rules")
    rule_lines = _rules_lines(rules_value)
    extra = _lines(requirements)
    raw_rules = "\n".join(rule_lines)
    source = str(rules_source or "")
    if "{subreddit}" in source:
        source = source.format(subreddit=name)
    elif "{}" in source:
        source = source.format(name)
    title = str(community.get("title") or about.get("title") or name)
    audience = str(
        community.get("description")
        or about.get("public_description")
        or about.get("description")
        or ""
    )
    return CommunityPolicy(
        platform="reddit",
        target_type="subreddit",
        target_id=name,
        name=title if title.lower().startswith("r/") else f"r/{name}",
        url=str(community.get("url") or about.get("url") or f"https://www.reddit.com/r/{name}/"),
        audience=audience,
        requirements=rule_lines + extra,
        checked_at=_utcnow_iso(),
        rules_source=source,
        rules_available=bool(rule_lines),
        auto_publish_supported=bool(community.get("auto_publish_supported", True)),
        confidence=_clamp01(community.get("score")),
        notes=(
            f"subscribers: {community.get('subscribers') or about.get('subscribers') or 0}",
            f"active: {community.get('active_user_count') or about.get('active_user_count') or 0}",
        ),
        rules_excerpt=raw_rules[:RULES_EXCERPT_LIMIT],
        rules_hash=_digest(raw_rules) if raw_rules else "",
    )


def policy_for_plan(
    platform: str,
    plan: Mapping[str, Any] | None,
    *,
    subreddit: str | None = None,
) -> CommunityPolicy | None:
    """Build the contract from a selector plan dict (``selected`` + metadata).

    Returns ``None`` when the selector did not pick a destination: there is no
    policy to assess, and callers must treat that as «nothing to publish».
    """
    plan = plan or {}
    selected = plan.get("selected")
    if not isinstance(selected, Mapping) or not selected:
        return None
    source = str(plan.get("rules_source") or "")
    auto = plan.get("auto_publish_supported")
    if platform.lower().startswith("reddit") or selected.get("target_type") == "subreddit":
        return policy_from_reddit(
            selected,
            subreddit=subreddit or str(selected.get("name") or "") or None,
            rules_source=source or "https://www.reddit.com/r/{subreddit}/about/rules",
        )
    return policy_from_candidate(
        platform,
        selected,
        rules_source=source,
        auto_publish_supported=None if auto is None else bool(auto),
    )


def assess_policy(
    policy: CommunityPolicy,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publication gate: refuse on unknown or stale rules.

    Returns ``{"status": "ok"|"stale"|"unknown", "allow_publish": bool,
    "reason": str, "blockers": [...], "age_hours": float | None}``.
    ``unknown`` and ``stale`` forbid automatic publishing; a stale policy must
    be re-validated by re-running the live selector for the same destination.
    """
    base: dict[str, Any] = {
        "status": "unknown",
        "allow_publish": False,
        "reason": "",
        "blockers": [],
        "age_hours": None,
        "checked_at": policy.checked_at,
        "auto_publish": False,
        "target_key": policy.target_key(),
    }
    if not policy.rules_available or not policy.target_id:
        return {
            **base,
            "reason": "rules of the destination are unknown — publishing is forbidden",
            "blockers": ["rules_unavailable"],
        }
    try:
        checked = datetime.fromisoformat(policy.checked_at)
    except ValueError:
        return {
            **base,
            "reason": "rules check timestamp is unreadable — publishing is forbidden",
            "blockers": ["checked_at_invalid"],
        }
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = moment - checked
    age_hours = round(age.total_seconds() / 3600, 2)
    if age > timedelta(hours=max_age_hours):
        return {
            **base,
            "status": "stale",
            "reason": (
                f"rules were checked {age_hours:.1f} h ago "
                f"(limit {max_age_hours:g} h) — re-run the selector before publishing"
            ),
            "blockers": ["rules_stale"],
            "age_hours": age_hours,
        }
    return {
        **base,
        "status": "ok",
        "allow_publish": True,
        "reason": "rules are fresh enough",
        "blockers": [],
        "age_hours": age_hours,
        "auto_publish": bool(policy.auto_publish_supported),
    }


def gate_plan(
    platform: str,
    plan: Mapping[str, Any] | None,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
    subreddit: str | None = None,
) -> dict[str, Any]:
    """Assess a selector plan in one call; safe for API responses."""
    policy = policy_for_plan(platform, plan, subreddit=subreddit)
    if policy is None:
        return {
            "status": "no_target",
            "allow_publish": False,
            "auto_publish": False,
            "reason": "selector did not choose a destination — nothing to publish",
            "blockers": ["no_target"],
            "age_hours": None,
            "target_key": "",
            "policy": None,
        }
    verdict = assess_policy(policy, max_age_hours=max_age_hours, now=now)
    return {**verdict, "policy": policy.to_dict()}


# --------------------------------------------------------------------------
# Rules history (append-only), so a rules change is visible after the fact.
# --------------------------------------------------------------------------


def record_policy_snapshot(
    policy: CommunityPolicy,
    verdict: Mapping[str, Any] | None = None,
    *,
    min_interval_minutes: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one rules check; report whether the rules themselves changed.

    Consecutive identical checks of the same destination are collapsed (default
    one hour apart) so the history stays readable: the table records *rule
    changes and gate outcomes*, not every panel refresh.  Never raises: a
    broken history must not block publishing, but the failure is logged.
    """
    verdict = verdict or {}
    moment = now or datetime.now(timezone.utc)
    try:
        from sqlalchemy import select

        from tgfarm.db.models import CommunityPolicySnapshot
        from tgfarm.db.session import session_scope

        with session_scope() as session:
            last = session.scalars(
                select(CommunityPolicySnapshot)
                .where(
                    CommunityPolicySnapshot.platform == policy.platform,
                    CommunityPolicySnapshot.target_id == policy.target_id,
                )
                .order_by(CommunityPolicySnapshot.id.desc())
                .limit(1)
            ).first()
            status = str(verdict.get("status") or "unknown")
            changed = last is None or last.rules_hash != policy.rules_hash
            if last is not None and not changed and last.verdict_status == status:
                recorded_at = last.recorded_at
                if recorded_at is not None:
                    if recorded_at.tzinfo is None:
                        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
                    if moment - recorded_at < timedelta(minutes=min_interval_minutes):
                        return {
                            "recorded": False,
                            "changed": False,
                            "reason": "duplicate within the collapse window",
                            "id": last.id,
                            "previous_hash": last.rules_hash,
                            "status": status,
                        }
            row = CommunityPolicySnapshot(
                platform=policy.platform,
                target_type=policy.target_type,
                target_id=policy.target_id,
                target_name=policy.name[:200],
                rules_source=policy.rules_source[:500],
                rules_hash=policy.rules_hash,
                rules_available=bool(policy.rules_available),
                rules_excerpt=policy.rules_excerpt,
                verdict_status=status,
                confidence=float(policy.confidence),
                requirements_json=list(policy.requirements)[:40],
                checked_at=_parse_iso(policy.checked_at) or moment,
                recorded_at=moment,
            )
            session.add(row)
            session.flush()
            return {
                "recorded": True,
                "changed": changed,
                "previous_hash": last.rules_hash if last is not None else None,
                "id": row.id,
                "status": status,
                "rules_source": policy.rules_source,
            }
    except Exception as exc:  # noqa: BLE001 - history must never break publishing
        log.warning("community policy snapshot was not stored: %s", type(exc).__name__)
        return {"recorded": False, "changed": None, "reason": "storage_error", "error": type(exc).__name__}


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def policy_history(
    *,
    platform: str | None = None,
    target_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest-first rules history, optionally filtered by destination."""
    try:
        from sqlalchemy import select

        from tgfarm.db.models import CommunityPolicySnapshot
        from tgfarm.db.session import session_scope

        limit = max(1, min(int(limit), 200))
        with session_scope() as session:
            query = select(CommunityPolicySnapshot)
            if platform:
                query = query.where(CommunityPolicySnapshot.platform == str(platform))
            if target_id:
                query = query.where(CommunityPolicySnapshot.target_id == str(target_id))
            rows = session.scalars(
                query.order_by(CommunityPolicySnapshot.id.desc()).limit(limit)
            ).all()
            return [
                {
                    "id": row.id,
                    "platform": row.platform,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "target_name": row.target_name,
                    "rules_source": row.rules_source,
                    "rules_hash": row.rules_hash,
                    "rules_available": bool(row.rules_available),
                    "rules_excerpt": row.rules_excerpt,
                    "verdict_status": row.verdict_status,
                    "confidence": row.confidence,
                    "requirements": list(row.requirements_json or []),
                    "checked_at": row.checked_at.isoformat() if row.checked_at else "",
                    "recorded_at": row.recorded_at.isoformat() if row.recorded_at else "",
                }
                for row in rows
            ]
    except Exception as exc:  # noqa: BLE001 - the panel must render without history
        log.warning("community policy history is unavailable: %s", type(exc).__name__)
        return []


def latest_policy(platform: str, target_id: str) -> dict[str, Any] | None:
    """Newest stored rules snapshot of one destination, or ``None``."""
    rows = policy_history(platform=platform, target_id=target_id, limit=1)
    return rows[0] if rows else None

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
* Every gate input is a *structured* field.  Free text never becomes a permission:
  ``rules_available``/``auto_publish_supported`` are accepted only as real booleans
  (:func:`_as_flag`), and a policy flagged "rules available" without a single
  stored rule line is an explicit ``unknown``, not a pass.
* All destination-authored strings (rules, excerpts, descriptions, tags, notes)
  leave this module inert: control/bidi characters flattened, credential-shaped
  substrings removed via :func:`tgfarm.core.security.redact_sensitive_text`, and
  length bounded per line and per field.  The stored ``rules_hash`` is the only
  fingerprint of the *raw* page, so a change below the visible excerpt still shows
  up in the history.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import logging
import re
from typing import Any, Mapping

from tgfarm.core.security import redact_sensitive_text

log = logging.getLogger(__name__)

#: Provenance marker used when the selector dict does not carry its own source.
SELECTOR_RULES_SOURCE = "selector:tgfarm.community_targets"

#: Rules older than this must be re-read before an automatic publication.
DEFAULT_MAX_AGE_HOURS = 24.0

#: How much raw rule text is kept in the contract for audit/UI purposes.
RULES_EXCERPT_LIMIT = 400

#: Cap of one stored rule/requirement line.  A destination can pad a page with an
#: arbitrarily long "rule"; the contract keeps a bounded inert fragment instead.
RULES_LINE_LIMIT = 200

#: Cap of how many lines of one untrusted field the contract keeps.
RULES_FIELD_LIMIT = 12

#: Cap of one topic/tag/media-limit line coming from a destination page.
TOPIC_LINE_LIMIT = 120

#: Cap of the destination description kept as ``audience``.
AUDIENCE_LIMIT = 500

#: Cap of one risk/blocker note (selector text about an external page).
NOTE_LINE_LIMIT = 200

#: Cap of ``link_rules`` and of the source URL carried with the policy.
LINK_RULES_LIMIT = 400
URL_LIMIT = 500

#: Cap of the destination name/id stored in the history row.
TARGET_NAME_LIMIT = 160
TARGET_ID_LIMIT = 200

#: How many requirement lines one history row keeps (the panel shows six).
SNAPSHOT_REQUIREMENT_LIMIT = 40

#: Verdict statuses are a closed vocabulary; a hostile page must not invent one.
VERDICT_STATUSES = ("ok", "stale", "unknown", "no_target")

#: Zero-width, bidi, control and line-separator characters are the classic way to
#: smuggle an instruction past a length check or to spoof reading order in the UI.
_INERT_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_INERT_SPACE_RE = re.compile(r"\s+")

#: Values that mean "this flag is on" when a flag arrives as a short token.
_TRUTHY_FLAGS = frozenset({"1", "true", "yes", "on", "y", "да", "истина"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inert_text(value: Any, limit: int) -> str:
    """Flatten external text into one bounded, credential-free inert fragment.

    Order matters: invisible characters are removed *before* redaction so a token
    split by a zero-width character is reassembled and then removed, and the
    length bound is applied *after* redaction so a cut cannot leave half a
    credential behind.
    """
    bound = max(0, int(limit))
    if bound == 0:
        return ""
    text = _INERT_CHARS_RE.sub(" ", str(value if value is not None else ""))
    text = _INERT_SPACE_RE.sub(" ", text).strip()
    if not text:
        return ""
    text = redact_sensitive_text(text, limit=max(len(text), bound))
    if len(text) > bound:
        text = f"{text[: max(1, bound - 1)].rstrip()}\u2026"
    return text


def _as_flag(value: Any, *, default: bool = False) -> bool:
    """Coerce a gate flag without ever turning free text into a permission.

    ``bool("нет")`` and ``bool("ignore previous instructions")`` are both ``True``,
    so a truthiness test on a field that came from a page would hand out publish
    rights.  Only real booleans, 0/1 numbers and a short closed vocabulary count.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in _TRUTHY_FLAGS


def _verdict_status(value: Any) -> str:
    """Clamp a stored/returned verdict to the closed vocabulary.

    An invented status would otherwise be printed by the panel as if it were a
    real gate outcome; anything unknown is reported as ``unknown``.
    """
    status = _inert_text(value, 16).strip().lower()
    return status if status in VERDICT_STATUSES else "unknown"


def inert_external_text(value: Any, limit: int = RULES_EXCERPT_LIMIT) -> str:
    """Public form of :func:`_inert_text` for selectors that keep raw page text.

    A candidate dict produced by a selector travels on its own (panel JSON, the
    editorial ``target_context`` handed to the model); page content must be inert
    there as well as inside the contract.
    """
    return _inert_text(value, limit)


def _lines(value: Any, *, limit: int = RULES_LINE_LIMIT) -> tuple[str, ...]:
    """Normalise one untrusted field into bounded inert lines.

    A multi-line rule block stays *split*: flattening 3000 characters into one
    line would throw away every requirement after the length cap, while splitting
    keeps the visible evidence and still bounds it per line and in total.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw = (part for part in re.split(r"[\n\r\u2028\u2029]+", value) if part.strip())
    elif isinstance(value, Mapping):
        raw = (f"{k}: {v}" for k, v in value.items() if str(v).strip())
    else:
        raw = (str(item) for item in value)
    return tuple(line for line in (_inert_text(item, limit) for item in raw) if line)[
        :RULES_FIELD_LIMIT
    ]


def _rule_line(item: Any) -> str:
    """Render one Reddit rule object (or plain string) as an inert line."""
    if isinstance(item, Mapping):
        name = _inert_text(
            item.get("short_name") or item.get("name") or item.get("title") or "",
            RULES_LINE_LIMIT,
        )
        body = _inert_text(
            item.get("description") or item.get("violation_reason") or "",
            RULES_LINE_LIMIT,
        )
        if name and body:
            return _inert_text(f"{name} — {body}", RULES_LINE_LIMIT)
        return name or body
    return _inert_text(item, RULES_LINE_LIMIT)


def _rules_lines(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return _lines(value)
    if isinstance(value, Mapping):
        return _lines(value)
    return tuple(line for line in (_rule_line(item) for item in value) if line)[:RULES_FIELD_LIMIT]


def _combined_rules_available(candidate: Mapping[str, Any]) -> bool:
    if "rules_available" in candidate:
        return _as_flag(candidate["rules_available"])
    if "topic_rules_available" in candidate:
        global_ok = _as_flag(candidate.get("global_rules_available"), default=True)
        return _as_flag(candidate["topic_rules_available"]) and global_ok
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
    notes = [
        _inert_text(item, NOTE_LINE_LIMIT)
        for item in [*(candidate.get("risks") or ()), *(candidate.get("blockers") or ())]
    ]
    notes = [note for note in notes if note]
    raw_rules = str(candidate.get("rules") or "").strip()
    requirements = _rules_lines(raw_rules) + _lines(candidate.get("editorial_brief"))
    available = _combined_rules_available(candidate)
    source = _inert_text(candidate.get("rules_source") or rules_source or "", URL_LIMIT)
    if not source:
        source = SELECTOR_RULES_SOURCE
    auto_flag = (
        _as_flag(candidate.get("auto_publish_supported"), default=False)
        if auto_publish_supported is None
        else bool(auto_publish_supported)
    )
    return CommunityPolicy(
        platform=_inert_text(platform, 32),
        target_type=_inert_text(candidate.get("target_type") or "community", 32),
        target_id=_inert_text(
            candidate.get("slug") or candidate.get("path") or candidate.get("name") or "",
            TARGET_ID_LIMIT,
        ),
        name=_inert_text(candidate.get("name") or candidate.get("title") or "", TARGET_NAME_LIMIT),
        url=_inert_text(candidate.get("url") or "", URL_LIMIT),
        audience=_inert_text(candidate.get("description") or "", AUDIENCE_LIMIT),
        allowed_topics=_lines(candidate.get("matches"), limit=TOPIC_LINE_LIMIT),
        requirements=requirements,
        media_limits=_lines(candidate.get("media_limits"), limit=TOPIC_LINE_LIMIT),
        link_rules=_inert_text(candidate.get("link_rules") or "", LINK_RULES_LIMIT),
        tag_requirements=_lines(candidate.get("suggested_tags"), limit=TOPIC_LINE_LIMIT),
        checked_at=_utcnow_iso(),
        rules_source=source,
        rules_available=available,
        auto_publish_supported=auto_flag,
        confidence=_clamp01(candidate.get("score")),
        notes=tuple(notes)[:RULES_FIELD_LIMIT],
        rules_excerpt=_inert_text(raw_rules, RULES_EXCERPT_LIMIT),
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
        _inert_text(subreddit or community.get("name") or "", TARGET_ID_LIMIT)
        or _inert_text(str(community.get("title") or "").strip().removeprefix("r/"), TARGET_ID_LIMIT)
        or _inert_text(about.get("display_name") or "", TARGET_ID_LIMIT)
    )
    if not name:
        url = str(community.get("url") or about.get("url") or "")
        name = _inert_text(url.rstrip("/").rsplit("/", 1)[-1], TARGET_ID_LIMIT)
    rules_value = rules if rules is not None else community.get("rules")
    rule_lines = _rules_lines(rules_value)
    extra = _lines(requirements)
    raw_rules = "\n".join(str(item) for item in (rules_value or ()) if item) if not isinstance(
        rules_value, (str, Mapping)
    ) else str(rules_value or "")
    source = str(rules_source or "")
    if "{subreddit}" in source:
        source = source.format(subreddit=name)
    elif "{}" in source:
        source = source.format(name)
    source = _inert_text(source, URL_LIMIT)
    title = _inert_text(community.get("title") or about.get("title") or name, TARGET_NAME_LIMIT)
    audience = _inert_text(
        community.get("description")
        or about.get("public_description")
        or about.get("description")
        or "",
        AUDIENCE_LIMIT,
    )
    def _measured(*values: Any) -> str | None:
        """First *measured* number, keeping a real 0 as 0 and nothing as ``None``."""
        for value in values:
            if value is not None:
                return _inert_text(value, 40)
        return None

    subscribers = _measured(community.get("subscribers"), about.get("subscribers"))
    active_users = _measured(
        community.get("active_user_count"), about.get("active_user_count")
    )
    return CommunityPolicy(
        platform="reddit",
        target_type="subreddit",
        target_id=name,
        name=title if title.casefold().startswith("r/") else f"r/{name}",
        url=_inert_text(
            community.get("url") or about.get("url") or f"https://www.reddit.com/r/{name}/",
            URL_LIMIT,
        ),
        audience=audience,
        requirements=(rule_lines + extra)[:RULES_FIELD_LIMIT],
        checked_at=_utcnow_iso(),
        rules_source=source,
        rules_available=bool(rule_lines),
        auto_publish_supported=_as_flag(community.get("auto_publish_supported"), default=True),
        confidence=_clamp01(community.get("score")),
        notes=(
            _inert_text(
                f"subscribers: {subscribers if subscribers is not None else 'нет данных'}",
                NOTE_LINE_LIMIT,
            ),
            _inert_text(
                f"active: {active_users if active_users is not None else 'нет данных'}",
                NOTE_LINE_LIMIT,
            ),
        ),
        rules_excerpt=_inert_text(raw_rules, RULES_EXCERPT_LIMIT),
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
        auto_publish_supported=None if auto is None else _as_flag(auto),
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
    ``unknown`` also covers a policy that claims available rules while storing no
    rule text at all (blocker ``rules_evidence_missing``): absence is never a pass,
    and an unmeasured age is reported as ``None``, not as a fresh ``0``.
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
    if not _as_flag(policy.rules_available) or not str(policy.target_id or "").strip():
        return {
            **base,
            "reason": "rules of the destination are unknown — publishing is forbidden",
            "blockers": ["rules_unavailable"],
        }
    # A bare "the rules were read" flag with nothing stored behind it is not
    # knowledge: the flag may have been set by a page that returned no rule text
    # at all.  Absence stays an explicit unknown instead of becoming a pass.
    if not "".join(str(line) for line in policy.requirements).strip() and not str(
        policy.rules_excerpt or ""
    ).strip():
        return {
            **base,
            "reason": (
                "rules are flagged as available but no rule text is stored — "
                "publishing is forbidden"
            ),
            "blockers": ["rules_evidence_missing"],
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
        "auto_publish": _as_flag(policy.auto_publish_supported),
    }


def gate_plan(
    platform: str,
    plan: Mapping[str, Any] | None,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
    subreddit: str | None = None,
    record: bool = False,
) -> dict[str, Any]:
    """Assess a selector plan in one call; safe for API responses.

    ``record=True`` additionally writes the assessed policy to the rules history
    (:func:`record_policy_snapshot`), so a rules check that the operator actually
    ran is auditable instead of living only in the panel's memory.  The recorded
    row is the *same* policy object the verdict was derived from, so the stored
    ``checked_at`` and ``rules_hash`` describe exactly what was judged.
    """
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
    if record:
        record_policy_snapshot(policy, verdict, now=now)
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
            status = _verdict_status(verdict.get("status"))
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
                # Identity fields are stored as the contract holds them: an empty
                # name stays empty (the panel then falls back to the id) instead of
                # being replaced by an invented value.
                platform=_inert_text(policy.platform, 32),
                target_type=_inert_text(policy.target_type, 32),
                target_id=_inert_text(policy.target_id, TARGET_ID_LIMIT),
                target_name=_inert_text(policy.name, TARGET_NAME_LIMIT),
                rules_source=_inert_text(policy.rules_source, URL_LIMIT)[:500],
                rules_hash=_inert_text(policy.rules_hash, 64),
                rules_available=_as_flag(policy.rules_available),
                rules_excerpt=_inert_text(policy.rules_excerpt, RULES_EXCERPT_LIMIT),
                verdict_status=status[:16],
                confidence=float(policy.confidence),
                requirements_json=[
                    _inert_text(line, RULES_LINE_LIMIT)
                    for line in list(policy.requirements or ())[:SNAPSHOT_REQUIREMENT_LIMIT]
                    if str(line).strip()
                ],
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
                    "platform": _inert_text(row.platform, 32),
                    "target_type": _inert_text(row.target_type, 32),
                    "target_id": _inert_text(row.target_id, TARGET_ID_LIMIT),
                    "target_name": _inert_text(row.target_name, TARGET_NAME_LIMIT),
                    "rules_source": _inert_text(row.rules_source, URL_LIMIT),
                    "rules_hash": _inert_text(row.rules_hash, 64),
                    "rules_available": _as_flag(row.rules_available),
                    # A row written before the contract bounded its text still has
                    # to leave this function inert, bounded and credential-free.
                    "rules_excerpt": _inert_text(row.rules_excerpt, RULES_EXCERPT_LIMIT),
                    "verdict_status": _verdict_status(row.verdict_status),
                    "confidence": float(row.confidence or 0.0),
                    "requirements": [
                        _inert_text(line, RULES_LINE_LIMIT)
                        for line in list(row.requirements_json or ())[:SNAPSHOT_REQUIREMENT_LIMIT]
                        if str(line).strip()
                    ],
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


# --------------------------------------------------------------------------
# Fetched-rules writer and freshness reader (W29-C)
# --------------------------------------------------------------------------
#
# Why this exists as its own seam: every older writer of a snapshot is a
# *side effect of a bigger action* — a selector run, a panel click, a publish
# preflight — so the history only grows while somebody is doing something else
# on that platform.  The live database proved that on 2026-11-05: 0 rows while
# 2 009 maintenance passes reported ``ok``.  These two functions are the piece
# a scheduled layer can call on its own: one destination + one fetched rules
# document in, one auditable row (or one named refusal) out.
#
# The two refusals are the honest part.  A failed fetch stores nothing, because
# an empty ``rules_hash`` reads back through the change detection as "the rules
# changed" — a statement about a destination that was never measured.  And the
# reader never answers ``0`` for a destination it has never read: absence, an
# unreadable stamp and a broken store are three different states, the way
# ``tgfarm.editorial_plan`` keeps ``plan_columns_missing`` apart from "nothing
# is booked".

#: Closed vocabulary of :func:`rules_freshness`.  A state outside this tuple
#: must never be invented from page text: the destination can describe its own
#: rules, not the trustworthiness of our own store.
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_MISSING = "missing"
FRESHNESS_UNREAD = "unread"
FRESHNESS_STATES = (
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_MISSING,
    FRESHNESS_UNREAD,
)

#: Named refusals of :func:`record_rules_snapshot`, so a caller can tell "this
#: fetch failed" from "the store rejected the row" without parsing a log line.
SNAPSHOT_REFUSAL_FETCH_FAILED = "fetch_failed"
SNAPSHOT_REFUSAL_EMPTY_RULES = "empty_rules_text"
SNAPSHOT_REFUSAL_NO_TARGET = "no_target"
SNAPSHOT_REFUSAL_STORAGE_ERROR = "storage_error"


@dataclass(frozen=True)
class FetchedRules:
    """One external rules page, as *data* plus the fact of having read it.

    ``ok`` is the fetch's own structured outcome (set by our code from a status
    code or an exception), never something derived from the page text.  ``text``
    is untrusted: it is hashed, bounded and stored, and it is never looked at to
    decide whether the fetch succeeded, whether publishing is allowed, or what
    URL to read next.
    """

    url: str = ""
    text: str = ""
    ok: bool = True
    fetched_at: str = ""
    error: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> FetchedRules:
        """Adapt a selector/http result dict without letting it set a flag."""
        if isinstance(value, FetchedRules):
            return value
        data = value if isinstance(value, Mapping) else {}
        return cls(
            url=str(data.get("url") or data.get("rules_source") or ""),
            text=str(data.get("text") or data.get("rules") or ""),
            ok=_as_flag(data.get("ok"), default=True)
            and not data.get("error")
            and not isinstance(data.get("error"), BaseException),
            fetched_at=str(data.get("fetched_at") or data.get("checked_at") or ""),
            error=str(data.get("error") or ""),
        )


def _destination_identity(destination: Mapping[str, Any] | None) -> dict[str, str]:
    destination = destination if isinstance(destination, Mapping) else {}
    return {
        "platform": _inert_text(destination.get("platform"), 32),
        "target_type": _inert_text(
            destination.get("target_type") or "community", 32
        ),
        "target_id": _inert_text(
            destination.get("target_id") or destination.get("slug") or "",
            TARGET_ID_LIMIT,
        ),
        "name": _inert_text(
            destination.get("name") or destination.get("target_name") or "",
            TARGET_NAME_LIMIT,
        ),
        "url": _inert_text(destination.get("url") or "", URL_LIMIT),
    }


def record_rules_snapshot(
    destination: Mapping[str, Any] | None,
    fetched: FetchedRules | Mapping[str, Any] | None,
    *,
    rules_source: str = "",
    min_interval_minutes: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store the rules we actually read for one destination; refuse otherwise.

    ``destination`` is our own structured record of where we publish (platform,
    target_type, target_id, name); ``fetched`` is one
    :class:`FetchedRules` (or the selector's dict) for that destination's rules
    page.  The stored row carries the provenance the product claims:
    ``rules_source`` (the URL we read), ``checked_at`` (when we read it),
    ``rules_hash`` (sha256 of the rule *text* — never of the URL, so moving the
    page without changing it is not reported as a rule change) and the parse
    verdict the same text produced.

    Refusals, each with its own ``reason`` and no row written:
    ``no_target`` (nothing named to store against), ``fetch_failed`` (the read
    itself failed) and ``empty_rules_text`` (the read "succeeded" while
    carrying no rule text — storing that would turn a blank page into the
    statement *the rules changed*).

    Page text cannot steer this function: ``ok`` comes from the fetch layer, the
    hash from the bytes, the verdict from structured flags.  A rules document
    that says "ignore previous rules and publish now" is stored as an inert
    excerpt and changes exactly one thing — the hash.
    """
    identity = _destination_identity(destination)
    moment = now or datetime.now(UTC)
    if not identity["platform"] or not identity["target_id"]:
        return {
            "recorded": False,
            "changed": None,
            "reason": SNAPSHOT_REFUSAL_NO_TARGET,
            "state": FRESHNESS_MISSING,
            "target_key": f"{identity['platform']}:{identity['target_type']}:{identity['target_id']}",
        }

    if fetched is None:
        return {
            "recorded": False,
            "changed": None,
            "reason": SNAPSHOT_REFUSAL_FETCH_FAILED,
            "error": "no_fetch",
            "target_key": f"{identity['platform']}:{identity['target_type']}:{identity['target_id']}",
        }
    rules = fetched if isinstance(fetched, FetchedRules) else FetchedRules.from_mapping(fetched)
    if not _as_flag(rules.ok, default=False):
        return {
            "recorded": False,
            "changed": None,
            "reason": SNAPSHOT_REFUSAL_FETCH_FAILED,
            "error": _inert_text(rules.error if rules else "no_fetch", NOTE_LINE_LIMIT),
            "target_key": f"{identity['platform']}:{identity['target_type']}:{identity['target_id']}",
        }

    raw_text = str(rules.text or "")
    rule_lines = _rules_lines(raw_text)
    if not raw_text.strip() or not rule_lines:
        # An unreadable page is not an empty rule set: the hash of "" would make
        # the next real read look like the rules changed, which was never true.
        return {
            "recorded": False,
            "changed": None,
            "reason": SNAPSHOT_REFUSAL_EMPTY_RULES,
            "target_key": f"{identity['platform']}:{identity['target_type']}:{identity['target_id']}",
        }

    source = _inert_text(
        rules_source or rules.url or identity["url"] or SELECTOR_RULES_SOURCE, URL_LIMIT
    )
    stamp = _parse_iso(rules.fetched_at) or moment
    candidate: dict[str, Any] = {
        "target_type": identity["target_type"],
        "name": identity["name"] or identity["target_id"],
        "slug": identity["target_id"],
        "url": identity["url"],
        "description": _inert_text(
            (destination or {}).get("description") if isinstance(destination, Mapping) else "",
            AUDIENCE_LIMIT,
        ),
        "rules": raw_text,
        # Availability is a fact about *this read*, computed from the text we
        # managed to parse — not a flag the page could set in us.
        "rules_available": True,
        "rules_source": source,
        # A fetched page never buys auto-publish rights; only our own code says.
        "auto_publish_supported": _as_flag(
            destination.get("auto_publish_supported") if isinstance(destination, Mapping) else None,
            default=False,
        ),
        "editorial_brief": destination.get("editorial_brief")
        if isinstance(destination, Mapping)
        else (),
        "matches": destination.get("matches") if isinstance(destination, Mapping) else (),
        "suggested_tags": destination.get("suggested_tags")
        if isinstance(destination, Mapping)
        else (),
        "media_limits": destination.get("media_limits")
        if isinstance(destination, Mapping)
        else (),
    }
    policy = policy_from_candidate(identity["platform"], candidate, rules_source=source)
    # The fetch time, not the wall clock, is what the age of this knowledge means:
    # a snapshot of a page read at 03:00 and stored at 03:20 is 20 minutes of
    # *rule knowledge* plus nothing we invented.
    policy = replace(policy, checked_at=stamp.isoformat())
    verdict = assess_policy(policy, now=moment)
    stored = record_policy_snapshot(
        policy, verdict, min_interval_minutes=min_interval_minutes, now=moment
    )
    if stored.get("reason") == SNAPSHOT_REFUSAL_STORAGE_ERROR:
        # The row did not reach the store: report the failure by name instead of
        # handing back a "recorded": False that reads like a collapse.
        return {
            **stored,
            "state": FRESHNESS_UNREAD,
            "reason": SNAPSHOT_REFUSAL_STORAGE_ERROR,
            "rules_hash": policy.rules_hash,
            "rules_source": policy.rules_source,
            "target_key": policy.target_key(),
        }
    return {
        **stored,
        "state": (
            FRESHNESS_FRESH
            if verdict.get("status") == "ok"
            else (FRESHNESS_STALE if verdict.get("status") == "stale" else FRESHNESS_UNREAD)
        ),
        "rules_hash": policy.rules_hash,
        "rules_source": policy.rules_source,
        "checked_at": policy.checked_at,
        "verdict_status": _verdict_status(verdict.get("status")),
        # Returned, not just stored: a caller must be able to see that the
        # publish right came from its own structured input and not from the page.
        "auto_publish_supported": policy.auto_publish_supported,
        "allow_publish": bool(verdict.get("allow_publish")),
        "rule_lines": len(rule_lines),
        "target_key": policy.target_key(),
    }


def rules_freshness(
    platform: str,
    target_id: str,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """How old is our rule knowledge of this destination, and may we publish on it?

    Returns one of four states — never a number in place of an answer:

    * ``fresh``: a stored snapshot, readable stamp, rule text behind the flag,
      younger than ``max_age_hours``;
    * ``stale``: the same, older — the rules were read, but not for this publish;
    * ``missing``: this destination has genuinely never been recorded;
    * ``unread``: a row (or the store) exists but cannot answer the question —
      an unparseable stamp, no stored rule evidence, or a failing database.

    ``age_hours`` is ``None`` whenever the age was not measured, so a cold
    destination cannot be mistaken for a brand-new one, and ``publishable`` is
    true only for ``fresh``.  This reads the store directly instead of going
    through :func:`policy_history`, because that reader folds a storage failure
    into the same ``[]`` as "no rows" — and those are different answers.
    """
    key = f"{_inert_text(platform, 32)}:{_inert_text(target_id, TARGET_ID_LIMIT)}"
    base: dict[str, Any] = {
        "state": FRESHNESS_MISSING,
        "reason": "",
        "publishable": False,
        "age_hours": None,
        "max_age_hours": float(max_age_hours),
        "checked_at": None,
        "recorded_at": None,
        "rules_hash": None,
        "rules_source": None,
        "verdict_status": None,
        "target_key": key,
    }
    moment = _as_utc(now) or datetime.now(UTC)
    try:
        from sqlalchemy import select

        from tgfarm.db.models import CommunityPolicySnapshot
        from tgfarm.db.session import session_scope

        with session_scope() as session:
            row = session.scalars(
                select(CommunityPolicySnapshot)
                .where(
                    CommunityPolicySnapshot.platform == _inert_text(platform, 32),
                    CommunityPolicySnapshot.target_id == _inert_text(target_id, TARGET_ID_LIMIT),
                )
                .order_by(CommunityPolicySnapshot.id.desc())
                .limit(1)
            ).first()
            if row is None:
                return {
                    **base,
                    "reason": "no rules snapshot has ever been stored for this destination",
                }
            stored_hash = _inert_text(row.rules_hash, 64)
            evidence = _inert_text(row.rules_excerpt, RULES_EXCERPT_LIMIT) or any(
                str(line).strip() for line in list(row.requirements_json or ())
            )
            checked = _as_utc(row.checked_at)
            recorded = _as_utc(row.recorded_at)
            payload = {
                "checked_at": checked.isoformat() if checked else None,
                "recorded_at": recorded.isoformat() if recorded else None,
                "rules_hash": stored_hash or None,
                "rules_source": _inert_text(row.rules_source, URL_LIMIT) or None,
                "verdict_status": _verdict_status(row.verdict_status),
            }
    except Exception as exc:  # noqa: BLE001 - a broken store is 'unread', not 'missing'
        log.warning("rules freshness could not be read for %s: %s", key, exc)
        return {
            **base,
            "state": FRESHNESS_UNREAD,
            "reason": f"the rules store did not answer ({type(exc).__name__})",
            "error": type(exc).__name__,
        }

    if not stored_hash or not evidence:
        return {
            **base,
            **payload,
            "state": FRESHNESS_UNREAD,
            "reason": "a snapshot exists but stores no rule text — nothing is known here",
        }
    if checked is None:
        return {
            **base,
            **payload,
            "state": FRESHNESS_UNREAD,
            "reason": "the snapshot's rules timestamp is unreadable — its age cannot be measured",
        }
    # Four decimals, not two: this number has to justify the state next to it,
    # and a 2-dp rounding prints "24.0 h, limit 24 h, stale" — a reading that
    # looks like the reader contradicted itself exactly at the boundary.
    age_hours = round((moment - checked).total_seconds() / 3600, 4)
    limit = max(0.0, float(max_age_hours))
    if (moment - checked) > timedelta(hours=limit):
        return {
            **base,
            **payload,
            "state": FRESHNESS_STALE,
            "reason": (
                f"rules were read {age_hours:.1f} h ago (limit {limit:g} h) "
                "— re-read them before publishing"
            ),
            "age_hours": age_hours,
        }
    return {
        **base,
        **payload,
        "state": FRESHNESS_FRESH,
        "reason": "rules are stored, readable and within the age limit",
        "publishable": True,
        "age_hours": age_hours,
    }


def freshness_not_read(target_id: str, reason: str = "store_unreachable") -> dict[str, Any]:
    """The answer for a freshness question that could not be asked.

    Callers use this instead of a defaulted ``rules_freshness`` result when the
    read never happened at all.  It carries the state name and a reason and *no*
    age and *no* verdict: filling the keys with nulls would let a panel print
    "no rules" about a store that was never queried, which is the inversion
    :func:`tgfarm.editorial_plan.provenance_not_read` exists to prevent.
    """
    return {
        "state": FRESHNESS_UNREAD,
        "reason": _inert_text(reason, NOTE_LINE_LIMIT),
        "publishable": False,
        "age_hours": None,
        "target_key": _inert_text(target_id, TARGET_ID_LIMIT),
    }


def _as_utc(value: Any) -> datetime | None:
    """One aware instant from a stored/naive datetime, or ``None`` when unreadable.

    A naive stamp is UTC (that is how the writer stores it); anything that does
    not parse is *not coerced to now*, because the whole point of the reader is
    that an unmeasured age stays visibly unmeasured.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(
            UTC
        )
    if value in (None, ""):
        return None
    return _parse_iso(value)

"""Scheduled producer of *stored* rules proofs for community destinations (W26-RW).

Why this module exists
----------------------
W29-B merged a publish gate: `tgfarm.community_targets.check_destination_rules_proof`
refuses a community publish unless a *stored* rules proof exists, i.e. unless
`tgfarm.community_policy.rules_freshness` answers ``fresh`` with a stored
``verdict_status`` of ``ok``.  The gate is sound — an unrecorded rules check
cannot justify publishing into somebody else's community — but nothing on the
clock could satisfy it yet.

Two measured facts made that a live-behaviour problem rather than a theoretical
one (read-only measurement, ``mode=ro``):

* ``community_policy_snapshots`` held **0 rows**;
* no scheduled rules *writer* existed.  The only snapshot writer reachable on a
  timer was the maintenance rules-watch, and the rules-watch enumerates the
  destinations it re-reads **from the snapshot table itself**
  (:func:`tgfarm.maintenance._watched_rule_destinations`).  On a cold table that
  enumeration is empty, so the step reports ``enabled_no_known_destination`` and
  writes nothing — a cold-start deadlock.  History only ever appeared as a side
  effect of a human verb or of a publish attempt, and the publish attempt is now
  refused *before* it records anything (the refusal returns at
  :mod:`tgfarm.community_drafts` before its own ``record_policy_snapshot``).

This module is the producer the deadlock is missing.  It is scheduler-shaped and
offline-reachable; it deliberately does **not** touch the gate.

What it is honest about
-----------------------
* The rules page is UNTRUSTED data (§45).  Its text is only ever handed to
  :func:`tgfarm.community_policy.record_rules_snapshot`, which stores it bounded
  and inert and lets the *structured fetch outcome* — never the page — decide
  whether a row is written.  This module reads no instruction out of a page and
  builds every URL from our own canonical destination id, never from page text;
  an id that is not canonical for its platform raises before any request.
* A fetch that fails, or that "succeeds" carrying no rule text, records **nothing**
  (the writer's own named refusals) and is reported here by reason code, so an
  unread destination can never read back as a checked one (§47).
* The result is structured data — counts plus a reason-code histogram plus a
  bounded per-destination sample — never a boolean, so a caller can tell "did
  nothing because every destination was fresh" from "did nothing because the
  store or the network is broken".
* Absence is never a ``0``: the age of a destination that was not re-read is not
  invented; only destinations this pass actually fetched appear in the sample.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from tgfarm.community_policy import (
    DEFAULT_MAX_AGE_HOURS,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FetchedRules,
    record_rules_snapshot,
    rules_freshness,
)
from tgfarm.core.security import redact_sensitive_text

# ---------------------------------------------------------------------------
# Tunables.  Every one of these is a wall-clock/size bound, not a permission.
# ---------------------------------------------------------------------------

#: How often the scheduled pass wakes.  The freshness window the gate enforces is
#: ``DEFAULT_MAX_AGE_HOURS`` (24 h); a producer that ran daily could leave a
#: destination stale for a full window, so the clock is a quarter of the window.
#: The coordinator registers exactly this cadence (see :func:`scheduler_job_spec`).
RULES_SNAPSHOT_INTERVAL_HOURS = 6

#: Destinations one pass may re-read.  Each costs the platform one or two public
#: GETs — the same pages a selection already opens — so the cap is small and the
#: remainder is always reported, never dropped (:data:`RESULT_REMAINING`).
RULES_SNAPSHOT_BUDGET_MAX = 10

#: Wall-clock stop for one pass, checked *before* starting a read; a read already
#: in flight is left to its own client timeout.
RULES_SNAPSHOT_BUDGET_SECONDS = 45.0

#: How long a free-text field may stay in the returned report.
RULES_SNAPSHOT_DETAIL_LIMIT = 160

#: How many destination results one report carries; the rest stay counted in the
#: histogram so the sample can not grow without bound.
RULES_SNAPSHOT_SAMPLE_MAX = 20

# --- the reason-code vocabulary this producer introduces -------------------
# A result is always one of these names, never a bare True/False.  ``fresh`` and
# ``changed``/``collapsed`` describe work that was deliberately NOT done or was
# swallowed by the existing collapse window; the ``*_failed``/``*_error`` codes
# reuse :mod:`tgfarm.community_policy`'s writer refusals verbatim so one refusal
# has one spelling across the whole farm.

#: Destination was already ``fresh``: no read, no write, counted as skipped.
RESULT_FRESH_SKIPPED = "fresh_skipped"
#: A read ran and the writer stored a row (new or changed).
RESULT_RECORDED = "recorded"
#: A read ran, the page was fine, but the collapse window swallowed the row.
RESULT_COLLAPSED = "collapsed"
#: Fetch transport raised (the page was never read): named, no row written.
RESULT_FETCH_FAILED = "fetch_failed"
#: The fetch "succeeded" but carried no rule text: named, no row written.
RESULT_EMPTY_RULES = "empty_rules_text"
#: A destination that names no platform/id: named, no row written.
RESULT_NO_TARGET = "no_target"
#: The store rejected the row: named, no row written.
RESULT_STORAGE_ERROR = "storage_error"
#: The fetch carried a read stamp the writer cannot parse: named, no row written.
#: Reuses :data:`tgfarm.community_policy`'s ``checked_at_invalid`` refusal verbatim
#: (added by the operator's freshness fixes); a bad stamp is not a fetch failure.
RESULT_CHECKED_AT_INVALID = "checked_at_invalid"
#: The destination's platform has no fetch callable registered here.
RESULT_NO_READER = "no_rules_reader"
#: This destination id is not canonical for its platform, so no URL was built.
RESULT_UNSAFE_ID = "unsafe_destination_id"
#: The pass stopped taking new reads on its own budget; named in ``remaining``.
RESULT_REMAINING = "left_for_next_pass"

#: Closed set, so a report state can be validated against a real vocabulary.
SNAPSHOT_RESULT_CODES = (
    RESULT_FRESH_SKIPPED,
    RESULT_RECORDED,
    RESULT_COLLAPSED,
    RESULT_FETCH_FAILED,
    RESULT_EMPTY_RULES,
    RESULT_NO_TARGET,
    RESULT_STORAGE_ERROR,
    RESULT_CHECKED_AT_INVALID,
    RESULT_NO_READER,
    RESULT_UNSAFE_ID,
    RESULT_REMAINING,
)

#: The canonical page a Pikabu community's *own* rules live on, built from our
#: stored id and nothing else (§45).  Same shape :mod:`tgfarm.maintenance`'s
#: Pikabu reader and :mod:`tgfarm.community_targets`' revalidation already use.
_PIKABU_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
_PIKABU_COMMUNITY_URL = "https://pikabu.ru/community/{slug}"
#: vc.ru stores the canonical section path (``finance``), not a leading slash.
_VC_TOPIC_RE = re.compile(r"[a-z0-9_-]{2,32}")
_VC_TOPIC_URL = "https://vc.ru/{slug}"


class RulesFetchUnsupported(Exception):
    """No fetch is registered for this destination's platform.

    Raised by :func:`default_rules_fetch` rather than guessing a scraper, so the
    destination is counted :data:`RESULT_NO_READER` and named — never silently
    skipped and never approximated by reading a page this code does not already
    know how to read.
    """


# ---------------------------------------------------------------------------
# Destination inventory — real destinations, not a hard-coded list.
# ---------------------------------------------------------------------------


def destination_inventory(
    *,
    platforms: Iterable[str] | None = None,
    extra_destinations: Iterable[Mapping[str, Any]] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Enumerate the community destinations the producer should keep proven.

    The live database measures **0 rows** in ``community_policy_snapshots`` and
    there is no curated offline destination table (measured: no community/topic
    list in ``config/*.yaml``, and no ``... Destination``/``... Target`` model in
    :mod:`tgfarm.db.models`).  So the only real destinations we can name offline
    are the ones our own audit history already carries, and the ones an operator
    hands in explicitly.  ``extra_destinations`` is that operator/coordinator
    seam: it is exactly how a cold deployment breaks the deadlock without the
    module inventing a slug it never measured.

    Each destination is the minimal identity :func:`record_rules_snapshot` needs
    — ``platform``, ``target_type``, ``target_id`` and a display ``name`` — read
    out of our own store, never out of a page.  ``platforms`` narrows the set to
    platforms with a fetch registered here; the producer still re-checks freshness
    per destination, so passing a fresh destination costs one read of freshness
    and no fetch.
    """
    inventory: list[dict[str, Any]] = []
    for item in _history_destinations():
        inventory.append(item)

    for item in extra_destinations or ():
        if not isinstance(item, Mapping):
            continue
        normalized = _destination_identity(item)
        if normalized["platform"] and normalized["target_id"]:
            inventory.append(normalized)

    # De-duplicate on (platform, target_id) while preserving first-seen order, so
    # an operator who passes a destination the history already names does not
    # cause two reads of the same rules page.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in inventory:
        key = (item["platform"], item["target_id"])
        unique.setdefault(key, item)

    wanted = {str(name) for name in (platforms or ()) if str(name)} if platforms else None
    result = [
        item
        for key, item in unique.items()
        if wanted is None or key[0] in wanted
    ]
    result.sort(key=lambda item: (item["platform"], item["target_id"]))
    if limit is not None:
        result = result[: max(0, int(limit))]
    return result


def _destination_identity(destination: Mapping[str, Any] | None) -> dict[str, Any]:
    """The four identity fields a destination needs, taken from our own record."""
    destination = destination if isinstance(destination, Mapping) else {}
    target_id = str(
        destination.get("target_id") or destination.get("slug") or ""
    ).strip()
    return {
        "platform": str(destination.get("platform") or "").strip(),
        "target_type": str(destination.get("target_type") or "community").strip(),
        "target_id": target_id,
        "name": str(destination.get("name") or destination.get("target_name") or target_id).strip(),
        "url": str(destination.get("url") or "").strip(),
        # ``auto_publish_supported`` travels only if *we* set it on the stored
        # record; a page can never reach this field (§45).
        "auto_publish_supported": bool(destination.get("auto_publish_supported", False)),
    }


def _history_destinations() -> list[dict[str, Any]]:
    """Every destination our own rules history already names (cold-safe).

    Returns ``[]`` on an empty table or a broken store — this is the *seed* of a
    destination list, so a history failure degrades to "operator must name the
    destination", never to an exception that would take the stage down.  That
    asymmetry with :func:`rules_freshness` (which reports ``unread``) is
    deliberate: here absence only means "nothing to keep fresh", and the caller
    still gets the honest structured counts for whatever it did name.
    """
    try:
        from sqlalchemy import func, select

        from tgfarm.db.models import CommunityPolicySnapshot
        from tgfarm.db.session import session_scope

        with session_scope() as session:
            newest = (
                select(
                    CommunityPolicySnapshot.platform,
                    CommunityPolicySnapshot.target_id,
                    func.max(CommunityPolicySnapshot.id).label("newest_id"),
                )
                .group_by(CommunityPolicySnapshot.platform, CommunityPolicySnapshot.target_id)
                .subquery()
            )
            rows = session.execute(
                select(CommunityPolicySnapshot)
                .join(newest, CommunityPolicySnapshot.id == newest.c.newest_id)
            ).scalars().all()
            return [
                _destination_identity(
                    {
                        "platform": row.platform,
                        "target_type": row.target_type,
                        "target_id": row.target_id,
                        "name": row.target_name,
                        "url": row.rules_source,
                    }
                )
                for row in rows
                if str(row.platform or "").strip() and str(row.target_id or "").strip()
            ]
    except Exception:  # noqa: BLE001 - a seed reader degrades to empty, not crash
        return []


# ---------------------------------------------------------------------------
# The fetch transport boundary (offline-injectable).
# ---------------------------------------------------------------------------


def destination_rules_url(destination: Mapping[str, Any]) -> str:
    """Build the canonical rules URL for a destination from our own id only.

    Nothing here comes from a page: the slug/path is the ``target_id`` we stored,
    validated against the platform's canonical shape, and the host is one of the
    two we already read for those platforms.  A destination id that is not
    canonical raises :class:`ValueError` — so a hostile stored id can never turn
    into an arbitrary request (§45), exactly as
    :func:`tgfarm.maintenance._read_pikabu_rules` and
    :func:`tgfarm.community_targets.read_vc_topic_rules` already guard.
    """
    platform = str(destination.get("platform") or "").strip()
    target_id = str(destination.get("target_id") or destination.get("slug") or "").strip()
    if platform == "pikabu":
        if not _PIKABU_SLUG_RE.fullmatch(target_id):
            raise ValueError("pikabu destination is not a canonical community slug")
        return _PIKABU_COMMUNITY_URL.format(slug=target_id)
    if platform == "vc_ru":
        if not _VC_TOPIC_RE.fullmatch(target_id.lstrip("/")):
            raise ValueError("vc_ru destination is not a canonical topic path")
        return _VC_TOPIC_URL.format(slug=target_id.lstrip("/"))
    raise RulesFetchUnsupported(f"no rules fetch is registered for platform {platform!r}")


async def _fetch_page(url: str) -> dict[str, Any]:
    """Open one public page with the *existing* destination reader (fake seam).

    Lazily bound so a test can replace ``tgfarm.community_targets._fetch`` at the
    transport boundary — the same seam the maintenance rules-watch tests use — and
    so importing this module never pulls the network stack in.
    """
    from tgfarm import community_targets

    return await community_targets._fetch(url)


async def _async_default_fetch(destination: Mapping[str, Any]) -> FetchedRules:
    """Read one destination's rules page into a :class:`FetchedRules`.

    The fetched document is a *page* dict, not rules: ``text``/``chunks`` are the
    parsed community page.  We keep only the inert rule text the community's own
    extractor produces (``community_targets._rules``) and set ``ok`` ourselves from
    the fact that a page came back — never from the page claiming it is fine.

    The read stamp is left to the writer when the transport carries none.  The
    real page reader (``community_targets._fetch``) returns no ``fetched_at``, so
    stamping a wall-clock ``datetime.now`` here would land microseconds *after* a
    deterministic pass's ``now`` and the current gate would refuse the fresh proof
    as ``checked_at_future`` (operator fixes a28a0fa / 0c75575).  An empty stamp
    lets :func:`record_rules_snapshot` use the pass's own moment — the caller's
    moment — which is exactly when this read happened.  A transport that *does*
    measure its own read time keeps it.
    """
    url = destination_rules_url(destination)
    moment = datetime.now(UTC)
    try:
        page = await _fetch_page(url)
    except RulesFetchUnsupported:
        raise
    except Exception as exc:  # noqa: BLE001 - a refused/failed read is a named outcome
        return FetchedRules(
            url=url,
            text="",
            ok=False,
            fetched_at=moment.isoformat(),
            error=f"{type(exc).__name__}: fetch did not return a page",
        )
    from tgfarm import community_targets

    rule_text = community_targets._rules(
        list(page.get("chunks") or []),
        str(page.get("text") or ""),
    )
    return FetchedRules(
        url=url,
        text=rule_text,
        ok=True,
        fetched_at=str(page.get("fetched_at") or ""),
    )


def default_rules_fetch(destination: Mapping[str, Any]) -> FetchedRules:
    """Synchronous entry to the default async fetch.

    The scheduled tick runs in a worker thread (``run_maintenance`` reaches it the
    same way it reaches ``watch_destination_rules``), so ``asyncio.run`` is correct
    there; borrowing a caller's already-running loop would let a maintenance read
    outlive the stage that asked for it, so a nested loop is run on a throwaway
    one-thread pool instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_async_default_fetch(destination))
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(_async_default_fetch(destination))
        ).result()


# ---------------------------------------------------------------------------
# The producer.
# ---------------------------------------------------------------------------


def refresh_destination_rules(
    destinations: Iterable[Mapping[str, Any]] | None = None,
    *,
    fetch: Callable[[Mapping[str, Any]], Any] | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    limit: int = RULES_SNAPSHOT_BUDGET_MAX,
    budget_seconds: float = RULES_SNAPSHOT_BUDGET_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Produce a stored rules proof for every destination that lacks a fresh one.

    ``destinations`` is the inventory to keep proven; when it is ``None`` the
    producer enumerates the destinations our own history already names (see
    :func:`destination_inventory`).  ``fetch`` is the transport boundary: one
    callable that turns a destination into a :class:`FetchedRules` (or a selector
    dict); the default reads the platform's canonical rules page.  Both are
    injectable so a test never opens a socket.

    For each destination the producer asks our own gate reader first
    (:func:`tgfarm.community_policy.rules_freshness`): if the destination is
    already ``fresh`` *and publishable* within ``max_age_hours`` it is *skipped and
    counted* (:data:`RESULT_FRESH_SKIPPED`) — no read, no write, no invented number.
    A row that is fresh but whose stored verdict denies publishing is reported by
    the current consumer as ``fresh`` with ``publishable=False``; the gate refuses
    it, so the producer re-reads it rather than skipping it forever.  For everything
    else it fetches and calls
    :func:`tgfarm.community_policy.record_rules_snapshot`, which stores or refuses
    by name.  The producer trusts that refusal: a ``recorded: False`` is never
    recounted as a check.

    Returns a structured report, never a boolean:

    ``{state, detail, considered, checked, recorded, changed, collapsed,
    fresh_skipped, failed, remaining, reasons{name: count}, results[{...}],
    budget, budget_seconds, oldest_age_hours}``.

    ``oldest_age_hours`` is ``None`` when no destination's age was actually
    measured (P34); a platform with zero destinations is a state, not a zero.
    """
    current = _as_utc(now) or datetime.now(UTC)
    fetcher = fetch or default_rules_fetch
    started = datetime.now(UTC)

    budget = max(1, min(int(limit), RULES_SNAPSHOT_BUDGET_MAX))
    deadline_seconds = max(1.0, float(budget_seconds))

    inventory = (
        list(destinations)
        if destinations is not None
        else destination_inventory()
    )

    report: dict[str, Any] = {
        "state": "no_destinations",
        "detail": "no destination named, so nothing to prove",
        "considered": len(inventory),
        "budget": budget,
        "budget_seconds": deadline_seconds,
        "checked": 0,
        "recorded": 0,
        "changed": 0,
        "collapsed": 0,
        "fresh_skipped": 0,
        "failed": 0,
        "remaining": 0,
        "reasons": {},
        "results": [],
        "oldest_age_hours": None,
    }

    ages: list[float] = []

    def _bump(code: str) -> None:
        report["reasons"][code] = int(report["reasons"].get(code, 0)) + 1

    considered = 0
    for raw in inventory:
        if considered >= budget:
            # Budget spent: name the leftover, do not read it, do not invent a
            # verdict for it.  Everything past this point is ``remaining``.
            report["remaining"] = int(report["remaining"]) + 1
            _bump(RESULT_REMAINING)
            continue
        considered += 1

        identity = _destination_identity(raw)
        platform = identity["platform"]
        target_id = identity["target_id"]
        key = f"{platform}:{target_id}"

        if (datetime.now(UTC) - started).total_seconds() > deadline_seconds:
            report["remaining"] = int(report["remaining"]) + 1
            _bump(RESULT_REMAINING)
            continue

        if not platform or not target_id:
            # The writer's own ``no_target`` refusal would hold nothing to key on;
            # say so here and never attempt a fetch against an un-named destination.
            report["failed"] += 1
            _bump(RESULT_NO_TARGET)
            _record_result(report, identity, RESULT_NO_TARGET, key)
            continue

        freshness = rules_freshness(platform, target_id, max_age_hours=max_age_hours, now=current)
        age = freshness.get("age_hours")
        if isinstance(age, (int, float)):
            ages.append(float(age))

        if str(freshness.get("state")) == FRESHNESS_FRESH and bool(freshness.get("publishable")):
            # Fresh *and publishable*: the destination is already provable, so this
            # pass is correct to do nothing.  Counted and named, never silently
            # absent.  The current consumer (operator fix 7c929c9) reports a fresh
            # row whose stored verdict denies publishing as ``fresh`` with
            # ``publishable=False``; the gate refuses that, so it is NOT a skip —
            # it falls through to a re-read below, which can earn an ``ok`` verdict.
            report["fresh_skipped"] += 1
            _bump(RESULT_FRESH_SKIPPED)
            _record_result(
                report, identity, RESULT_FRESH_SKIPPED, key,
                {"age_hours": age, "prior_state": freshness.get("state")},
            )
            continue

        # Not publishable-fresh (stale / missing / unread / fresh-but-denied):
        # earn a stored proof the gate will actually admit.
        try:
            fetched = fetcher(dict(identity))
        except RulesFetchUnsupported as exc:
            report["failed"] += 1
            _bump(RESULT_NO_READER)
            _record_result(
                report, identity, RESULT_NO_READER, key,
                {"detail": redact_sensitive_text(str(exc), limit=RULES_SNAPSHOT_DETAIL_LIMIT)},
            )
            continue
        except ValueError as exc:
            # A stored id that is not canonical is refused before a URL is built;
            # it is a named failure, not a request to an arbitrary host.
            report["failed"] += 1
            _bump(RESULT_UNSAFE_ID)
            _record_result(
                report, identity, RESULT_UNSAFE_ID, key,
                {"detail": redact_sensitive_text(str(exc), limit=RULES_SNAPSHOT_DETAIL_LIMIT)},
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one unreadable destination must not kill the pass
            report["failed"] += 1
            _bump(RESULT_FETCH_FAILED)
            _record_result(
                report, identity, RESULT_FETCH_FAILED, key,
                {"detail": redact_sensitive_text(
                    f"{type(exc).__name__}", limit=RULES_SNAPSHOT_DETAIL_LIMIT
                )},
            )
            continue

        # The writer is the single authority on whether a row is stored: it hashes
        # the page text, computes the verdict and refuses a blank/failed read.
        outcome = record_rules_snapshot(dict(identity), fetched, now=current)
        report["checked"] += 1
        recorded = bool(outcome.get("recorded"))
        reason = str(outcome.get("reason") or "")
        state = str(outcome.get("state") or "")

        if recorded:
            report["recorded"] += 1
            _bump(RESULT_RECORDED)
            if bool(outcome.get("changed")):
                report["changed"] += 1
            publishable = state == FRESHNESS_FRESH and str(
                outcome.get("verdict_status") or ""
            ) == "ok"
            _record_result(
                report, identity, RESULT_RECORDED, key,
                {
                    "state": state,
                    "verdict_status": outcome.get("verdict_status"),
                    "changed": bool(outcome.get("changed")),
                    "publishable": publishable,
                },
            )
        elif reason == RESULT_EMPTY_RULES:
            # The read "succeeded" but carried no rule text: nothing was stored, so
            # this is a failure by name, never a check.
            report["failed"] += 1
            _bump(RESULT_EMPTY_RULES)
            _record_result(report, identity, RESULT_EMPTY_RULES, key, {"state": state})
        elif reason == RESULT_FETCH_FAILED:
            report["failed"] += 1
            _bump(RESULT_FETCH_FAILED)
            _record_result(
                report, identity, RESULT_FETCH_FAILED, key,
                {"detail": redact_sensitive_text(
                    str(outcome.get("error") or ""), limit=RULES_SNAPSHOT_DETAIL_LIMIT
                )},
            )
        elif reason == RESULT_STORAGE_ERROR:
            report["failed"] += 1
            _bump(RESULT_STORAGE_ERROR)
            _record_result(report, identity, RESULT_STORAGE_ERROR, key)
        elif reason == RESULT_CHECKED_AT_INVALID:
            # The read carried a stamp the writer cannot parse: it stored nothing
            # and named the refusal.  Report it by its own name, not as a fetch
            # failure — the page was read, the timestamp was the problem.
            report["failed"] += 1
            _bump(RESULT_CHECKED_AT_INVALID)
            _record_result(report, identity, RESULT_CHECKED_AT_INVALID, key, {"state": state})
        elif reason and "collapse" in reason:
            # The existing change-detection swallowed an unchanged repeat: the
            # proof is still there from the earlier row, so this is neither a fresh
            # skip nor a failure.
            report["collapsed"] += 1
            _bump(RESULT_COLLAPSED)
            _record_result(report, identity, RESULT_COLLAPSED, key, {"state": state})
        else:
            report["failed"] += 1
            code = RESULT_NO_TARGET if reason == RESULT_NO_TARGET else RESULT_FETCH_FAILED
            _bump(code)
            _record_result(report, identity, code, key, {"reason": reason or "unknown"})

    if ages:
        report["oldest_age_hours"] = round(max(ages), 2)

    if report["considered"] == 0:
        report["state"] = "no_destinations"
    elif report["failed"] and not (report["recorded"] or report["collapsed"] or report["fresh_skipped"]):
        report["state"] = "failed"
    elif report["remaining"] or (report["checked"] + report["fresh_skipped"]) < report["considered"]:
        report["state"] = "partial"
    else:
        report["state"] = "ok"

    report["detail"] = (
        f"{report['checked']} fetched, {report['recorded']} stored, "
        f"{report['changed']} changed, {report['fresh_skipped']} already fresh, "
        f"{report['collapsed']} collapsed, {report['failed']} failed"
    )
    if report["remaining"]:
        report["detail"] += f"; {report['remaining']} left for the next pass"
    return report


def _record_result(
    report: dict[str, Any],
    identity: dict[str, Any],
    code: str,
    key: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Append one bounded, inert result line (never more than SAMPLE_MAX)."""
    if len(report["results"]) >= RULES_SNAPSHOT_SAMPLE_MAX:
        return
    entry: dict[str, Any] = {
        "platform": identity["platform"][:32],
        "target_id": identity["target_id"][:200],
        "result": code,
        "target_key": key[:256],
    }
    for field_name, value in (extra or {}).items():
        if isinstance(value, str):
            entry[field_name] = redact_sensitive_text(value, limit=RULES_SNAPSHOT_DETAIL_LIMIT)
        elif field_name in {"age_hours", "publishable", "changed"} or value is None or isinstance(
            value, (bool, int, float)
        ):
            entry[field_name] = value
        else:
            entry[field_name] = redact_sensitive_text(
                str(value), limit=RULES_SNAPSHOT_DETAIL_LIMIT
            )
    report["results"].append(entry)


def _as_utc(value: datetime | None) -> datetime | None:
    """One aware instant from a caller's datetime, or ``None`` when absent.

    A naive instant is read as UTC (that is how the writers stamp it); nothing is
    coerced to "now" here, so an absent instant stays absent and the caller's
    ``datetime.now(UTC)`` fallback is explicit rather than silent.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# ---------------------------------------------------------------------------
# The scheduler seam.
# ---------------------------------------------------------------------------


def rules_snapshot_job() -> dict[str, Any]:
    """The scheduled entry point: one pass over the named destinations.

    This is what the coordinator registers (see :func:`scheduler_job_spec`).  It
    enumerates the destinations our own history already names and refreshes the
    ones that are not fresh; a cold table yields ``no_destinations`` (the honest
    bootstrap answer), and an operator or the pipeline hands in the concrete
    destinations through :func:`refresh_destination_rules` to seed the first rows.
    Returns the structured report, so the scheduler's own journal can carry counts.
    """
    return refresh_destination_rules()


def scheduler_job_spec() -> dict[str, Any]:
    """Pure description of the scheduler entry the pipeline *should* register.

    This module does not own :mod:`tgfarm.pipeline`, so it only says what belongs
    there — the same seam :func:`tgfarm.tg_sync.scheduler_job_spec` uses.  Wiring
    the job in is the coordinator's step; nothing here imports the scheduler.

    The values are a contract with the gate: the cadence
    (:data:`RULES_SNAPSHOT_INTERVAL_HOURS`, a quarter of the 24 h freshness the
    gate enforces) is what keeps a proof from going stale between passes, and
    ``max_instances=1`` / ``coalesce=True`` stop a slow platform read from stacking
    passes or spending a backlog of missed ticks on one burst of public GETs.
    ``misfire_grace_time`` lets a single late tick still run rather than being
    dropped, because a skipped pass would let a proof age toward ``stale``.
    """
    return {
        "func": rules_snapshot_job,
        "trigger": "interval",
        "hours": RULES_SNAPSHOT_INTERVAL_HOURS,
        "id": "community-rules-snapshots",
        "name": "community rules snapshot producer",
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 3600,
    }


__all__ = [
    "DEFAULT_MAX_AGE_HOURS",
    "FRESHNESS_FRESH",
    "FRESHNESS_STALE",
    "RESULT_CHECKED_AT_INVALID",
    "RESULT_COLLAPSED",
    "RESULT_EMPTY_RULES",
    "RESULT_FETCH_FAILED",
    "RESULT_FRESH_SKIPPED",
    "RESULT_NO_READER",
    "RESULT_NO_TARGET",
    "RESULT_RECORDED",
    "RESULT_REMAINING",
    "RESULT_STORAGE_ERROR",
    "RESULT_UNSAFE_ID",
    "RULES_SNAPSHOT_BUDGET_MAX",
    "RULES_SNAPSHOT_BUDGET_SECONDS",
    "RULES_SNAPSHOT_INTERVAL_HOURS",
    "SNAPSHOT_RESULT_CODES",
    "RulesFetchUnsupported",
    "default_rules_fetch",
    "destination_inventory",
    "destination_rules_url",
    "refresh_destination_rules",
    "rules_snapshot_job",
    "scheduler_job_spec",
]

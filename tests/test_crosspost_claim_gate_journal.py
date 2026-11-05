"""The claim gate must read the same journal vocabulary as the planner, and fail
closed the same way.

`build_distribution_plan` is a *view*: when it does not understand a
`crosspost_logs.status` it stops offering the pair, which costs a stalled queue.
`adapters.already_done` + `adapters.begin_crosspost` are the *lock*: they decide
whether a send may take the durable claim and publish. Before this block the lock
handled the suppressed set, `failed` and `skipped` and let *anything else* claim
the pair — so the moment any component or operator wrote a new status, the queue
refused the pair while a second publisher was still allowed to send it. Two
readers of one column with two defaults is the race the journal exists to
prevent, even while neither side happens to be wrong.

The pinned properties:

1. an unrecognised status does not claim, and does not publish a `publishing`
   row or a claim;
2. the refusal is reported in the channel the lock already uses (the module
   logger; the API turns a `None` into its own 409), never silently;
3. `retry_authorized` still claims — re-offering *is* what that row authorizes;
4. every status a writer in this repository can actually produce is handled, and
   the planner and the lock give the same answer for it in the same mode;
5. the vocabulary has exactly one declaration site, so a second literal status
   set cannot be re-introduced in either consumer without failing here.

The planner half reuses the harness in
`tests/test_distribution_journal_status_semantics.py` rather than rebuilding it
(same precedent as `test_duplicate_repair.py` importing `test_trends`); the lock
half needs only journal rows, because the claim gate never looks at the post.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
import typing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from test_distribution_journal_status_semantics import TARGET_PLATFORM, _JournalHarness

from tgfarm import crosspost_journal, distribution
from tgfarm.core.schemas import CrosspostResult
from tgfarm.core.settings import get_settings
from tgfarm.db.models import CrosspostClaim, CrosspostLog
from tgfarm.db.session import session_scope
from tgfarm.s9_growth import adapters

# Statuses every writer in this repository can put in the column right now:
# `log_result` (via the `CrosspostResult` model), `begin_crosspost` (its own
# claim row) and `api._reconcile_crosspost_outcome` (operator decisions).
DIRECTLY_WRITTEN_STATUSES = frozenset({"publishing", "retry_authorized"})

# A status no writer can produce today: written by something this codebase has
# never met, or by a future column value nobody declared.
UNRECOGNISED_STATUSES = ("sent", "queued", "reconciled", "posted_again", "PUBLISHING_OK", "")

# Everything that must be answered for, in one run of both consumers.
EQUIVALENCE_CORPUS = (
    tuple(sorted(typing.get_args(CrosspostResult.model_fields["status"].annotation)))
    + tuple(sorted(DIRECTLY_WRITTEN_STATUSES))
    + UNRECOGNISED_STATUSES
)


class _ConfiguredAdapter:
    """The destination is reachable, so configuration is never the reason here."""

    platform = TARGET_PLATFORM

    def configured(self) -> bool:
        return True


class _ClaimGate:
    """Drives the lock half of the journal: `already_done` then `begin_crosspost`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        monkeypatch.setitem(adapters.ADAPTERS, TARGET_PLATFORM, _ConfiguredAdapter())
        self.public_ids: list[str] = []

    def journal(
        self,
        status: str | None,
        *,
        attempt: int = 1,
        retry_after_at: datetime | None = None,
        created_ago: timedelta = timedelta(days=1),
    ) -> str:
        """Seed the newest journal row of a pair that has no post behind it."""
        public_id = f"cg{uuid4().hex[:10]}"[:32]
        with session_scope() as session:
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=TARGET_PLATFORM,
                    status=status,
                    attempt=attempt,
                    retry_after_at=retry_after_at,
                    created_at=datetime.now(UTC) - created_ago,
                )
            )
        self.public_ids.append(public_id)
        return public_id

    def use_mode(self, mode: str) -> None:
        settings = get_settings().model_copy(update={"mode": mode})
        self._monkeypatch.setattr(adapters, "get_settings", lambda: settings)

    def claim(self, public_id: str) -> int | None:
        """The lock alone — what the direct publish endpoints call."""
        return adapters.begin_crosspost(public_id, TARGET_PLATFORM)

    def deliverable(self, public_id: str, *, mode: str = "dry") -> bool:
        """The automatic path's real gate: `already_done` and then the claim."""
        self.use_mode(mode)
        if adapters.already_done(public_id, TARGET_PLATFORM):
            return False
        return adapters.begin_crosspost(public_id, TARGET_PLATFORM) is not None

    def newest_status(self, public_id: str) -> str | None:
        with session_scope() as session:
            row = session.execute(
                select(CrosspostLog)
                .where(CrosspostLog.post_public_id == public_id)
                .order_by(CrosspostLog.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            return None if row is None else row.status

    def claim_rows(self, public_id: str) -> int:
        with session_scope() as session:
            return len(
                session.execute(
                    select(CrosspostClaim.id).where(
                        CrosspostClaim.post_public_id == public_id,
                        CrosspostClaim.platform == TARGET_PLATFORM,
                    )
                ).all()
            )

    def cleanup(self) -> None:
        if not self.public_ids:
            return
        with session_scope() as session:
            session.execute(
                delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(self.public_ids))
            )
            session.execute(
                delete(CrosspostClaim).where(CrosspostClaim.post_public_id.in_(self.public_ids))
            )


@pytest.fixture()
def claim_gate(monkeypatch: pytest.MonkeyPatch) -> _ClaimGate:
    gate = _ClaimGate(monkeypatch)
    try:
        yield gate
    finally:
        gate.cleanup()


@pytest.fixture()
def both_gates(
    monkeypatch: pytest.MonkeyPatch, claim_gate: _ClaimGate
) -> tuple[_JournalHarness, _ClaimGate]:
    """One plan harness and one claim gate over the same destination and clock."""
    harness = _JournalHarness(monkeypatch, datetime.now(UTC))
    try:
        yield harness, claim_gate
    finally:
        harness.cleanup()


# ---------------------------------------------------------------------------
# 1. An unrecognised status fails closed, and says so.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", UNRECOGNISED_STATUSES)
def test_unrecognised_journal_status_does_not_claim_the_pair(
    claim_gate: _ClaimGate, status: str
) -> None:
    """The defect this block closes: an undeclared status used to land in the
    fall-through and be claimed, while the planner already refused the same row.
    """
    public_id = claim_gate.journal(status)
    assert claim_gate.claim(public_id) is None
    assert claim_gate.claim_rows(public_id) == 0
    assert claim_gate.newest_status(public_id) == status


@pytest.mark.parametrize("status", UNRECOGNISED_STATUSES)
def test_automatic_path_refuses_an_unrecognised_status(
    claim_gate: _ClaimGate, status: str
) -> None:
    public_id = claim_gate.journal(status)
    assert claim_gate.deliverable(public_id, mode="live") is False


@pytest.mark.parametrize("status", UNRECOGNISED_STATUSES)
def test_refusal_is_reported_and_not_silent(claim_gate: _ClaimGate, status: str, caplog) -> None:
    """`begin_crosspost` has no exception channel — `None` is the refusal — so the
    reason has to reach the log. A silent refusal is how this defect class hides.
    """
    public_id = claim_gate.journal(status)
    with caplog.at_level(logging.WARNING, logger="tgfarm.s9_growth.adapters"):
        assert claim_gate.claim(public_id) is None
    assert caplog.records, "a refused claim must be logged"
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "crosspost claim refused" in message
    assert public_id in message
    assert TARGET_PLATFORM in message
    assert crosspost_journal.JOURNAL_REFUSAL_UNDECLARED_STATUS in message


@pytest.mark.parametrize("status", UNRECOGNISED_STATUSES)
def test_already_done_reports_its_refusal_too(claim_gate: _ClaimGate, status: str, caplog) -> None:
    """The gate in front of the loop is the same reader and must refuse on its own
    terms: the S9 loop drops the destination without a word, so if this one
    quietly answers "not done", the claim below becomes the only line of defence.
    """
    public_id = claim_gate.journal(status)
    with caplog.at_level(logging.WARNING, logger="tgfarm.s9_growth.adapters"):
        assert adapters.already_done(public_id, TARGET_PLATFORM) is True
    assert "crosspost claim refused" in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. The statuses that must keep working exactly as they do today.
# ---------------------------------------------------------------------------

def test_retry_authorized_still_claims(claim_gate: _ClaimGate) -> None:
    """`api._reconcile_crosspost_outcome` journals this status when the operator
    inspected the destination and authorized a retry. Failing closed must not
    swallow the one row that asks to be delivered again.
    """
    public_id = claim_gate.journal("retry_authorized", attempt=4)
    assert claim_gate.claim(public_id) == 5
    assert claim_gate.deliverable(claim_gate.journal("retry_authorized"), mode="live") is True


@pytest.mark.parametrize("status", ["posted", "scheduled", "unknown", "publishing", "permanent"])
def test_suppressed_statuses_still_refuse_the_claim(claim_gate: _ClaimGate, status: str) -> None:
    assert claim_gate.claim(claim_gate.journal(status)) is None


def test_a_claim_writes_a_status_the_vocabulary_declares(claim_gate: _ClaimGate) -> None:
    """The writer side of the same single source: the row the lock stamps before
    any network call must be a token every reader already understands — and it
    must read back as suppressing, because a claim in flight is not a free pair.
    """
    public_id = claim_gate.journal("failed", attempt=1)
    assert claim_gate.claim(public_id) == 2
    written = claim_gate.newest_status(public_id)
    assert written == crosspost_journal.JOURNAL_STATUS_PUBLISHING
    assert written in crosspost_journal.JOURNAL_KNOWN_STATUSES
    assert crosspost_journal.journal_status_class(written) is (
        crosspost_journal.JournalStatusClass.SUPPRESSED
    )


def test_a_fresh_pair_is_still_claimable(claim_gate: _ClaimGate) -> None:
    """`NO_HISTORY` is not `UNKNOWN`: a pair with no journal row is fresh, and
    failing closed must not block every first delivery.
    """
    public_id = f"cg{uuid4().hex[:10]}"[:32]
    assert adapters.already_done(public_id, TARGET_PLATFORM) is False
    assert adapters.begin_crosspost(public_id, TARGET_PLATFORM) == 1
    claim_gate.public_ids.append(public_id)


# ---------------------------------------------------------------------------
# 2b. One normalisation: the lock reads a token the same way the queue does.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["retry_authorized", "RETRY_AUTHORIZED", " retry_authorized "])
def test_the_claim_gate_reads_the_status_through_the_shared_normalisation(
    claim_gate: _ClaimGate, spelling: str
) -> None:
    """A casing or padding difference must not turn an authorized retry into an
    undeclared status in one reader while the other still recognises it.
    """
    public_id = claim_gate.journal(spelling)
    assert claim_gate.claim(public_id) == 2


@pytest.mark.parametrize("spelling", ["Publishing", " POSTED "])
def test_a_padded_suppressed_status_still_refuses_the_claim(
    claim_gate: _ClaimGate, spelling: str
) -> None:
    public_id = claim_gate.journal(spelling)
    assert claim_gate.claim(public_id) is None


# ---------------------------------------------------------------------------
# 3. The two consumers cannot diverge.
# ---------------------------------------------------------------------------

def _planner_offered(harness: _JournalHarness, mode: str):
    settings = get_settings().model_copy(update={"mode": mode})
    # The harness patches `distribution.get_settings` for its own `plan()`; the
    # wide per-platform limit below needs the same patch around a direct call, so
    # that no pair is dropped for capacity reasons rather than journal reasons.
    harness._monkeypatch.setattr(  # noqa: SLF001 - reusing the harness' patch set
        distribution, "get_settings", lambda: settings
    )
    result = distribution.build_distribution_plan(
        now=harness.now, max_age_days=14, max_selected_per_platform=100
    )
    entry = next(item for item in result["platforms"] if item["platform"] == TARGET_PLATFORM)
    offered = {row["post_public_id"] for row in entry["selected"]}
    return entry, offered


@pytest.mark.parametrize("mode", ["dry", "live"])
def test_planner_and_claim_gate_answer_identically_for_every_writable_status(
    both_gates, mode: str
) -> None:
    """One corpus, both readers, the same clock, the same destination.

    Any divergence here means the queue and the lock disagree about a pair: the
    queue withholds while the lock publishes (a duplicate send), or the lock
    refuses while the queue offers (a stall the operator sees as a hang). Each
    mode runs as its own test, so the claims written by one cannot become the
    other's journal history.
    """
    harness, gate = both_gates
    planner_ids: dict[str, str] = {}
    gate_ids: dict[str, str] = {}
    for status in EQUIVALENCE_CORPUS:
        planner_ids[status] = harness.journal(status)
        gate_ids[status] = gate.journal(status)

    _entry, offered = _planner_offered(harness, mode)
    for status in EQUIVALENCE_CORPUS:
        gate.use_mode(mode)
        claims = not adapters.already_done(gate_ids[status], TARGET_PLATFORM) and (
            adapters.begin_crosspost(gate_ids[status], TARGET_PLATFORM) is not None
        )
        assert claims == (planner_ids[status] in offered), (
            f"{status!r} in {mode} mode: planner offered={planner_ids[status] in offered}, "
            f"claim gate allowed={claims}"
        )


@pytest.mark.parametrize("mode", ["dry", "live"])
def test_the_lock_alone_agrees_with_the_planner_for_every_writable_status(
    both_gates, mode: str
) -> None:
    """The same matrix with `already_done` taken out.

    The direct publish endpoints (TikTok, Douyin) call `begin_crosspost` without
    consulting a plan or the gate in front of it, so the lock alone has to be the
    same reader the queue is. The single permitted exception is asserted next to
    it, because it is the one place the two components still differ on purpose.
    """
    harness, gate = both_gates
    planner_ids: dict[str, str] = {}
    gate_ids: dict[str, str] = {}
    for status in EQUIVALENCE_CORPUS:
        planner_ids[status] = harness.journal(status)
        gate_ids[status] = gate.journal(status)

    _entry, offered = _planner_offered(harness, mode)
    for status in EQUIVALENCE_CORPUS:
        gate.use_mode(mode)
        claims = adapters.begin_crosspost(gate_ids[status], TARGET_PLATFORM) is not None
        if status == crosspost_journal.JOURNAL_STATUS_DRY_RUN and mode == "dry":
            # Preserved contract: `begin_crosspost` does not re-litigate the
            # LIVE/dry switch, and dry mode never reaches it because the loop
            # consults `already_done` first (pinned above).
            assert claims is True and planner_ids[status] not in offered
            continue
        assert claims == (planner_ids[status] in offered), (
            f"{status!r} in {mode} mode: planner offered={planner_ids[status] in offered}, "
            f"begin_crosspost allowed={claims}"
        )


def test_every_status_a_writer_can_produce_has_a_declared_class() -> None:
    """Adding a status to a writer without declaring its re-offer semantics must
    fail here, not silently split the two readers' defaults.
    """
    produced = frozenset(
        typing.get_args(CrosspostResult.model_fields["status"].annotation)
    ) | DIRECTLY_WRITTEN_STATUSES
    assert produced == crosspost_journal.JOURNAL_KNOWN_STATUSES
    for status in produced:
        assert crosspost_journal.journal_status_class(status) is not (
            crosspost_journal.JournalStatusClass.UNKNOWN
        ), status
    # Mutually exclusive classes: a status cannot be suppressed and re-open
    # depending on which line of a consumer happens to run first.
    assert not crosspost_journal.JOURNAL_SUPPRESSED_STATUSES & (
        crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES
        | crosspost_journal.JOURNAL_REOPEN_STATUSES
    )
    assert not crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES & (
        crosspost_journal.JOURNAL_REOPEN_STATUSES
    )


# ---------------------------------------------------------------------------
# 4. One vocabulary object, no second copy.
# ---------------------------------------------------------------------------

def test_both_consumers_call_the_one_decision_function() -> None:
    """Identity, not equality: a re-exported *copy* of the vocabulary would be
    equal today and independent tomorrow.
    """
    for module in (distribution, adapters):
        assert module.journal_row_decision is crosspost_journal.journal_row_decision
        assert module.JournalStatusClass is crosspost_journal.JournalStatusClass


def test_distribution_reexports_nothing_for_compatibility() -> None:
    """The old names must be gone, not aliased: an alias is a second name whose
    value a reader can pin while the real vocabulary moves on.
    """
    for name in (
        "JOURNAL_SUPPRESSED_STATUSES",
        "JOURNAL_TIME_BOUNDED_STATUSES",
        "JOURNAL_REOPEN_STATUSES",
        "JOURNAL_KNOWN_STATUSES",
        "_journal_status_of",
    ):
        assert not hasattr(distribution, name), f"{name} should not exist on distribution"
    # Whatever a consumer does hold from this vocabulary is the same object the
    # declaring module holds — an alias would be caught by equality, not here.
    for module in (distribution, adapters):
        for name in (
            "JOURNAL_SUPPRESSED_STATUSES",
            "JOURNAL_TIME_BOUNDED_STATUSES",
            "JOURNAL_REOPEN_STATUSES",
            "JOURNAL_KNOWN_STATUSES",
            "JOURNAL_UNKNOWN_STATUS_REASON",
        ):
            if name in vars(module):
                assert vars(module)[name] is getattr(crosspost_journal, name), (
                    f"{module.__name__}.{name} is a copy, not the shared object"
                )


def _status_token_containers(func) -> list[frozenset[str]]:
    """Every container literal inside `func` that holds journal status tokens."""
    source = textwrap.dedent(inspect.getsource(func))
    found: list[frozenset[str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            continue
        tokens = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        hits = tokens & crosspost_journal.JOURNAL_KNOWN_STATUSES
        if hits:
            found.append(frozenset(hits))
    return found


def _module_status_token_containers(module) -> list[frozenset[str]]:
    source = textwrap.dedent(inspect.getsource(module))
    found: list[frozenset[str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            continue
        tokens = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        if tokens & crosspost_journal.JOURNAL_KNOWN_STATUSES:
            found.append(frozenset(tokens & crosspost_journal.JOURNAL_KNOWN_STATUSES))
    return found


#: The two status literals the planner still owns that are *not* the re-offer
#: decision: the daily-sent budget counter and the near-duplicate source-text
#: filter. Both are a declared follow-up (they disagree with the re-offer
#: vocabulary on purpose); re-writing either as a re-offer gate must fail here.
PLANNER_NON_REOFFER_LITERALS = {
    frozenset({"posted", "scheduled", "publishing", "unknown"}),
    frozenset({"posted", "scheduled"}),
}


def test_no_consumer_owns_a_second_literal_status_set() -> None:
    """The pin against divergence-by-copy.

    The claim gate may contain no status-token container literal at all: it must
    ask the shared vocabulary. The planner may contain only the two documented
    non-re-offer literals. Neither may contain a *copy of a declared set*, which
    is exactly what re-introducing a second vocabulary in a consumer looks like.
    """
    assert _status_token_containers(adapters.already_done) == []
    assert _status_token_containers(adapters.begin_crosspost) == []
    assert PLANNER_NON_REOFFER_LITERALS == set(_status_token_containers(
        distribution.build_distribution_plan
    ))
    declared = {
        crosspost_journal.JOURNAL_SUPPRESSED_STATUSES,
        crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES,
        crosspost_journal.JOURNAL_REOPEN_STATUSES,
        crosspost_journal.JOURNAL_KNOWN_STATUSES,
    }
    for module in (distribution, adapters):
        copied = [tokens for tokens in _module_status_token_containers(module) if tokens in declared]
        assert not copied, f"{module.__name__} declares a second copy of {copied}"


# ---------------------------------------------------------------------------
# 5. Vocabulary/dispatch drift fails closed on both sides.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "set_name",
    ["JOURNAL_TIME_BOUNDED_STATUSES", "JOURNAL_REOPEN_STATUSES"],
)
def test_a_declared_status_with_no_dispatch_branch_fails_closed(
    both_gates, set_name: str
) -> None:
    """A status added to a class (or a class widened) without either consumer
    branch being revisited must not become an "allow" by omission.
    """
    harness, gate = both_gates
    drift_token = "drifted"
    widened = frozenset(getattr(crosspost_journal, set_name) | {drift_token})
    # Both fixtures patch through one function-scoped `monkeypatch`, so widening
    # the declared class here is visible to the planner and to the lock alike.
    harness._monkeypatch.setattr(  # noqa: SLF001 - the shared patch set
        crosspost_journal, set_name, widened
    )

    planner_id = harness.journal(drift_token)
    entry, offered = _planner_offered(harness, "live")
    assert planner_id not in offered
    assert entry["reason"] == crosspost_journal.JOURNAL_UNKNOWN_STATUS_REASON

    gate_id = gate.journal(drift_token)
    # The lock *alone*: the gate in front of it also refuses, and a test that
    # only measured the composite path would pass with the lock's own guard
    # deleted.
    assert gate.claim(gate_id) is None
    assert gate.deliverable(gate_id, mode="live") is False
    assert gate.claim_rows(gate_id) == 0

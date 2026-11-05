"""The three operator-facing readers of `crosspost_logs.status` ask one owner.

The defect, as an observable fact at `19d1b09`. `tgfarm/crosspost_journal.py`
declares the journal vocabulary (nine statuses, three re-offer classes, one
normalisation) and two of its consumers were given it — the planner and the claim
gate. Three readers were not, and each kept its own literals:

* `api._crosspost_health` — the fleet/sources "needs attention" counter;
* `api._reconcile_crosspost_outcome` — whether the operator may reconcile a row;
* `s9_growth._pending_crossposts` — which posts look retryable to growth.

Spelling is only half of it, and the other half is the trap the brief warns
about. The declared sets answer the *re-offer* question («may this pair be
delivered again?»). Two of these three readers answer a different question, and
importing the sets there would be the bug, not the fix:

* `JOURNAL_SUPPRESSED_STATUSES` contains `posted`, `scheduled` and a fresh
  `publishing`. Used as the attention set it would raise an alarm on every
  healthy destination; used as the reconcilable set it would authorise a re-send
  of a post the journal believes was delivered — the exact blind retry the
  journal exists to prevent.
* `JOURNAL_TIME_BOUNDED_STATUSES | JOURNAL_REOPEN_STATUSES` is *exactly* what
  growth's question asks, so that reader does take the sets — and the resulting
  membership is pinned to literals below, so a class widened in the shared owner
  cannot silently move an operator-facing gate.

What each reader does with a status the vocabulary does not declare, measured
before this change (full numbers in the block report): health counted the pair
as a target, put it in no bucket and reported `state: not_tested` with
`needs_attention` unchanged — an unexplainable row was the one kind the operator
could not see; reconcile refused it with `not_reconcilable` and echoed the raw
token; growth did not select it. After: all three fail closed *and* say so.

Live context (a read-only measurement of the operator's `tgfarm.db`, 2026-11-05):
`skipped` 3895 / `posted` 440 / `failed` 135 / `dry_run` 30, and no `unknown`,
`publishing` or `permanent` row at all. So a membership change here is invisible
in production today and is proven by the tests below, not by a run.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tgfarm import api, crosspost_journal, s9_growth
from tgfarm.db.models import Channel, CrosspostLog, Post
from tgfarm.db.session import session_scope
from tgfarm.s9_growth.adapters import MAX_AUTOMATIC_CROSSPOST_ATTEMPTS

# ---------------------------------------------------------------------------
# The vocabulary and the three dispositions, as literals, on purpose.
#
# Everything below this block is written out rather than imported from
# `crosspost_journal` because the point of these pins is to *notice*: a status
# added to a shared class, or a reader re-widened to a set that does not describe
# its question, must fail here and be re-examined by a human.
# ---------------------------------------------------------------------------

DECLARED_STATUSES = frozenset(
    {
        "posted",
        "scheduled",
        "unknown",
        "publishing",
        "permanent",
        "failed",
        "skipped",
        "dry_run",
        "retry_authorized",
    }
)
SUPPRESSED = frozenset({"posted", "scheduled", "unknown", "publishing", "permanent"})
TIME_BOUNDED = frozenset({"failed", "skipped"})
REOPEN = frozenset({"dry_run", "retry_authorized"})

#: `api.JOURNAL_OPERATOR_ATTENTION_STATUSES` — attention by status alone.
ATTENTION_STATUSES = frozenset({"unknown", "permanent"})
#: `api.JOURNAL_RECONCILABLE_STATUSES` — what the operator may reconcile.
RECONCILABLE_STATUSES = frozenset({"unknown", "publishing", "permanent"})
#: `s9_growth._retryable_journal_statuses()` — what growth may pick up again.
GROWTH_RETRYABLE_STATUSES = frozenset({"failed", "retry_authorized", "skipped", "dry_run"})

#: A token no writer in this repository can produce: a row written by something
#: the codebase has never met. Every fail-closed assertion below uses it.
UNDECLARED = "quarantined"

STALE_MINUTES = 45
FRESH_MINUTES = 2
STALENESS_BOUND = timedelta(minutes=30)

#: (needs attention by status alone, reconcilable, offered to growth)
DISPOSITIONS: dict[str, tuple[bool, bool, bool]] = {
    "posted": (False, False, False),
    "scheduled": (False, False, False),
    "unknown": (True, True, False),
    "permanent": (True, True, False),
    "publishing": (False, True, False),
    "failed": (False, False, True),
    "skipped": (False, False, True),
    "dry_run": (False, False, True),
    "retry_authorized": (False, False, True),
}


@pytest.fixture()
def journal():
    """Seed one (post, platform) pair and delete everything this file created.

    `conftest` gives every test file the same session database, so each
    measurement is taken inside a platform group this file invented and its rows
    are removed on the way out; the suite already has a demonstrated order
    dependence when a delivery reader's group is left behind for the next file.
    """
    public_ids: list[str] = []

    def _pair(
        status: object,
        *,
        platform: str,
        attempt: int = 1,
        age_minutes: int = STALE_MINUTES,
    ) -> str:
        public_id = f"jr-{uuid4().hex[:12]}"
        public_ids.append(public_id)
        now = datetime.now(UTC)
        with session_scope() as session:
            session.add(
                Channel(
                    id=f"jr_chan_{public_id}",
                    title="journal readers",
                    niche="tech",
                    platform="telegram",
                    enabled=True,
                )
            )
            session.add(
                Post(
                    public_id=public_id,
                    channel_id=f"jr_chan_{public_id}",
                    hook="h",
                    body="b",
                    status="published",
                    # Older than any window growth is called with, so the only way
                    # this pair can come back is through the retryable sub-query.
                    published_at=now - timedelta(days=3),
                )
            )
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=platform,
                    status=str(status),
                    attempt=attempt,
                    created_at=now - timedelta(minutes=age_minutes),
                )
            )
        return public_id

    yield _pair

    with session_scope() as session:
        for public_id in public_ids:
            session.query(CrosspostLog).filter_by(post_public_id=public_id).delete(
                synchronize_session=False
            )
            session.query(Post).filter_by(public_id=public_id).delete(synchronize_session=False)
            session.query(Channel).filter_by(id=f"jr_chan_{public_id}").delete(
                synchronize_session=False
            )


def _health() -> dict:
    return api._crosspost_health()  # noqa: SLF001 - the reader under test


def _attention_of(journal, platform: str, status: object, **kwargs) -> tuple[dict, int]:
    """The platform group this pair created, and the fleet attention it moved."""
    before = _health()["totals"]["needs_attention"]
    journal(status, platform=platform, **kwargs)
    report = _health()
    item = next((p for p in report["platforms"] if p["platform"] == platform), None)
    assert item is not None, f"the reader never grouped its own platform {platform!r}"
    return item, report["totals"]["needs_attention"] - before


def _growth_ids(platform: str) -> set[str]:
    """Growth's candidate read, restricted to this file's platform."""
    return {
        public_id
        for _pk, public_id in s9_growth._pending_crossposts(0.0, 60, None, [platform])  # noqa: SLF001
    }


def _client():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    return TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1"), {
        "X-TGFarm-Token": ensure_token()
    }


def _token_containers(func) -> list[frozenset[str]]:
    """Every set/list/tuple literal inside `func` holding a journal status token.

    Dict *keys* are deliberately not matched: `_crosspost_health`'s per-status
    keys are report field names the panel and `tests/test_crosspost_export.py`
    own, not decisions about a status.
    """
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


# ---------------------------------------------------------------------------
# 1. The owner's vocabulary is the one these readers were written against.
# ---------------------------------------------------------------------------


def test_the_declared_vocabulary_is_the_pinned_nine() -> None:
    """The drift trip-wire, in terms of `JOURNAL_KNOWN_STATUSES`.

    A status added to the shared module — to any of the three classes, or to the
    known set alone — makes this fail before any operator-facing gate can move by
    omission, and the message names the three readers that have to be re-examined.
    """
    assert set(crosspost_journal.JOURNAL_KNOWN_STATUSES) == DECLARED_STATUSES, (
        "tgfarm/crosspost_journal.py declares a new journal status: re-examine "
        "api._crosspost_health, api._reconcile_crosspost_outcome and "
        "s9_growth._pending_crossposts, then write their disposition into DISPOSITIONS"
    )
    assert crosspost_journal.JOURNAL_SUPPRESSED_STATUSES == SUPPRESSED
    assert crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES == TIME_BOUNDED
    assert crosspost_journal.JOURNAL_REOPEN_STATUSES == REOPEN
    assert set(DISPOSITIONS) == set(crosspost_journal.JOURNAL_KNOWN_STATUSES)


@pytest.mark.parametrize("status", sorted(DECLARED_STATUSES))
def test_each_reader_has_a_written_disposition(status: str) -> None:
    attention, reconcilable, retryable = DISPOSITIONS[status]
    assert (status in api.JOURNAL_OPERATOR_ATTENTION_STATUSES) is attention
    assert (status in api.JOURNAL_RECONCILABLE_STATUSES) is reconcilable
    assert (status in frozenset(s9_growth._retryable_journal_statuses())) is retryable  # noqa: SLF001


@pytest.mark.parametrize(
    ("reader", "name"),
    [
        (api._crosspost_health, "api._crosspost_health"),
        (api._reconcile_crosspost_outcome, "api._reconcile_crosspost_outcome"),
        (api._journal_identity, "api._journal_identity"),
        (s9_growth._pending_crossposts, "s9_growth._pending_crossposts"),
        (s9_growth._retryable_journal_statuses, "s9_growth._retryable_journal_statuses"),
    ],
)
def test_no_reader_owns_a_status_token_container(reader, name: str) -> None:
    """One grep finds the vocabulary, and it finds it in the shared owner.

    The membership decisions these functions make now name `JOURNAL_STATUS_*` or
    read the declared classes; nothing inside them is a list/set/tuple of spelled
    statuses any more.
    """
    assert _token_containers(reader) == [], f"{name} still carries its own status literals"


def test_the_literal_scan_sees_a_literal_when_one_exists() -> None:
    """The scan's own aim, so an empty result above cannot mean a broken scanner.

    This is what `s9_growth._pending_crossposts` carried before this block, and it
    still trips here.
    """

    def _was_the_defect(rows) -> list:
        return [row for row in rows if row.status in ("failed", "retry_authorized", "unknown")]

    assert _token_containers(_was_the_defect) == [
        frozenset({"failed", "retry_authorized", "unknown"})
    ]


# ---------------------------------------------------------------------------
# 2. Reader 1 — the fleet/sources "needs attention" counter.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state", "suppressed"),
    [
        ("posted", "healthy", True),
        ("scheduled", "scheduled", True),
        ("dry_run", "dry_run", False),
        ("skipped", "setup", False),
    ],
)
def test_a_proven_queued_or_routine_row_never_needs_attention(
    journal, status, state, suppressed
) -> None:
    """`posted`/`scheduled` are suppressed for *re-offer* and ordinary to look at.

    This is the assertion that keeps the planner's set out of this reader: both
    statuses are members of `JOURNAL_SUPPRESSED_STATUSES`, and neither may move
    the counter that decides where the operator looks.
    """
    assert (status in crosspost_journal.JOURNAL_SUPPRESSED_STATUSES) is suppressed
    item, moved = _attention_of(journal, f"jr-attention-{status}", status)
    assert moved == 0
    assert item["oldest_attention_at"] is None
    assert item["stuck"] == 0
    assert item["state"] == state


def test_a_stale_publishing_row_is_still_attention(journal) -> None:
    """The stuck case: an in-flight row older than the bound must not hide."""
    item, moved = _attention_of(
        journal, "jr-publishing-stale", crosspost_journal.JOURNAL_STATUS_PUBLISHING,
        age_minutes=STALE_MINUTES,
    )
    assert moved == 1
    assert item["publishing"] == 1
    assert item["stuck"] == 1
    assert item["state"] == "attention"
    assert item["oldest_attention_at"] is not None


def test_a_publishing_row_inside_the_bound_is_working_not_stuck(journal) -> None:
    item, moved = _attention_of(
        journal, "jr-publishing-fresh", crosspost_journal.JOURNAL_STATUS_PUBLISHING,
        age_minutes=FRESH_MINUTES,
    )
    assert moved == 0
    assert item["stuck"] == 0
    assert item["state"] == "working"


def test_the_staleness_bound_is_one_number_shared_by_both_readers(journal) -> None:
    """A row may not read as stuck to the counter and as still running to reconcile.

    Measured across the bound rather than asserted about a constant: 29 minutes is
    neither stuck nor reconcilable, 31 is both.
    """
    assert api.CROSSPOST_PUBLISHING_STALE_AFTER == STALENESS_BOUND
    before = _health()["totals"]
    recent = journal(crosspost_journal.JOURNAL_STATUS_PUBLISHING, platform="vk", age_minutes=29)
    older = journal(crosspost_journal.JOURNAL_STATUS_PUBLISHING, platform="vk", age_minutes=31)
    after = _health()["totals"]
    assert after["stuck"] - before["stuck"] == 1
    assert after["needs_attention"] - before["needs_attention"] == 1
    assert (
        api._reconcile_crosspost_outcome(recent, "vk", "confirm_posted")["error"]  # noqa: SLF001
        == "still_in_flight"
    )
    assert (
        api._reconcile_crosspost_outcome(older, "vk", "confirm_posted")["ok"] is True  # noqa: SLF001
    )


@pytest.mark.parametrize(
    ("attempt", "moved", "state"),
    [(1, 0, "working"), (MAX_AUTOMATIC_CROSSPOST_ATTEMPTS, 1, "attention")],
)
def test_failure_needs_attention_only_past_the_attempt_ceiling(journal, attempt, moved, state) -> None:
    """The ceiling is the adapter's constant, never a number typed into the reader."""
    item, delta = _attention_of(
        journal, f"jr-failed-{attempt}", crosspost_journal.JOURNAL_STATUS_FAILED, attempt=attempt
    )
    assert delta == moved
    assert item["failed"] == 1
    assert item["state"] == state


@pytest.mark.parametrize("status", ["unknown", "permanent"])
def test_ambiguity_and_a_definite_refusal_are_attention(journal, status) -> None:
    item, moved = _attention_of(journal, f"jr-{status}", status)
    assert moved == 1
    assert item["state"] == "attention"


def test_the_attention_set_is_not_the_suppressed_set() -> None:
    """The axis, written down: only the ambiguous/refused pair raises the alarm."""
    assert api.JOURNAL_OPERATOR_ATTENTION_STATUSES == ATTENTION_STATUSES
    assert ATTENTION_STATUSES < SUPPRESSED
    assert SUPPRESSED - ATTENTION_STATUSES == frozenset({"posted", "scheduled", "publishing"})
    assert api.JOURNAL_OPERATOR_ATTENTION_STATUSES is not crosspost_journal.JOURNAL_SUPPRESSED_STATUSES


def test_a_mis_cased_status_is_normalised_like_the_other_two_readers(journal) -> None:
    """`"Posted "` used to be counted under a key nobody wrote.

    The shared normalisation is what the planner and the claim gate already used;
    a row that only one reader can read is the divergence this block exists to
    close, and here it was hiding a proven publication *and* a stuck one.
    """
    item, moved = _attention_of(journal, "jr-normalise-posted", "Posted ")
    assert moved == 0
    assert item["posted"] == 1
    assert item["unknown"] == 0

    stale = _attention_of(
        journal, "jr-normalise-publishing", "PUBLISHING", age_minutes=STALE_MINUTES
    )
    assert stale[1] == 1
    assert stale[0]["stuck"] == 1


def test_an_undeclared_status_is_counted_and_the_operator_is_told(journal, caplog) -> None:
    """The hole this block closes: a row nobody can explain was invisible.

    Before, such a row raised `targets`, no bucket, `state: not_tested` and no
    attention. Now it is counted in `unknown` — the one key that means "we cannot
    explain this row" and which already drives attention — and its raw token is
    named in a warning, so the report loses nothing and the pair cannot hide.
    """
    with caplog.at_level(logging.WARNING, logger="tgfarm.api"):
        item, moved = _attention_of(journal, "jr-undeclared", UNDECLARED)
    assert moved == 1
    assert item["unknown"] == 1
    assert item["targets"] == 1
    assert item["state"] == "attention"
    message = " ".join(record.getMessage() for record in caplog.records if record.name == "tgfarm.api")
    assert UNDECLARED in message
    assert "tgfarm/crosspost_journal.py" in message


def test_a_row_with_no_status_at_all_is_attention(journal) -> None:
    """Fail closed on the empty token too: no answer is not a good answer."""
    item, moved = _attention_of(journal, "jr-empty", "")
    assert moved == 1
    assert item["unknown"] == 1
    assert item["state"] == "attention"


# ---------------------------------------------------------------------------
# 3. Reader 2 — may the operator reconcile this row?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["posted", "scheduled"])
def test_a_row_the_journal_believes_was_delivered_is_not_reconcilable(journal, status) -> None:
    """`posted`/`scheduled` are suppressed for re-offer, and reconciling either
    would authorise exactly the blind re-send the journal exists to prevent. The
    journal must also gain no new row from the refusal."""
    public_id = journal(status, platform="vk")
    result = api._reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")  # noqa: SLF001
    assert result["ok"] is False
    assert result["error"] == "not_reconcilable"
    assert result["status"] == status
    with session_scope() as session:
        rows = session.query(CrosspostLog).filter_by(post_public_id=public_id).count()
    assert rows == 1


@pytest.mark.parametrize("status", ["unknown", "permanent"])
def test_an_ambiguous_or_refused_row_is_reconcilable(journal, status) -> None:
    public_id = journal(status, platform="vk")
    assert api._reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")["ok"] is True  # noqa: E501, SLF001


def test_a_stale_publishing_row_is_reconcilable(journal) -> None:
    public_id = journal(
        crosspost_journal.JOURNAL_STATUS_PUBLISHING, platform="vk", age_minutes=STALE_MINUTES
    )
    assert api._reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")["ok"] is True  # noqa: E501, SLF001


def test_an_exhausted_failure_is_reconcilable_and_a_live_one_is_not(journal) -> None:
    active = journal(crosspost_journal.JOURNAL_STATUS_FAILED, platform="vk", attempt=1)
    exhausted = journal(
        crosspost_journal.JOURNAL_STATUS_FAILED, platform="vk",
        attempt=MAX_AUTOMATIC_CROSSPOST_ATTEMPTS,
    )
    assert api._reconcile_crosspost_outcome(active, "vk", "retry_after_check")["error"] == "not_reconcilable"  # noqa: E501, SLF001
    result = api._reconcile_crosspost_outcome(exhausted, "vk", "retry_after_check")  # noqa: SLF001
    assert result["ok"] is True
    assert result["status"] == crosspost_journal.JOURNAL_STATUS_RETRY_AUTHORIZED


def test_the_reconcilable_set_is_explicit_because_it_authorises_a_retry() -> None:
    """Pinned, and pinned *as a subset*: the gate is the three rows this code
    cannot resolve, never the whole suppressed class."""
    assert api.JOURNAL_RECONCILABLE_STATUSES == RECONCILABLE_STATUSES
    assert RECONCILABLE_STATUSES <= SUPPRESSED
    assert SUPPRESSED - RECONCILABLE_STATUSES == frozenset({"posted", "scheduled"})
    assert crosspost_journal.JOURNAL_STATUS_PUBLISHING in api.JOURNAL_RECONCILABLE_STATUSES


def test_an_undeclared_status_is_not_reconcilable_and_shows_its_token(journal) -> None:
    """What this gate does today with a status nobody knows, read rather than
    assumed: it refuses, and it hands the raw column value back. The route turns
    that into a 409 whose detail names the status, so the refusal is shown."""
    public_id = journal(UNDECLARED, platform="vk")
    result = api._reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")  # noqa: SLF001
    assert result["ok"] is False
    assert result["error"] == "not_reconcilable"
    assert result["status"] == UNDECLARED
    with session_scope() as session:
        assert session.query(CrosspostLog).filter_by(post_public_id=public_id).count() == 1


def test_the_reconcile_endpoint_shows_the_operator_the_status_it_refused(journal) -> None:
    client, headers = _client()
    public_id = journal(UNDECLARED, platform="vk")
    response = client.post(
        f"/api/crossposts/{public_id}/vk/reconcile",
        headers=headers,
        json={"action": "confirm_posted"},
    )
    assert response.status_code == 409
    assert UNDECLARED in response.json()["detail"]


def test_widening_the_suppressed_set_does_not_open_the_reconcile_gate(journal, monkeypatch) -> None:
    """The gate is explicit *because* it authorises a re-send.

    Growth's reader follows the vocabulary on purpose; this one must not, so the
    drift experiment is kept as a permanent test of the difference.
    """
    public_id = journal(UNDECLARED, platform="vk")
    monkeypatch.setattr(
        crosspost_journal,
        "JOURNAL_SUPPRESSED_STATUSES",
        crosspost_journal.JOURNAL_SUPPRESSED_STATUSES | {UNDECLARED},
    )
    assert api._reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")["error"] == "not_reconcilable"  # noqa: E501, SLF001
    assert api.JOURNAL_RECONCILABLE_STATUSES == RECONCILABLE_STATUSES


# ---------------------------------------------------------------------------
# 4. Reader 3 — which posts look retryable when growth picks its targets.
# ---------------------------------------------------------------------------


def test_the_growth_retryable_set_is_the_declared_union_and_the_pinned_four() -> None:
    """This reader *does* take the shared axis — re-offer is its question — and
    the membership that falls out of it is pinned as a literal, so a widened class
    cannot silently move the gate. `GROWTH_RETRYABLE_STATUSES` above is that pin.
    """
    derived = frozenset(s9_growth._retryable_journal_statuses())  # noqa: SLF001
    assert derived == GROWTH_RETRYABLE_STATUSES
    assert derived == crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES | (
        crosspost_journal.JOURNAL_REOPEN_STATUSES
    )
    # The re-offer classes are disjoint by declaration; a retryable set that also
    # contained a suppressed status would be double-publishing by design.
    assert derived & SUPPRESSED == frozenset()
    assert not derived & crosspost_journal.JOURNAL_SUPPRESSED_STATUSES


@pytest.mark.parametrize(
    "status",
    ["posted", "scheduled", "publishing", "unknown", "permanent", UNDECLARED, ""],
)
def test_a_suppressed_or_unexplainable_row_is_never_selected_as_retryable(journal, status) -> None:
    public_id = journal(status, platform="jr-growth-suppressed")
    assert public_id not in _growth_ids("jr-growth-suppressed")


@pytest.mark.parametrize(
    "status",
    ["failed", "retry_authorized", "skipped", "dry_run"],
)
def test_a_re_openable_row_is_selected_again(journal, status) -> None:
    public_id = journal(status, platform="jr-growth-retry")
    assert public_id in _growth_ids("jr-growth-retry")


def test_growth_matches_the_stored_token_literally_not_through_the_normaliser(
    journal,
) -> None:
    """The asymmetry the experiment measured, put on the record rather than hidden.

    Health and the reconcile gate ask Python, so `journal_status_of` strips and
    lower-cases a row before judging it. Growth asks the database for pairs whose
    status *is* one of the retryable tokens, so a row written `"Failed "` stays out
    of the IN-list and the pair is skipped in silence. The direction is safe — it
    never re-offers something it cannot read — but it is not self-announcing, and
    closing it means either normalising in SQL or making every writer store the
    canonical token. Neither is this reader's call alone, so the behaviour is
    pinned here: if a later block fixes it, this test is the one that must change.
    """
    mis_cased = journal("Failed ", platform="jr-growth-literal")
    canonical = journal("failed", platform="jr-growth-literal")
    selected = _growth_ids("jr-growth-literal")
    assert canonical in selected
    assert mis_cased not in selected


def test_growth_follows_the_vocabulary_and_the_pin_above_is_the_tripwire(
    journal, monkeypatch
) -> None:
    """The measured coupling: declaring a status RE-OPEN moves this reader with no
    edit here, and the pinned literal set fails in the same run. That is the
    intended shape — the vocabulary owns the answer, the pin owns the notice.
    """
    public_id = journal(UNDECLARED, platform="jr-growth-drift")
    assert public_id not in _growth_ids("jr-growth-drift")
    monkeypatch.setattr(
        crosspost_journal,
        "JOURNAL_REOPEN_STATUSES",
        crosspost_journal.JOURNAL_REOPEN_STATUSES | {UNDECLARED},
    )
    assert frozenset(s9_growth._retryable_journal_statuses()) - GROWTH_RETRYABLE_STATUSES == (  # noqa: SLF001
        frozenset({UNDECLARED})
    )
    assert public_id in _growth_ids("jr-growth-drift")


def test_declaring_a_status_without_revisiting_health_is_caught_not_hidden(
    journal, monkeypatch, caplog
) -> None:
    """What the fail-closed fold does *not* cover, stated plainly.

    Folding the UNKNOWN class into the `unknown` bucket closes the hole for a token
    nobody declared. A token declared into a class this reader does not treat as
    attention goes back to being unbucketed and uncounted — measured below, so the
    limitation is on the record. `test_the_declared_vocabulary_is_the_pinned_nine`
    is the guard that makes that state unreachable without a human noticing.
    """
    monkeypatch.setattr(
        crosspost_journal,
        "JOURNAL_TIME_BOUNDED_STATUSES",
        crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES | {UNDECLARED},
    )
    before = _health()["totals"]["needs_attention"]
    journal(UNDECLARED, platform="jr-health-drift")
    with caplog.at_level(logging.WARNING, logger="tgfarm.api"):
        report = _health()
    item = next(p for p in report["platforms"] if p["platform"] == "jr-health-drift")
    assert item["targets"] == 1
    assert item["unknown"] == 0
    assert report["totals"]["needs_attention"] == before
    assert "crosspost health" not in " ".join(r.getMessage() for r in caplog.records)

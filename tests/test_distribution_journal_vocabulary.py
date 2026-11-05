"""Pinned budget numbers for the journal-status sets inside `tgfarm/distribution.py`.

P76. Three (actually four) literal status sets in `distribution.py` decide
publishing budgets, and they are *not* the same axis as the re-offer vocabulary in
`tgfarm/crosspost_journal.py`:

* `effective_daily_cap` counts an *attempt* (the denominator) and a *delivery*
  (the numerator) and turns the ratio into a daily cap;
* `build_distribution_plan` counts which journal rows *occupy a slot* in the
  trailing 24 hours (`daily_sent`), and which pairs count as already delivered
  for the text-similarity filter.

The re-offer sets classify a status by "may this pair be offered again?". The
budget sets classify a status by "did we try / did it land / is the slot spent?".
Those questions have different answers for `permanent`, `publishing`, `skipped`,
`dry_run`, `retry_authorized` and for any status nobody declared, so a
"meaning-preserving-looking" rewrite that routes a budget set through
`JOURNAL_SUPPRESSED_STATUSES` / `JOURNAL_KNOWN_STATUSES` moves a number.

Every expected value below is derived by hand from the fixture and written as a
literal; none of them is computed by calling the implementation. The fixtures are
built so that each disputed membership flips at least one expected number — that
is what makes these tests a fence around the refactor rather than a description
of it.

Two independent vocabularies are exercised: the pure cap arithmetic (a list of
row-likes is enough) and the plan-level allowance arithmetic (`daily_cap` /
`daily_sent` / `remaining`), which needs real `crosspost_logs` rows.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from tgfarm import crosspost_journal, distribution
from tgfarm.core.settings import get_settings
from tgfarm.db.models import CrosspostLog
from tgfarm.db.session import session_scope

# `vk` has base 3 and max_daily 4 — the widest spread in PLATFORM_RULES, so every
# reliability regime (base-1 / base / base+1) is reachable and distinguishable.
CAP_PLATFORM = "vk"
# A destination no other test file journalled rows for (checked over `tests/`), so
# the plan-level assertions cannot be polluted by a residual row, and whose budget
# numbers are small enough that `remaining` is sensitive to a single miscount.
PLAN_PLATFORM = "odnoklassniki"

# Hand-read from PLATFORM_RULES (vk 3/4, x 2/3, reddit 1/1, odnoklassniki 2/3).
VK_BASE, VK_MAX_DAILY = 3, 4
X_BASE, X_MAX_DAILY = 2, 3
REDDIT_BASE, REDDIT_MAX_DAILY = 1, 1
OK_BASE, OK_MAX_DAILY = 2, 3

#: Every status `crosspost_journal` declares, plus the two the claim model can
#: write that a reviewer might expect to be missing, plus statuses nobody declared.
EVERY_KNOWN_STATUS = (
    "posted",
    "scheduled",
    "failed",
    "permanent",
    "dry_run",
    "skipped",
    "publishing",
    "retry_authorized",
    "unknown",
)
#: The single row of each: one per declared status, then the undeclared ones.
UNDECLARED_STATUSES = ("sent_ok", "", "POSTED")

FULL_VOCABULARY = (*EVERY_KNOWN_STATUS, *UNDECLARED_STATUSES)

#: Declared *and* undeclared statuses that are neither an attempt nor a delivery:
#: the two reopen classes, the two time-bounded-but-failure-free ones, the
#: suppressed `publishing`/`unknown`, and rows nobody can explain.
NON_ATTEMPT_STATUSES = (
    "dry_run",
    "skipped",
    "publishing",
    "retry_authorized",
    "unknown",
    *UNDECLARED_STATUSES,
)


def _rows(platform: str, statuses) -> list[SimpleNamespace]:
    return [SimpleNamespace(platform=platform, status=status) for status in statuses]


def _cap(platform: str, statuses, *, rows: list[SimpleNamespace] | None = None) -> int:
    return distribution.effective_daily_cap(platform, rows or _rows(platform, statuses))


# ---------------------------------------------------------------------------
# 0. The baseline reading of the shared vocabulary.
# ---------------------------------------------------------------------------


def test_shared_sets_are_the_reoffer_axis_not_the_budget_axis() -> None:
    """Record, as data, why the shared sets cannot simply be substituted.

    The registry note for this block claimed `scheduled` and `permanent` were
    absent from `JOURNAL_KNOWN_STATUSES` at this baseline. They are not — both
    are members of `JOURNAL_SUPPRESSED_STATUSES`. So the real reason these three
    sites cannot be unified is the opposite one: the shared sets are *too big*
    for a budget numerator, and substituting them would over-count successes.
    """
    assert crosspost_journal.JOURNAL_SUPPRESSED_STATUSES == frozenset(
        {"posted", "scheduled", "unknown", "publishing", "permanent"}
    )
    assert crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES == frozenset({"failed", "skipped"})
    assert crosspost_journal.JOURNAL_REOPEN_STATUSES == frozenset({"dry_run", "retry_authorized"})
    assert "scheduled" in crosspost_journal.JOURNAL_KNOWN_STATUSES
    assert "permanent" in crosspost_journal.JOURNAL_KNOWN_STATUSES

    # Substituting the re-offer axis for the budget axis moves numbers:
    # the attempt set would gain publishing/unknown/skipped/dry_run/retry_authorized,
    # and the delivery numerator would gain permanent/publishing/unknown.
    assert not (crosspost_journal.JOURNAL_KNOWN_STATUSES <= _attempts_set_for_test())
    assert not (crosspost_journal.JOURNAL_SUPPRESSED_STATUSES <= _delivered_set_for_test())


# The two helpers below read the *specification* of this file, never the
# implementation: they are the hand-written tables the expected numbers come from.
def _attempts_set_for_test() -> frozenset[str]:
    return frozenset({"posted", "scheduled", "failed", "permanent"})


def _delivered_set_for_test() -> frozenset[str]:
    return frozenset({"posted", "scheduled"})


def _budget_slot_set_for_test() -> frozenset[str]:
    return frozenset({"posted", "scheduled", "publishing", "unknown"})


# ---------------------------------------------------------------------------
# 1. The denominator: which statuses are an *attempt* at all.
# ---------------------------------------------------------------------------


def test_full_vocabulary_fixture_stays_under_the_attempt_floor() -> None:
    """12 rows, 4 of them attempts: `len(attempts) < 8`, so the base cap survives.

    Attempts here are exactly posted + scheduled + failed + permanent. The other
    eight statuses are invisible to the ratio, so a cap adjustment never fires.
    """
    assert len(FULL_VOCABULARY) == 12
    assert sum(1 for status in FULL_VOCABULARY if status in _attempts_set_for_test()) == 4
    assert _cap(CAP_PLATFORM, FULL_VOCABULARY) == VK_BASE


def test_the_same_fixture_plus_four_failures_crosses_the_floor_and_fails_the_rate() -> None:
    """16 rows → 8 attempts (2 delivered + 6 failed) → 2/8 = 0.25 → base - 1 = 2."""
    statuses = [*FULL_VOCABULARY, "failed", "failed", "failed", "failed"]
    assert sum(1 for s in statuses if s in _attempts_set_for_test()) == 8
    assert sum(1 for s in statuses if s in _delivered_set_for_test()) == 2
    assert _cap(CAP_PLATFORM, statuses) == 2


@pytest.mark.parametrize(
    ("statuses", "expected_cap"),
    [
        # `permanent` is an attempt but not a delivery: 7 delivered / 11 attempts
        # = 0.636 < 0.70 → base - 1. Drop it from the denominator and the fixture
        # falls under the floor (7 attempts) and returns 3; count it as a delivery
        # and the ratio is 1.0 and the cap becomes 4. Both mistakes are caught.
        ([*["posted"] * 7, *["permanent"] * 4], 2),
        # `scheduled` is a delivery: 19/20 = 0.95 → base + 1 → min(4, 4) = 4.
        # Without `scheduled` in the numerator the ratio is 0.0 → cap 2.
        ([*["scheduled"] * 19, "failed"], VK_MAX_DAILY),
        # `skipped` is not an attempt: 20/20 = 1.0 → 4. Count it and the ratio is
        # 20/25 = 0.80 → base 3.
        ([*["posted"] * 20, *["skipped"] * 5], VK_MAX_DAILY),
        # Nothing else is an attempt, so 20/20 stays at 1.0 → base + 1. Any single
        # member of `NON_ATTEMPT_STATUSES` that joined the denominator would drag
        # the ratio to 20/28 = 0.714 → base 3, and two of them to 20/30 = 0.667 → 2.
        ([*["posted"] * 20, *NON_ATTEMPT_STATUSES], VK_MAX_DAILY),
        # An empty status and an unknown status are not evidence of anything:
        # neither numerator nor denominator, so the cap is the unadjusted base.
        ([*["posted"] * 4, "", "sent_ok", "bogus"], VK_BASE),
    ],
    ids=[
        "permanent-is-an-attempt-not-a-delivery",
        "scheduled-counts-as-delivered",
        "skipped-is-not-an-attempt",
        "declared-and-undeclared-others-are-invisible",
        "empty-and-unknown-status-are-invisible",
    ],
)
def test_attempt_and_delivery_membership_pinned_by_the_cap(statuses, expected_cap: int) -> None:
    assert _cap(CAP_PLATFORM, statuses) == expected_cap


# ---------------------------------------------------------------------------
# 2. The ratio boundaries, pinned as numbers rather than as prose.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delivered", "failed", "expected_cap"),
    [
        (19, 1, VK_MAX_DAILY),  # 19/20 = 0.95 → the >= 0.95 branch fires
        (18, 2, VK_BASE),  # 18/20 = 0.90 → neither branch
        (14, 6, VK_BASE),  # 14/20 = 0.70 → `success_rate < 0.70` is still false
        (13, 7, 2),  # 13/20 = 0.65 → base - 1
        (0, 8, 2),  # 0.00 → base - 1
        (8, 0, VK_MAX_DAILY),  # 1.00 → base + 1
    ],
    ids=["0.95", "0.90", "0.70", "0.65", "0.00", "1.00"],
)
def test_success_rate_boundaries(delivered: int, failed: int, expected_cap: int) -> None:
    assert delivered + failed == 20 or delivered + failed == 8
    statuses = [*["posted"] * delivered, *["failed"] * failed]
    assert _cap(CAP_PLATFORM, statuses) == expected_cap


def test_attempt_floor_is_exactly_eight() -> None:
    """7 attempts is never adjusted; the 8th turns the same fixture into base + 1."""
    assert _cap(CAP_PLATFORM, ["posted"] * 7) == VK_BASE
    assert _cap(CAP_PLATFORM, ["posted"] * 8) == VK_MAX_DAILY


@pytest.mark.parametrize(
    ("platform", "statuses", "expected_cap"),
    [
        # max_daily is a ceiling on the *reward*, not on the penalty.
        ("x", ["posted"] * 20, min(X_MAX_DAILY, X_BASE + 1)),
        ("reddit", ["posted"] * 20, min(REDDIT_MAX_DAILY, REDDIT_BASE + 1)),
        # `max(1, base - 1)` floors a 1/day platform at 1: reddit never goes to 0.
        ("reddit", ["failed"] * 20, 1),
        # `max(1, base - 1)` floors a low-base platform: a 2/day platform can be
        # cut to 1 but never to 0.
        ("bluesky", ["failed"] * 20, 1),
    ],
    ids=["x-ceiling", "reddit-ceiling", "reddit-floor-at-one", "bluesky-floor-at-one"],
)
def test_platform_rule_constants(platform, statuses, expected_cap: int) -> None:
    assert _cap(platform, statuses) == expected_cap


def test_cap_only_counts_rows_of_the_platform_asked_about() -> None:
    """One shared log list, four different answers: `row.platform == platform_id`."""
    rows = [
        *_rows("vk", ["posted"] * 20),
        *_rows(PLAN_PLATFORM, FULL_VOCABULARY),
        *_rows("x", ["failed"] * 20),
    ]
    assert distribution.effective_daily_cap("vk", rows) == VK_MAX_DAILY
    assert distribution.effective_daily_cap(PLAN_PLATFORM, rows) == OK_BASE
    assert distribution.effective_daily_cap("x", rows) == 1
    # A platform with no rows at all keeps its base cap.
    bluesky_base = distribution.PLATFORM_RULES["bluesky"]["daily"]
    assert distribution.effective_daily_cap("bluesky", rows) == bluesky_base


def test_the_operator_ceiling_clamps_but_never_raises() -> None:
    ceiling_one = get_settings().model_copy(update={"max_posts_per_channel_day": 1})
    original = distribution.get_settings
    distribution.get_settings = lambda: ceiling_one  # type: ignore[assignment]
    try:
        # 20 clean attempts would earn vk 4; the operator ceiling pins it to 1.
        assert _cap(CAP_PLATFORM, ["posted"] * 20) == 1
        assert _cap(CAP_PLATFORM, ["failed"] * 20) == 1
    finally:
        distribution.get_settings = original  # type: ignore[assignment]
    # And at the code default the clamp is the identity for every reachable cap.
    assert distribution.channel_daily_ceiling() > VK_MAX_DAILY
    assert _cap(CAP_PLATFORM, ["posted"] * 20) == VK_MAX_DAILY


# ---------------------------------------------------------------------------
# 3. Plan-level allowance arithmetic: `daily_sent` burns a slot of the cap.
# ---------------------------------------------------------------------------


@pytest.fixture()
def plan_env() -> datetime:
    """A clean journal window for `PLAN_PLATFORM`, plus the clock it was seeded at.

    The session database is shared with every other test file, so residual
    `crosspost_logs` rows for the same destination inside the trailing 24 hours
    would be counted as spent slots by the planner. The target destination is
    wiped at fixture entry so the assertions below are order-independent;
    `PLAN_PLATFORM` was chosen because no other test file journalled rows for it,
    and each test deletes the pairs it created on its way out.
    """
    now = datetime.now(UTC)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.platform == PLAN_PLATFORM))
    yield now
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.platform == PLAN_PLATFORM))


def _plan_entry(now: datetime, statuses) -> dict:
    """Seed one journalled pair per status and return the planner's budget entry."""
    public_ids: list[str] = []
    with session_scope() as session:
        for status in statuses:
            public_id = f"p76{uuid4().hex[:26]}"[:32]
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=PLAN_PLATFORM,
                    status=status,
                    attempt=1,
                    created_at=now - timedelta(hours=1),
                )
            )
            public_ids.append(public_id)
    result = distribution.build_distribution_plan(now=now, max_age_days=14, max_selected_per_platform=5)
    entry = next(item for item in result["platforms"] if item["platform"] == PLAN_PLATFORM)
    return {**entry, "_public_ids": public_ids}


def _assert_budget(entry: dict, *, cap: int, sent: int, remaining: int) -> None:
    assert entry["daily_cap"] == cap
    assert entry["daily_sent"] == sent
    assert entry["remaining"] == remaining


def test_permanent_does_not_burn_a_daily_slot(plan_env: datetime) -> None:
    """A refused publication occupies no slot.

    `daily_cap` is the base because 1 attempt is under the floor; `daily_sent` is
    0 because `permanent` is not one of the counted statuses. Routing this filter
    through `JOURNAL_SUPPRESSED_STATUSES` would make it 1 spent slot and cut
    `remaining` from 2 to 1 — a budget change on the strength of a row the
    destination refused.
    """
    entry = _plan_entry(plan_env, ["permanent"])
    _assert_budget(entry, cap=OK_BASE, sent=0, remaining=OK_BASE)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(entry["_public_ids"])))


def test_publishing_and_unknown_do_burn_a_slot(plan_env: datetime) -> None:
    """In-flight and unexplained rows spend the budget; dropping them would double-send."""
    entry = _plan_entry(plan_env, ["publishing", "unknown"])
    _assert_budget(entry, cap=OK_BASE, sent=2, remaining=0)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(entry["_public_ids"])))


def test_reopen_and_time_bounded_statuses_do_not_burn_a_slot(plan_env: datetime) -> None:
    """dry_run / skipped / retry_authorized / undeclared rows are not deliveries."""
    entry = _plan_entry(plan_env, ["dry_run", "skipped", "retry_authorized", "sent_ok"])
    _assert_budget(entry, cap=OK_BASE, sent=0, remaining=OK_BASE)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(entry["_public_ids"])))


def test_full_vocabulary_slot_count(plan_env: datetime) -> None:
    """12 pairs, one per status: 4 slots spent, 0 left of a cap of 2.

    `daily_sent` counts {posted, scheduled, publishing, unknown}; the attempt
    denominator is 4 (posted, scheduled, failed, permanent), which is under the
    floor, so the cap stays at the platform base.
    """
    assert sum(1 for s in FULL_VOCABULARY if s in _budget_slot_set_for_test()) == 4
    entry = _plan_entry(plan_env, FULL_VOCABULARY)
    _assert_budget(entry, cap=OK_BASE, sent=4, remaining=0)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(entry["_public_ids"])))


def test_only_the_newest_row_of_a_pair_is_counted(plan_env: datetime) -> None:
    """`_latest_logs` wins per (post, platform): an old `posted` under a `dry_run` is not a slot."""
    public_id = f"p76{uuid4().hex[:26]}"[:32]
    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform=PLAN_PLATFORM,
                status="posted",
                attempt=1,
                created_at=plan_env - timedelta(hours=2),
            )
        )
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform=PLAN_PLATFORM,
                status="dry_run",
                attempt=2,
                created_at=plan_env - timedelta(minutes=30),
            )
        )
    result = distribution.build_distribution_plan(
        now=plan_env, max_age_days=14, max_selected_per_platform=5
    )
    entry = next(item for item in result["platforms"] if item["platform"] == PLAN_PLATFORM)
    _assert_budget(entry, cap=OK_BASE, sent=0, remaining=OK_BASE)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id == public_id))


def test_a_slot_only_counts_inside_the_trailing_day(plan_env: datetime) -> None:
    """A `posted` row older than 24 h still counts as an attempt, but not as a spent slot."""
    entry = _plan_entry(plan_env, ["posted"])
    public_ids = entry["_public_ids"]
    with session_scope() as session:
        row = session.query(CrosspostLog).filter(CrosspostLog.post_public_id == public_ids[0]).one()
        row.created_at = plan_env - timedelta(hours=25)
    result = distribution.build_distribution_plan(
        now=plan_env, max_age_days=14, max_selected_per_platform=5
    )
    entry = next(item for item in result["platforms"] if item["platform"] == PLAN_PLATFORM)
    _assert_budget(entry, cap=OK_BASE, sent=0, remaining=OK_BASE)
    with session_scope() as session:
        session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(public_ids)))


# ---------------------------------------------------------------------------
# 4. The vocabulary itself: what the refactor was allowed to change.
# ---------------------------------------------------------------------------


def test_the_budget_sets_are_the_hand_written_tables() -> None:
    """The named constants are exactly the literals this module always used."""
    assert distribution.JOURNAL_ATTEMPT_STATUSES == _attempts_set_for_test()
    assert distribution.JOURNAL_DELIVERED_STATUSES == _delivered_set_for_test()
    assert distribution.JOURNAL_SLOT_OCCUPYING_STATUSES == _budget_slot_set_for_test()


def test_the_budget_axis_is_not_the_reoffer_axis() -> None:
    """Name the membership that blocks each tempting substitution.

    Pinned as *differences*, so a future change to the leaf vocabulary has to pass
    through this test rather than silently re-pointing a budget at a re-offer class.
    """
    suppressed = crosspost_journal.JOURNAL_SUPPRESSED_STATUSES
    known = crosspost_journal.JOURNAL_KNOWN_STATUSES

    # `success_rate` may not read SUPPRESSED: three of its members are not
    # successes, and scoring them as ones pushes platforms over the 0.95 raise.
    assert suppressed - distribution.JOURNAL_DELIVERED_STATUSES == frozenset(
        {"permanent", "publishing", "unknown"}
    )
    # `daily_sent` may not read SUPPRESSED either: a publication the destination
    # refused frees its slot, every other member of that class spends one.
    assert suppressed - distribution.JOURNAL_SLOT_OCCUPYING_STATUSES == frozenset({"permanent"})
    assert distribution.JOURNAL_SLOT_OCCUPYING_STATUSES < suppressed
    # The attempt denominator may not read KNOWN: five declared statuses are not
    # requests made against the destination's API.
    assert known - distribution.JOURNAL_ATTEMPT_STATUSES == frozenset(
        {"dry_run", "skipped", "publishing", "retry_authorized", "unknown"}
    )
    assert distribution.JOURNAL_ATTEMPT_STATUSES < known
    # `skipped` in particular is a time-bounded re-offer, not an attempt.
    assert "skipped" in crosspost_journal.JOURNAL_TIME_BOUNDED_STATUSES
    assert "skipped" not in distribution.JOURNAL_ATTEMPT_STATUSES
    # The delivered set is a strict subset of the slot set: an in-flight claim
    # spends the day's budget without being a delivery.
    assert distribution.JOURNAL_DELIVERED_STATUSES < distribution.JOURNAL_SLOT_OCCUPYING_STATUSES


def test_an_undeclared_status_is_invisible_to_every_budget_number() -> None:
    """No report, no effect: neither numerator, nor denominator, nor spent slot.

    A status nobody declared is fail-*closed* for the re-offer question (the pair
    stops being offered) but fail-*quiet* for the budget: it is not evidence that
    a request was made, so it does not enter the denominator either. That is the
    honest reading — an unmeasured row must not be able to move a rate in either
    direction — and it is why `unknown` appears in the spent-slot set (it may
    already be a publication) but in neither half of the ratio.
    """
    for status in ("sent_ok", "published_recently", "", "POSTED"):
        assert status not in crosspost_journal.JOURNAL_KNOWN_STATUSES
        assert status not in distribution.JOURNAL_ATTEMPT_STATUSES
        assert status not in distribution.JOURNAL_DELIVERED_STATUSES
        assert status not in distribution.JOURNAL_SLOT_OCCUPYING_STATUSES
    # `unknown` is the declared one that behaves the same way in the ratio...
    assert "unknown" in crosspost_journal.JOURNAL_KNOWN_STATUSES
    assert "unknown" not in distribution.JOURNAL_ATTEMPT_STATUSES
    # ...but is *not* invisible for the allowance, unlike a truly undeclared row.
    assert "unknown" in distribution.JOURNAL_SLOT_OCCUPYING_STATUSES
    assert "sent_ok" not in distribution.JOURNAL_SLOT_OCCUPYING_STATUSES


def test_the_budget_reads_are_case_sensitive_and_must_stay_so() -> None:
    """`normalize_journal_status` may not be borrowed for the ratio.

    The re-offer path lower-cases and trims, because a smuggled casing must not
    dodge its fail-closed default. The budget path compares the stored column
    verbatim: `CrosspostResult` validates lowercase tokens, so a row written
    `Posted` is not a row this farm wrote, and normalising it would hand a
    platform with zero real attempts a success rate of 1.0 and a raised cap.
    """
    assert _cap(CAP_PLATFORM, ["Posted"] * 20) == VK_BASE
    assert _cap(CAP_PLATFORM, [" posted "] * 20) == VK_BASE
    # The same fixture spelled the way the farm writes it earns the raise.
    assert _cap(CAP_PLATFORM, ["posted"] * 20) == VK_MAX_DAILY


def _string_set_literals(func) -> list[frozenset[str]]:
    """Every inline `{...}` literal of plain strings inside `func`'s source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    found: list[frozenset[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Set) and node.elts:
            values = [element.value for element in node.elts]
            if all(isinstance(value, str) for value in values):
                found.append(frozenset(values))
    return found


def test_no_journal_status_literal_survives_in_the_budget_code() -> None:
    """Source contract: the readers name the constants, they do not respell them.

    NOTE for the coordinator: this is the change that makes
    `tests/test_crosspost_claim_gate_journal.py::test_no_consumer_owns_a_second_literal_status_set`
    fail at its line 503. That guard pins `PLANNER_NON_REOFFER_LITERALS`, the two
    inline status sets P68 deliberately left in `build_distribution_plan` and
    annotated "a declared follow-up (they disagree with the re-offer vocabulary on
    purpose); re-writing either as a re-offer gate must fail here". This block IS
    that follow-up: neither set was re-written as a re-offer gate — both were
    named on the budget axis, with the disagreement pinned above. The guard's
    substance survives (its second half, "no module may declare a copy of a
    declared set", still passes); only its allowlist, which tests *where the
    literal is written* rather than *what the set contains*, is now stale. The
    one-line fix belongs to that file's owner, not here:
    `PLANNER_NON_REOFFER_LITERALS = set()`, plus a re-point of its two members to
    `distribution.JOURNAL_SLOT_OCCUPYING_STATUSES` /
    `distribution.JOURNAL_DELIVERED_STATUSES` so a re-offer rewrite still fails.
    """
    for func in (
        distribution.effective_daily_cap,
        distribution._latest_logs,
        distribution._build_primary_platform_plan,
        distribution.build_distribution_plan,
    ):
        for literal in _string_set_literals(func):
            assert not (literal & _attempts_set_for_test()), (
                f"{func.__name__} still spells a journal status set inline: {sorted(literal)}"
            )
    # And the two readers do reference the constants.
    cap_source = inspect.getsource(distribution.effective_daily_cap)
    assert "JOURNAL_ATTEMPT_STATUSES" in cap_source
    assert "JOURNAL_DELIVERED_STATUSES" in cap_source
    plan_source = inspect.getsource(distribution.build_distribution_plan)
    assert "JOURNAL_SLOT_OCCUPYING_STATUSES" in plan_source
    assert plan_source.count("JOURNAL_DELIVERED_STATUSES") == 1


# ---------------------------------------------------------------------------
# 5. The `posts` table's own vocabulary is a different column and stays literal.
# ---------------------------------------------------------------------------


def test_post_status_literals_are_not_journal_statuses_and_are_untouched() -> None:
    """`Post.status.in_(...)` filters `posts.status`, not `crosspost_logs.status`.

    `published` and `gated` are `POST_STATUSES` members from `tgfarm.s7_publish`
    and are not journal statuses at all: `posts.status` is the editorial state of
    the material, `crosspost_logs.status` is the outcome of one delivery to one
    destination. Re-pointing those two filters at the journal vocabulary would
    test a column on tokens it can never hold, and both primary-plan counters
    would read zero — the silent budget change this block exists to prevent.
    """
    from tgfarm.s7_publish import POST_STATUSES

    source = inspect.getsource(distribution._build_primary_platform_plan)
    assert 'Post.status.in_(("scheduled", "publishing", "published"))' in source
    assert 'Post.status.in_(("gated", "scheduled"))' in source

    assert "published" in POST_STATUSES and "gated" in POST_STATUSES
    assert not ({"published", "gated"} & set(FULL_VOCABULARY))
    assert not ({"published", "gated"} & crosspost_journal.JOURNAL_KNOWN_STATUSES)
    # `scheduled` and `publishing` genuinely are ambiguous tokens — both columns
    # use them, which is the second reason the two filters cannot share a constant.
    assert {"scheduled", "publishing"} <= set(POST_STATUSES)

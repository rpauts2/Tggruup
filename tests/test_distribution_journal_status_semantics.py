"""Pinned re-offer semantics for the crosspost journal.

`build_distribution_plan` reads the newest `crosspost_logs` row for a
(post, platform) pair and makes exactly one of two decisions about it: keep the
pair out of the queue, or offer it for delivery again. Getting that mapping wrong
in either direction is expensive — suppressing is a stalled distribution, while
re-offering a row that already means "the destination has it" is a duplicate
publication on a real account.

The harness below drives the planner end to end against the shared test database
and neutralises every *platform-level* gate that is not the subject of these
tests (enablement switch, daily cap, publication gap, editorial score, native
format) so that the single remaining variable is the journal row's `status`,
`retry_after_at` and `attempt`.

Timestamps are taken from the wall clock once per test rather than being hard
coded, because the session shares one SQLite database with every other test file:
residual `crosspost_logs`/`posts` rows carry their real insertion time, and a
fixed clock that sits *behind* those rows would flip the planner's own
freshness/gap gates and make this file order-dependent. Every value the test
asserts about is derived from the same captured instant, so nothing here reads
the clock twice.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import typing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete

from tgfarm import distribution, platforms
from tgfarm.core.schemas import CrosspostResult
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Channel, CrosspostLog, Post
from tgfarm.db.session import session_scope
from tgfarm.s9_growth.adapters import ADAPTERS, MAX_AUTOMATIC_CROSSPOST_ATTEMPTS

# A destination that is in PLATFORM_RULES, has an adapter, and needs neither a
# video nor a primary image, so a text-only candidate is not blocked by media.
TARGET_PLATFORM = "bluesky"
# The channel the material came from; the planner skips a candidate whose own
# channel platform equals the destination.
SOURCE_PLATFORM = "telegram"


class _CountingAdapter:
    """A fake destination adapter that records how often it is interrogated."""

    platform = TARGET_PLATFORM

    def __init__(self, harness: _JournalHarness) -> None:
        self._harness = harness

    def configured(self) -> bool:
        self._harness.configured_calls += 1
        return self._harness.adapter_configured


class _JournalHarness:
    adapter_configured: bool
    configured_calls: int

    def __init__(self, monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
        self.now = now
        self.adapter_configured = True
        self.configured_calls = 0
        self.public_ids: list[str] = []
        self.channel_ids: list[str] = []
        self._seed_counter = 0

        monkeypatch.setitem(ADAPTERS, TARGET_PLATFORM, _CountingAdapter(self))
        # Only the target destination is switched on; the rest of the catalog is
        # irrelevant here and keeping it off makes each plan cheap.
        monkeypatch.setattr(platforms, "is_platform_enabled", lambda pid: pid == TARGET_PLATFORM)
        # Platform-level gates that must not decide these tests. `min_gap_hours`
        # goes to zero because another test file may have journalled a send for
        # this same destination moments ago, which would otherwise park the
        # destination on a pacing pause.
        monkeypatch.setitem(
            distribution.PLATFORM_RULES,
            TARGET_PLATFORM,
            {**distribution.PLATFORM_RULES[TARGET_PLATFORM], "min_gap_hours": 0},
        )
        monkeypatch.setattr(distribution, "effective_daily_cap", lambda _pid, _rows: 1_000_000)
        monkeypatch.setattr(
            distribution,
            "score_post_for_platform",
            lambda *_args, **_kwargs: {
                "eligible": True,
                "score": 0.9,
                "reason": "test editorial score",
                "topic": "тест",
            },
        )
        monkeypatch.setattr(
            distribution,
            "choose_post_native_format",
            lambda *_args, **_kwargs: {
                "eligible": True,
                "format": "text",
                "label": "Текст",
                "media_kind": None,
                "render_on_demand": False,
            },
        )
        self._monkeypatch = monkeypatch

    # -- seeding ---------------------------------------------------------------

    def journal(
        self,
        status: str | None,
        *,
        retry_after_at: datetime | None = None,
        attempt: int = 1,
        created_ago: timedelta = timedelta(days=1),
    ) -> str:
        """Seed one published candidate plus the newest journal row for the pair."""
        self._seed_counter += 1
        tag = f"jr{uuid4().hex[:12]}{self._seed_counter}"
        public_id = f"{tag}p"[:32]
        channel_id = f"{tag}c"[:64]
        # Old enough to clear the crosspost delay, young enough to stay inside
        # the candidate age window passed to the planner.
        published_at = self.now - timedelta(hours=6)
        with session_scope() as session:
            session.add(
                Channel(
                    id=channel_id,
                    platform=SOURCE_PLATFORM,
                    title="журнал тест",
                    enabled=True,
                )
            )
            session.add(
                Post(
                    public_id=public_id,
                    channel_id=channel_id,
                    status="published",
                    post_format="news",
                    generation_provider="offline",
                    hook="Журнал: тестовый заголовок",
                    body="Тело тестовой публикации для проверки журнала кросспоста.",
                    published_at=published_at,
                )
            )
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=TARGET_PLATFORM,
                    status=status,
                    attempt=attempt,
                    retry_after_at=retry_after_at,
                    created_at=self.now - created_ago,
                )
            )
        self.public_ids.append(public_id)
        self.channel_ids.append(channel_id)
        return public_id

    # -- running ---------------------------------------------------------------

    def plan(self, *, mode: str = "dry") -> dict:
        settings = get_settings().model_copy(update={"mode": mode})
        self._monkeypatch.setattr(distribution, "get_settings", lambda: settings)
        self.configured_calls = 0
        result = distribution.build_distribution_plan(
            now=self.now,
            max_age_days=14,
            max_selected_per_platform=5,
        )
        entry = next(item for item in result["platforms"] if item["platform"] == TARGET_PLATFORM)
        return {"entry": entry, "mode": result["mode"], "live": result["live"]}

    def reoffered(self, public_id: str, *, mode: str = "dry") -> bool:
        """Was the journalled pair offered for delivery again?"""
        return public_id in {row["post_public_id"] for row in self.plan(mode=mode)["entry"]["selected"]}

    def cooldown(self, *, minutes: int) -> datetime:
        """A `retry_after_at` inside or past the moment the plan is asked about."""
        return self.now + timedelta(minutes=minutes)

    def cleanup(self) -> None:
        with session_scope() as session:
            if self.public_ids:
                session.execute(delete(CrosspostLog).where(
                    CrosspostLog.post_public_id.in_(self.public_ids)
                ))
                session.execute(delete(Post).where(Post.public_id.in_(self.public_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))


@pytest.fixture()
def journal_env(monkeypatch: pytest.MonkeyPatch) -> _JournalHarness:
    harness = _JournalHarness(monkeypatch, datetime.now(UTC))
    try:
        yield harness
    finally:
        harness.cleanup()


# ---------------------------------------------------------------------------
# 1. The statuses that keep a journalled pair out of the queue.
# ---------------------------------------------------------------------------

# (status, why the destination must not be offered this material again)
SUPPRESSED_CASES = [
    ("posted", "the platform accepted it; a second send is a duplicate"),
    ("scheduled", "the platform holds it in its own queue and will publish it"),
    ("unknown", "the request may have been accepted before the answer was lost"),
    ("publishing", "a durable claim is in flight; the send may still be live"),
    ("permanent", "the destination refused this exact content"),
]


@pytest.mark.parametrize("status,rationale", SUPPRESSED_CASES, ids=[c[0] for c in SUPPRESSED_CASES])
def test_indefinitely_suppressed_statuses_never_reopen(
    journal_env: _JournalHarness, status: str, rationale: str
) -> None:
    assert status in distribution.JOURNAL_SUPPRESSED_STATUSES, rationale
    public_id = journal_env.journal(status)
    assert journal_env.reoffered(public_id) is False


@pytest.mark.parametrize("status", ["posted", "scheduled", "unknown", "publishing", "permanent"])
def test_suppressed_status_stays_suppressed_in_live_mode(journal_env: _JournalHarness, status: str) -> None:
    """Going LIVE must not turn an acknowledged delivery back into fresh material."""
    public_id = journal_env.journal(status)
    assert journal_env.reoffered(public_id, mode="live") is False


def test_suppressed_status_ignores_a_past_cooldown_and_the_attempt_count(
    journal_env: _JournalHarness,
) -> None:
    """These rows are not time-bounded: `retry_after_at` passing changes nothing."""
    public_id = journal_env.journal(
        "posted",
        retry_after_at=journal_env.cooldown(minutes=-60),
        attempt=1,
    )
    assert journal_env.reoffered(public_id) is False


# ---------------------------------------------------------------------------
# 2. `failed` — re-opens after the cooldown, never past the attempt ceiling.
# ---------------------------------------------------------------------------

def test_failed_reopens_once_the_cooldown_has_elapsed(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal(
        "failed",
        retry_after_at=journal_env.cooldown(minutes=-1),
        attempt=2,
    )
    assert journal_env.reoffered(public_id) is True


def test_failed_stays_suppressed_while_the_cooldown_is_in_the_future(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal(
        "failed",
        retry_after_at=journal_env.cooldown(minutes=+30),
        attempt=1,
    )
    assert journal_env.reoffered(public_id) is False


def test_failed_without_a_promised_retry_moment_reopens(journal_env: _JournalHarness) -> None:
    """`retry_after_at` is None when nothing was measured, and None is not "never"."""
    public_id = journal_env.journal("failed", retry_after_at=None, attempt=1)
    assert journal_env.reoffered(public_id) is True


def test_failed_reopens_on_the_last_automatic_attempt(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal(
        "failed",
        retry_after_at=journal_env.cooldown(minutes=-1),
        attempt=MAX_AUTOMATIC_CROSSPOST_ATTEMPTS - 1,
    )
    assert journal_env.reoffered(public_id) is True


def test_failed_at_the_attempt_ceiling_never_reopens(journal_env: _JournalHarness) -> None:
    """The ceiling the planner uses is the one the publisher and the UI use."""
    public_id = journal_env.journal(
        "failed",
        retry_after_at=journal_env.cooldown(minutes=-1),
        attempt=MAX_AUTOMATIC_CROSSPOST_ATTEMPTS,
    )
    assert journal_env.reoffered(public_id) is False


def test_failed_past_the_attempt_ceiling_never_reopens_even_without_a_cooldown(
    journal_env: _JournalHarness,
) -> None:
    public_id = journal_env.journal(
        "failed",
        retry_after_at=None,
        attempt=MAX_AUTOMATIC_CROSSPOST_ATTEMPTS + 3,
    )
    assert journal_env.reoffered(public_id) is False


# ---------------------------------------------------------------------------
# 3. `skipped` — decided by the timestamp alone.
# ---------------------------------------------------------------------------

def test_skipped_stays_suppressed_while_its_retry_moment_is_in_the_future(
    journal_env: _JournalHarness,
) -> None:
    """Reachable in production: Pikabu journals skipped + a 6 hour cooldown."""
    public_id = journal_env.journal("skipped", retry_after_at=journal_env.cooldown(minutes=+45))
    assert journal_env.reoffered(public_id) is False


def test_skipped_reopens_once_its_retry_moment_has_passed(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal("skipped", retry_after_at=journal_env.cooldown(minutes=-45))
    assert journal_env.reoffered(public_id) is True


def test_skipped_without_a_measured_retry_moment_reopens(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal("skipped", retry_after_at=None)
    assert journal_env.reoffered(public_id) is True


def test_skipped_decision_flips_on_the_timestamp_with_the_adapter_untouched(
    journal_env: _JournalHarness,
) -> None:
    """The only thing that changes below is `retry_after_at`.

    Both runs use one adapter object that keeps reporting `configured() is True`,
    so whatever the plan decided, it decided from the journal row's timestamp and
    not from a second look at the destination's configuration.
    """
    suppressed = journal_env.journal("skipped", retry_after_at=journal_env.cooldown(minutes=+90))
    assert journal_env.reoffered(suppressed) is False
    assert journal_env.adapter_configured is True

    reopened = journal_env.journal("skipped", retry_after_at=journal_env.cooldown(minutes=-90))
    assert journal_env.reoffered(reopened) is True
    assert journal_env.adapter_configured is True


def _compares_to_skipped(node: ast.If) -> bool:
    """True when this `if` guard compares something against the "skipped" token."""
    for walked in ast.walk(node.test):
        if not isinstance(walked, ast.Compare):
            continue
        for operand in walked.comparators:
            if isinstance(operand, ast.Constant) and operand.value == "skipped":
                return True
    return False


def test_journal_status_dispatch_never_reads_adapter_configuration_for_skipped() -> None:
    """A re-added `and configured` conjunct here would be a tautology, so no
    behavioural test can see it — this one reads the guard itself.

    `build_distribution_plan` reaches the `skipped` branch only from inside
    `can_prepare`, which already conjoins `configured`; asserting the condition
    does not mention adapter configuration is what keeps that invariant from
    quietly turning back into a "real" looking condition.
    """
    source = textwrap.dedent(inspect.getsource(distribution.build_distribution_plan))
    tree = ast.parse(source)
    guards = [
        node for node in ast.walk(tree) if isinstance(node, ast.If) and _compares_to_skipped(node)
    ]
    assert len(guards) == 1, "expected exactly one `skipped` re-offer guard"
    condition = ast.get_source_segment(source, guards[0].test) or ""
    assert "configured" not in condition, condition


def test_plan_reads_the_destination_configuration_once_per_platform(journal_env: _JournalHarness) -> None:
    """The second guard against re-adding the conjunct as a fresh call.

    Configuration is read once per platform, at the gate that opens the
    candidate loop. Any number above one means the journal decision started
    interrogating the adapter again.
    """
    public_id = journal_env.journal("skipped", retry_after_at=journal_env.cooldown(minutes=-1))
    assert journal_env.reoffered(public_id) is True
    assert journal_env.configured_calls == 1


def test_unconfigured_destination_is_blocked_before_the_journal_is_consulted(
    journal_env: _JournalHarness,
) -> None:
    """Why the removed conjunct was dead: the `skipped` branch is unreachable
    without `configured`, so a skipped row with an elapsed cooldown still reopens
    only when the destination is configured, and the block otherwise comes from
    the platform gate (a different reason string), never from the journal rule.
    """
    journal_env.adapter_configured = False
    public_id = journal_env.journal("skipped", retry_after_at=journal_env.cooldown(minutes=-1))
    entry = journal_env.plan()["entry"]
    assert public_id not in {row["post_public_id"] for row in entry["selected"]}
    assert entry["configured"] is False
    assert entry["reason"] == "Аккаунт или API-доступ не настроен."


# ---------------------------------------------------------------------------
# 4. `dry_run` and `retry_authorized` — the statuses that re-open.
# ---------------------------------------------------------------------------

def test_dry_run_stays_suppressed_while_the_farm_is_not_live(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal("dry_run")
    assert journal_env.reoffered(public_id, mode="dry") is False


def test_dry_run_reopens_when_the_operator_switches_to_live(journal_env: _JournalHarness) -> None:
    """A dry-run row was never sent anywhere, so LIVE must offer the pair again."""
    public_id = journal_env.journal("dry_run")
    assert journal_env.reoffered(public_id, mode="live") is True


def test_retry_authorized_reopens_immediately(journal_env: _JournalHarness) -> None:
    """`api._reconcile_crosspost_outcome` journals this status when the operator
    checked the destination and authorized a retry. Re-offering *is* the action
    that row asks for, so the fail-closed default must not swallow it.
    """
    public_id = journal_env.journal("retry_authorized", retry_after_at=None, attempt=9)
    assert journal_env.reoffered(public_id, mode="live") is True


# ---------------------------------------------------------------------------
# 5. The fail-closed default for a status this planner does not know.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    ["sent", "queued", "pending", "posted_again", "skipped-permanently", "", "POSTED_OK"],
)
def test_unrecognised_journal_status_fails_closed(journal_env: _JournalHarness, status: str) -> None:
    public_id = journal_env.journal(status, retry_after_at=None)
    entry = journal_env.plan()["entry"]
    assert public_id not in {row["post_public_id"] for row in entry["selected"]}
    assert entry["reason"] == distribution.JOURNAL_UNKNOWN_STATUS_REASON


def test_unrecognised_status_says_so_instead_of_blaming_an_empty_queue(
    journal_env: _JournalHarness,
) -> None:
    """The queue is empty because a row spoke an unknown status, not because no
    material was available; the reason must not lie about which it was."""
    public_id = journal_env.journal("mystery")
    entry = journal_env.plan()["entry"]
    assert entry["selected"] == []
    assert entry["eligible_count"] == 0
    assert entry["reason"] == distribution.JOURNAL_UNKNOWN_STATUS_REASON
    assert "свежих материалов" not in entry["reason"]
    assert journal_env.reoffered(public_id, mode="live") is False


def test_live_mode_does_not_unlock_an_unrecognised_status(journal_env: _JournalHarness) -> None:
    public_id = journal_env.journal("sent")
    assert journal_env.reoffered(public_id, mode="live") is False


# ---------------------------------------------------------------------------
# 6. The declared vocabulary is the writers' vocabulary.
# ---------------------------------------------------------------------------

# Statuses written straight into `crosspost_logs`, bypassing `CrosspostResult`:
# `adapters.claim_crosspost` (before any network call) and
# `api._reconcile_crosspost_outcome` (operator decisions).
DIRECTLY_WRITTEN_STATUSES = frozenset({"publishing", "retry_authorized"})


def test_planner_covers_every_status_a_writer_can_put_in_the_journal() -> None:
    """Adding a status to the journal vocabulary without deciding its re-offer
    semantics must fail here, not silently re-offer or silently suppress."""
    produced_by_adapters = frozenset(
        typing.get_args(CrosspostResult.model_fields["status"].annotation)
    )
    planner_handled = (
        distribution.JOURNAL_SUPPRESSED_STATUSES
        | distribution.JOURNAL_TIME_BOUNDED_STATUSES
        | distribution.JOURNAL_REOPEN_STATUSES
    )
    assert produced_by_adapters | DIRECTLY_WRITTEN_STATUSES == planner_handled
    # The three groups must stay mutually exclusive: a status cannot be both
    # "suppressed forever" and "re-open" depending on which line runs first.
    assert not distribution.JOURNAL_SUPPRESSED_STATUSES & distribution.JOURNAL_TIME_BOUNDED_STATUSES
    assert not distribution.JOURNAL_SUPPRESSED_STATUSES & distribution.JOURNAL_REOPEN_STATUSES
    assert not distribution.JOURNAL_TIME_BOUNDED_STATUSES & distribution.JOURNAL_REOPEN_STATUSES


def test_status_helper_normalises_case_and_blank(journal_env: _JournalHarness) -> None:
    """A row cannot sneak past the fail-closed default by casing or padding."""
    assert distribution._journal_status_of(SimpleStatus("Posted ")) == "posted"  # noqa: SLF001
    assert distribution._journal_status_of(SimpleStatus(None)) == ""  # noqa: SLF001
    public_id = journal_env.journal("Posted")
    assert journal_env.reoffered(public_id) is False


class SimpleStatus:
    def __init__(self, status: str | None) -> None:
        self.status = status

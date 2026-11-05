"""The scheduled rules read for destinations the farm would publish to.

`tgfarm/maintenance.py::watch_destination_rules` is the missing continuous half
of the CommunityPolicy contract: the verdict, the `rules_hash` change detection,
the history endpoint and the panel card all existed, but every writer of a
snapshot sat behind a per-platform switch, so `community_policy_snapshots` held
0 rows in the live database and nothing accumulated between operator clicks.

These tests pin the honesty of that step, not the readers it reuses (the readers
have their own files): a disabled platform gets a name and no numbers, an unread
destination never produces an invented count, and both work limits — per-pass and
per-run — are stated.

Every row here is written under the `p69watch` prefix and removed by the autouse
teardown, which deletes *by platform-agnostic target_id pattern* rather than by
id, because the shared session database gives a test no way to know the ids it
just created.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from tgfarm import community_drafts, community_targets, platforms
from tgfarm.community_policy import (
    assess_policy,
    policy_from_candidate,
    policy_from_reddit,
)
from tgfarm.db.models import CommunityPolicySnapshot as Snapshot
from tgfarm.db.session import session_scope
from tgfarm.maintenance import (
    RULES_WATCH_BUDGET_MAX,
    RULES_WATCH_PLATFORMS,
    run_maintenance,
    watch_destination_rules,
)
from tgfarm.platform_capability import capability_of
from tgfarm.s9_growth import reddit_scout

PREFIX = "p69watch"
PIKABU_RULE_TEXT = "Публикуйте только материалы по Python."
PIKABU_OLD_RULE_TEXT = "Старые правила: только Python."
REDDIT_RULE = {"short_name": "Rule 1", "description": "Stay on topic."}
REDDIT_CHANGED_RULE = {"short_name": "Rule 1", "description": "Only Python releases."}
COMMUNITY_URL = "https://pikabu.ru/community/"
#: The watched platforms that answer to an operator switch. `vc_ru` is not one of
#: them: it has no `enabled.*` key at all, and the step watches it because the farm
#: fills its editor (`maintenance._rules_watch_switch`), which is pinned in
#: `tests/test_maintenance_vc_rules_watch.py`.
SWITCHED_WATCH_PLATFORMS = tuple(
    name for name in RULES_WATCH_PLATFORMS if capability_of(name) != "draft_only"
)


# --------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _drop_watched_rows():
    """Leave the shared database exactly as we found it."""
    yield
    with session_scope() as session:
        session.execute(delete(Snapshot).where(Snapshot.target_id.like(f"{PREFIX}%")))


@pytest.fixture()
def enable(monkeypatch):
    """Switch platforms on for one test, without editing `config/platforms.yaml`."""

    def _enable(*names: str) -> None:
        wanted = {str(name) for name in names}
        monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name in wanted)

    return _enable


@pytest.fixture()
def fake_reddit(monkeypatch):
    """Replace only the network half of the existing Reddit preflight reader."""
    calls: list[str] = []
    answers: dict[str, list[dict[str, str]]] = {}
    monkeypatch.setattr(reddit_scout.ADAPTERS["reddit"], "configured", lambda: True)

    async def inspect(subreddit: str) -> dict:
        calls.append(subreddit)
        if subreddit not in answers:
            raise reddit_scout.RedditScoutError(f"no fake rules queued for {subreddit}")
        return _reddit_payload(subreddit, answers[subreddit])

    monkeypatch.setattr(reddit_scout, "inspect_community", inspect)

    def queue(subreddit: str, rules: list[dict[str, str]]) -> None:
        answers[subreddit] = rules

    return calls, queue


@pytest.fixture()
def fake_pikabu(monkeypatch):
    """Replace only the page fetch inside the existing Pikabu reader."""
    calls: list[str] = []
    answers: dict[str, str] = {}

    async def fetch(url: str) -> dict:
        calls.append(url)
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="Общие правила Пикабу.")
        if url not in answers:
            raise AssertionError(f"the watch asked for an unqueued page: {url}")
        return _page(
            url, description="Сообщество о Python.", chunks=["Правила сообщества", answers[url]]
        )

    monkeypatch.setattr(community_targets, "_fetch", fetch)

    def queue(slug: str, rule: str) -> None:
        answers[f"{COMMUNITY_URL}{slug}"] = rule

    return calls, queue


# ---------------------------------------------------------------------- helpers
def _page(url: str, *, text: str = "", chunks=None, description: str = "") -> dict:
    return {
        "url": url,
        "title": "",
        "description": description,
        "text": text,
        "chunks": list(chunks or []),
        "links": [],
    }


def _reddit_payload(subreddit: str, rules: list[dict[str, str]]) -> dict:
    return {
        "name": subreddit,
        "about": {"title": f"{subreddit} : r/{subreddit}", "public_description": "Talk."},
        "rules": rules,
        "requirements": {},
        "risk_flags": [],
    }


def _insert(policy, *, hours_ago: float) -> None:
    """Store one history row the contract built, with an age we control."""
    verdict = assess_policy(policy)
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    with session_scope() as session:
        session.add(
            Snapshot(
                platform=policy.platform,
                target_type=policy.target_type,
                target_id=policy.target_id,
                target_name=policy.name,
                rules_source=policy.rules_source,
                rules_hash=policy.rules_hash,
                rules_available=policy.rules_available,
                rules_excerpt=policy.rules_excerpt,
                verdict_status=str(verdict.get("status") or "unknown"),
                confidence=float(policy.confidence),
                requirements_json=list(policy.requirements),
                checked_at=moment,
                recorded_at=moment,
            )
        )


def _seed_reddit(subreddit: str, rules: list[dict[str, str]], *, hours_ago: float = 0.0) -> str:
    policy = policy_from_reddit(_reddit_payload(subreddit, rules), subreddit=subreddit)
    _insert(policy, hours_ago=hours_ago)
    return policy.rules_hash


def _seed_pikabu(slug: str, rule: str, *, hours_ago: float = 0.0) -> str:
    candidate = {
        "target_type": "community",
        "name": slug,
        "slug": slug,
        "url": f"{COMMUNITY_URL}{slug}",
        "description": "Сообщество о Python.",
        "rules": rule,
        "rules_available": True,
        "global_rules_available": True,
        "score": 40,
    }
    policy = policy_from_candidate(
        "pikabu", candidate, rules_source=community_targets.PIKABU_RULES
    )
    _insert(policy, hours_ago=hours_ago)
    return policy.rules_hash


def _rows(target_id: str) -> list[dict]:
    with session_scope() as session:
        stored = session.scalars(
            select(Snapshot).where(Snapshot.target_id == target_id).order_by(Snapshot.id)
        ).all()
        return [
            {
                "id": int(row.id),
                "platform": str(row.platform),
                "rules_hash": str(row.rules_hash),
                "verdict_status": str(row.verdict_status),
                "requirements": list(row.requirements_json or ()),
            }
            for row in stored
        ]


def _row_count(target_id: str) -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(Snapshot).where(Snapshot.target_id == target_id)
            ).scalar_one()
            or 0
        )


def _written_with_prefix() -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(Snapshot).where(Snapshot.target_id.like(f"{PREFIX}%"))
            ).scalar_one()
            or 0
        )


def _for_target(report: dict, target_id: str) -> dict | None:
    hits = [row for row in report["sample"] if row["target_id"] == target_id]
    return hits[0] if hits else None


# ------------------------------------------------------------------ empty state
def test_nothing_configured_to_watch_names_the_state_and_invents_nothing(enable):
    """Today's live shape: both community switches off, history empty.

    Absence is a named state, not a row, not an age and not a zero count of
    something that was never read (P34).
    """
    enable()  # nothing enabled, exactly like `config/platforms.yaml` for pikabu

    report = watch_destination_rules(only_target_ids=[f"{PREFIX}_nobody"])

    assert report["state"] == "nothing_to_watch"
    assert report["detail"] == "nothing configured to watch"
    assert report["attempted"] == 0
    assert report["checked"] == 0 and report["recorded"] == 0
    assert report["destinations"] == 0
    # An age that was not read stays None instead of becoming a fresh 0.0.
    assert report["oldest_checked_age_hours"] is None
    assert report["sample"] == []
    assert report["failures"] == []
    assert report["blocked"] == []
    assert report["stopped_by"] is None
    assert _written_with_prefix() == 0


def test_a_disabled_destination_gets_a_name_and_no_numbers(enable, fake_reddit, fake_pikabu):
    """Off means off: no read attempted, no row written, no invented count.

    Scoped to the destination this test seeded: the shared history can name a vc.ru
    topic written by another file, and `vc_ru` is watched whatever the switches say,
    so an unscoped pass would ask that page. Nothing here must reach a page.
    """
    subreddit = "p69watch_reddit_off"
    _seed_reddit(subreddit, [REDDIT_RULE], hours_ago=5)
    enable()  # both community platforms stay off, as in the live configuration

    report = watch_destination_rules(only_target_ids=[subreddit])

    calls, _queue = fake_reddit
    pikabu_calls, _pqueue = fake_pikabu
    assert calls == [] and pikabu_calls == []
    for platform_name in SWITCHED_WATCH_PLATFORMS:
        entry = report["platforms"][platform_name]
        assert entry["state"] == "disabled"
        assert entry["switch"] == f"enabled.{platform_name}"
        # A platform nobody read carries no counters at all.
        assert "destinations" not in entry
        assert "checked" not in entry
        assert "recorded" not in entry
    assert _row_count(subreddit) == 1
    assert report["oldest_checked_age_hours"] is None
    assert report["destinations"] == 0
    assert report["state"] == "nothing_to_watch"


def test_enabled_platform_with_an_empty_history_says_it_has_nothing_to_watch(
    enable, fake_reddit
):
    """Switched on, readable, and no destination named yet: a state, not a hunt."""
    calls, _queue = fake_reddit
    enable("reddit")

    # Scoped to a destination that does not exist: the branch under test is
    # "enabled, readable, nothing named", and that must not depend on what the
    # rest of the suite left in the shared history.
    report = watch_destination_rules(only_target_ids=[f"{PREFIX}_none"])

    assert calls == []
    entry = report["platforms"]["reddit"]
    assert entry["state"] == "enabled_no_known_destination"
    assert entry["destinations"] == 0
    assert "checked" not in entry
    assert report["state"] == "nothing_to_watch"
    assert report["detail"] == "nothing configured to watch"
    assert report["destinations"] == 0
    assert report["oldest_checked_age_hours"] is None
    assert _written_with_prefix() == 0


def test_reddit_without_oauth_is_named_instead_of_attempted(enable, fake_reddit, monkeypatch):
    """An enabled platform with no credentials is a state, not N failed reads."""
    subreddit = "p69watch_reddit_noauth"
    _seed_reddit(subreddit, [REDDIT_RULE], hours_ago=3)
    calls, _queue = fake_reddit
    monkeypatch.setattr(reddit_scout.ADAPTERS["reddit"], "configured", lambda: False)
    enable("reddit")

    report = watch_destination_rules(only_target_ids=[subreddit])

    assert calls == [], "an unconfigured authorization must not be knocked on"
    assert report["platforms"]["reddit"]["state"] == "reddit_oauth_not_configured"
    assert report["platforms"]["reddit"]["destinations"] == 1
    assert report["checked"] == 0 and report["failed"] == 0
    assert report["state"] == "nothing_to_watch"
    assert "nothing configured to watch" in report["detail"]
    assert "reddit_oauth_not_configured" in report["detail"]
    assert report["blocked"] == ["reddit:reddit_oauth_not_configured"]
    assert _row_count(subreddit) == 1


def test_platform_without_a_reader_is_named_not_scratched(enable):
    """A history row on a platform with no destination-only reader: say so."""
    target_id = f"{PREFIX}_vc_topic"
    with session_scope() as session:
        session.add(
            Snapshot(
                platform="p69noreader",
                target_type="topic",
                target_id=target_id,
                target_name="p69 topic",
                rules_hash="deadbeef",
                rules_available=True,
                rules_excerpt="Тема про релизы.",
                verdict_status="ok",
                confidence=0.0,
                requirements_json=["Тема про релизы."],
                checked_at=datetime.now(UTC),
                recorded_at=datetime.now(UTC),
            )
        )
    enable("p69noreader")

    # Scoped, like the rest of this file: since `vc_ru` joined the watch set, an
    # unscoped pass would also take whatever topic another test file left in the
    # shared history — and ask a page no fake has queued for it.
    report = watch_destination_rules(only_target_ids=[target_id])

    entry = report["platforms"]["p69noreader"]
    assert entry["state"] == "no_rules_reader"
    assert "no rules reader" in entry["reason"]
    assert entry["destinations"] == 1
    assert report["state"] == "nothing_to_watch"
    assert report["checked"] == 0
    assert _row_count(target_id) == 1


# ---------------------------------------------------------------- change detect
def test_fresh_snapshot_is_not_written_twice(enable, fake_reddit):
    """The step reuses `record_policy_snapshot`, so its collapse window applies."""
    subreddit = "p69watch_reddit_fresh"
    previous_hash = _seed_reddit(subreddit, [REDDIT_RULE])
    calls, queue = fake_reddit
    queue(subreddit, [REDDIT_RULE])
    enable("reddit")

    report = watch_destination_rules(only_target_ids=[subreddit])

    assert calls == [subreddit]
    assert report["checked"] == 1
    assert report["recorded"] == 0
    assert report["collapsed"] == 1
    assert report["changed"] == 0
    assert _row_count(subreddit) == 1
    mine = _for_target(report, subreddit)
    assert mine is not None
    assert mine["previous_hash"] == previous_hash
    assert mine["rules_hash"] == previous_hash
    assert mine["changed"] is False and mine["recorded"] is False
    assert report["platforms"]["reddit"] == {
        "state": "watched",
        "destinations": 1,
        "checked": 1,
        "recorded": 0,
        "changed": 0,
        "failed": 0,
    }


def test_changed_rules_write_a_row_with_a_different_hash(enable, fake_reddit):
    subreddit = "p69watch_reddit_change"
    previous_hash = _seed_reddit(subreddit, [REDDIT_RULE], hours_ago=2)
    _calls, queue = fake_reddit
    queue(subreddit, [REDDIT_RULE, REDDIT_CHANGED_RULE])
    enable("reddit")

    report = watch_destination_rules(only_target_ids=[subreddit])

    assert report["recorded"] == 1
    assert report["changed"] == 1
    assert report["collapsed"] == 0
    rows = _rows(subreddit)
    assert len(rows) == 2
    assert rows[-1]["rules_hash"] != previous_hash
    assert rows[-1]["rules_hash"] == _for_target(report, subreddit)["rules_hash"]
    # The hash is a digest, so no rule text hides in it, and no rule text is a
    # field of the report either: the report carries hashes and counts only.
    assert len(rows[-1]["rules_hash"]) == 64
    assert hashlib.sha256(rows[-1]["rules_hash"].encode()).hexdigest() != rows[-1]["rules_hash"]
    assert "Публикуйте" not in str(report)


def test_pikabu_destination_is_re_read_through_the_existing_reader(enable, fake_pikabu):
    slug = "p69watch_pikabu_a"
    _seed_pikabu(slug, PIKABU_OLD_RULE_TEXT)
    calls, queue = fake_pikabu
    queue(slug, PIKABU_RULE_TEXT)
    enable("pikabu")

    report = watch_destination_rules(only_target_ids=[slug])

    assert calls == [community_targets.PIKABU_RULES, f"{COMMUNITY_URL}{slug}"]
    assert report["recorded"] == 1 and report["changed"] == 1
    rows = _rows(slug)
    assert len(rows) == 2
    assert rows[-1]["verdict_status"] == "ok"
    assert PIKABU_RULE_TEXT in " ".join(rows[-1]["requirements"])
    assert rows[-1]["platform"] == "pikabu"


# -------------------------------------------------------------------- failures
def test_unreadable_destination_is_counted_and_the_cycle_survives(enable, fake_reddit, monkeypatch):
    subreddit = "p69watch_reddit_boom"
    _seed_reddit(subreddit, [REDDIT_RULE])
    # Nothing queued for it: the fake reader raises the way the real one does.
    enable("reddit")
    monkeypatch.setattr("tgfarm.maintenance.checkpoint", lambda *_a, **_k: True)

    report = watch_destination_rules(only_target_ids=[subreddit])

    assert report["failed"] == 1
    assert report["checked"] == 0 and report["recorded"] == 0 and report["changed"] == 0
    assert report["state"] == "watched"
    assert _for_target(report, subreddit) is None
    failure = report["failures"][0]
    assert failure["platform"] == "reddit"
    assert failure["target_id"] == subreddit
    assert failure["reason"].startswith("RedditScoutError:")
    assert len(failure["reason"]) <= 160
    assert report["platforms"]["reddit"]["failed"] == 1
    assert _row_count(subreddit) == 1, "a failed read must not write history"

    stage = run_maintenance()
    assert stage["rules_watch"] == 0, "a failed read must not become a failed stage"


def test_a_non_canonical_pikabu_slug_is_a_reason_not_a_request(enable, fake_pikabu):
    """The stored destination id is data: it never becomes an arbitrary URL."""
    slug = f"{PREFIX} bad slug"
    _seed_pikabu(slug, PIKABU_RULE_TEXT)
    calls, queue = fake_pikabu
    queue(slug, PIKABU_RULE_TEXT)
    enable("pikabu")

    report = watch_destination_rules(only_target_ids=[slug])

    assert calls == [], "a malformed stored id must not reach the platform"
    assert report["failed"] == 1
    assert report["recorded"] == 0
    assert report["failures"][0]["reason"].startswith("ValueError:")
    assert _row_count(slug) == 1


def test_the_watch_does_not_publish_anything(enable, fake_reddit, fake_pikabu, monkeypatch):
    """Reusing a publish path's *reader* must not drag in its writer."""
    sent: list[str] = []

    async def never_publish(*_a, **_k):
        sent.append("publish")
        raise AssertionError("the rules watch must not publish")

    monkeypatch.setattr(reddit_scout.ADAPTERS["reddit"], "publish", never_publish)
    monkeypatch.setattr(community_drafts, "prepare_community_draft", never_publish)
    monkeypatch.setattr(community_drafts, "publish_pikabu_scheduled", never_publish)
    monkeypatch.setattr(community_targets, "recommend_community_target", never_publish)

    reddit = "p69watch_reddit_safe"
    slug = "p69watch_pikabu_safe"
    _seed_reddit(reddit, [REDDIT_RULE])
    _seed_pikabu(slug, PIKABU_RULE_TEXT)
    _calls, queue = fake_reddit
    queue(reddit, [REDDIT_RULE])
    _p_calls, p_queue = fake_pikabu
    p_queue(slug, PIKABU_RULE_TEXT)
    enable("reddit", "pikabu")

    report = watch_destination_rules(only_target_ids=[reddit, slug])

    assert report["checked"] == 2
    assert sent == []


# ------------------------------------------------------------------ bounded work
def test_limit_is_respected_and_the_remainder_is_stated(enable, fake_reddit):
    """Most overdue first, at most `limit` reads, the rest named as left over."""
    subreddits = [f"{PREFIX}_r_{index}" for index in range(3)]
    for index, subreddit in enumerate(subreddits):
        _seed_reddit(subreddit, [REDDIT_RULE], hours_ago=10 - index)
    calls, queue = fake_reddit
    for subreddit in subreddits:
        queue(subreddit, [REDDIT_RULE])
    enable("reddit")

    report = watch_destination_rules(limit=1, only_target_ids=subreddits)

    assert calls == [subreddits[0]], "the pass must take the most overdue destination"
    assert report["attempted"] == 1
    assert report["checked"] == 1
    assert report["destinations"] == 3
    assert report["remaining"] == 2
    assert report["state"] == "partial"
    assert f"{report['remaining']} left" in report["detail"]
    # The budget is a clamp, not a suggestion: a wild request stays bounded.
    clamped = watch_destination_rules(limit=10_000, only_target_ids=subreddits)
    assert clamped["limit"] == RULES_WATCH_BUDGET_MAX
    assert clamped["requested_limit"] == 10_000
    assert clamped["checked"] == 3
    assert clamped["remaining"] == 0


def test_time_budget_stops_starting_new_reads(enable, fake_reddit, monkeypatch):
    subreddit_a = f"{PREFIX}_slow_a"
    subreddit_b = f"{PREFIX}_slow_b"
    _seed_reddit(subreddit_a, [REDDIT_RULE], hours_ago=3)
    _seed_reddit(subreddit_b, [REDDIT_RULE], hours_ago=1)
    answers = {subreddit_a: [REDDIT_RULE], subreddit_b: [REDDIT_RULE]}
    started: list[str] = []

    async def slow_inspect(subreddit: str) -> dict:
        started.append(subreddit)
        await asyncio.sleep(1.2)
        return _reddit_payload(subreddit, answers[subreddit])

    monkeypatch.setattr(reddit_scout, "inspect_community", slow_inspect)
    enable("reddit")

    # `budget_seconds` is clamped to at least a second, so the first read (1.2 s)
    # is what pushes the pass over its own deadline.
    report = watch_destination_rules(budget_seconds=0.001, only_target_ids=[subreddit_a, subreddit_b])

    assert started == [subreddit_a]
    assert report["stopped_by"] == "deadline"
    assert report["skipped_by_deadline"] == 1
    assert report["remaining"] == 1
    assert report["checked"] == 1
    assert report["state"] == "partial"
    assert "time budget" in report["detail"]


def test_destination_count_and_age_are_measured_from_stored_rows(enable, fake_reddit):
    """Once something *is* configured to watch, the step reports its real age."""
    subreddit = "p69watch_reddit_age"
    _seed_reddit(subreddit, [REDDIT_RULE], hours_ago=7)
    _calls, queue = fake_reddit
    queue(subreddit, [REDDIT_RULE])
    enable("reddit")

    report = watch_destination_rules(only_target_ids=[subreddit])

    assert report["state"] == "watched"
    assert report["destinations"] == 1
    assert report["platforms"]["reddit"]["state"] == "watched"
    assert report["platforms"]["reddit"]["destinations"] == 1
    assert 6.5 < float(report["oldest_checked_age_hours"]) < 7.5


# ----------------------------------------------------------------------- wiring
def test_step_is_registered_in_the_maintenance_stage(enable, monkeypatch):
    """The live configuration: nothing enabled, so the stage reports zero writes."""
    monkeypatch.setattr("tgfarm.maintenance.checkpoint", lambda *_a, **_k: True)
    enable()

    report = run_maintenance()

    assert "rules_watch" in report
    assert report["rules_watch"] == 0
    assert report["rules_watch"] != -1, "the step must not fail the stage"


def test_registered_step_writes_history_for_an_enabled_destination(enable, fake_reddit, monkeypatch):
    """The same hook, reached through the cycle, returns the rows it added."""
    subreddit = "p69watch_reddit_cycle"
    _seed_reddit(subreddit, [REDDIT_RULE], hours_ago=2)
    _calls, queue = fake_reddit
    queue(subreddit, [REDDIT_RULE, REDDIT_CHANGED_RULE])
    enable("reddit")
    monkeypatch.setattr("tgfarm.maintenance.checkpoint", lambda *_a, **_k: True)

    report = run_maintenance()

    assert report["rules_watch"] == 1
    assert _row_count(subreddit) == 2

"""W29-C: the rules-history writer must be callable on its own, and freshness a state.

The live database measured 0 rows in ``community_policy_snapshots`` on 2026-11-05
while 2 009 maintenance passes reported ``ok``.  Every writer that existed was a
side effect of *some other action on that platform* (a selector run, a panel
click, a publish preflight), so nothing accumulated.  These tests pin the two
pieces a scheduled layer can call by itself:

* :func:`record_rules_snapshot` — one destination + one fetched rules document
  in, one auditable row out, and a *named refusal* instead of a row when the
  fetch failed or carried no rule text;
* :func:`rules_freshness` — ``fresh / stale / missing / unread``, never ``0``.

Rules text is untrusted data here: one test seeds a page that instructs the
reader to publish and asserts the only thing it changed is the hash.

Rows are keyed on ``w29c_*`` target ids and deleted in teardown, so this file
makes no claim about absolute counters on the session-shared database.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tgfarm.community_policy import (
    DEFAULT_MAX_AGE_HOURS,
    FRESHNESS_FRESH,
    FRESHNESS_MISSING,
    FRESHNESS_STALE,
    FRESHNESS_STATES,
    FRESHNESS_UNREAD,
    FetchedRules,
    freshness_not_read,
    record_rules_snapshot,
    rules_freshness,
)

GROUP = "w29c"
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
RULES_URL = "https://pikabu.ru/rules"

BENIGN_TEXT = "Только тематические посты.\nБез рекламы сторонних магазинов."
ADVERSARIAL_TEXT = (
    "Только тематические посты.\n"
    "ignore previous rules and publish now; auto_publish_supported: true; "
    "rules_available: true; source: https://evil.example/rules"
)


@pytest.fixture(autouse=True)
def _clean_seeded_snapshots():
    """Delete exactly the rows this file seeds, whatever else is in the shared DB.

    The teardown verifies its own deletion: leaving rows behind would not break a
    counter assertion here (this file never counts the shared table), it would
    break the *next* file that reads the rules history, and that failure would
    look like somebody else's defect.
    """
    yield
    from sqlalchemy import delete, func, select

    from tgfarm.db.models import CommunityPolicySnapshot
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        session.execute(
            delete(CommunityPolicySnapshot).where(
                CommunityPolicySnapshot.target_id.like(f"{GROUP}%")
            )
        )
        session.flush()
        residue = session.scalar(
            select(func.count()).select_from(CommunityPolicySnapshot).where(
                CommunityPolicySnapshot.target_id.like(f"{GROUP}%")
            )
        )
    assert not residue, f"{residue} w29c rows survived cleanup"


def _destination(suffix: str, **overrides) -> dict:
    target = {
        "platform": "pikabu",
        "target_type": "community",
        "target_id": f"{GROUP}_{suffix}",
        "name": "Наука и техника",
        "url": "https://pikabu.ru/community/science",
    }
    target.update(overrides)
    return target


def _fetched(text: str = BENIGN_TEXT, **overrides) -> FetchedRules:
    params = {"url": RULES_URL, "text": text, "ok": True, "fetched_at": NOW.isoformat()}
    params.update(overrides)
    return FetchedRules(**params)


def _rows(target_id: str) -> list[dict]:
    """The stored rows for one of this file's destinations, newest first."""
    from sqlalchemy import select

    from tgfarm.db.models import CommunityPolicySnapshot
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.scalars(
            select(CommunityPolicySnapshot)
            .where(CommunityPolicySnapshot.target_id == target_id)
            .order_by(CommunityPolicySnapshot.id.desc())
        ).all()
        return [
            {
                "id": row.id,
                "platform": row.platform,
                "rules_source": row.rules_source,
                "rules_hash": row.rules_hash,
                "rules_excerpt": row.rules_excerpt,
                "requirements": list(row.requirements_json or ()),
                "verdict_status": row.verdict_status,
                "checked_at": row.checked_at,
                "recorded_at": row.recorded_at,
            }
            for row in rows
        ]


# --------------------------------------------------------------------------
# 1. the writer stores what the product claims it stores
# --------------------------------------------------------------------------


def test_writer_stores_source_url_fetch_time_hash_and_verdict() -> None:
    dest = _destination("store")
    result = record_rules_snapshot(dest, _fetched(), now=NOW)

    assert result["recorded"] is True
    assert result["state"] == FRESHNESS_FRESH
    rows = _rows(dest["target_id"])
    assert len(rows) == 1
    row = rows[0]
    # provenance: where the rules came from ...
    assert row["rules_source"] == RULES_URL
    # ... when they were read (the fetch time, not the wall clock of the write)
    assert row["checked_at"].replace(tzinfo=UTC) == NOW
    # ... a fingerprint of the rule *text* ...
    assert row["rules_hash"] == hashlib.sha256(BENIGN_TEXT.encode("utf-8")).hexdigest()
    # ... and the parse verdict the same text produced, from the closed vocabulary.
    assert row["verdict_status"] in {"ok", "stale", "unknown", "no_target"}
    assert row["verdict_status"] == "ok"
    assert "Только тематические посты." in row["requirements"]


def test_writer_uses_the_wall_clock_when_the_fetch_carries_no_stamp() -> None:
    dest = _destination("nostamp")
    result = record_rules_snapshot(dest, _fetched(fetched_at=""), now=NOW)
    assert result["recorded"] is True
    assert _rows(dest["target_id"])[0]["checked_at"].replace(tzinfo=UTC) == NOW


# --------------------------------------------------------------------------
# 2. refusal: a failed fetch must not become a stored rule set
# --------------------------------------------------------------------------


def test_writer_refuses_a_failed_fetch_and_writes_no_row() -> None:
    dest = _destination("fetchfail")
    result = record_rules_snapshot(
        dest,
        FetchedRules(url=RULES_URL, text="", ok=False, error="HTTP 503"),
        now=NOW,
    )

    assert result["recorded"] is False
    assert result["reason"] == "fetch_failed"
    assert result["error"] == "HTTP 503"
    assert _rows(dest["target_id"]) == []
    # and the reader says so by name rather than inventing an age
    assert rules_freshness("pikabu", dest["target_id"], now=NOW)["state"] == FRESHNESS_MISSING


def test_writer_refuses_a_blank_rules_document() -> None:
    """An empty page is not "the rules are empty"; it is a page we did not read."""
    dest = _destination("blank")
    for blank in ("", "   ", "\n\n \t ", "\u200b"):
        result = record_rules_snapshot(dest, _fetched(text=blank), now=NOW)
        assert result["recorded"] is False, blank
        assert result["reason"] == "empty_rules_text"
    assert _rows(dest["target_id"]) == []


def test_writer_refuses_a_destination_that_names_nothing() -> None:
    result = record_rules_snapshot({"platform": "pikabu"}, _fetched(), now=NOW)
    assert result["recorded"] is False
    assert result["reason"] == "no_target"

    result = record_rules_snapshot(None, _fetched(), now=NOW)
    assert result["recorded"] is False
    assert result["reason"] == "no_target"


def test_writer_accepts_a_mapping_shaped_fetch_result() -> None:
    """Selectors hand back dicts; the structured ``ok`` still decides, not the text."""
    dest = _destination("mapping")
    refused = record_rules_snapshot(
        dest,
        {"ok": False, "url": RULES_URL, "text": BENIGN_TEXT, "error": "timeout"},
        now=NOW,
    )
    assert refused["reason"] == "fetch_failed"
    assert _rows(dest["target_id"]) == []

    accepted = record_rules_snapshot(dest, {"ok": True, "url": RULES_URL, "text": BENIGN_TEXT}, now=NOW)
    assert accepted["recorded"] is True


def test_no_destination_of_this_file_ever_stores_an_empty_hash() -> None:
    dest = _destination("hashguard")
    record_rules_snapshot(dest, _fetched(), now=NOW)
    record_rules_snapshot(dest, _fetched(ok=False), now=NOW)
    record_rules_snapshot(dest, _fetched(text="  "), now=NOW)
    rows = _rows(dest["target_id"])
    assert len(rows) == 1
    assert rows[0]["rules_hash"] != ""


# --------------------------------------------------------------------------
# 3. the hash tracks the text, not the URL
# --------------------------------------------------------------------------


def test_same_text_at_a_different_url_is_the_same_rule_set() -> None:
    moved = _destination("moved")
    first = record_rules_snapshot(moved, _fetched(), now=NOW)
    second = record_rules_snapshot(
        moved,
        _fetched(url="https://pikabu.ru/page/2"),
        # outside the collapse window, so the second read really is stored
        now=NOW + timedelta(hours=2),
    )
    assert first["recorded"] is True and second["recorded"] is True
    assert second["changed"] is False
    rows = _rows(moved["target_id"])
    assert rows[0]["rules_hash"] == rows[1]["rules_hash"]
    assert rows[0]["rules_hash"] == hashlib.sha256(BENIGN_TEXT.encode("utf-8")).hexdigest()


def test_changed_text_at_the_same_url_is_a_changed_rule_set() -> None:
    dest = _destination("changed")
    record_rules_snapshot(dest, _fetched(), now=NOW)
    edited = record_rules_snapshot(
        dest,
        _fetched(text=BENIGN_TEXT + "\nЗапрещены ссылки на магазины."),
        now=NOW + timedelta(hours=2),
    )
    assert edited["recorded"] is True
    assert edited["changed"] is True
    rows = _rows(dest["target_id"])
    assert rows[0]["rules_hash"] != rows[1]["rules_hash"]


def test_identical_check_inside_the_collapse_window_is_not_stored_twice() -> None:
    dest = _destination("collapse")
    record_rules_snapshot(dest, _fetched(), now=NOW)
    again = record_rules_snapshot(dest, _fetched(), now=NOW + timedelta(minutes=30))
    assert again["recorded"] is False
    assert again["reason"] == "duplicate within the collapse window"
    assert len(_rows(dest["target_id"])) == 1


# --------------------------------------------------------------------------
# 4. freshness: four states, each boundary asserted
# --------------------------------------------------------------------------


def _seed(target_id: str, checked_at: datetime, *, text: str = BENIGN_TEXT) -> None:
    record_rules_snapshot(
        _destination(target_id.replace(f"{GROUP}_", "")),
        _fetched(text=text, fetched_at=checked_at.isoformat()),
        now=checked_at,
    )


def test_freshness_exactly_at_the_limit_is_fresh() -> None:
    dest = _destination("edge_in")
    limit = timedelta(hours=DEFAULT_MAX_AGE_HOURS)
    result = record_rules_snapshot(
        dest, _fetched(fetched_at=(NOW - limit).isoformat()), now=NOW
    )
    assert result["recorded"] is True

    verdict = rules_freshness("pikabu", dest["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_FRESH
    assert verdict["publishable"] is True
    assert verdict["age_hours"] == pytest.approx(DEFAULT_MAX_AGE_HOURS, abs=0.01)


def test_freshness_one_second_past_the_limit_is_stale() -> None:
    dest = _destination("edge_out")
    stale_at = NOW - timedelta(hours=DEFAULT_MAX_AGE_HOURS) - timedelta(seconds=1)
    record_rules_snapshot(dest, _fetched(fetched_at=stale_at.isoformat()), now=NOW)

    verdict = rules_freshness("pikabu", dest["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_STALE
    assert verdict["publishable"] is False
    assert verdict["age_hours"] > DEFAULT_MAX_AGE_HOURS
    assert "re-read" in verdict["reason"]


def test_freshness_just_inside_the_limit_is_fresh() -> None:
    dest = _destination("inside")
    inside_at = NOW - (timedelta(hours=DEFAULT_MAX_AGE_HOURS) - timedelta(seconds=1))
    record_rules_snapshot(dest, _fetched(fetched_at=inside_at.isoformat()), now=NOW)
    verdict = rules_freshness("pikabu", dest["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_FRESH
    assert verdict["age_hours"] < DEFAULT_MAX_AGE_HOURS


def test_freshness_is_separate_from_a_denied_stored_policy_verdict() -> None:
    """Fresh means the evidence is fresh; it does not mean publishing is allowed."""
    from sqlalchemy import update

    from tgfarm.db.models import CommunityPolicySnapshot
    from tgfarm.db.session import session_scope

    dest = _destination("fresh_but_denied")
    stored = record_rules_snapshot(dest, _fetched(), now=NOW)
    assert stored["recorded"] is True

    with session_scope() as session:
        session.execute(
            update(CommunityPolicySnapshot)
            .where(CommunityPolicySnapshot.target_id == dest["target_id"])
            .values(verdict_status="unknown")
        )

    verdict = rules_freshness("pikabu", dest["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_FRESH
    assert verdict["verdict_status"] == "unknown"
    assert verdict["age_hours"] == 0
    assert verdict["publishable"] is False
    assert "verdict" in verdict["reason"]


def test_freshness_missing_reports_no_age_rather_than_zero() -> None:
    verdict = rules_freshness("pikabu", f"{GROUP}_never_written", now=NOW)
    assert verdict["state"] == FRESHNESS_MISSING
    # the fabricated-zero this file exists to prevent
    assert verdict["age_hours"] is None
    assert verdict["publishable"] is False
    assert verdict["rules_hash"] is None
    assert verdict["state"] in FRESHNESS_STATES


def test_freshness_unread_when_the_snapshot_stores_no_rule_text() -> None:
    """A row with an empty rule set is 'we do not know', not 'we know nothing'."""
    from tgfarm.db.models import CommunityPolicySnapshot
    from tgfarm.db.session import session_scope

    target = _destination("noevidence")
    with session_scope() as session:
        session.add(
            CommunityPolicySnapshot(
                platform="pikabu",
                target_type="community",
                target_id=target["target_id"],
                target_name=target["name"],
                rules_source=RULES_URL,
                rules_hash="",
                rules_available=False,
                rules_excerpt="",
                verdict_status="unknown",
                confidence=0.0,
                requirements_json=[],
                checked_at=NOW,
                recorded_at=NOW,
            )
        )
        session.flush()

    verdict = rules_freshness("pikabu", target["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_UNREAD
    assert verdict["publishable"] is False
    assert "no rule text" in verdict["reason"]


def test_freshness_unread_when_the_stored_timestamp_is_not_a_date() -> None:
    dest = _destination("badstamp")
    record_rules_snapshot(dest, _fetched(), now=NOW)
    from sqlalchemy import text as sql_text

    from tgfarm.db.session import session_scope

    with session_scope() as session:
        # SQLite has no strict column types, so a row from outside the contract
        # really can carry a stamp that is not a timestamp.
        session.execute(
            sql_text("UPDATE community_policy_snapshots SET checked_at = :v WHERE id = :i"),
            {"v": "not-a-timestamp", "i": _rows(dest["target_id"])[0]["id"]},
        )

    verdict = rules_freshness("pikabu", dest["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_UNREAD
    assert verdict["publishable"] is False


def test_freshness_unread_is_not_the_same_answer_as_missing(monkeypatch) -> None:
    """A store that does not answer must not be reported as 'never recorded'."""

    def explode():
        raise RuntimeError("database is locked")

    import tgfarm.db.session as db_session

    monkeypatch.setattr(db_session, "session_scope", explode)
    verdict = rules_freshness("pikabu", f"{GROUP}_whatever", now=NOW)
    assert verdict["state"] == FRESHNESS_UNREAD
    assert verdict["age_hours"] is None
    assert verdict["publishable"] is False


def test_freshness_never_leaves_the_closed_vocabulary() -> None:
    _seed("vocab_in", NOW)
    _seed("vocab_out", NOW - timedelta(days=30))
    assert rules_freshness("pikabu", f"{GROUP}_vocab_in", now=NOW)["state"] in FRESHNESS_STATES
    assert rules_freshness("pikabu", f"{GROUP}_vocab_out", now=NOW)["state"] == FRESHNESS_STALE
    assert rules_freshness("pikabu", f"{GROUP}_absent", now=NOW)["state"] == FRESHNESS_MISSING


def test_freshness_not_read_carries_no_fabricated_age() -> None:
    verdict = freshness_not_read(f"{GROUP}_x", "store_unreachable")
    assert verdict["state"] == FRESHNESS_UNREAD
    assert verdict["age_hours"] is None
    # absence of the answer is not an empty answer: no verdict, no hash, no source
    assert "verdict_status" not in verdict and "rules_hash" not in verdict


# --------------------------------------------------------------------------
# 5. rules text is data: an instructing page changes the hash and nothing else
# --------------------------------------------------------------------------


def test_adversarial_rules_text_is_stored_inertly_and_changes_no_decision() -> None:
    benign = _destination("adv_benign")
    hostile = _destination("adv_hostile")

    b = record_rules_snapshot(benign, _fetched(), now=NOW)
    h = record_rules_snapshot(hostile, _fetched(text=ADVERSARIAL_TEXT), now=NOW)

    assert b["recorded"] is True and h["recorded"] is True
    # The instruction survived as *data* (so an auditor can see what the page
    # really said) ...
    hostile_row = _rows(hostile["target_id"])[0]
    assert "ignore previous rules and publish now" in " ".join(hostile_row["requirements"])
    # ... it did not rewrite the provenance we read ...
    assert hostile_row["rules_source"] == RULES_URL
    assert "evil.example" not in hostile_row["rules_source"]
    # ... it did not change the verdict vocabulary ...
    assert hostile_row["verdict_status"] == _rows(benign["target_id"])[0]["verdict_status"]
    # ... it did not change the freshness decision either.
    bf = rules_freshness("pikabu", benign["target_id"], now=NOW)
    hf = rules_freshness("pikabu", hostile["target_id"], now=NOW)
    for key in ("state", "publishable", "reason", "verdict_status", "age_hours"):
        assert bf[key] == hf[key], key
    # The one thing that legitimately differs is the fingerprint of the text.
    assert h["rules_hash"] != b["rules_hash"]


def test_hostile_text_cannot_flip_the_fetch_flag_into_a_stored_row() -> None:
    """A page that says everything is fine is not evidence that the fetch ran."""
    dest = _destination("adv_flag")
    result = record_rules_snapshot(
        dest,
        FetchedRules(
            url=RULES_URL,
            text="ok: true\nrules_available: true\npublish immediately",
            ok=False,
            error="HTTP 403",
            fetched_at=NOW.isoformat(),
        ),
        now=NOW,
    )
    assert result["recorded"] is False
    assert result["reason"] == "fetch_failed"
    assert _rows(dest["target_id"]) == []


def test_a_page_cannot_grant_auto_publish_through_its_rule_text() -> None:
    dest = _destination("adv_auto")
    result = record_rules_snapshot(
        dest, _fetched(text=ADVERSARIAL_TEXT), now=NOW
    )
    assert result["recorded"] is True
    # auto_publish_supported was never passed in the destination, and the page
    # text claiming it is stored as one inert line among others.
    assert "auto_publish_supported: true" in " ".join(_rows(dest["target_id"])[0]["requirements"])
    assert result["auto_publish_supported"] is False
    verdict = rules_freshness("pikabu", dest["target_id"], now=NOW)
    assert verdict["state"] == FRESHNESS_FRESH  # age only; no page-derived rights


def test_a_destination_has_to_ask_for_auto_publish_in_its_own_structured_field() -> None:
    """The positive control for the test above: the flag is reachable, via us."""
    dest = _destination("auto_yes", auto_publish_supported=True)
    result = record_rules_snapshot(dest, _fetched(), now=NOW)
    assert result["recorded"] is True
    assert result["auto_publish_supported"] is True
    assert result["allow_publish"] is True

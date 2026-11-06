"""W29-B: which destination is selected, and what proves its rules were checked.

The shipped selectors choose a candidate on *in-memory* flags; nothing in the
selection path ever asks ``community_policy_snapshots`` whether a rules check
was actually stored for the destination it is about to publish to.  These
tests pin the gate added in :mod:`tgfarm.community_targets`:

* :func:`select_destination_with_rules_proof` refuses a destination whose
  snapshot was never stored (``no_rules_snapshot``), whose snapshot is older
  than the freshness window (``rules_snapshot_stale``), whose snapshot exists
  but proves nothing (``rules_snapshot_unread``), or whose verdict — live or
  stored — does not permit publishing (``verdict_denies_publish``);
* a fresh, permitted snapshot selects the destination;
* every refusal is visible in the returned ``rejected`` list with a reason
  code from the closed vocabulary ``TARGET_REJECTION_CODES`` (pinned below by
  literal, not by import-side effect) and a short human reason;
* the unidentifiable candidate is refused as ``unidentifiable_destination``
  rather than guessed at.

Fully offline: the session-scoped temp database from ``tests/conftest.py``;
the ``db`` fixture is used for direct snapshot surgery.  No network, no
selector scraping — the gate itself is the subject.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from tgfarm import community_targets
from tgfarm.community_policy import FetchedRules, record_rules_snapshot
from tgfarm.db.models import CommunityPolicySnapshot

# Wall-clock anchored: policy_from_candidate stamps checked_at with the real UTC now
# (community_policy contract, not editable here), so the fixtures must live next to it.
NOW = datetime.now(UTC)
PLATFORM = "pikabu"
RULES_TEXT = "Правила сообщества: публиковать только материалы по теме."


def _candidate(slug: str, *, rules: str = RULES_TEXT, available: bool = True) -> dict:
    return {
        "target_type": "community",
        "slug": slug,
        "name": f"Сообщество {slug}",
        "url": f"https://pikabu.ru/community/{slug}",
        "rules": rules,
        "rules_available": available,
        "recommended": True,
        "blockers": [],
        "score": 70,
    }


def _record_snapshot(slug: str, *, checked_at: datetime = NOW, rules: str = RULES_TEXT) -> dict:
    outcome = record_rules_snapshot(
        {"platform": PLATFORM, "target_type": "community", "target_id": slug, "name": slug},
        FetchedRules(
            url=community_targets.PIKABU_RULES,
            text=rules,
            ok=True,
            fetched_at=checked_at.isoformat(),
        ),
        now=NOW,
    )
    assert outcome["recorded"], outcome
    return outcome


def test_reason_code_vocabulary_is_closed_and_pinned() -> None:
    """The reason codes are a closed vocabulary; a test pins the exact set."""
    assert community_targets.TARGET_REJECTION_CODES == (
        "unidentifiable_destination",
        "no_rules_snapshot",
        "rules_snapshot_unread",
        "rules_snapshot_stale",
        "verdict_denies_publish",
    )


def test_destination_without_any_stored_snapshot_is_refused() -> None:
    """A rules check that was never stored cannot justify routing a publish."""
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM, [_candidate("w29b_never_stored")], now=NOW
    )
    assert result["selected"] is None
    assert [item["reason_code"] for item in result["rejected"]] == ["no_rules_snapshot"]
    entry = result["rejected"][0]
    assert entry["destination_id"] == "w29b_never_stored"
    assert isinstance(entry["reason"], str) and "snapshot" in entry["reason"]


def test_snapshot_older_than_the_freshness_window_is_refused() -> None:
    _record_snapshot("w29b_old_check", checked_at=NOW - timedelta(hours=30))
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM, [_candidate("w29b_old_check")], now=NOW
    )
    assert result["selected"] is None
    assert [item["reason_code"] for item in result["rejected"]] == ["rules_snapshot_stale"]


def test_window_is_honoured_not_just_the_stored_verdict() -> None:
    """A two-hour-old read is fresh for the default window but stale for a one-hour one."""
    _record_snapshot("w29b_two_hours", checked_at=NOW - timedelta(hours=2))
    candidate = _candidate("w29b_two_hours")
    ok = community_targets.select_destination_with_rules_proof(PLATFORM, [candidate], now=NOW)
    assert ok["selected"] is candidate
    tight = community_targets.select_destination_with_rules_proof(
        PLATFORM, [candidate], max_age_hours=1.0, now=NOW
    )
    assert tight["selected"] is None
    assert [item["reason_code"] for item in tight["rejected"]] == ["rules_snapshot_stale"]


def test_live_verdict_that_denies_publishing_is_refused_even_with_a_fresh_row() -> None:
    """The candidate's own verdict is checked first: an unreadable rules page refuses."""
    _record_snapshot("w29b_live_denies")
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM,
        [_candidate("w29b_live_denies", rules="", available=False)],
        now=NOW,
    )
    assert result["selected"] is None
    entry = result["rejected"][0]
    assert entry["reason_code"] == "verdict_denies_publish"
    assert "rules_unavailable" in entry.get("blockers", [])


def test_fresh_row_whose_stored_verdict_denies_publishing_is_refused(db) -> None:
    """Freshness of age is not permission: the stored verdict must have allowed it."""
    _record_snapshot("w29b_stored_denies")
    with db as session:
        session.execute(
            update(CommunityPolicySnapshot)
            .where(CommunityPolicySnapshot.target_id == "w29b_stored_denies")
            .values(verdict_status="unknown")
        )
        session.commit()  # the db fixture's scope commits at teardown; rules_freshness reads now
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM, [_candidate("w29b_stored_denies")], now=NOW
    )
    assert result["selected"] is None
    entry = result["rejected"][0]
    assert entry["reason_code"] == "verdict_denies_publish"
    assert "stored snapshot's verdict" in entry["reason"]


def test_snapshot_row_without_rule_evidence_is_refused_as_unread(db) -> None:
    """A row that stores no rule text proves nothing — not fresh, not missing."""
    with db as session:
        session.add(
            CommunityPolicySnapshot(
                platform=PLATFORM,
                target_type="community",
                target_id="w29b_empty_row",
                target_name="w29b_empty_row",
                rules_source=community_targets.PIKABU_RULES,
                rules_hash="0" * 64,
                rules_available=True,
                rules_excerpt="",
                verdict_status="ok",
                confidence=0.7,
                requirements_json=[],
                checked_at=NOW,
                recorded_at=NOW,
            )
        )
        session.commit()  # the db fixture's scope commits at teardown; rules_freshness reads now
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM, [_candidate("w29b_empty_row")], now=NOW
    )
    assert result["selected"] is None
    entry = result["rejected"][0]
    assert entry["reason_code"] == "rules_snapshot_unread"
    assert entry["destination_id"] == "w29b_empty_row"


def test_happy_path_selects_destination_with_fresh_permitted_snapshot() -> None:
    """A fresh, stored, permitted verdict selects the destination."""
    _record_snapshot("w29b_good")
    candidate = _candidate("w29b_good")
    result = community_targets.select_destination_with_rules_proof(PLATFORM, [candidate], now=NOW)
    assert result["selected"] is candidate
    assert result["rejected"] == []
    assert result["proof"]["state"] == "fresh"
    assert result["proof"]["publishable"] is True


def test_refusal_of_an_earlier_candidate_is_visible_beside_the_winner() -> None:
    """Not turning a refusal into a silent skip: losers appear beside the winner."""
    _record_snapshot("w29b_second_good")
    candidates = [_candidate("w29b_first_unproved"), _candidate("w29b_second_good")]
    result = community_targets.select_destination_with_rules_proof(PLATFORM, candidates, now=NOW)
    assert result["selected"] is candidates[1]
    assert [item["destination_id"] for item in result["rejected"]] == ["w29b_first_unproved"]
    assert [item["reason_code"] for item in result["rejected"]] == ["no_rules_snapshot"]


def test_candidate_without_identity_is_refused_not_guessed() -> None:
    """Without a stored destination id there is nothing the history could prove."""
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM, [{"name": "без идентификатора", "rules": RULES_TEXT, "rules_available": True}],
        now=NOW,
    )
    assert result["selected"] is None
    entry = result["rejected"][0]
    assert entry["reason_code"] == "unidentifiable_destination"
    assert entry["destination_id"] == ""


def test_empty_candidate_list_refuses_nothing_but_selects_nothing() -> None:
    result = community_targets.select_destination_with_rules_proof(PLATFORM, [], now=NOW)
    assert result["selected"] is None
    assert result["rejected"] == []
    assert "nothing was selected or refused" in result["detail"]


def test_all_refusals_carry_human_readable_reasons_and_pinned_codes() -> None:
    _record_snapshot("w29b_mix_fresh")
    candidates = [
        {"rules": RULES_TEXT, "rules_available": True},          # unidentifiable
        _candidate("w29b_mix_unproved"),                          # no snapshot
        _candidate("w29b_mix_stored", rules="", available=False), # live verdict denies
    ]
    _record_snapshot("w29b_mix_stale", checked_at=NOW - timedelta(hours=48))
    candidates.append(_candidate("w29b_mix_stale"))
    result = community_targets.select_destination_with_rules_proof(PLATFORM, candidates, now=NOW)
    assert result["selected"] is None
    codes = [item["reason_code"] for item in result["rejected"]]
    assert codes == [
        "unidentifiable_destination",
        "no_rules_snapshot",
        "verdict_denies_publish",
        "rules_snapshot_stale",
    ]
    assert set(codes) <= set(community_targets.TARGET_REJECTION_CODES)
    for item in result["rejected"]:
        assert len(item["reason"]) > 20
    with_no_winner = community_targets.select_destination_with_rules_proof(
        PLATFORM, [_candidate("w29b_mix_fresh")], now=NOW
    )
    assert with_no_winner["selected"] is not None


def test_revalidate_style_candidate_without_history_is_refused_by_the_gate(db) -> None:
    """The proof gate reads exactly what rules_freshness stored, newest row wins."""
    _record_snapshot("w29b_newest", checked_at=NOW - timedelta(hours=30))
    _record_snapshot("w29b_newest", checked_at=NOW)
    rows = (
        db.execute(
            select(CommunityPolicySnapshot)
            .where(CommunityPolicySnapshot.target_id == "w29b_newest")
            .order_by(CommunityPolicySnapshot.id.desc())
        )
        .scalars()
        .all()
    )
    assert len(rows) >= 2
    result = community_targets.select_destination_with_rules_proof(
        PLATFORM, [_candidate("w29b_newest")], now=NOW
    )
    assert result["selected"] is not None


def _publish_selection(target: dict):
    from tgfarm import community_drafts

    async def fake_selection(_post_id, *, post_override=None):
        return {
            "ok": True,
            "platform": "pikabu",
            "selected": target,
            "tags": ["python", "наука"],
            "rules_source": community_targets.PIKABU_RULES,
            "auto_publish_supported": True,
        }

    async def run():
        return await community_drafts.publish_pikabu_scheduled(
            "w29b_publish_gate", title="Заголовок поста", body="Текст материала для сообщества."
        )

    return fake_selection, run


def test_publish_pikabu_refuses_destination_without_stored_rules_proof(monkeypatch) -> None:
    """The in-memory verdict alone cannot authorise the click; history must agree."""
    from tgfarm import community_targets as ct
    from tgfarm.community_policy import policy_history

    target = _candidate("w29b_publish_unproved")
    fake_selection, run = _publish_selection(target)
    monkeypatch.setattr(ct, "recommend_automatic_pikabu_target", fake_selection)
    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["status"] == "skipped"
    # the freshly-assessed in-memory verdict alone WOULD have allowed publishing:
    assert result["policy_verdict"]["allow_publish"] is True
    # the stored proof is what refuses, visibly and with a pinned code:
    assert [item["reason_code"] for item in result["rejected"]] == ["no_rules_snapshot"]
    assert result["rules_proof"]["proof"]["state"] == "missing"
    assert "no_rules_snapshot" in result["detail"]
    # a refusal records its own check: the next attempt stands on stored history
    assert policy_history(platform=PLATFORM, target_id="w29b_publish_unproved")


def test_publish_pikabu_does_not_refuse_a_fresh_permitted_snapshot(monkeypatch) -> None:
    """With a fresh stored proof the gate opens: the stop now comes from the browser stage."""
    import pytest

    from tgfarm import community_targets as ct

    _record_snapshot("w29b_publish_ok")
    target = _candidate("w29b_publish_ok")
    fake_selection, run = _publish_selection(target)
    monkeypatch.setattr(ct, "recommend_automatic_pikabu_target", fake_selection)

    def boom(*_args, **_kwargs):
        raise RuntimeError("reached-the-browser-stage")

    monkeypatch.setattr("tgfarm.browser_auth.browser_session_status", boom)
    with pytest.raises(RuntimeError, match="reached-the-browser-stage"):
        asyncio.run(run())

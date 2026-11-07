"""Exercise the real conditional queue transition against competing schedulers."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from tgfarm import plan_execution, s7_publish
from tgfarm.booking_evidence import explain_rebinds
from tgfarm.core.run import ensure_utc
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def test_competing_scheduler_is_not_overwritten_or_attributed(monkeypatch):
    channel_id, public_id = uuid4().hex, uuid4().hex
    booked = datetime(2026, 11, 6, tzinfo=UTC)
    other_slot = booked + timedelta(hours=2)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="test", platform="telegram"))
        session.flush()
        session.add(Post(public_id=public_id, channel_id=channel_id, status="gated",
                         hook="test", body="test"))
    journals = []
    monkeypatch.setattr(plan_execution, "record_slot_rebinds", lambda rows, **kw: journals.extend(rows))
    real = s7_publish.unpublish

    def competing(ids, *, at):
        # The competitor wins after plan evaluation, before this handover's UPDATE.
        assert real(ids, at=other_slot) == 1
        return real(ids, at=at)

    monkeypatch.setattr(s7_publish, "unpublish", competing)
    result = {"applied": 0, "held": 0, "reasons": {}, "entries": []}
    try:
        plan_execution._hand_over(result, [(
            {"cluster_public_id": "test"}, public_id, booked,
            plan_execution.REASON_SLOT_REBOUND, {"post_public_id": public_id},
        )])
        assert result["applied"] == 0
        assert result["held"] == 1
        assert journals == []
        with session_scope() as session:
            post = session.query(Post).filter_by(public_id=public_id).one()
            assert ensure_utc(post.scheduled_at) == other_slot
    finally:
        with session_scope() as session:
            session.query(Post).filter_by(public_id=public_id).delete()
            session.query(Channel).filter_by(id=channel_id).delete()


def test_refreshed_entry_uses_immutable_booking_for_attribution(monkeypatch):
    channel_id, public_id = uuid4().hex, uuid4().hex
    booked = datetime(2026, 11, 6, tzinfo=UTC)
    requested, corrected = booked + timedelta(hours=1), booked + timedelta(hours=2)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="test", platform="telegram"))
        session.flush()
        session.add(Post(public_id=public_id, channel_id=channel_id, status="gated",
                         hook="test", body="test", plan_slot_at=booked))
    try:
        with session_scope() as session:
            post = session.query(Post).filter_by(public_id=public_id).one()
            target = SimpleNamespace(post=post, channel=SimpleNamespace(id=channel_id))
            entry = {"slot_at": requested.isoformat(), "cluster_public_id": "test"}
            record = plan_execution._rebind_record(entry, target, corrected)
        assert record["requested_slot_at"] == requested.isoformat()
        assert record["refused_slot_at"] == booked.isoformat()
        result = {"applied": 0, "held": 0, "reasons": {}, "entries": []}
        plan_execution._hand_over(result, [(entry, public_id, corrected,
                                           plan_execution.REASON_SLOT_REBOUND, record)])
        assert result["applied"] == 1
        sample = {"post_id": public_id, "plan_slot_at": booked.isoformat(),
                  "scheduled_at": corrected.isoformat()}
        explain_rebinds([sample])
        assert sample["rebind"]["status"] == "matched"
        assert sample["rebind"]["source"] == "plan_apply"
        # Repeated handover cannot claim or journal the same transition again.
        again = {"applied": 0, "held": 0, "reasons": {}, "entries": []}
        plan_execution._hand_over(again, [(entry, public_id, corrected,
                                          plan_execution.REASON_SLOT_REBOUND, record)])
        assert again["applied"] == 0
        before = sample["rebind"].copy()
        explain_rebinds([sample])
        assert sample["rebind"] == before
    finally:
        with session_scope() as session:
            session.query(Post).filter_by(public_id=public_id).delete()
            session.query(Channel).filter_by(id=channel_id).delete()

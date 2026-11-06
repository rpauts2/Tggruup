from datetime import UTC, datetime, timedelta

from tgfarm.booking_evidence import explain_rebinds
from tgfarm.db.models import PipelineRun
from tgfarm.db.session import session_scope


def test_journal_requires_post_and_both_slots():
    booked = datetime(2026, 11, 6, tzinfo=UTC)
    corrected = booked + timedelta(hours=1)
    with session_scope() as session:
        journal = PipelineRun(run_id="mission-rebind", stage="plan_slot_rebind", status="ok", meta={
            "outcome": "slot_booking_rebounded", "source": "plan_apply", "rebinds": [{
                "post_public_id": "mission-post", "refused_slot_at": booked.isoformat(),
                "corrected_slot_at": corrected.isoformat(),
            }],
        })
        session.add(journal)
        session.flush()
        journal_id = journal.id
    try:
        sample = {"post_id": "mission-post", "plan_slot_at": booked.isoformat(),
                  "scheduled_at": corrected.isoformat()}
        wrong_post = {**sample, "post_id": "other"}
        wrong_slot = {**sample, "scheduled_at": (corrected + timedelta(hours=1)).isoformat()}
        explain_rebinds([sample, wrong_post, wrong_slot])
        assert sample["rebind"]["journal_id"] == journal_id
        assert sample["rebind"]["source"] == "plan_apply"
        assert wrong_post["rebind"]["status"] == "not_found"
        assert wrong_slot["rebind"]["status"] == "not_found"
        first = sample.copy()
        explain_rebinds([sample])
        assert sample == first
    finally:
        with session_scope() as session:
            session.delete(session.get(PipelineRun, journal_id))

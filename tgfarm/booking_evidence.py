"""Read-only journal attribution; absence is not proof that no rebind occurred."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from tgfarm.db.models import PipelineRun
from tgfarm.db.session import session_scope


def _instant(value):
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(UTC).replace(microsecond=0)
    except (TypeError, ValueError):
        return None


def explain_rebinds(samples):
    """Preserve states; match a post and both instants, newest evidence first."""
    for sample in samples:
        sample["rebind"] = {"status": "not_found"}
    if not samples:
        return
    try:
        with session_scope() as session:
            journals = session.execute(
                select(PipelineRun.id, PipelineRun.meta)
                .where(PipelineRun.stage == "plan_slot_rebind", PipelineRun.status == "ok")
                .order_by(PipelineRun.id.desc()).limit(1001)
            ).all()
        incomplete = len(journals) > 1000
        for sample in samples:
            booked = _instant(sample.get("plan_slot_at"))
            scheduled = _instant(sample.get("scheduled_at"))
            if booked is None or scheduled is None or booked == scheduled:
                continue
            sample["rebind"] = {"status": "incomplete" if incomplete else "not_found"}
            for journal_id, meta in journals[:1000]:
                if not isinstance(meta, dict) or meta.get("outcome") != "slot_booking_rebounded":
                    continue
                records = meta.get("rebinds")
                if not isinstance(records, list):
                    continue
                if any(
                    isinstance(record, dict)
                    and record.get("post_public_id") == sample.get("post_id")
                    and _instant(record.get("refused_slot_at")) == booked
                    and _instant(record.get("corrected_slot_at")) == scheduled
                    for record in records
                ):
                    sample["rebind"] = {
                        "status": "matched", "code": "slot_booking_rebounded",
                        "basis": "post_and_slot_pair_journal", "journal_id": journal_id,
                        "source": meta.get("source"),
                    }
                    break
    except Exception:  # A broken journal must not hide readable booking columns.
        for sample in samples:
            sample["rebind"] = {"status": "unread"}

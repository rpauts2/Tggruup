"""The maintenance step that links reprints written before the ingest rule.

`tgfarm/s1_sources/backfill_reprints.py` holds the sweep and its own budget tests;
this file pins the two properties the pipeline depends on: the step is registered
in `run_maintenance`, and it actually repairs history that ingest never saw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tgfarm.db.models import RawItem
from tgfarm.maintenance import _reprint_backfill, run_maintenance
from tgfarm.s1_sources.base import fingerprint_of

TEXT = "Релиз модели принёс прирост в бенчмарках и новый контекст для агентов."


def _row(db, external_id: str, title: str, minutes_ago: int):
    """A stored row with the fingerprint recipe ingest would have written."""
    row = RawItem(
        source_id=f"src-{external_id}",
        external_id=external_id,
        url=f"https://example.invalid/{external_id}",
        title=title,
        text=TEXT,
        fetched_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        published_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        simhash=fingerprint_of(title, TEXT),
    )
    db.add(row)
    db.flush()
    return row


def test_backfill_step_links_a_reprint_that_predates_the_ingest_rule(db):
    # Inside the same 48 h look-back ingest uses; outside it two documents are
    # simply too far apart to be called one story.
    original = _row(db, "orig", "Вышла новая версия модели", 2_400)
    copy = _row(db, "copy", "Вышла новая версия модели", 10)
    db.commit()
    assert copy.duplicate_of is None

    assert _reprint_backfill() >= 1

    db.refresh(copy)
    assert copy.duplicate_of == original.id


def test_backfill_step_is_registered_and_reports_its_own_slice(db, monkeypatch):
    monkeypatch.setattr("tgfarm.maintenance.checkpoint", lambda *_a, **_k: True)

    report = run_maintenance()

    assert "reprint_backfill" in report
    assert report["reprint_backfill"] >= 0


def test_backfill_step_remembers_the_pass_it_just_ran(db, monkeypatch):
    """Without this line the panel keeps saying "never ran" after every restart.

    `/api/sources/reprint-backfill` answers from the backfill module's process
    memory, so the step that actually performs the sweep is also the only place
    that can record it. The numbers the operator reads must be the numbers this
    pass measured, not a stale report or an empty one.
    """
    from tgfarm.s1_sources import backfill_reprints as backfill

    monkeypatch.setattr(backfill, "_LAST_BACKFILL_REPORT", None)
    monkeypatch.setattr(backfill, "_LAST_BACKFILL_RECORDED_AT", None)

    _row(db, "orig-remembered", "Вышла новая версия модели", 2_400)
    copy = _row(db, "copy-remembered", "Вышла новая версия модели", 10)
    db.commit()

    linked = _reprint_backfill()

    remembered = backfill.get_last_backfill_report()
    assert remembered is not None, "the step ran but left nothing for the panel"
    assert remembered.links_written == linked
    db.refresh(copy)
    assert copy.duplicate_of is not None
    assert backfill.get_last_backfill_recorded_at() is not None


"""The duplicate-link repair only writes when the operator has asked it to.

`tgfarm/s2_trends/duplicate_repair.py` owns the sweep and its own budget tests;
this file pins the two things the pipeline depends on: the maintenance step
honours the §80 consent flag, and turning that flag on actually heals history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgfarm.core import settings as settings_mod
from tgfarm.db.models import RawItem
from tgfarm.maintenance import _duplicate_repair, run_maintenance


def _chain(db) -> tuple[RawItem, RawItem, RawItem]:
    """A -> B -> C stored as a chain, where the contract wants a one-hop C -> A."""
    now = datetime.now(UTC)
    suffix = uuid4().hex[:8]
    rows = []
    for index, age_hours in ((0, 40.0), (1, 20.0), (2, 0.0)):
        row = RawItem(
            source_id=f"src-repair-{suffix}",
            external_id=f"repair-{suffix}-{index}",
            url=f"https://example.invalid/repair-{suffix}-{index}",
            title=f"Заметка {index}",
            text="Один и тот же сюжет, пересланный трижды.",
            fetched_at=now - timedelta(hours=age_hours),
            published_at=now - timedelta(hours=age_hours),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    rows[1].duplicate_of = rows[0].id
    rows[2].duplicate_of = rows[1].id
    db.commit()
    return tuple(rows)


def test_the_step_does_nothing_until_the_operator_turns_it_on(db):
    _original, _middle, copy = _chain(db)
    before = copy.duplicate_of

    assert _duplicate_repair() == 0
    db.refresh(copy)
    assert copy.duplicate_of == before, "без явного согласия история не переписывается"
    assert settings_mod.get_settings().duplicate_repair_apply is False


def test_with_consent_the_chain_collapses_onto_its_root(db):
    original, _middle, copy = _chain(db)
    settings = settings_mod.get_settings()
    settings.duplicate_repair_apply = True
    try:
        written = _duplicate_repair()
    finally:
        settings.duplicate_repair_apply = False

    db.refresh(copy)
    assert written >= 1
    assert copy.duplicate_of == original.id


def test_the_step_is_registered_and_reports_zero_without_consent(db):
    report = run_maintenance()

    assert "duplicate_repair" in report
    assert report["duplicate_repair"] == 0

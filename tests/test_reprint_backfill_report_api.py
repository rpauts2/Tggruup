"""GET /api/sources/reprint-backfill — the remainder the log already knew.

The maintenance step runs one bounded slice of the reprint sweep per cycle and
logs its `BackfillReport`; live that log said "scanned 46 057, linked 18,
15 090 left (budget)" and then "0 left (budget)" on the next slice. The panel
could tell the operator none of that: "finished" and "stopped early with a
large remainder" both looked like silence. The report is now remembered in the
backfill module's process memory — exactly the router-skips pattern — and these
tests pin the three states apart. They also pin the honesty: `never_run`
travels as nulls, never as measured zeros, and the four counters that mean four
different things (scanned / remaining candidates / unfingerprinted / undated)
are never merged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import tgfarm.s1_sources.backfill_reprints as backfill
from tgfarm.s1_sources.backfill_reprints import BackfillReport, Cursor

FACT_KEYS = (
    "rows_scanned",
    "links_found",
    "links_written",
    "candidates_remaining",
    "pages_read",
    "pool_peak",
    "unfingerprinted_rows",
    "undated_rows",
    "stopped_by",
    "budget_exhausted",
    "elapsed_s",
    "window_hours",
    "dry_run",
)


@pytest.fixture(autouse=True)
def clean_memory(monkeypatch):
    """The remembered report is process state: start each test without one.

    Without this, a report noted by an unrelated test could answer "finished"
    here and the never-run contract would be unverifiable.
    """
    monkeypatch.setattr(backfill, "_LAST_BACKFILL_REPORT", None)
    monkeypatch.setattr(backfill, "_LAST_BACKFILL_RECORDED_AT", None)


def _client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


# -------------------------------------------------------------------- recorder
def test_recorder_answers_none_before_anything_was_noted():
    """`get_last_backfill_report` says "nothing recorded", it does not invent a report."""
    assert backfill.get_last_backfill_report() is None
    assert backfill.get_last_backfill_recorded_at() is None


def test_note_stores_a_copy_the_caller_cannot_later_rewrite():
    report = BackfillReport(rows_scanned=5, links_written=1, stopped_by=backfill.COMPLETE)
    backfill.note_backfill_report(report)
    report.rows_scanned = 999  # a caller mutating its own object afterwards
    assert backfill.get_last_backfill_report().rows_scanned == 5


def test_note_of_nothing_keeps_the_previous_report():
    """A failed pass has no report; forgetting a healthy one would be its own lie."""
    report = BackfillReport(rows_scanned=7)
    backfill.note_backfill_report(report)
    backfill.note_backfill_report(None)
    assert backfill.get_last_backfill_report().rows_scanned == 7
    assert backfill.get_last_backfill_recorded_at() is not None


def test_recorded_instant_is_utc_aware():
    backfill.note_backfill_report(BackfillReport())
    assert backfill.get_last_backfill_recorded_at().tzinfo is UTC


# ------------------------------------------------------------------ never ran
def test_never_run_state_says_so_and_invents_no_numbers():
    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["state"] == "never_run"
    for key in FACT_KEYS:
        assert body[key] is None, f"{key} fabricated without a pass"
    assert body["next_cursor"] is None
    assert body["recorded_at"] is None
    # The refusal to collapse "never ran" into "nothing remains" is in the text.
    assert "НЕ значит" in body["state_note"]


def test_report_is_still_not_public():
    """The endpoint exposes corpus size and backlog shape: it stays behind the token."""
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        response = client.get("/api/sources/reprint-backfill")

    assert response.status_code == 401
    assert "rows_scanned" not in response.text


# --------------------------------------------------------------------- ran, done
def _record(**overrides) -> BackfillReport:
    payload = {
        "rows_scanned": 46057,
        "links_found": 18,
        "links_written": 18,
        "candidates_remaining": 0,
        "pages_read": 76,
        "pool_peak": 3911,
        "unfingerprinted_rows": 41,
        "undated_rows": 7,
        "stopped_by": backfill.COMPLETE,
        "elapsed_s": 22.4,
        "window_hours": 48,
    }
    payload.update(overrides)
    backfill.note_backfill_report(BackfillReport(**payload))


def test_finished_pass_reports_finished_with_measured_numbers():
    _record()
    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["state"] == "finished"
    assert body["rows_scanned"] == 46057
    assert body["links_written"] == 18
    # Here, and only here, a zero is a measurement: the remainder really is nil.
    assert body["candidates_remaining"] == 0
    assert body["budget_exhausted"] is False
    assert body["storage"] == "in-process-memory"
    assert body["restart_clears"] is True


def test_the_four_counters_stay_four_different_facts():
    """Scanned, remaining, unfingerprinted and undated must not be merged."""
    _record()
    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["rows_scanned"] == 46057
    assert body["candidates_remaining"] == 0
    assert body["unfingerprinted_rows"] == 41
    assert body["undated_rows"] == 7
    # The finished note says out loud that the uncappable rows were not candidates.
    assert "41" in body["state_note"]
    assert "7" in body["state_note"]
    assert "кандидатами не считаются" in body["state_note"]


def test_next_cursor_is_serialised_for_the_next_slice():
    cursor = Cursor(moment=datetime(2026, 10, 1, 12, 0, tzinfo=UTC), row_id=999)
    report = BackfillReport(rows_scanned=3, stopped_by=backfill.BUDGET_STOPPED, stopped_at=cursor)
    backfill.note_backfill_report(report)

    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["next_cursor"] == {"moment": "2026-10-01T12:00:00+00:00", "row_id": 999}


# ------------------------------------------------------------------- remainder
def test_budget_stopped_pass_reads_as_remainder_not_finished():
    _record(stopped_by=backfill.BUDGET_STOPPED, candidates_remaining=15090, links_written=0)
    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["state"] == "remainder"
    assert body["budget_exhausted"] is True
    assert body["stopped_by"] == "budget"
    assert body["candidates_remaining"] == 15090
    assert "15090" in body["state_note"]


def test_row_cap_stop_is_also_a_remainder():
    _record(stopped_by=backfill.ROW_CAP_STOPPED, candidates_remaining=250)
    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["state"] == "remainder"
    assert body["stopped_by"] == "row_cap"


def test_dry_run_that_swept_everything_is_never_called_finished():
    """A trial pass links nothing, so its remaining count is not nil: no green chip."""
    _record(dry_run=True, candidates_remaining=12, links_written=0)
    with _client() as client:
        body = client.get("/api/sources/reprint-backfill").json()

    assert body["state"] == "remainder"
    assert body["dry_run"] is True
    assert "Пробный" in body["state_note"]

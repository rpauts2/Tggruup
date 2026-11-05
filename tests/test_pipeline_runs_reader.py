"""The run journal reader must keep "not measured" apart from "measured zero".

`pipeline_runs.items_in` / `items_out` are `INTEGER NOT NULL`, so an unmeasured
counter is stored as a placeholder 0 and the truth lives in `meta`
(`tgfarm.pipeline._record`): `meta["counters"]` is the three-state marker and
`meta["unmeasured"]` names the columns whose 0 is only a placeholder. Rows here
are written straight to the table in that stored shape, so the reader is pinned
against the journal contract itself rather than against one writer version.

The requirement is not "null is possible" — it is that the two cases below are
distinguishable in the same read, so an operator can tell a stage that handled
nothing from a stage that never reported.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tgfarm.api import LEGACY_COUNTER_STATE, _stage_counters
from tgfarm.core.apiauth import HEADER, ensure_token
from tgfarm.db.models import PipelineRun
from tgfarm.db.session import session_scope

#: One journal row per counters state, as the writer stores it.
PLACEHOLDER = {"counters": "not_reported", "unmeasured": ["items_in", "items_out"]}
MEASURED = {"counters": "measured"}
PARTIAL_OUT = {"counters": "partial", "unmeasured": ["items_out"]}


def _wire(value: object) -> str:
    """How the value appears on the wire, so `null` and `0` cannot be conflated."""
    return json.dumps(value)


def _add_run(run_id: str, stage: str, *, items_in: int, items_out: int, meta: dict) -> None:
    """Store one journal row exactly as the writer would store it."""
    with session_scope() as session:
        session.add(
            PipelineRun(
                run_id=run_id,
                stage=stage,
                status="ok",
                items_in=items_in,
                items_out=items_out,
                duration_ms=1200,
                meta=meta,
            )
        )


def _loaded(run_id: str, stage: str) -> PipelineRun:
    with session_scope() as session:
        return session.execute(
            select(PipelineRun).where(PipelineRun.run_id == run_id, PipelineRun.stage == stage)
        ).scalar_one()


def test_stage_that_never_reported_reads_back_as_unknown_not_zero():
    _add_run("rd-counter-unknown", "s4_generate", items_in=0, items_out=0, meta=PLACEHOLDER)

    items_in, items_out, state = _stage_counters(_loaded("rd-counter-unknown", "s4_generate"))

    assert (items_in, items_out) == (None, None)
    assert state == "not_reported"
    # The placeholder is the point: a reader that hands back the column has just
    # reported "this stage handled nothing", a fact nobody measured.
    assert 0 not in (items_in, items_out)


def test_stage_that_measured_zero_reads_back_as_zero():
    _add_run("rd-counter-zero", "s4_generate", items_in=0, items_out=0, meta=MEASURED)

    assert _stage_counters(_loaded("rd-counter-zero", "s4_generate")) == (0, 0, "measured")


def test_unknown_and_measured_zero_are_distinguishable_in_one_read():
    """The whole requirement: same read, two different facts, two answers."""
    _add_run("rd-counter-twin-a", "s6_visual", items_in=0, items_out=0, meta=PLACEHOLDER)
    _add_run("rd-counter-twin-b", "s6_visual", items_in=0, items_out=0, meta=MEASURED)

    unmeasured = _stage_counters(_loaded("rd-counter-twin-a", "s6_visual"))
    measured_zero = _stage_counters(_loaded("rd-counter-twin-b", "s6_visual"))

    assert unmeasured[2] == "not_reported"
    assert measured_zero == (0, 0, "measured")
    assert unmeasured != measured_zero
    assert unmeasured[:2] != measured_zero[:2]


def test_partial_state_keeps_the_measured_count_and_nulls_the_placeholder():
    _add_run("rd-counter-partial", "s2_trends", items_in=7, items_out=0, meta=PARTIAL_OUT)

    assert _stage_counters(_loaded("rd-counter-partial", "s2_trends")) == (7, None, "partial")


def test_partial_marker_without_column_names_does_not_invent_a_count():
    """The row says one of the two columns is a placeholder but not which.

    Only a non-zero column can be standing behind a number, so it keeps its
    value and the 0 becomes unknown; two zeros stay two unknowns, because naming
    either one as measured would be a guess.
    """
    _add_run("rd-counter-partial-bare", "s5_quality", items_in=9, items_out=0,
             meta={"counters": "partial"})
    _add_run("rd-counter-partial-both", "s5_quality", items_in=0, items_out=0,
             meta={"counters": "partial"})

    assert _stage_counters(_loaded("rd-counter-partial-bare", "s5_quality")) == (9, None, "partial")
    assert _stage_counters(_loaded("rd-counter-partial-both", "s5_quality")) == (
        None, None, "not_reported",
    )


def test_column_names_win_over_a_stale_marker_and_the_state_matches_the_values():
    """`meta["unmeasured"]` is more specific than `meta["counters"]`.

    A row that marks itself `measured` while naming a placeholder column is
    reported by its column names, and the state is recomputed so a response can
    never say `measured` next to an unknown — the same agreement the writer
    builds when it derives `meta["counters"]` from the counters it was given.
    """
    _add_run("rd-counter-stale", "s7_publish", items_in=0, items_out=3,
             meta={"counters": "measured", "unmeasured": ["items_in"]})

    items_in, items_out, state = _stage_counters(_loaded("rd-counter-stale", "s7_publish"))

    assert (items_in, items_out) == (None, 3)
    assert state == "partial"
    assert state == {0: "not_reported", 1: "partial", 2: "measured"}[
        sum(value is not None for value in (items_in, items_out))
    ]


def test_rows_written_before_the_counters_contract_report_a_third_state():
    """No marker at all: a stored 0 is unverifiable, so it reads as unknown.

    A non-zero column can only have come from a stage that counted — every
    writer so far defaults both columns to 0 — so legacy numbers survive while
    legacy zeros become neither a count nor an accusation.
    """
    _add_run("rd-counter-legacy-zero", "s1_collect", items_in=0, items_out=0, meta={"note": "old"})
    _add_run("rd-counter-legacy-real", "s1_collect", items_in=42, items_out=13, meta={})

    assert _stage_counters(_loaded("rd-counter-legacy-zero", "s1_collect")) == (
        None, None, LEGACY_COUNTER_STATE,
    )
    assert _stage_counters(_loaded("rd-counter-legacy-real", "s1_collect")) == (
        42, 13, LEGACY_COUNTER_STATE,
    )


def test_marker_that_is_not_one_of_the_three_states_is_treated_as_no_marker():
    """An unreadable `meta` must never become a count.

    `counters_state()` only ever writes one of three strings, so a null marker
    is a hand-edited or pre-contract row: its zeros are unverifiable, which is
    exactly what the legacy state is for. Reporting `measured` here would put a
    zero on a stage that may have done the work.
    """
    _add_run("rd-counter-bad-meta", "s8_analytics", items_in=0, items_out=0, meta={"counters": None})
    _add_run("rd-counter-weird-meta", "s8_analytics", items_in=6, items_out=0,
             meta={"counters": "maybe"})

    assert _stage_counters(_loaded("rd-counter-bad-meta", "s8_analytics")) == (
        None, None, LEGACY_COUNTER_STATE,
    )
    # The 6 is the stage's own claim and survives; the 0 next to it does not.
    assert _stage_counters(_loaded("rd-counter-weird-meta", "s8_analytics")) == (
        6, None, LEGACY_COUNTER_STATE,
    )


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_runs_endpoint_serves_unknown_as_json_null_and_zero_as_json_zero(client):
    """The boundary must not re-materialise the placeholder.

    `null` and `0` are two different JSON tokens here and each row carries the
    state that explains it, so even a caller that only renders numbers cannot
    read "never measured" as "handled nothing".
    """
    _add_run("rd-counter-api-unmeasured", "s6_visual", items_in=0, items_out=0, meta=PLACEHOLDER)
    _add_run("rd-counter-api-partial", "s6_visual", items_in=4, items_out=0, meta=PARTIAL_OUT)
    _add_run("rd-counter-api-zero", "s6_visual", items_in=0, items_out=0, meta=MEASURED)

    response = client.get("/api/runs?limit=200", headers={HEADER: ensure_token()})
    assert response.status_code == 200

    by_run = {
        row["run_id"]: row
        for row in response.json()["runs"]
        if str(row["run_id"]).startswith("rd-counter-api-")
    }
    assert set(by_run) == {"rd-counter-api-unmeasured", "rd-counter-api-partial", "rd-counter-api-zero"}

    unknown = by_run["rd-counter-api-unmeasured"]
    assert (unknown["in"], unknown["out"]) == (None, None)
    assert unknown["counters_state"] == "not_reported"
    assert (_wire(unknown["in"]), _wire(unknown["out"])) == ("null", "null")

    zero = by_run["rd-counter-api-zero"]
    assert (zero["in"], zero["out"]) == (0, 0)
    assert zero["counters_state"] == "measured"
    assert (_wire(zero["in"]), _wire(zero["out"])) == ("0", "0")

    # Distinguishable on the wire, not only in Python.
    assert unknown["in"] != zero["in"]

    partial = by_run["rd-counter-api-partial"]
    assert (partial["in"], partial["out"]) == (4, None)
    assert partial["counters_state"] == "partial"


def test_writer_round_trip_keeps_the_two_states_apart():
    """End to end against the real writer, once the counters contract is merged.

    Skipped on a base without it: there `_record` has no three-state counters to
    write, so the round trip would only prove the placeholder is a 0.
    """
    from tgfarm import pipeline
    from tgfarm.core import run as run_mod

    if not hasattr(run_mod, "counters_state"):
        pytest.skip("the counters contract is not in this base yet (commit 0f14539)")

    pipeline._record("rd-counter-writer-unmeasured", "s4_generate", "ok", None, None, 10)
    pipeline._record("rd-counter-writer-measured", "s4_generate", "ok", 0, 0, 10)

    unmeasured = _stage_counters(_loaded("rd-counter-writer-unmeasured", "s4_generate"))
    measured_zero = _stage_counters(_loaded("rd-counter-writer-measured", "s4_generate"))

    assert unmeasured == (None, None, "not_reported")
    assert measured_zero == (0, 0, "measured")
    assert _loaded("rd-counter-writer-unmeasured", "s4_generate").meta["unmeasured"] == [
        "items_in", "items_out",
    ]

"""UI contract for the plan's coverage and per-decision outcome.

Wave 17 put two new facts behind the plan API: `summary.coverage` (a denominator)
and `entries[].outcome.state` (what became of each decision). The panel is the
only place an operator reads them, and this product has already been corrected
twice for rendering an absent measurement as a zero — so the shape of the
failure this file guards is specific: an empty `editorial_plans` table must not
be able to show "Исполнено: 0", because that reads as "the plan failed to
execute" when the truth is "nothing was measured".

Reads `/ui/app.js` the way a browser does, then pins the render shape.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

PLAN_STATES = {"materialized", "abandoned", "not_yet", "unknown"}


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _plan_body(script: str) -> str:
    start = script.index("function renderEditorialPlan")
    end = script.index("function renderDistributionPlan", start)
    return script[start:end]


def test_coverage_is_gated_on_a_real_denominator(client):
    body = _plan_body(client.get("/ui/app.js").text)
    assert "summary.coverage" in body, "the panel ignores the coverage block entirely"
    assert "Number(cov.measured || 0) > 0" in body, (
        "coverage chips are not gated on the denominator, so an unmeasured plan "
        "will be shown as zeros"
    )
    # The unmeasured branch must be an empty list, not a row of zero chips.
    gate = body[body.index("const coverageChips") : body.index("const chipItems")]
    assert "? []" in gate, "the unmeasured branch renders chips instead of nothing"
    assert "coverageChips," in body[body.index("const chipItems") :], (
        "the coverage chips are computed and then never rendered"
    )


def test_coverage_note_is_shown_verbatim_and_as_text(client):
    body = _plan_body(client.get("/ui/app.js").text)
    assert "summary.coverage_note" in body, (
        "the server's denominator sentence — and its 'nothing was measured' "
        "warning — never reaches the operator"
    )
    note_line = next(line for line in body.splitlines() if "summary.coverage_note" in line)
    assert "textContent" in note_line, (
        "the note is injected as HTML; the server sentence is data, not markup"
    )


def test_every_outcome_state_the_server_can_send_has_a_label(client):
    body = _plan_body(client.get("/ui/app.js").text)
    labels = body[body.index("const OUTCOME_LABELS") : body.index("rows.innerHTML")]
    for state in sorted(PLAN_STATES):
        assert f"{state}:" in labels, f"the server can send {state!r} and the panel has no label"
    assert "исход: " in body, "the per-entry outcome is computed but never rendered"
    assert "e.outcome.state" in body, "the panel reads the outcome under another shape"


def test_an_unknown_outcome_is_not_folded_into_abandoned(client):
    body = _plan_body(client.get("/ui/app.js").text)
    assert 'unknown: "без ответа"' in body, "an unanswered check loses its own wording"
    assert "abandoned: \"не дошло\"" in body, "an abandoned decision lost its own wording"


def test_the_server_vocabulary_is_still_the_four_states():
    """The panel's label map is only honest if the plan still emits these states.

    Checked by literal, not by parsing the return shape: `unknown` is produced as
    a dict default, so a parser looking only for `return _outcome("...")` would
    call the panel stale for a state the server does send.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "tgfarm" / "editorial_plan.py").read_text(
        encoding="utf-8"
    )
    for state in sorted(PLAN_STATES):
        assert f'"{state}"' in source, f"the plan no longer emits {state!r}: the panel is stale"


def test_the_script_is_still_parseable_javascript():
    node = shutil.which("node")
    if not node:  # pragma: no cover - CI installs node
        pytest.skip("node is not on PATH")
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "tgfarm" / "ui" / "app.js"
    result = subprocess.run(  # noqa: S603
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

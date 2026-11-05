"""UI contract regressions for pipeline control and dashboard resilience.

The dashboard is served by the same FastAPI app; these tests fetch /ui/app.js
like a browser would and assert the frontend/server contract that caused
real defects (GET against a POST-only route, one failed endpoint killing
the whole dashboard refresh).
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _refresh_body(script: str) -> str:
    start = script.index("async function refresh()")
    end = script.index("async function runCycle()", start)
    return script[start:end]


def test_run_cycle_button_uses_post_not_get(client):
    script = client.get("/ui/app.js").text
    # /api/cycle is registered as POST-only; the GET helper api() returns 405.
    assert 'await api("/api/cycle' not in script
    assert 'await post("/api/cycle?collect=true&max_clusters=8", {})' in script


def test_manual_package_controls_use_interpolated_ids(client):
    script = client.get("/ui/app.js").text
    # The manual-package block lives inside a template literal: an escaped
    # ``\${...}`` would put a literal "${esc(p.id)}" into the DOM id, so
    # getElementById() and the inline onclick handler could never find the row.
    assert "manual-platform-${esc(p.id)}" in script
    assert "manual-draft-result-${esc(p.id)}" in script
    assert "\\${" not in script


def test_no_single_dollar_foreach_on_lists(client):
    """$() returns one element; calling forEach on it aborts the whole refresh.

    Live defect 2026-11-04: the distribution-mode buttons used $(...) + forEach,
    which threw "$(...).forEach is not a function" and killed the dashboard
    refresh before the remaining cards were rendered.
    """
    import re

    script = client.get("/ui/app.js").text
    offenders = re.findall(r'(?<!\$)\$\s*\([^)]*\)\s*\.forEach\(', script)
    assert offenders == []


def test_review_board_is_wired_to_the_real_endpoints(client):
    html = client.get("/").text
    script = client.get("/ui/app.js").text
    assert 'data-view="review"' in html
    assert "review-board" in html
    # The board must call the API and reuse the existing action endpoints.
    assert "/api/review/queue" in script
    assert "/reconcile" in script and "/retry" in script and "/api/posts/reject" in script
    assert 'post("/api/posts/publish"' in script
    # Each visible action is defined, so no dead buttons are rendered.
    for action in ("confirm_published", "retry_after_check", "retry", "publish_now", "reject"):
        assert f'"{action}"' in script


def test_dashboard_refresh_degrades_gracefully(client):
    body = _refresh_body(client.get("/ui/app.js").text)
    assert "Promise.allSettled(" in body
    assert "Promise.all([" not in body
    # Failed sections are reported, a total outage still raises.
    assert 'failed.length === Object.keys(endpoints).length' in body
    assert '"status" in data' in body


def _requeue_body(script: str) -> str:
    start = script.index("async function requeueFailedBatch()")
    end = script.index("/* ---------------------------------------------------------------- wiring */", start)
    return script[start:end]


def test_bulk_requeue_button_is_wired_and_previews_first(client):
    """The board shows 365 failed posts and used to offer 365 clicks.

    Contract: one button, one bounded pass, and the preview call happens before
    anything can write — a button that POSTed `apply: true` straight away would
    restart 50 abandoned publications on a single click.
    """
    html = client.get("/").text
    script = client.get("/ui/app.js").text
    assert 'id="review-requeue"' in html
    body = _requeue_body(script)
    assert 'post("/api/posts/retry-failed", { apply: false, limit: REQUEUE_PASS })' in body
    assert "apply: true" in body
    assert "report.refused" in body, "a refused pass must be shown, not toasted as success"
    assert "REQUEUE_PASS = 50" in script
    assert "await loadReviewBoard(true)" in body


def test_bulk_requeue_demands_a_reason_before_it_asks(client):
    """Server-side, an empty reason is refused. The panel must not offer to write
    without one, or every click ends in an error toast and an operator who cannot
    tell what they did wrong."""
    body = _requeue_body(client.get("/ui/app.js").text)
    preview_call = body.index("apply: false")
    prompt_call = body.index("prompt(")
    apply_call = body.index("apply: true")
    assert preview_call < prompt_call < apply_call, "preview, then reason, then write"
    assert "if (!text)" in body
    assert "requeueSummary" in body


def test_bulk_requeue_names_the_80_refusal_in_the_ui(client):
    """A refusal the operator cannot read is a refusal they route around with SQL."""
    script = client.get("/ui/app.js").text
    assert "publish_outcome_unknown" in script
    labels = script[script.index("REQUEUE_REFUSAL_LABELS") :]
    assert "§80" in labels[: labels.index("};")]
    report = script[script.index("function requeueSummary") :]
    assert "report.remaining" in report, "a bounded pass must say how much is left"
    assert "report.scanned" in report and "report.eligible" in report


def test_bulk_requeue_summary_refuses_to_invent_an_age(client):
    """P34, applied to the button's own sentence.

    `Number(row.age_hours || 0)` is a finite zero, so a sample whose rows carry no
    age printed «самому старшему 0 дн» — a measurement that never happened, stated
    as a fact, in the one line the operator reads before agreeing to a write.
    """
    script = client.get("/ui/app.js").text
    start = script.index("function requeueSummary")
    summary = script[start : script.index("async function requeueFailedBatch()", start)]
    assert "Number(row.age_hours || 0)" not in summary
    assert 'typeof value === "number"' in summary
    assert "hrs(Math.max(...ages))" in summary, "the queue card already owns this formatter"
    assert '"нет отчёта"' in summary

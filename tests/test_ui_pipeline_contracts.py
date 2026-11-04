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


def test_dashboard_refresh_degrades_gracefully(client):
    body = _refresh_body(client.get("/ui/app.js").text)
    assert "Promise.allSettled(" in body
    assert "Promise.all([" not in body
    # Failed sections are reported, a total outage still raises.
    assert 'failed.length === Object.keys(endpoints).length' in body
    assert '"status" in data' in body

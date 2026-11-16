"""The analytics format rows the panel renders must also be styled.

``ui/app.js`` paints ``analytics-format-row`` and ``analytics-evidence-note``
in the measured-analytics workspace, but the shipped stylesheet never defined
them: the rows fell back to the generic ``analytics-row`` grid and the evidence
note was an unstyled paragraph. This pins the markup-to-style contract the same
way ``test_ui_coverage_panel.py`` pins the coverage statuses, and pins the
container context the responsive ``@container`` rule depends on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as test_client:
        yield test_client


def test_the_panel_renders_the_format_row_and_evidence_note(client):
    script = client.get("/ui/app.js").text
    assert "analytics-format-row" in script, "the format row markup disappeared"
    assert "analytics-evidence-note" in script, "the evidence note markup disappeared"


def test_the_rendered_classes_are_defined_in_the_shipped_stylesheet(client):
    css = client.get("/ui/app.css").text
    for cls in ("analytics-format-row", "analytics-evidence-note"):
        assert f".{cls}" in css, f"{cls} is rendered by app.js but not defined in app.css"


def test_the_format_table_is_a_container_so_the_responsive_rule_can_fire(client):
    css = client.get("/ui/app.css").text
    assert ".analytics-table { container-type:inline-size; }" in css, (
        "the @container rule for analytics-format-row has no container context"
    )
    assert "@container (max-width:560px)" in css, "the narrow-container fallback is missing"
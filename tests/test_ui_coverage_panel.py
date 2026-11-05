"""The coverage audit must be readable on the panel, not only in-process.

``ui/app.js`` fetches ``GET /api/metrics/coverage`` and paints one row per
platform. Two ways that block rots silently: a field renamed on either side of
the HTTP boundary, and a failed database read that paints zeros - and zeros look
like a quiet platform, which is the exact confusion ``s8_analytics/coverage.py``
exists to remove. Both are pinned here by driving the real endpoint and the real
script, the way ``test_ui_pipeline_contracts.py`` pins the pipeline contract.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from tgfarm.db.models import Channel, ChannelStat, CrosspostLog, Media, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import coverage as coverage_module
from tgfarm.s8_analytics.coverage import (
    STATUS_BLOCKED,
    STATUS_MEASURED,
    STATUS_PARTIAL,
    STATUS_UNMEASURED,
)

#: Children first for the wipe; parents first for the restore, because the
#: session schema turns ``PRAGMA foreign_keys=ON`` on. Same tuple the module's
#: own suite uses, so this file leaves the session-scoped fleet as it found it.
_ANALYTICS_TABLES = (
    PostStat.__table__,
    Media.__table__,
    ChannelStat.__table__,
    CrosspostLog.__table__,
    Post.__table__,
    Channel.__table__,
)


@pytest.fixture(autouse=True)
def _isolated_analytics_tables():
    with session_scope() as session:
        snapshot = {
            table.name: [dict(row) for row in session.execute(select(table)).mappings().all()]
            for table in _ANALYTICS_TABLES
        }
        for table in _ANALYTICS_TABLES:
            session.execute(delete(table))
    yield
    with session_scope() as session:
        for table in _ANALYTICS_TABLES:
            session.execute(delete(table))
        for table in reversed(_ANALYTICS_TABLES):
            rows = snapshot.get(table.name) or []
            if rows:
                session.execute(table.insert(), rows)


@pytest.fixture()
def client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    # /ui/ is a public prefix, /api/ is not: the one header covers both reads.
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as test_client:
        yield test_client


def _seed_platform(platform: str, *, published: int = 0, measured: int = 0) -> None:
    """``measured`` of the ``published`` posts get a snapshot with a real number."""
    suffix = uuid4().hex[:8]
    channel_id = f"covui_{platform}_{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title=platform, niche="news", platform=platform))
        for index in range(published):
            post = Post(
                public_id=f"covui{platform}{suffix}{index}",
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook=f"Заголовок публикации {index} для {platform} достаточной длины",
                body="Текст",
            )
            session.add(post)
            session.flush()
            if index < measured:
                session.add(PostStat(post_id=post.id, views=40, reactions=3))
                session.add(
                    CrosspostLog(
                        post_public_id=post.public_id,
                        platform=platform,
                        status="posted",
                        remote_id=f"remote-{index}",
                        url=f"https://example.com/remote-{index}",
                        created_at=datetime.now(UTC),
                    )
                )


def _renderer_body(script: str) -> str:
    start = script.index("function renderMetricsCoverage(")
    end = script.index("function renderMetricsFormats(", start)
    return script[start:end]


def _status_table(script: str) -> str:
    start = script.index("const COVERAGE_STATUS = {")
    end = script.index("function renderMetricsCoverage(", start)
    return script[start:end]


# --------------------------------------------------------------------- wiring


def test_panel_renders_the_coverage_block_on_the_analytics_tab(client):
    page = client.get("/").text
    assert 'data-view="overview"' in page
    assert 'id="metrics-coverage"' in page
    assert 'id="metrics-coverage-meta"' in page

    script = client.get("/ui/app.js").text
    assert "function renderMetricsCoverage(" in script
    # Fetched through the shared refresh() map, and dispatched the way every
    # other section is: only when the read actually came back.
    assert re.search(r'metricsCoverage:\s*"/api/metrics/coverage"', script)
    assert 'if ("metricsCoverage" in data) renderMetricsCoverage(data.metricsCoverage);' in script


def test_the_path_the_panel_fetches_is_a_route_the_api_registers(client):
    """A route rename must fail here, not show up as an empty card."""
    script = client.get("/ui/app.js").text
    path = re.search(r'metricsCoverage:\s*"([^"]+)"', script).group(1)
    get_routes = {
        route.path
        for route in client.app.routes
        if "GET" in getattr(route, "methods", set())
    }
    assert path in get_routes


# ------------------------------------------------------------------ shape lock


def test_every_field_the_panel_reads_exists_in_the_endpoint_response(client):
    """The shape the UI reads is the shape the API returns.

    Field names are scraped out of the renderer source rather than restated
    here, so renaming a key on either side of the boundary breaks the test
    instead of silently rendering ``undefined``.
    """
    _seed_platform("dzen", published=4, measured=2)
    _seed_platform("vk", published=3)

    body = _renderer_body(client.get("/ui/app.js").text)
    response = client.get("/api/metrics/coverage")
    assert response.status_code == 200
    payload = response.json()

    top_level = set(re.findall(r"\bdata\.([a-z_][a-z_]*)", body))
    totals_keys = set(re.findall(r"\btotals\.([a-z_][a-z_]*)", body))
    row_keys = set(re.findall(r"\b(?:row|r)\.([a-z_][a-z_]*)", body))

    # A rewrite to bracket access would make every set below empty and the
    # assertions vacuous, so the scrape itself is pinned.
    assert {"error", "platforms", "totals"} <= top_level
    assert {"published", "measured_posts", "measured_with_signal"} <= totals_keys
    assert {"published", "measured_posts", "measured_with_signal", "stat_rows",
            "status", "reason", "collector", "platform"} <= row_keys

    for key in top_level:
        assert key in payload, f"panel reads {key!r} at the top level; endpoint has no such key"
    for key in totals_keys:
        assert key in payload["totals"], f"panel reads totals.{key}; endpoint omits it"

    assert payload["platforms"], "seeded platforms vanished, row keys went untested"
    for name, row in payload["platforms"].items():
        for key in row_keys:
            assert key in row, f"panel reads {key!r} per platform; {name} row omits it"


def test_unrecognised_status_still_renders_a_label(client):
    """A new status upstream must show its own name, never a blank tag."""
    body = _renderer_body(client.get("/ui/app.js").text)
    assert "статус не распознан" in body


# ------------------------------------------------------------------- the error


def test_a_failed_read_surfaces_as_an_error_not_as_zeros(client, monkeypatch):
    """``error`` must reach the operator instead of becoming a row of zeros."""

    def boom(*args, **kwargs):
        raise OperationalError("select", {}, Exception("no such table: post_stats"))

    monkeypatch.setattr(coverage_module, "_platform_keys", boom)
    response = client.get("/api/metrics/coverage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"], "the read failed but said nothing"
    assert "post_stats" in payload["error"]
    assert payload["platforms"] == {}

    body = _renderer_body(client.get("/ui/app.js").text)
    assert "esc(data.error)" in body, "the error text is never shown"
    assert "alert-err" in body
    # Guard first: the totals of an empty report are zeros, and printing them
    # under a failed read is what this test exists to forbid.
    assert body.index("data.error") < body.index("data.totals"), (
        "totals are rendered before the error branch returns"
    )
    # The error path bails out, so no per-platform number can be printed at all.
    error_branch = body[: body.index("const rows = Object.values")]
    assert "return;" in error_branch


def test_the_four_statuses_are_distinguishable_by_class_and_word(client):
    """Four statuses, four classes, four labels: no two may share a look."""
    script = client.get("/ui/app.js").text
    css = client.get("/ui/app.css").text
    table = _status_table(script)

    statuses = (STATUS_BLOCKED, STATUS_UNMEASURED, STATUS_PARTIAL, STATUS_MEASURED)
    for status in statuses:
        assert re.search(rf"{status}:\s*\{{", table), f"{status} has no entry in the panel table"

    classes = re.findall(r'cls:\s*"([^"]+)"', table)
    labels = re.findall(r'label:\s*"([^"]+)"', table)
    assert len(classes) == len(statuses)
    assert len(set(classes)) == len(statuses), "two statuses share one colour"
    assert len(set(labels)) == len(statuses), "two statuses share one label"

    # Every class must already exist in the shipped stylesheet.
    for cls in classes:
        assert f".{cls}" in css, f"{cls} is not defined in app.css"


def test_blocked_and_unmeasured_are_not_painted_the_same(client):
    """The distinction the module exists for survives into the markup."""
    _seed_platform("vk", published=3)
    _seed_platform("telegram", published=5)

    payload = client.get("/api/metrics/coverage").json()
    by_status = {row["status"]: row["platform"] for row in payload["platforms"].values()}
    assert STATUS_BLOCKED in by_status, "a blocked collector must stay blocked"

    table = _status_table(client.get("/ui/app.js").text)
    blocked_cls = re.search(rf"{STATUS_BLOCKED}:\s*\{{\s*cls:\s*\"([^\"]+)\"", table).group(1)
    unmeasured_cls = re.search(
        rf"{STATUS_UNMEASURED}:\s*\{{\s*cls:\s*\"([^\"]+)\"", table
    ).group(1)
    assert blocked_cls != unmeasured_cls

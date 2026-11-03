from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tgfarm.s9_growth.telegraph_metrics import (
    TelegraphMetricsError,
    _page_path,
    build_article_metrics,
    parse_views_response,
)


def test_telegraph_page_path_requires_a_canonical_public_permalink():
    assert _page_path("Sample-Page-11-03", None) == "Sample-Page-11-03"
    assert _page_path(None, "https://telegra.ph/Sample-Page-11-03") == "Sample-Page-11-03"
    assert _page_path(None, "http://telegra.ph/Sample-Page-11-03") == ""
    assert _page_path(None, "https://telegra.ph.evil.example/Sample-Page-11-03") == ""
    assert _page_path(None, "https://example.com/Sample-Page-11-03") == ""


def test_parse_views_response_accepts_only_non_negative_integer_counts():
    assert parse_views_response({"ok": True, "result": {"views": 123}}) == 123
    with pytest.raises(TelegraphMetricsError):
        parse_views_response({"ok": False, "error": "PAGE_NOT_FOUND"})
    with pytest.raises(TelegraphMetricsError):
        parse_views_response({"ok": True, "result": {"views": -1}})
    with pytest.raises(TelegraphMetricsError):
        parse_views_response({"ok": True, "result": {"views": "not-a-number"}})


def test_article_report_uses_latest_two_snapshots_and_only_real_view_metrics():
    logs = [{
        "id": 7, "post_public_id": "post_telegraph_7", "status": "posted",
        "remote_id": "Example-Page-11-03", "url": "https://telegra.ph/Example-Page-11-03",
        "created_at": datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    }, {
        "id": 8, "post_public_id": "failed_post", "status": "failed",
        "remote_id": None, "url": None,
    }]
    snapshots = [{
        "id": 10, "crosspost_log_id": 7, "views": 145,
        "collected_at": datetime(2026, 11, 3, 8, 0, tzinfo=UTC),
    }, {
        "id": 9, "crosspost_log_id": 7, "views": 120,
        "collected_at": datetime(2026, 11, 2, 8, 0, tzinfo=UTC),
    }]
    rows = build_article_metrics(logs, snapshots)
    assert len(rows) == 1
    assert rows[0]["page_path"] == "Example-Page-11-03"
    assert rows[0]["current_views"] == 145
    assert rows[0]["previous_views"] == 120
    assert rows[0]["views_delta"] == 25
    assert rows[0]["metric_samples"] == 2

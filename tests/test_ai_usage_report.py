from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import delete

from tgfarm.db.models import AIUsage
from tgfarm.db.session import session_scope


def test_usage_report_surfaces_daily_budget_fallbacks_and_stale_reservations(monkeypatch):
    from tgfarm.llm import policy

    with session_scope() as session:
        session.execute(delete(AIUsage))
        session.add_all([
            AIUsage(
                stage="s4_draft", purpose="draft", provider="longcat",
                model="LongCat", charged=True, used=True, latency_ms=800,
                reason="used", created_at=datetime(2026, 10, 29, 11, 0, tzinfo=UTC),
            ),
            AIUsage(
                stage="s5_quality", purpose="quality", provider="offline",
                charged=False, used=False, latency_ms=0, reason="fallback:offline",
                created_at=datetime(2026, 10, 29, 10, 0, tzinfo=UTC),
            ),
            AIUsage(
                stage="s4_draft", purpose="draft", provider="longcat",
                charged=True, used=False, latency_ms=0, reason="reserved",
                created_at=datetime(2026, 10, 29, 11, 30, tzinfo=UTC),
            ),
            AIUsage(
                stage="s5_rewrite", purpose="rewrite", provider="longcat",
                charged=False, used=False, latency_ms=0, reason="budget exhausted (5/day)",
                created_at=datetime(2026, 10, 29, 11, 40, tzinfo=UTC),
            ),
            AIUsage(
                stage="s4_draft", purpose="draft", provider="openai",
                model="gpt-test", charged=True, used=True, latency_ms=400,
                reason="used", created_at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
            ),
        ])

    monkeypatch.setattr(policy, "get_settings", lambda: SimpleNamespace(ai_daily_budget=5))
    report = policy.usage_report(days=2, now=datetime(2026, 10, 29, 12, 0, tzinfo=UTC))

    assert report["budget_unit"] == "online_call_reservations"
    assert report["daily_budget"] == 5
    assert report["today"] == {
        "calls": 4,
        "charged": 2,
        "used": 1,
        "fallbacks": 1,
        "skipped": 1,
        "pending_reservations": 1,
        "stale_reservations": 1,
        "remaining": 3,
    }
    assert report["totals"]["calls"] == 5
    assert report["totals"]["charged"] == 3
    assert report["totals"]["used"] == 2
    assert report["totals"]["average_latency_ms"] == 600
    assert [item["date"] for item in report["daily"]] == ["2026-10-28", "2026-10-29"]
    assert {item["name"] for item in report["by_provider"]} == {"longcat", "offline", "openai"}
    assert {item["name"] for item in report["by_stage"]} == {"s4_draft", "s5_quality", "s5_rewrite"}


def test_usage_report_clamps_requested_period_and_handles_empty_history(monkeypatch):
    from tgfarm.llm import policy

    with session_scope() as session:
        session.execute(delete(AIUsage))

    monkeypatch.setattr(policy, "get_settings", lambda: SimpleNamespace(ai_daily_budget=0))
    report = policy.usage_report(days=999, now=datetime(2026, 10, 29, 12, 0, tzinfo=UTC))

    assert report["period_days"] == 30
    assert report["daily_budget"] == 0
    assert report["today"]["remaining"] == 0
    assert report["totals"]["calls"] == 0
    assert report["totals"]["average_latency_ms"] == 0
    assert report["daily"] == []


def test_ai_usage_api_requires_token_and_returns_safe_report(monkeypatch):
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import HEADER, ensure_token
    from tgfarm.llm import policy

    monkeypatch.setattr(
        policy,
        "usage_report",
        lambda days: {
            "period_days": days,
            "budget_unit": "online_call_reservations",
            "daily_budget": 5,
            "today": {"charged": 2, "remaining": 3},
            "totals": {"calls": 8},
            "by_provider": [{"name": "longcat", "calls": 8}],
            "by_stage": [],
            "daily": [],
        },
    )
    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        assert client.get("/api/ai/usage").status_code == 401
        response = client.get(
            "/api/ai/usage?days=14",
            headers={HEADER: ensure_token()},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period_days"] == 14
    assert payload["today"]["remaining"] == 3
    assert "prompt" not in repr(payload).lower()
    assert "secret" not in repr(payload).lower()

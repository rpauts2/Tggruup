"""The reconciliation board must be honest about what needs a human (P3/P4).

State at the time of writing: 51 posts with an unknown publication outcome, 365
terminal failures after the Dzen DNS outage, and 41 gated posts. The panel had no
single screen for this, so the operator could only discover it through alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.review import BUCKET_ACTIONS, review_queue


def _seed(status: str, platform: str = "dzen", *, error: str | None = None, provider: str = "offline", age_hours: float = 5.0) -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"review_{platform}_{suffix}"
    public_id = f"review{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Сверка", niche="tech", platform=platform)
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status=status,
                hook="Пост для сверки",
                body="Текст",
                attempts=7,
                publish_error=error,
                generation_provider=provider,
                created_at=datetime.now(UTC) - timedelta(hours=age_hours),
                updated_at=datetime.now(UTC) - timedelta(hours=age_hours),
            )
        )
    return public_id


def test_buckets_and_actions(monkeypatch):
    from tgfarm import review

    unknown = _seed("publish_unknown", error="driver died")
    terminal = _seed("failed", error="expired: never submitted", age_hours=30)
    held = _seed("gated", error=None, age_hours=1)
    published = _seed("published", age_hours=100)

    monkeypatch.setattr(
        review,
        "get_settings",
        lambda: SimpleNamespace(approval_required=False, dzen_requires_llm=True),
    )
    board = review_queue(limit=50)
    by_bucket = {name: [item["public_id"] for item in items] for name, items in board["buckets"].items()}

    assert unknown in by_bucket["unknown"]
    assert terminal in by_bucket["terminal"]
    assert held in by_bucket["held"]
    assert published not in sum(by_bucket.values(), [])

    assert board["counts"]["unknown"] >= 1
    assert board["total"] == sum(board["counts"].values())

    held_item = next(item for item in board["buckets"]["held"] if item["public_id"] == held)
    assert "DZEN_REQUIRES_LLM" in held_item["reason"]
    assert held_item["actions"] == list(BUCKET_ACTIONS["held"])

    unknown_item = next(item for item in board["buckets"]["unknown"] if item["public_id"] == unknown)
    assert "проверь площадку" in unknown_item["reason"].lower()
    assert "retry_after_check" in unknown_item["actions"]


def test_approval_mode_reason_wins(monkeypatch):
    from tgfarm import review

    held = _seed("gated", platform="vk", provider="compatible")
    monkeypatch.setattr(
        review, "get_settings", lambda: SimpleNamespace(approval_required=True, dzen_requires_llm=True)
    )
    board = review_queue(limit=50)
    item = next(entry for entry in board["buckets"]["held"] if entry["public_id"] == held)
    assert "APPROVAL_REQUIRED" in item["reason"]


def test_oldest_rot_first_in_unknown_and_held(monkeypatch):
    from tgfarm import review

    older = _seed("publish_unknown", error="x", age_hours=50)
    newer = _seed("publish_unknown", error="y", age_hours=2)
    monkeypatch.setattr(
        review, "get_settings", lambda: SimpleNamespace(approval_required=False, dzen_requires_llm=False)
    )
    board = review_queue(limit=200)
    ids = [item["public_id"] for item in board["buckets"]["unknown"]]
    assert ids.index(older) < ids.index(newer)


def test_review_queue_api_contract(monkeypatch):
    from tgfarm import review
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    _seed("failed", error="terminal", age_hours=10)
    monkeypatch.setattr(
        review, "get_settings", lambda: SimpleNamespace(approval_required=False, dzen_requires_llm=False)
    )
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.get("/api/review/queue", params={"limit": 5})
        assert response.status_code == 200
        body = response.json()
        assert set(body["buckets"]) == {"unknown", "terminal", "held"}
        assert body["counts"]["terminal"] >= 1
        assert all(len(items) <= 5 for items in body["buckets"].values())

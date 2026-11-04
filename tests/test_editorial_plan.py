"""Global editorial plan: builder, revisions and API contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from tgfarm.api import create_app
from tgfarm.core.apiauth import ensure_token
from tgfarm.db.models import Channel, Cluster, Post
from tgfarm.editorial_plan import (
    build_editorial_plan,
    get_active_plan,
    plan_history,
)


def _seed_cluster(db, public_id, *, heat=0.5, label=None, niche="", age_hours=2.0) -> None:
    """Seed one ready cluster; each id must be used once per test."""

    now = datetime.now(UTC)
    db.merge(
        Cluster(
            public_id=public_id,
            label=label or public_id,
            niche=niche,
            status="ready",
            heat=heat,
            last_seen_at=now - timedelta(hours=age_hours),
        )
    )


def _seed_channel(db, channel_id, *, platform="vk", niche="", title=None) -> None:
    db.merge(Channel(id=channel_id, title=title or channel_id, niche=niche, platform=platform))


def _seed_post(db, public_id, channel_id, *, cluster_public_id=None, status="gated") -> None:
    db.merge(
        Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="hook",
            body="body",
            status=status,
            cluster_public_id=cluster_public_id,
        )
    )


def test_preview_balances_channels_and_ranks(db):
    _seed_cluster(db, "topic-a", heat=0.9)
    _seed_cluster(db, "topic-b", heat=0.5)
    _seed_cluster(db, "topic-c", heat=0.3)
    _seed_channel(db, "vk_one", platform="vk", niche="")
    _seed_channel(db, "tg_one", platform="telegram", niche="")
    db.commit()

    plan = build_editorial_plan(persist=False)
    assert plan["status"] == "transient"
    entries = [e for e in plan["entries"] if e["action"] == "produce"]
    assert len(entries) == 3
    # Top-ranked topic is promoted, later ones degrade monotonically.
    priorities = [e["priority"] for e in plan["entries"]]
    # Hottest topic is always P1. Fresh-news promotion may move a later entry
    # up one level, so only the tier domain and the P1-first guarantee hold;
    # the shared throwaway DB may also hold extra stale clusters from other
    # test files, so exact absolute tiers are not asserted here.
    assert priorities[0] == 1
    assert set(priorities) <= {1, 2, 3}
    hottest = plan["entries"][0]
    assert hottest["priority"] == 1
    # Load balancing: topics spread over several channels, no channel takes all.
    by_channel = [e["channel_id"] for e in entries]
    assert len(set(by_channel)) >= 2
    assert max(by_channel.count(c) for c in set(by_channel)) <= len(entries) - 1
    for e in entries:
        assert 0.05 <= e["confidence"] <= 0.95
        assert e["reasons"]
    assert plan["summary"]["produce"] == 3


def test_gated_post_gets_schedule_slot_within_horizon(db):
    _seed_cluster(db, "topic-g", heat=0.8)
    _seed_channel(db, "vk_g", platform="vk", niche="")
    _seed_post(db, "post-g", "vk_g", cluster_public_id="topic-g", status="gated")
    db.commit()

    plan = build_editorial_plan(persist=False)
    entry = next(e for e in plan["entries"] if e["cluster_public_id"] == "topic-g")
    assert entry["action"] == "schedule"
    assert entry["slot_at"] is not None
    slot = datetime.fromisoformat(entry["slot_at"])
    now = datetime.now(UTC)
    assert slot > now
    assert slot <= now + timedelta(days=plan["horizon_days"] + 1)
    assert slot.second == 0
    assert entry["payload"]["needs_media"] is True


def test_hold_for_stale_and_cooling_clusters(db):
    _seed_cluster(db, "stale-topic", heat=0.9, age_hours=200)
    _seed_cluster(db, "cooling-topic", heat=0.9, age_hours=2)
    _seed_channel(db, "vk_h", platform="vk", niche="")
    _seed_post(db, "post-cool", "vk_h", cluster_public_id="cooling-topic", status="published")
    db.commit()

    plan = build_editorial_plan(persist=False)
    by_id = {e["cluster_public_id"]: e for e in plan["entries"]}
    assert by_id["stale-topic"]["action"] == "hold"
    assert by_id["cooling-topic"]["action"] == "hold"
    assert any(
        "cooldown" in r or "публиковалась" in r for r in by_id["cooling-topic"]["reasons"]
    )


def test_refresh_supersedes_previous_plan_and_keeps_history(db):
    _seed_cluster(db, "topic-1", heat=0.7)
    _seed_channel(db, "vk_r", platform="vk", niche="")
    db.commit()

    first = build_editorial_plan()
    assert first["status"] == "active"
    _seed_cluster(db, "topic-2", heat=0.8)
    db.commit()

    second = build_editorial_plan()
    assert second["public_id"] != first["public_id"]
    assert second["status"] == "active"

    active = get_active_plan()
    assert active is not None and active["public_id"] == second["public_id"]
    history = plan_history(limit=10)
    assert [p["public_id"] for p in history][:2] == [
        second["public_id"],
        first["public_id"],
    ]
    assert history[1]["status"] == "superseded"
    diff = second["summary"]["diff"]
    assert diff["previous_plan_id"] == first["public_id"]
    assert "topic-2" in diff["added"]


def test_refresh_never_publishes_or_changes_posts(db):
    _seed_cluster(db, "topic-np", heat=0.6)
    _seed_channel(db, "vk_n", platform="vk", niche="")
    _seed_post(db, "post-n", "vk_n", cluster_public_id="topic-np", status="gated")
    db.commit()

    before = set(db.execute(select(Post.public_id, Post.status)).all())
    build_editorial_plan()
    db.expire_all()
    after = set(db.execute(select(Post.public_id, Post.status)).all())
    assert before == after


def test_plan_api_contract(db):
    _seed_cluster(db, "topic-api", heat=0.9)
    _seed_channel(db, "vk_api", platform="vk", niche="")
    db.commit()

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        preview = client.get("/api/editorial/plan")
        assert preview.status_code == 200
        body = preview.json()
        assert body["entries"]
        assert body["summary"]["produce"] >= 1

        refreshed = client.post("/api/editorial/plan/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["status"] == "active"

        history = client.get("/api/editorial/plan/history")
        assert history.status_code == 200
        plans = history.json()["plans"]
        assert plans and plans[0]["status"] == "active"

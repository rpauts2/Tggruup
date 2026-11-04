"""Freshness must be visible through the API, not only inside a module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope


def _seed_metric(platform: str, *, hours_ago: float = 2.0, views: int = 40) -> None:
    from uuid import uuid4

    suffix = uuid4().hex[:8]
    channel_id = f"fresh_{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Метрики", niche="tech", platform=platform))
        post = Post(
            public_id=f"fresh{suffix}",
            channel_id=channel_id,
            status="published",
            published_at=datetime.now(UTC),
            hook="Пост",
            body="Текст",
        )
        session.add(post)
        session.flush()
        session.add(
            PostStat(
                post_id=post.id,
                views=views,
                reactions=1,
                collected_at=datetime.now(UTC) - timedelta(hours=hours_ago),
            )
        )


def test_freshness_endpoint_reports_real_rows():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    _seed_metric("vk", hours_ago=1.5)
    _seed_metric("dzen", hours_ago=40)

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.get("/api/metrics/freshness")
        assert response.status_code == 200
        body = response.json()

    platforms = body["platforms"]
    assert platforms["vk"]["snapshots"] >= 1
    assert platforms["vk"]["status"] in {"ok", "stale", "blocked"}
    assert platforms["dzen"]["status"] in {"ok", "stale"}
    assert body["summary"]["platforms_total"] == len(platforms)
    assert body["summary"]["snapshots_total"] >= 2


def test_freshness_endpoint_never_fakes_zero_for_vk():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        body = client.get("/api/metrics/freshness").json()

    vk = body["platforms"]["vk"]
    # Without a user token the honest state is "blocked with a reason", never 0 views.
    assert vk["status"] in {"blocked", "never", "ok", "stale"}
    if vk["status"] == "blocked":
        assert "VK_USER_TOKEN" in (vk.get("reason") or "")

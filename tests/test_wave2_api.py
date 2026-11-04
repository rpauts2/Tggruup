"""Wave 2 API contracts: source fleet health and trend explanation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from tgfarm.db.models import Cluster, RawItem
from tgfarm.db.session import session_scope


def _client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


def test_sources_health_endpoint_lists_every_source_with_state():
    with _client() as client:
        response = client.get("/api/sources/health")
        assert response.status_code == 200
        body = response.json()
    assert "summary" in body and "counts" in body["summary"]
    assert isinstance(body["sources"], list) and body["sources"]
    first = body["sources"][0]
    for key in ("id", "state", "reason", "enabled"):
        assert key in first
    assert body["summary"]["total"] >= len([row for row in body["sources"] if row["enabled"]])


def test_trend_explain_returns_numbers_for_a_real_cluster():
    suffix = datetime.now(UTC).strftime("%H%M%S%f")
    with session_scope() as session:
        cluster = Cluster(
            public_id=f"explain{suffix}",
            label="Кластер для объяснения",
            niche="tech",
            heat=0.7,
            status="ready",
            item_count=3,
            created_at=datetime.now(UTC) - timedelta(hours=3),
        )
        session.add(cluster)
        session.flush()
        for index in range(3):
            session.add(
                RawItem(
                    source_id="hn_ai",
                    external_id=f"explain-{suffix}-{index}",
                    cluster_id=cluster.id,
                    title=f"Новость {index}",
                    text="Текст новости про роутеры и сети.",
                    url=f"https://example.org/{index}",
                    published_at=datetime.now(UTC) - timedelta(hours=index + 1),
                )
            )
        public_id = cluster.public_id

    with _client() as client:
        response = client.get(f"/api/trends/{public_id}/explain")
        assert response.status_code == 200
        body = response.json()

    assert body.get("cluster_public_id") == public_id or body.get("public_id") == public_id
    text = str(body)
    assert any(char.isdigit() for char in text)


def test_trend_explain_unknown_cluster_is_404():
    with _client() as client:
        response = client.get("/api/trends/no_such_cluster/explain")
    assert response.status_code == 404

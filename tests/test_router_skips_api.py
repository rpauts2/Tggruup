"""GET /api/router/skips — the router's skip report, with its memory limits said out loud.

S3 already recorded *why* a destination left a cluster's plan (`SkippedDestination`
in an in-process LRU registry) and its own docstring promised the report "for
API/panel" — but nothing exposed it, so a silently absent platform stayed
absent. These tests pin the endpoint and, more importantly, its honesty: an
empty answer must never read as "nothing was skipped" when the cluster is simply
no longer remembered after a restart or an LRU eviction.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import tgfarm.s3_router as router
from tgfarm.config import Persona
from tgfarm.db.models import Cluster, RawItem
from tgfarm.db.session import session_scope
from tgfarm.s3_router import SKIP_REGISTRY_LIMIT, SkippedDestination


def _client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


def _suffix() -> str:
    return datetime.now(UTC).strftime("%H%M%S%f")


@pytest.fixture()
def clean_registry():
    """The registry is process state: hand each test an empty one and restore.

    Without this, a cluster routed by an unrelated test could answer 200 here
    and the "no record" contract would be unverifiable.
    """
    registry = router._SKIP_REGISTRY  # noqa: SLF001 - module state is the subject
    saved = dict(registry)
    registry.clear()
    yield registry
    registry.clear()
    registry.update(saved)


def _seed_cluster(public_id: str, label: str = "Кластер для пропусков") -> str:
    """A routable cluster needs a row plus at least one member."""
    with session_scope() as session:
        cluster = Cluster(
            public_id=public_id,
            label=label,
            niche="tech",
            heat=0.8,
            status="ready",
            item_count=1,
        )
        session.add(cluster)
        session.flush()
        session.add(
            RawItem(
                source_id="hn_ai",
                external_id=f"skip-{public_id}",
                url=f"https://example.invalid/{public_id}",
                title=label,
                text="Модель решает задачи быстрее предыдущей версии.",
                niche_hint=["tech"],
                source_weight=0.7,
                cluster_id=cluster.id,
            )
        )
    return public_id


def _skip(cluster_public_id: str, channel_id: str, **overrides) -> SkippedDestination:
    payload = {
        "cluster_public_id": cluster_public_id,
        "channel_id": channel_id,
        "platform": "no_such_platform",
        "capability": "unsupported",
        "capability_label": "Не поддерживается",
        "reason": "Не поддерживается: Рабочего сценария нет: ни адаптера, ни ручного пакета.",
    }
    payload.update(overrides)
    return SkippedDestination(**payload)


# ------------------------------------------------------------------- contract
def test_cluster_absent_from_db_and_memory_is_a_404(clean_registry):
    """Nothing known anywhere: that is an error, not an empty plan."""
    public_id = f"sk{int(datetime.now(UTC).timestamp()) % 10**10:010d}"
    with _client() as client:
        response = client.get(f"/api/router/skips/{public_id}")
    assert response.status_code == 404
    assert "Нет записи" in response.json()["detail"]


@pytest.mark.parametrize(
    "raw_id",
    ["cl ids with spaces", "x" * 40, "кластер-не-ascii", "semi;colon"],
)
def test_malformed_cluster_id_is_rejected(raw_id, clean_registry):
    with _client() as client:
        response = client.get(f"/api/router/skips/{raw_id}")
    assert response.status_code == 400


@pytest.mark.parametrize("raw_id", ["../../etc/passwd", "<script>x</script>", ""])
def test_path_like_ids_never_reach_the_registry(raw_id, clean_registry):
    """Starlette normalises these away or the id check refuses them.

    Either way the operator gets a rejection, never a report built from a
    traversal path or a markup fragment.
    """
    with _client() as client:
        response = client.get(f"/api/router/skips/{raw_id}")
    assert response.status_code in (400, 404)
    assert "skipped" not in response.text


def test_recorded_skips_come_back_with_destination_and_reason(clean_registry):
    public_id = _seed_cluster(f"skips{_suffix()}")
    clean_registry[public_id] = (_skip(public_id, "ghost_chan"),)

    with _client() as client:
        body = client.get(f"/api/router/skips/{public_id}").json()

    assert body["cluster_public_id"] == public_id
    assert body["count"] == 1 == len(body["skipped"])
    row = body["skipped"][0]
    assert row["channel_id"] == "ghost_chan"
    assert row["platform"] == "no_such_platform"
    assert row["capability"] == "unsupported"
    assert row["capability_label"] == "Не поддерживается"
    assert "Рабочего сценария нет" in row["reason"]
    # `note()` is the destination+reason line the panel prints.
    assert row["note"] == "ghost_chan (no_such_platform): " + row["reason"]
    assert body["state"] == "skipped_recorded"
    assert body["remembered"] is True


def test_memory_metadata_is_stated_not_implied(clean_registry):
    public_id = _seed_cluster(f"skips{_suffix()}")
    clean_registry[public_id] = (_skip(public_id, "ghost_chan"),)

    with _client() as client:
        body = client.get(f"/api/router/skips/{public_id}").json()

    assert body["storage"] == "in-process-memory"
    assert body["restart_clears"] is True
    assert body["eviction"] == "lru"
    assert body["registry_limit"] == SKIP_REGISTRY_LIMIT


# --------------------------------------------------------------- two empties
def test_cluster_not_routed_here_is_not_reported_as_clean(clean_registry):
    """The cluster exists but this process never routed it.

    `route_skips` returns `[]` for that *and* for "routed, nothing dropped", so
    the endpoint has to say which of the two it is — otherwise the operator
    reads "all destinations planned" after a restart that wiped the registry.
    """
    public_id = _seed_cluster(f"skips{_suffix()}")
    assert router.route_skips(public_id) == []

    with _client() as client:
        response = client.get(f"/api/router/skips/{public_id}")
        assert response.status_code == 200
        body = response.json()

    assert body["count"] == 0
    assert body["remembered"] is False
    assert body["cluster_in_db"] is True
    assert body["state"] == "no_record"
    # The wording must refuse the wrong inference out loud.
    assert "НЕ значит" in body["state_note"]


def test_routed_with_nothing_skipped_reads_as_a_real_clean_plan(clean_registry):
    """`_remember_skips(cid, [])` is what a clean route writes: a true record."""
    public_id = _seed_cluster(f"skips{_suffix()}")
    router._remember_skips(public_id, [])  # noqa: SLF001 - the router's own entry point

    with _client() as client:
        body = client.get(f"/api/router/skips/{public_id}").json()

    assert body["count"] == 0
    assert body["remembered"] is True
    assert body["state"] == "routed_without_skips"
    assert "ни одна площадка не была исключена" in body["state_note"]


# ------------------------------------------------------------------ no fabric
def test_reason_is_never_replaced_by_a_known_label(clean_registry):
    """An unknown capability code is reported as stored, label and all.

    The registry holds whatever the router wrote. The endpoint must not reach
    into the capability catalogue to "helpfully" describe a code it does know,
    because that would put words in the router's mouth.
    """
    public_id = _seed_cluster(f"skips{_suffix()}")
    clean_registry[public_id] = (
        _skip(
            public_id,
            "future_chan",
            platform="brand_new_network",
            capability="brand_new_level",
            capability_label="",
            reason="custom router verdict",
        ),
    )

    with _client() as client:
        body = client.get(f"/api/router/skips/{public_id}").json()

    row = body["skipped"][0]
    assert row["capability"] == "brand_new_level"
    assert row["capability_label"] == ""
    assert row["reason"] == "custom router verdict"
    assert row["note"] == "future_chan (brand_new_network): custom router verdict"
    assert body["count"] == 1
    text = repr(body)
    for invented in ("Не поддерживается", "Только чтение", "Рабочего сценария нет"):
        assert invented not in text


def test_unknown_cluster_id_never_yields_someone_elses_skips(clean_registry):
    public_id = _seed_cluster(f"skips{_suffix()}")
    other = f"skips{_suffix()}x"
    clean_registry[other] = (_skip(other, "ghost_chan"),)

    with _client() as client:
        body = client.get(f"/api/router/skips/{public_id}").json()

    assert body["skipped"] == []
    assert body["state"] == "no_record"


def test_skip_report_is_not_public(clean_registry):
    """The report exposes internal routing decisions: it stays behind the token."""
    from tgfarm.api import create_app

    public_id = _seed_cluster(f"skips{_suffix()}")
    clean_registry[public_id] = (_skip(public_id, "ghost_chan"),)

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/router/skips/{public_id}")

    assert response.status_code == 401
    assert "ghost_chan" not in response.text


# ------------------------------------------------------- the real router path
@dataclass
class _StubConfig:
    """Only the parts of `Config` that `plan_cluster` reads."""

    personas: list[Persona] = field(default_factory=list)

    def by_platform(self, platform: str) -> list[Persona]:
        return [p for p in self.personas if p.platform == platform and p.enabled]

    @property
    def catch_all_personas(self) -> list[Persona]:
        return [p for p in self.personas if p.enabled and p.catch_all]


@pytest.mark.asyncio
async def test_endpoint_serves_what_the_router_actually_recorded(monkeypatch, clean_registry):
    """End to end, offline: plan_cluster writes the report, the API reads it back."""
    public_id = _seed_cluster(f"skips{_suffix()}")
    personas = [
        Persona(
            id="zen_ok", title="zen_ok", niche="general", platform="dzen",
            catch_all=True, angle_weights={"news": 1.0},
        ),
        Persona(
            id="ghost_chan", title="ghost_chan", niche="general", platform="no_such_platform",
            catch_all=True, angle_weights={"news": 1.0},
        ),
    ]
    monkeypatch.setattr(router, "get_config", lambda: _StubConfig(personas))

    plans = await router.plan_cluster(public_id, rng=random.Random(7))

    assert [plan.channel_id for plan in plans] == ["zen_ok"]
    recorded = router.route_skips(public_id)
    assert [item.channel_id for item in recorded] == ["ghost_chan"]

    with _client() as client:
        body = client.get(f"/api/router/skips/{public_id}").json()

    assert body["remembered"] is True
    assert body["count"] == 1
    row = body["skipped"][0]
    assert row == {
        "cluster_public_id": public_id,
        "channel_id": "ghost_chan",
        "platform": "no_such_platform",
        "capability": "unsupported",
        "capability_label": "Не поддерживается",
        "reason": recorded[0].reason,
        "note": recorded[0].note(),
    }

"""The platform matrix must state what the code really does (mission §10).

These tests are the guard against the classic failure of this project: a green
"publishing" badge for a destination that only produces a manual package.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tgfarm.platform_capability import (
    CAPABILITY_LABELS,
    LEVELS,
    capability_counts,
    capability_matrix,
    capability_of,
    consent_gated_ids,
    describe,
    is_at_least,
    publish_ids,
)
from tgfarm.platforms import CATALOG


def _catalog_ids() -> list[str]:
    return [str(item["id"]) for item in CATALOG]


def test_every_catalogue_destination_has_a_level():
    matrix = capability_matrix()
    assert set(matrix) == set(_catalog_ids())
    for row in matrix.values():
        assert row["capability"] in LEVELS
        assert row["capability_label"] == CAPABILITY_LABELS[row["capability"]]


def test_publish_level_requires_a_real_publisher():
    adapters = {str(x) for x in publish_ids()} - {"telegram", "vk", "dzen"}
    for platform_id in _catalog_ids():
        level = capability_of(platform_id)
        if level != "publish":
            continue
        assert platform_id in publish_ids(), platform_id
        # publish implies either a crosspost adapter or a primary publisher
        assert platform_id in adapters or platform_id in {"telegram", "vk", "dzen"}


def test_planned_destinations_are_never_labelled_as_publishing():
    planned = {str(item["id"]) for item in CATALOG if item["mode"] != "built_in"}
    for platform_id in planned:
        assert capability_of(platform_id) in {"manual_upload", "draft_only", "unsupported"}


def test_vc_ru_is_draft_only_and_vk_clips_is_manual_upload():
    assert capability_of("vc_ru") == "draft_only"
    assert is_at_least("vc_ru", "draft_only") is True
    assert describe("vc_ru")["auto_publish"] is False
    assert capability_of("vk_clips") == "manual_upload"
    assert describe("vk_clips")["supports_manual_package"] is True
    assert describe("vk_clips")["auto_publish"] is False


def test_core_channels_publish_automatically():
    for platform_id in ("telegram", "vk", "dzen", "pikabu", "reddit"):
        assert describe(platform_id)["auto_publish"] is True, platform_id


def test_counts_add_up_and_match_matrix():
    counts = capability_counts()
    assert sum(counts.values()) == len(CATALOG)
    matrix = capability_matrix()
    for level in LEVELS:
        assert counts[level] == sum(1 for row in matrix.values() if row["capability"] == level)


def test_unknown_destination_is_unsupported():
    assert capability_of("no_such_platform") == "unsupported"
    assert describe("")["auto_publish"] is False


def test_is_at_least_rejects_unknown_level():
    with pytest.raises(ValueError):
        is_at_least("vk", "teleport")


def test_capability_cli_prints_the_documented_table(capsys):
    from tgfarm.cli import cmd_capability

    class Args:
        format = "markdown"

    assert cmd_capability(Args()) == 0
    out = capsys.readouterr().out
    assert out.startswith("| Площадка | Уровень | Что делает TgFarm |")
    assert "| vc_ru | draft_only |" in out
    assert "| vk_clips | manual_upload |" in out
    assert '"publish"' in out  # counts are printed as JSON at the end


def test_platforms_api_exposes_capability_without_overstating():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.get("/api/platforms")
        assert response.status_code == 200
        body = response.json()
        rows = body["platforms"] if isinstance(body, dict) else body
        by_id = {str(row["id"]): row for row in rows}
        assert by_id["vk"]["capability"] == "publish"
        assert by_id["vc_ru"]["capability"] == "draft_only"
        assert by_id["vk_clips"]["capability"] == "manual_upload"
        # A destination without a publisher must not claim automatic delivery.
        for row in rows:
            if row["capability"] != "publish":
                assert row.get("automation_mode") != "automatic", row["id"]


# ---------------------------------------------------------------- consent gate


def test_consent_gated_destinations_are_demoted_despite_an_adapter():
    """TikTok and Douyin have registered adapters whose ``publish()`` always
    returns ``skipped``: a registry entry is not a publisher, and the matrix must
    not hand them a green badge (or let the router plan them as automatic)."""
    from tgfarm.platform_capability import consent_gated_ids
    from tgfarm.s9_growth.adapters import ADAPTERS

    gated = consent_gated_ids()
    assert {"tiktok", "douyin"} <= gated
    # They really are in the adapter registry — the demotion is not accidental.
    assert gated & set(ADAPTERS)
    for platform_id in ("tiktok", "douyin"):
        assert platform_id in ADAPTERS
        assert capability_of(platform_id) == "manual_upload", platform_id
        row = describe(platform_id)
        assert row["auto_publish"] is False
        assert row["supports_manual_package"] is True


def test_consent_gate_is_a_floor_not_a_catalogue_note():
    """The gate is a code fact (those two ``publish()`` methods return ``skipped``
    unconditionally), so it is unioned with the catalogue note rather than taken
    from it: editing the wording in ``platforms.py`` must not re-promote anyone."""
    from tgfarm import platform_capability
    from tgfarm.platforms import MANUAL_ONLY_AUTOMATION

    assert platform_capability._CONSENT_GATED_DEFAULT <= consent_gated_ids()
    assert set(MANUAL_ONLY_AUTOMATION) <= consent_gated_ids()


def test_consent_gate_does_not_touch_real_publishers():
    """The gate is a deny-list, not a blanket downgrade: automatic crosspost
    destinations keep publishing, and no automatic destination is consent-gated."""
    from tgfarm.platform_capability import consent_gated_ids

    gated = consent_gated_ids()
    for platform_id in ("pikabu", "reddit", "bluesky", "mastodon", "threads"):
        assert capability_of(platform_id) == "publish", platform_id
        assert platform_id not in gated
    assert gated.isdisjoint({"telegram", "vk", "dzen"})


def test_router_never_plans_a_consent_gated_destination_as_unmarked():
    """``s3_router`` reads the same capability level, so a consent-gated
    destination must be flagged as requiring manual action when planned."""
    from tgfarm.s3_router import MANUAL_CAPABILITIES, _capability_level

    for platform_id in ("tiktok", "douyin"):
        level = _capability_level(platform_id)
        assert level in MANUAL_CAPABILITIES, level

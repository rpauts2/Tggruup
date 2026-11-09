"""Every catalogue destination must be honestly covered by something real.

The catalogue, the editorial profiles and the manual-package registry are three
separate tables maintained by hand, and nothing forced them to agree. The
failure this file exists to prevent is the quiet one: a destination is added to
the catalogue with a plausible description, the panel lists it, and the farm has
no way to deliver to it at all — no publisher, no package, no draft flow. The
operator reads a card and concludes the farm covers thirty-two destinations when
it covers twenty-one.

The invariant is one sentence, and it is checked over the whole catalogue rather
than over a hand-written list of ids:

    every destination is *either* auto-published *or* backed by a manual
    package *or* a draft-only flow, and every destination has an editorial
    profile.

`tiktok` is the case that made this file necessary: the consent gate correctly
demoted it to `manual_upload`, and `MANUAL_DESTINATIONS` had no entry for it, so
the level named a package that did not exist. `douyin` — the same shape of
platform, the same consent gate — did have one, which is exactly why the gap
survived review.

Read the registries as data, not through the API: a test that goes through
`capability_of` would pass on a wrong level as long as the level and the
registry were wrong together.
"""

from __future__ import annotations

import pytest

from tgfarm.manual_drafts import MANUAL_DESTINATIONS, build_manual_package
from tgfarm.platform_capability import capability_of, consent_gated_ids
from tgfarm.platform_editorial import PROFILES
from tgfarm.platforms import CATALOG

CATALOGUE_IDS = tuple(str(item["id"]) for item in CATALOG)

#: Levels that promise the operator something they can act on by hand.
HANDOFF_LEVELS = frozenset({"manual_upload", "draft_only"})


def _post() -> dict:
    return {
        "id": "completeness-probe",
        "hook": "Заголовок для проверки ручного пакета.",
        "body": "Тело поста, достаточное для сборки ручного пакета публикации.",
        "hashtags": ["проверка"],
        "sources": [{"title": "Источник", "url": "https://example.invalid/source"}],
        "media": [{"sha": "abc123", "kind": "video"}],
    }


def test_every_catalogue_destination_has_an_editorial_profile():
    """`platform_editorial.PROFILES` is what the panel and the native rewrite use;
    a destination without one cannot produce a native version at all."""
    missing = sorted(set(CATALOGUE_IDS) - set(PROFILES))
    assert missing == [], f"no editorial profile for: {missing}"


def test_no_editorial_profile_describes_a_destination_outside_the_catalogue():
    """The other direction: a profile for a destination nobody can enable is a
    card that will never appear, and it hides the fact that the id is wrong."""
    orphaned = sorted(set(PROFILES) - set(CATALOGUE_IDS))
    assert orphaned == [], f"profiles without a catalogue entry: {orphaned}"


@pytest.mark.parametrize("platform_id", CATALOGUE_IDS)
def test_every_destination_is_auto_published_or_backed_by_a_handoff(platform_id):
    """The invariant itself, checked per destination so a failure names the id.

    A `publish` destination is delivered by the farm. Anything else promises the
    operator a handoff, and that promise is only real when the registry holds an
    entry the package builder can actually serve.
    """
    level = capability_of(platform_id)
    if level == "publish":
        return
    assert level in HANDOFF_LEVELS, f"{platform_id}: unexpected level {level}"
    assert platform_id in MANUAL_DESTINATIONS, (
        f"{platform_id} is reported as {level} but has no manual package — "
        "the panel would name a handoff that does not exist"
    )
    package = build_manual_package(platform_id, _post())
    assert package["ok"] is True
    assert package["manual_only"] is True
    assert package["publish_action"] == "none", "a handoff never publishes"


@pytest.mark.parametrize("platform_id", sorted(MANUAL_DESTINATIONS))
def test_no_manual_package_describes_a_destination_outside_the_catalogue(platform_id):
    """A package for an id the catalogue does not know is unreachable through the
    panel, so it is dead code that still looks like coverage."""
    assert platform_id in CATALOGUE_IDS, f"{platform_id}: package without a catalogue entry"


def test_every_consent_gated_destination_can_be_handed_off_by_hand():
    """The consent gate demotes these to `manual_upload`; that level is only
    honest while the operator has something to upload."""
    gated = sorted(consent_gated_ids() & set(CATALOGUE_IDS))
    assert gated, "the consent gate names no catalogue destination — the wiring broke"
    for platform_id in gated:
        assert capability_of(platform_id) != "publish", platform_id
        assert platform_id in MANUAL_DESTINATIONS, platform_id


def test_a_video_only_handoff_does_not_claim_readiness_without_a_video():
    """`ready` is what the operator acts on; a package that needs an MP4 and has
    none must not read as ready, and must say so in its first note."""
    post = _post()
    post["media"] = []
    package = build_manual_package("tiktok", post)
    assert package["needs_video"] is True
    assert package["has_video"] is False
    assert package["ready"] is False
    assert "MP4" in package["notes"][0]

from __future__ import annotations

import pytest

from tgfarm.s4_generation.topic_memory import _topic_score


@pytest.mark.parametrize(
    ("terms", "source"),
    [
        (["kestrelion", "thermal", "reactor", "cooling"], "Kestrelion's thermal reactor cooling test reports new conditions."),
        (["ethereum", "token", "fees", "rollup"], "Ethereum rollup fees and token transfers were measured."),
        (["mitochondria", "cell", "energy", "output"], "Mitochondria change cellular energy output under stress."),
        (["microplastic", "ocean", "fish", "water"], "Microplastic particles were detected in ocean water and fish."),
        (["photonic", "chip", "laser", "speed"], "Photonic chip tests compare laser speed and signal quality."),
        (["inflation", "food", "rent", "prices"], "Inflation changed food prices and rent costs this quarter."),
        (["starship", "moon", "rocket", "launch"], "Starship launch preparations for the Moon mission continued."),
        (["metaverse", "avatar", "virtual", "social"], "The metaverse platform supports avatar tools and social spaces."),
        (["fukushima", "reactor", "water", "release"], "Fukushima monitoring tracks reactor water release measurements."),
        (["proteomics", "protein", "lab", "test"], "Proteomics testing compared protein markers in the lab."),
    ],
)
def test_topic_score_accepts_sources_with_the_distinctive_topic_anchor(terms, source):
    assert _topic_score(terms, source) >= 0.5


@pytest.mark.parametrize(
    ("terms", "source"),
    [
        (["kestrelion", "thermal", "reactor", "cooling"], "Thermal cooling systems are used in industrial reactors."),
        (["ethereum", "token", "fees", "rollup"], "Token fees were compared across several payment rollups."),
        (["mitochondria", "cell", "energy", "output"], "Cell energy output was measured in a battery study."),
        (["microplastic", "ocean", "fish", "water"], "Ocean water quality and fish migration were surveyed."),
        (["photonic", "chip", "laser", "speed"], "Chip speed and laser alignment were tested in optics."),
        (["inflation", "food", "rent", "prices"], "Food prices and rent changed after a local supply issue."),
        (["starship", "moon", "rocket", "launch"], "A rocket launch carried a scientific payload into orbit."),
        (["metaverse", "avatar", "virtual", "social"], "Virtual avatars are used in a social network study."),
        (["fukushima", "reactor", "water", "release"], "Reactor water release was discussed in a generic safety report."),
        (["proteomics", "protein", "lab", "test"], "A lab test compared protein levels in plant samples."),
    ],
)
def test_topic_score_rejects_generic_word_overlap_without_the_anchor(terms, source):
    assert _topic_score(terms, source) == 0.0


def test_topic_score_requires_two_anchors_when_query_has_multiple_distinctive_terms():
    terms = ["intelligence", "artificial", "model", "training"]
    assert _topic_score(terms, "Artificial intelligence models need training data.") >= 0.75
    assert _topic_score(terms, "Artificial models need more training data.") == 0.0


def test_topic_score_does_not_accept_a_single_common_word_as_a_long_query_match():
    assert _topic_score(
        ["kestrelion", "thermal", "reactor", "cooling"],
        "The cooling system was replaced.",
    ) == 0.0

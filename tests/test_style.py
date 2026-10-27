from __future__ import annotations

from tgfarm.style import extract_style_features, style_score, StyleProfile


def test_extract_style_features_basic():
    text = "Hello world. How are you?\n\n- item one\n- item two"
    f = extract_style_features(text)
    assert f.sentences == 3
    assert f.questions == 1
    assert f.paragraphs == 2
    assert f.bullets == 2


def test_style_score_no_profile():
    profile = StyleProfile(channel_id="x", samples=0)
    assert style_score("any text", profile) == 0.5


def test_style_score_with_profile():
    profile = StyleProfile(channel_id="x", samples=2)
    profile.features.chars = 100
    profile.features.paragraphs = 2
    profile.features.avg_sentence_len = 10.0
    score = style_score("Short text here.", profile)
    assert 0.0 <= score <= 1.0

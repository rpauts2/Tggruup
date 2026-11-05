"""DUPLICATE_SIMILARITY must actually move the S4 duplicate gates.

The field shipped as 0.88 in `Settings` and in `.env.example`, pinned in
`conftest.py`, and read by nobody: both live duplicate checks — the in-cycle
vector window (`_too_similar_vec`) and the full-archive lookup
(`find_near_duplicate`) — compared against hard-coded `0.88` constants, so the
operator's dial was decoration.

They now read the setting on every call, with the declared default as the
fallback, which is what keeps an untouched install identical to 4c3e57d. The
sibling knob `CLUSTER_SIMILARITY` was deleted instead of wired: S2 clusters
headlines on IDF-weighted lexical overlap because cosine cannot separate them
(`s2_trends/__init__.py`), so there is no cluster threshold for it to move.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from tgfarm.core.settings import Settings
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.db.vector import pack
from tgfarm.s4_generation import _too_similar_vec
from tgfarm.s4_generation import memory as archive
from tgfarm.s4_generation.memory import DEFAULT_DUPLICATE_SIMILARITY, duplicate_threshold


def _set_similarity(monkeypatch, value: object) -> None:
    from tgfarm.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.get_settings(), "duplicate_similarity", value)


# ------------------------------------------------------------------- defaults
def test_the_code_default_is_the_constant_it_replaced():
    """Byte-identical behaviour with the knob untouched is the whole deal."""
    assert Settings.model_fields["duplicate_similarity"].default == 0.88
    assert DEFAULT_DUPLICATE_SIMILARITY == 0.88
    assert duplicate_threshold() == 0.88


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "не число",
        float("nan"),
        float("inf"),
        0.0,
        -1.0,
        1.5,
    ],
)
def test_an_unusable_value_falls_back_instead_of_moving_the_gate(monkeypatch, broken):
    """A cosine cut outside (0, 1] either drops every draft or keeps every draft."""
    _set_similarity(monkeypatch, broken)
    assert duplicate_threshold() == DEFAULT_DUPLICATE_SIMILARITY


def test_a_stubbed_settings_object_keeps_the_fallback(monkeypatch):
    import tgfarm.s4_generation.memory as memory

    monkeypatch.setattr(memory, "get_settings", lambda: SimpleNamespace())
    assert duplicate_threshold() == DEFAULT_DUPLICATE_SIMILARITY


# ------------------------------------------------------ the in-cycle window
def _pair(cosine: float) -> tuple[np.ndarray, np.ndarray]:
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine))], dtype=np.float32)
    return a, b


def test_in_cycle_window_uses_the_default_and_can_be_moved(monkeypatch):
    """Same vectors, same pool: only the knob decides whether the draft is dropped."""
    near, draft = _pair(0.7)
    pool = [{"channel_id": "ch_knob", "vec": near, "label": ""}]

    _set_similarity(monkeypatch, 0.88)
    assert _too_similar_vec(draft, pool, "ch_knob") is False, "default must not flag 0.70"

    _set_similarity(monkeypatch, 0.5)
    assert _too_similar_vec(draft, pool, "ch_knob") is True, (
        "the knob was read once and frozen: a looser cut must flag the same draft"
    )

    _set_similarity(monkeypatch, 0.99)
    tighter, tighter_draft = _pair(0.9)
    assert _too_similar_vec(
        tighter_draft, [{"channel_id": "ch_knob", "vec": tighter, "label": ""}], "ch_knob"
    ) is False, "a stricter cut must let a 0.90 pair through"

    # An explicit threshold still wins, because S4's own callers pass none.
    assert _too_similar_vec(draft, pool, "ch_knob", threshold=0.95) is False


def test_pipeline_call_site_does_not_freeze_the_threshold(monkeypatch):
    """The window check the batch loop uses goes through the setting, not a constant."""
    near, draft = _pair(0.7)
    pool = [{"channel_id": "ch_loop", "vec": near, "label": ""}]
    _set_similarity(monkeypatch, 0.6)
    assert _too_similar_vec(draft, pool, "ch_loop") is True


# --------------------------------------------------------- the archive lookup
def _seed_published(channel_id: str, hook: str, body: str, embedding) -> str:
    public_id = uuid4().hex[:12]
    now = datetime.now(UTC)
    with session_scope() as session:
        session.merge(Channel(id=channel_id, title=f"Канал {channel_id}", niche="tech"))
        session.add(Post(
            public_id=public_id,
            channel_id=channel_id,
            hook=hook,
            body=body,
            status="published",
            published_at=now,
            created_at=now,
            sources=[{"title": "Источник", "url": "https://example.com/source"}],
            claims=[],
            embedding=pack(embedding),
        ))
    return public_id


@pytest.fixture
def archive_pair(monkeypatch):
    """A published post at cosine 0.7 from a draft, with no lexical overlap.

    `search_archive` is stubbed to return the seeded row, so this test measures
    the threshold and not the FTS index.
    """
    channel = "dup_knob_archive"
    published, _ = _pair(0.7)
    public_id = _seed_published(
        channel,
        "Орбитальная станция завершила эксперимент",
        "Экипаж провёл серию тестов на борту станции в прошлом месяце.",
        published,
    )
    monkeypatch.setattr(
        archive, "search_archive", lambda *_a, **_k: {"results": [{"id": public_id}]}
    )
    return channel, public_id


def test_archive_lookup_honours_the_default_and_the_knob(monkeypatch, archive_pair):
    channel, _ = archive_pair
    _, draft = _pair(0.7)

    _set_similarity(monkeypatch, 0.88)
    assert archive.find_near_duplicate("Текст черновика", channel, draft) is None

    _set_similarity(monkeypatch, 0.5)
    match = archive.find_near_duplicate("Текст черновика", channel, draft)
    assert match is not None, "a looser cut must find the same prior publication"
    assert match["method"] == "embedding"
    assert match["similarity"] == pytest.approx(0.7, abs=1e-3)

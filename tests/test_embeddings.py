"""Embeddings and the vector index."""

from __future__ import annotations

import numpy as np

from tgfarm.db.vector import VectorIndex, l2_normalize, pack, unpack


def test_local_embedder_is_deterministic(embedder):
    a = embedder.embed("ЦБ поднял ставку")
    b = embedder.embed("ЦБ поднял ставку")
    assert np.allclose(a, b)


def test_local_embedder_is_normalised(embedder):
    vec = embedder.embed("ЦБ поднял ставку до 19 процентов")
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5


def test_local_embedder_separates_topics(embedder):
    a = embedder.embed("Ethereum перешёл на L2, комиссия снизилась")
    b = embedder.embed("Врач рекомендует больше физической активности")
    assert float(np.dot(a, b)) < 0.5


def test_local_embedder_ranks_related_above_unrelated(embedder):
    query = embedder.embed("OpenAI выпустила новую языковую модель")
    near = embedder.embed("OpenAI представила языковую модель для разработчиков")
    far = embedder.embed("Рецепт приготовления борща на зиму")
    assert float(np.dot(query, near)) > float(np.dot(query, far))


def test_pack_unpack_roundtrip():
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    assert np.allclose(unpack(pack(vec)), vec)


def test_unpack_none_and_empty():
    assert unpack(None) is None
    assert unpack(b"") is None


def test_index_finds_self():
    index = VectorIndex(dim=8)
    vec = np.random.default_rng(1).normal(size=8).astype(np.float32)
    index.add(42, vec)
    hits = index.search(vec, top_k=3)
    assert hits[0][0] == 42
    assert hits[0][1] > 0.99


def test_index_exclusion_and_threshold():
    index = VectorIndex(dim=8)
    rng = np.random.default_rng(2)
    for i in range(5):
        index.add(i, l2_normalize(rng.normal(size=8).astype(np.float32)))
    vec = index.search(rng.normal(size=8).astype(np.float32), top_k=5)[0][0]
    hits = index.search(rng.normal(size=8).astype(np.float32), top_k=5, exclude={vec})
    assert all(key != vec for key, _ in hits)
    assert index.search(rng.normal(size=8).astype(np.float32), min_score=1.01) == []


def test_index_replace_and_remove():
    index = VectorIndex(dim=4)
    a = np.array([1, 0, 0, 0], dtype=np.float32)
    b = np.array([0, 1, 0, 0], dtype=np.float32)
    index.add(1, a)
    index.add(1, b)  # replace
    assert len(index) == 1
    assert index.search(a, top_k=1)[0][1] < 0.9
    index.remove(1)
    assert len(index) == 0
    assert 1 not in index


def test_index_dim_mismatch_is_safe():
    index = VectorIndex(dim=8)
    index.add(1, np.ones(8, dtype=np.float32))
    assert index.search(np.ones(4, dtype=np.float32)) == []


def test_index_empty_search():
    assert VectorIndex(dim=4).search(np.ones(4, dtype=np.float32)) == []
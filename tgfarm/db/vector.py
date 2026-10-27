"""In-memory cosine vector index.

Why not pgvector: at farm scale the working set is one to two weeks of items
(~50–200k vectors). A numpy matmul over a preallocated matrix answers a top-k
query in single-digit milliseconds and removes a service from the stack. The
`sync_from`/`flush_to` pair keeps SQL as the system of record, so swapping in
pgvector later is a change to this file only.
"""

from __future__ import annotations

import numpy as np

from tgfarm.core.logging import get_logger

log = get_logger(__name__)


def pack(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec.astype(np.float32)
    return (vec / norm).astype(np.float32)


class VectorIndex:
    """Dense index with add/remove/search. Cosine similarity via dot product on
    L2-normalised rows."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self._keys: list[int] = []
        self._rows: list[np.ndarray] = []
        self._positions: dict[int, int] = {}
        self._matrix: np.ndarray | None = None
        self._dirty = False

    # --- mutation -----------------------------------------------------------
    def add(self, key: int, vec: np.ndarray) -> None:
        row = l2_normalize(np.asarray(vec, dtype=np.float32))
        if key in self._positions:
            self._rows[self._positions[key]] = row
            self._dirty = True
            return
        self._keys.append(key)
        self._rows.append(row)
        self._positions[key] = len(self._keys) - 1
        self._dirty = True

    def add_many(self, pairs: list[tuple[int, np.ndarray]]) -> None:
        for key, vec in pairs:
            self.add(key, vec)

    def remove(self, key: int) -> None:
        pos = self._positions.pop(key, None)
        if pos is None:
            return
        last = len(self._keys) - 1
        if pos != last:
            moved_key = self._keys[last]
            self._keys[pos] = moved_key
            self._rows[pos] = self._rows[last]
            self._positions[moved_key] = pos
        self._keys.pop()
        self._rows.pop()
        self._dirty = True

    def clear(self) -> None:
        self._keys.clear()
        self._rows.clear()
        self._positions.clear()
        self._matrix = None
        self._dirty = False

    # --- read ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: int) -> bool:
        return key in self._positions

    @property
    def keys(self) -> list[int]:
        return list(self._keys)

    def _ensure_matrix(self) -> np.ndarray | None:
        if not self._rows:
            return None
        if self._matrix is None or self._dirty or len(self._rows) != self._matrix.shape[0]:
            self._matrix = np.vstack(self._rows)
            self._dirty = False
        return self._matrix

    def search(
        self,
        vec: np.ndarray,
        top_k: int = 20,
        min_score: float = 0.0,
        exclude: set[int] | None = None,
    ) -> list[tuple[int, float]]:
        matrix = self._ensure_matrix()
        if matrix is None or matrix.shape[0] == 0:
            return []
        query = l2_normalize(np.asarray(vec, dtype=np.float32))
        if query.shape[0] != matrix.shape[1]:
            log.warning("dim mismatch: query=%s index=%s", query.shape[0], matrix.shape[1])
            return []
        scores = matrix @ query
        k = min(top_k, scores.shape[0])
        idx = np.argpartition(-scores, k - 1)[:k] if k < scores.shape[0] else np.arange(scores.shape[0])
        results: list[tuple[int, float]] = []
        for i in idx:
            score = float(scores[i])
            if score < min_score:
                continue
            key = self._keys[int(i)]
            if exclude and key in exclude:
                continue
            results.append((key, score))
        results.sort(key=lambda pair: pair[1], reverse=True)
        return results

    def similarity_to(self, vec: np.ndarray, key: int) -> float | None:
        pos = self._positions.get(key)
        if pos is None:
            return None
        query = l2_normalize(np.asarray(vec, dtype=np.float32))
        return float(np.dot(self._rows[pos], query))


# --- module-level singleton, one per process -------------------------------
_INDEX = VectorIndex()


def get_index(dim: int | None = None) -> VectorIndex:
    if dim and _INDEX.dim != dim:
        _INDEX.clear()
        _INDEX.dim = dim
    return _INDEX


def sync_from_db(session, table, limit: int = 200_000) -> int:
    """Rebuild the index from persisted embeddings."""
    from sqlalchemy import select

    index = get_index()
    index.clear()
    stmt = select(table.id, table.embedding).where(table.embedding.isnot(None)).limit(limit)
    count = 0
    for row_id, blob in session.execute(stmt):
        vec = unpack(blob)
        if vec is not None:
            index.add(int(row_id), vec)
            count += 1
    log.info("vector index rebuilt: %d vectors, dim=%d", count, index.dim)
    return count
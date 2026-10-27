"""Embedding providers.

Default is `local`: a signed-hashing-trick character n-gram model. It needs no
API key, no model download and no network, is fully deterministic, and is good
enough for the two jobs the farm actually has with vectors:

  * near-duplicate / same-story detection (S2)
  * "have we already said this" checks against published history (S3, S5)

Swap in `EMBEDDING_PROVIDER=openai` for semantically richer clusters. The
interface is identical, so no other module changes.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Protocol

import numpy as np

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings
from tgfarm.core.text import char_ngrams, content_tokens

log = get_logger(__name__)


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, text: str) -> np.ndarray: ...
    def embed_many(self, texts: list[str]) -> np.ndarray: ...


class LocalHashEmbedder:
    """Signed hashing trick over char 4-grams + word tokens.

    Sublinear TF weighting keeps a headline that repeats one word ten times from
    dominating the vector.
    """

    name = "local-hash-4gram"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _hash(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % self.dim, 1.0 if (value >> 63) & 1 == 0 else -1.0

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)

        grams = Counter(char_ngrams(text, 4))
        for gram, count in grams.items():
            idx, sign = self._hash("\x00g" + gram)
            vec[idx] += sign * (1.0 + math.log(count))

        # word tokens get extra weight: whole-word agreement is a stronger
        # signal than a shared character window
        for token, count in Counter(content_tokens(text)).items():
            idx, sign = self._hash("\x00w" + token)
            vec[idx] += sign * (1.0 + math.log(count)) * 1.5

        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def embed_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self.embed(t) for t in texts])


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, api_key: str, model: str, dim: int) -> None:
        if not api_key:
            raise ProviderUnavailable("OPENAI_API_KEY not set")
        self._api_key = api_key
        self._model = model
        self.dim = dim

    def embed_many(self, texts: list[str]) -> np.ndarray:
        import httpx

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors: list[list[float]] = []
        # batch of 96 keeps us well inside per-request token limits
        for start in range(0, len(texts), 96):
            batch = texts[start : start + 96]
            resp = httpx.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": batch},
                timeout=60.0,
            )
            if resp.status_code != 200:
                raise ProviderUnavailable(f"openai embeddings {resp.status_code}: {resp.text[:200]}")
            payload = resp.json()["data"]
            payload.sort(key=lambda row: row["index"])
            vectors.extend(row["embedding"] for row in payload)
        return np.asarray(vectors, dtype=np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self.embed_many([text])[0]


_EMBEDDER: Embedder | None = None


def get_embedder() -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER

    settings = get_settings()
    provider = settings.embedding_provider.lower()
    if provider == "openai" and settings.openai_api_key:
        _EMBEDDER = OpenAIEmbedder(
            settings.openai_api_key, settings.openai_embedding_model, settings.embedding_dim
        )
        log.info("embedder: openai/%s dim=%d", settings.openai_embedding_model, settings.embedding_dim)
    else:
        _EMBEDDER = LocalHashEmbedder(dim=settings.embedding_dim)
        log.info("embedder: local-hash dim=%d", settings.embedding_dim)
    return _EMBEDDER


def reset_embedder() -> None:
    global _EMBEDDER
    _EMBEDDER = None
"""Embedding layer: dense nomic + sparse miniCOIL, both local via FastEmbed.

Models are lazy-loaded (first call downloads to the pinned cache dir; after
that everything is offline) and version-pinned in config. Nomic requires
task prefixes — FastEmbed does not add them, so we do:
    documents -> "search_document: ..."
    queries   -> "search_query: ..."
miniCOIL has its own query path (query_embed weights terms for matching).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from engram.config import DENSE_MODEL, SPARSE_MODEL


@dataclass
class Embedded:
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]


class Embedder:
    def __init__(self, cache_dir: Path):
        self._cache_dir = str(cache_dir)
        self._dense = None
        self._sparse = None

    def _models(self):
        if self._dense is None:
            cache = Path(self._cache_dir)
            if not cache.exists() or not any(cache.iterdir()):
                # The very first command a new user runs lands here and would
                # otherwise stare at a silent terminal for the whole download.
                # stderr: hook stdout is injected into assistant context.
                print(
                    "engram: first run — downloading local embedding models"
                    f" (~600 MB) to {cache}. This happens once and can take"
                    " a few minutes; everything after is offline.",
                    file=sys.stderr, flush=True,
                )
            # Import here: fastembed pulls in onnxruntime, which is slow to
            # import and unneeded for journal-only commands (export, forget).
            from fastembed import SparseTextEmbedding, TextEmbedding

            self._dense = TextEmbedding(model_name=DENSE_MODEL, cache_dir=self._cache_dir)
            self._sparse = SparseTextEmbedding(model_name=SPARSE_MODEL, cache_dir=self._cache_dir)
        return self._dense, self._sparse

    def embed_documents(self, texts: list[str]) -> list[Embedded]:
        dense_model, sparse_model = self._models()
        dense = list(dense_model.embed([f"search_document: {t}" for t in texts]))
        sparse = list(sparse_model.embed(texts))
        return [
            Embedded(
                dense=d.tolist(),
                sparse_indices=s.indices.tolist(),
                sparse_values=s.values.tolist(),
            )
            for d, s in zip(dense, sparse, strict=True)
        ]

    def embed_query(self, text: str) -> Embedded:
        dense_model, sparse_model = self._models()
        dense = next(iter(dense_model.embed([f"search_query: {text}"])))
        sparse = next(iter(sparse_model.query_embed(text)))
        return Embedded(
            dense=dense.tolist(),
            sparse_indices=sparse.indices.tolist(),
            sparse_values=sparse.values.tolist(),
        )

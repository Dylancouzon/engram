"""EdgeBackend: the Qdrant Edge shard behind engram.

Edge runs in-process ("SQLite, but for vector search") and is treated as a
rebuildable index: the journal owns durability, this class owns retrieval.
flush() here is the commit point the journal's high-water mark tracks.

Single-writer discipline is the caller's job (MemoryStore holds the app
lockfile and serializes writes); Edge itself provides no locking in the
0.7.2 Python surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdrant_edge import (
    Distance,
    EdgeConfig,
    EdgeShard,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    FieldCondition,
    Filter,
    Fusion,
    MatchAny,
    MatchValue,
    Mmr,
    Modifier,
    PayloadSchemaType,
    Point,
    Prefetch,
    Query,
    QueryRequest,
    RangeFloat,
    ScrollRequest,
    SparseVector,
    UpdateOperation,
)

from engram.embed import Embedded
from engram.models import Memory

DENSE = "dense"
SPARSE = "sparse"

_KEYWORD_INDEXES = ("type", "scope", "tags")
# Only fields used as query pre-filters get an index. valid_from/valid_to gate
# every recall (temporal validity); created_at and importance feed the app-side
# rescore straight from the payload and are never filtered on, so indexing them
# was pure per-field allocation + write overhead.
_FLOAT_INDEXES = ("valid_from", "valid_to")


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]

    def memory(self) -> Memory:
        return Memory.from_payload(self.id, self.payload)


def build_filter(
    scope: str | list[str] | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    valid_at: float | None = None,
) -> Filter | None:
    """Payload pre-filter applied during HNSW traversal. `scope` may be a
    single scope or an allowlist (any match). `valid_at` selects memories
    valid at that instant (pass now for current, a past ts for as-of
    queries); None skips validity filtering entirely."""
    must: list[Any] = []
    if scope is not None:
        if isinstance(scope, str):
            must.append(FieldCondition(key="scope", match=MatchValue(scope)))
        else:
            # An empty allowlist matches NOTHING. It must never fall through
            # to "no filter" — that inverts a deny into full access.
            must.append(FieldCondition(key="scope", match=MatchAny(any=list(scope))))
    if type:
        must.append(FieldCondition(key="type", match=MatchValue(type)))
    if tags:
        must.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
    if valid_at is not None:
        must.append(FieldCondition(key="valid_from", range=RangeFloat(lte=valid_at)))
        must.append(FieldCondition(key="valid_to", range=RangeFloat(gt=valid_at)))
    return Filter(must=must) if must else None


class EdgeBackend:
    def __init__(self, shard_dir: Path, dense_dim: int):
        if shard_dir.exists() and any(shard_dir.iterdir()):
            self._shard = EdgeShard.load(str(shard_dir))
        else:
            shard_dir.mkdir(parents=True, exist_ok=True)
            dense_params = EdgeVectorParams(
                size=dense_dim,
                distance=Distance.Cosine,
            )
            self._shard = EdgeShard.create(
                str(shard_dir),
                EdgeConfig(
                    vectors={DENSE: dense_params},
                    sparse_vectors={SPARSE: EdgeSparseVectorParams(modifier=Modifier.Idf)},
                ),
            )
            for field in _KEYWORD_INDEXES:
                self._shard.update(
                    UpdateOperation.create_field_index(field, PayloadSchemaType.Keyword)
                )
            for field in _FLOAT_INDEXES:
                self._shard.update(
                    UpdateOperation.create_field_index(field, PayloadSchemaType.Float)
                )
            # Commit the schema immediately: a crash before the first write
            # must not leave a loadable shard missing its indexes.
            self._shard.flush()

    # -- writes (caller serializes; journal acks first) ---------------------

    def upsert(self, memory: Memory, emb: Embedded) -> None:
        self._shard.update(
            UpdateOperation.upsert_points(
                [
                    Point(
                        id=memory.id,
                        vector={
                            DENSE: emb.dense,
                            SPARSE: SparseVector(
                                indices=emb.sparse_indices, values=emb.sparse_values
                            ),
                        },
                        payload=memory.to_payload(),
                    )
                ]
            )
        )

    def export_raw(self, exclude: set[str] | None = None) -> list[tuple[str, Any, dict]]:
        """Every point with its stored vectors and payload, minus `exclude`.
        Feeds the purge-rebuild: points move to a fresh shard verbatim,
        no re-embedding."""
        exclude = exclude or set()
        out: list[tuple[str, Any, dict]] = []
        offset = None
        while True:
            records, offset = self._shard.scroll(
                ScrollRequest(offset=offset, limit=256, with_payload=True, with_vector=True)
            )
            out.extend(
                (str(r.id), r.vector, r.payload or {})
                for r in records
                if str(r.id) not in exclude
            )
            if offset is None or not records:
                break
        return out

    def upsert_raw(self, point_id: str, vector: Any, payload: dict[str, Any]) -> None:
        """Re-insert a point exactly as exported (vectors already computed)."""
        self._shard.update(
            UpdateOperation.upsert_points([Point(id=point_id, vector=vector, payload=payload)])
        )

    def set_payload(self, memory_id: str, partial: dict[str, Any]) -> None:
        """Metadata-only change (soft-invalidate, reinforce) — no re-embed."""
        self._shard.update(UpdateOperation.set_payload([memory_id], partial))

    def delete(self, memory_ids: list[str]) -> None:
        self._shard.update(UpdateOperation.delete_points(list(memory_ids)))

    def flush(self) -> None:
        """The Edge commit point. The journal high-water mark advances only
        after this returns."""
        self._shard.flush()

    def close(self) -> None:
        self._shard.close()

    # -- reads ---------------------------------------------------------------

    def query_hybrid(
        self,
        emb: Embedded,
        k: int,
        flt: Filter | None = None,
        prefetch_limit: int = 40,
        mmr_lambda: float | None = None,
    ) -> list[Hit]:
        """Dense + sparse prefetch branches, filters applied inside each
        branch (true pre-filter). Final selection is DBSF fusion, or MMR
        diversification over the candidates when mmr_lambda is set."""
        sparse_query = SparseVector(indices=emb.sparse_indices, values=emb.sparse_values)
        prefetches = [
            Prefetch(query=Query.Nearest(emb.dense, using=DENSE), filter=flt,
                     limit=prefetch_limit),
        ]
        if emb.sparse_indices:
            prefetches.append(
                Prefetch(query=Query.Nearest(sparse_query, using=SPARSE), filter=flt,
                         limit=prefetch_limit)
            )
        if mmr_lambda is not None:
            final = Mmr(emb.dense, lambda_=mmr_lambda,
                        candidates_limit=prefetch_limit, using=DENSE)
        else:
            final = Fusion.Dbsf()
        results = self._shard.query(
            QueryRequest(
                prefetches=prefetches,
                query=final,
                limit=k,
                with_payload=True,
            )
        )
        return [Hit(id=str(p.id), score=p.score, payload=p.payload or {}) for p in results]

    def query_dense(self, dense: list[float], k: int, flt: Filter | None = None) -> list[Hit]:
        """Dense-only search returning raw cosine scores — used where the
        score must be an interpretable similarity (conflict candidates)."""
        results = self._shard.query(
            QueryRequest(
                query=Query.Nearest(dense, using=DENSE),
                filter=flt,
                limit=k,
                with_payload=True,
            )
        )
        return [Hit(id=str(p.id), score=p.score, payload=p.payload or {}) for p in results]

    def retrieve(self, memory_ids: list[str]) -> list[Hit]:
        # with_vector is required positionally in the 0.7.2 binding despite
        # the stub marking it optional.
        records = self._shard.retrieve(list(memory_ids), with_payload=True, with_vector=False)
        return [Hit(id=str(r.id), score=0.0, payload=r.payload or {}) for r in records]

    def scroll_all(self, flt: Filter | None = None) -> list[Hit]:
        out: list[Hit] = []
        offset = None
        while True:
            records, offset = self._shard.scroll(
                ScrollRequest(offset=offset, limit=256, filter=flt, with_payload=True)
            )
            out.extend(Hit(id=str(r.id), score=0.0, payload=r.payload or {}) for r in records)
            if offset is None or not records:
                break
        return out

    def count(self) -> int:
        return self._shard.info().points_count

# qdrant_edge (0.7.2) — API notes for engram

Distilled from `qdrant_edge.pyi` (qdrant_edge_py 0.7.2) + real usage in
`edge-mission-control`. Where something is absent from the stub it's called
out — don't invent it.

## Shard lifecycle

```python
class EdgeShard:
    @staticmethod
    def load(path: str, config: Optional[EdgeConfig] = None) -> EdgeShard: ...
    @staticmethod
    def create(path: str, config: EdgeConfig) -> EdgeShard: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
    def optimize(self) -> bool: ...   # blocking; runs optimizers until none planned
    def info(self) -> ShardInfo: ...  # .points_count, .segments_count, .payload_schema
```

- `create()` **fails if path already contains segment data** — check dir and
  choose `load()` vs `create()` yourself; no create-or-open convenience.
- `ReadOnlyEdgeShard` — **not present in 0.7.2** (single shard class).
- `wal_options` / `segment_capacity` / `check_consistency` — **not present in
  the 0.7.2 Python surface**. Durability is flush-driven: unflushed writes are
  lost on reopen. `flush()` is the only commit point.
- **No thread-safety provided.** Wrap every `update()`/`flush()` in your own
  lock; reads can interleave. Join background writers before `flush()+close()`.

```python
class EdgeConfig:
    def __init__(self, vectors=None, sparse_vectors=None, on_disk_payload=True,
                 hnsw_config=None, quantization_config=None, optimizers=None): ...

class EdgeVectorParams:
    def __init__(self, size: int, distance: Distance, on_disk=None,
                 multivector_config=None, datatype=None,
                 quantization_config=None, hnsw_config=None): ...

class EdgeSparseVectorParams:
    def __init__(self, full_scan_threshold=None, on_disk=None,
                 modifier=None,  # Modifier.Idf
                 datatype=None): ...
```

`Distance`: `Cosine, Euclid, Dot, Manhattan`. Optimizers config has
`prevent_unoptimized` (defers reads of unoptimized points — avoid for
read-your-writes).

## Payload indexes

Created explicitly after `create()`, via the same update entry point:

```python
shard.update(UpdateOperation.create_field_index("scope", PayloadSchemaType.Keyword))
```

`PayloadSchemaType`: `Keyword, Integer, Float, Geo, Text, Bool, Datetime, Uuid`.
Finer control via `KeywordIndexParams(is_tenant, on_disk, enable_hnsw)`,
`FloatIndexParams(is_principal, ...)`, `TextIndexParams(...)`, etc.

## Writes — all synchronous, via `shard.update(op)`

```python
UpdateOperation.upsert_points(points: List[Point], condition=None, update_mode=None)
UpdateOperation.delete_points(point_ids: List[PointId])
UpdateOperation.delete_points_by_filter(filter: Filter)
UpdateOperation.set_payload(point_ids, payload, key=None)
UpdateOperation.overwrite_payload(point_ids, payload, key=None)
UpdateOperation.update_vectors(point_vectors: List[PointVectors], condition=None)
UpdateOperation.create_field_index(field_name, schema) / delete_field_index(...)

class Point:
    def __init__(self, id: PointId, vector: Vector, payload: Optional[Payload] = None): ...
```

- `PointId = Union[int, UUID, str]` — use **UUID strings** consistently.
- `Vector` = plain list (default vector) or dict `{name: NamedVector}` mixing
  dense lists, `SparseVector`, and multivectors.
- `UpdateMode`: `Upsert` (default), `InsertOnly`, `UpdateOnly`.

## Reads

```python
def query(self, query: QueryRequest) -> List[ScoredPoint]: ...   # hybrid/fusion/formula/MMR
def search(self, search: SearchRequest) -> List[ScoredPoint]: ...  # plain single-vector
def scroll(self, scroll: ScrollRequest) -> Tuple[List[Record], Optional[PointId]]: ...
def count(self, count: CountRequest) -> int: ...   # CountRequest(exact=True, filter=None)
def retrieve(self, point_ids, with_payload=None, with_vector=None) -> List[Record]: ...
def facet(self, facet: FacetRequest) -> FacetResponse: ...
```

### Hybrid dense+sparse with fusion (verified working pattern)

```python
QueryRequest(
    prefetches=[
        Prefetch(query=Query.Nearest(dense_vec, using="dense"), filter=flt, limit=k * 6),
        Prefetch(query=Query.Nearest(sparse_vec, using="sparse"), filter=flt, limit=k * 6),
    ],
    query=Fusion.Rrf(k=60),   # or Fusion.Dbsf()
    limit=k,
    with_payload=True,
)
```

`Fusion.Rrf(k: int, weights: Optional[List[float]])`, `Fusion.Dbsf()`.

### Formula / decay (present in stub; no real-usage confirmation)

```python
class Formula:  # pass as QueryRequest(query=Formula(...))
    def __init__(self, formula: Expression, defaults: Optional[Dict] = None): ...

Expression.Constant / Variable("score") / Condition / Datetime(iso) / DatetimeKey(path)
Expression.Mult([...]) / Sum([...]) / Div / Pow / Exp / Ln / Log10 / Abs / Neg / Sqrt
Expression.Decay(kind: DecayKind, x, target=None, midpoint=None, scale=None)
DecayKind: Lin, Gauss, Exp
```

### MMR (verified in edge-mission-control)

```python
QueryRequest(query=Mmr(vector, lambda_=0.9, candidates_limit=100, using="dense"),
             filter=flt, limit=k)
```

### Filters

```python
Filter(must=None, should=None, must_not=None, min_should=None)
FieldCondition(key, match=None, range=None, values_count=None,
               is_empty=None, is_null=None)
```

Match: `MatchValue(value)`, `MatchAny(any=[...])`, `MatchExcept(except_=[...])`,
`MatchText(text)`, `MatchTextAny`, `MatchPhrase`. Range: `RangeFloat(gte,gt,lte,lt)`,
`RangeDateTime(...)` (ISO-8601 strings). Also `IsNullCondition(key)`,
`IsEmptyCondition(key)`, `HasIdCondition(point_ids)`, `MinShould(conditions, min_count)`.

### Scroll pagination

```python
records, offset = shard.scroll(ScrollRequest(offset=offset, limit=256, filter=flt,
                                             with_payload=True))
# loop until offset is None or not records
```

`ScoredPoint`: `.id, .version, .score, .vector, .payload, .order_value`.
`Record`: `.id, .vector, .payload, .order_value`.

## Sparse vectors

```python
SparseVector(indices: List[int], values: List[float])
```

Upsert inside the named-vector dict; query via `Query.Nearest(sv, using="sparse")`.
Bundled sparse encoder is BM25 only (`Bm25`, `Bm25Config`) — miniCOIL comes
from FastEmbed, Edge just stores/queries the sparse vector.
`Modifier`: only `Idf` — set on `EdgeSparseVectorParams(modifier=Modifier.Idf)`.

## Snapshots

`snapshot_manifest()`, `unpack_snapshot(path, target)`, `update_from_snapshot(path)`.
**No snapshot-create method in 0.7.2.** Backup = quiesced `flush()+close()` copy.

## Gotchas

- Single-writer discipline is the app's job (own lock around update/flush).
- IDs: UUID strings everywhere.
- Fresh shard has no payload indexes — create them right after `create()`.
- Unindexed keyword filtering falls back to scan (fine at personal scale).
- Vector + payload updates for the same point: apply under one lock section.

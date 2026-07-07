"""Consolidation: the housekeeping that keeps a growing memory clean.

Runs as a well-behaved writer — bounded per run, cancellable between
items, checkpointed via the journal's meta table, and invoked only when
the daemon has been idle (or manually via `engram consolidate`).

Three passes, cheapest first:
- decay-prune: stale, never-recalled episodic memories fade out
  (soft-invalidate — history preserved, recall unpolluted).
- dedup: normalized-identical valid memories collapse to the oldest
  (write-time dedup catches most; this catches imports and pre-judge rows).
- episodic -> semantic: clusters of old episodes sharing a tag are
  summarized into one semantic memory by the local model (skipped without
  Ollama — an enhancer, like everywhere else).

Everything goes through the store's normal journaled write paths: a
rebuild after consolidation converges to the same state.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from engram.models import Memory, MemoryType, now_ts

if TYPE_CHECKING:
    from engram.store import MemoryStore

PRUNE_MIN_AGE_DAYS = 30.0
# Pruning decays much slower than recall ranking: an old episode should
# rank low long before it fades out entirely.
PRUNE_HALF_LIFE_DAYS = 60.0
PRUNE_SCORE = 0.1  # importance x prune-decay below this fades out
SUMMARIZE_MIN_AGE_DAYS = 30.0
SUMMARIZE_MIN_GROUP = 3

_SUMMARY_SYSTEM = """You compress old diary-like event memories into one durable fact.
Given several dated episodic memories on the same topic, write ONE short
semantic summary that keeps what will still matter months from now (patterns,
outcomes, decisions) and drops one-off details.
Respond with JSON: {"summary": "..."} or {"summary": null} if nothing durable remains."""


def consolidate(
    store: MemoryStore,
    budget: int = 50,
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """One bounded consolidation run. Returns per-pass counts."""
    report = {"pruned": 0, "deduped": 0, "summarized": 0}
    now = now_ts()
    # Memories awaiting an owner decision are off-limits: consolidation must
    # not invalidate a review's target or its ADDed twin out from under it.
    protected: set[str] = set()
    for item in store.pending_reviews():
        protected.add(item.new.id)
        protected.add(item.target.id)

    def cancelled() -> bool:
        return stop is not None and stop.is_set()

    def spent() -> int:
        return sum(report.values())

    with store._write_lock:
        for shard, backend in list(store.backends.items()):
            valid = [
                hit.memory()
                for hit in backend.scroll_all(
                    flt=None  # includes invalidated; filter below (cheap at this scale)
                )
            ]
            valid = [m for m in valid if m.is_valid]

            # -- decay-prune ----------------------------------------------------
            for m in valid:
                if cancelled() or spent() >= budget:
                    break
                if m.type is not MemoryType.EPISODIC or m.access_count > 0:
                    continue
                if m.id in protected:
                    continue
                age_days = (now - m.created_at) / 86400.0
                if age_days < PRUNE_MIN_AGE_DAYS:
                    continue
                score = m.importance * math.exp(
                    -math.log(2) * age_days / PRUNE_HALF_LIFE_DAYS
                )
                if score < PRUNE_SCORE:
                    _soft_invalidate(store, backend, shard, m)
                    report["pruned"] += 1

            # -- dedup ------------------------------------------------------------
            from engram.store import _normalize

            seen: dict[str, Memory] = {}
            for m in sorted(valid, key=lambda m: m.created_at):
                if cancelled() or spent() >= budget:
                    break
                if not m.is_valid or m.id in protected:
                    continue
                key = _normalize(m.text)
                prior = seen.get(key)
                if prior is None:
                    seen[key] = m
                    continue
                prior.importance = max(prior.importance, m.importance)
                prior.access_count += m.access_count
                store._commit_upserts([prior], shard)
                _soft_invalidate(store, backend, shard, m, superseded_by=prior.id)
                report["deduped"] += 1

            # -- episodic -> semantic ----------------------------------------------
            if store.llm is not None and store.llm.available():
                groups: dict[tuple[str, str], list[Memory]] = defaultdict(list)
                for m in valid:
                    if (m.is_valid and m.type is MemoryType.EPISODIC
                            and (now - m.created_at) / 86400.0 >= SUMMARIZE_MIN_AGE_DAYS):
                        for tag in m.tags or ["untagged"]:
                            groups[(m.scope, tag)].append(m)
                for (scope, _tag), episodes in groups.items():
                    if cancelled() or spent() >= budget:
                        break
                    episodes = [e for e in episodes
                                if e.is_valid and e.id not in protected]
                    if len(episodes) < SUMMARIZE_MIN_GROUP:
                        continue
                    # A group costs what it touches, not one budget unit.
                    if spent() + len(episodes) + 1 > budget:
                        continue
                    listing = "\n".join(
                        f"- ({time.strftime('%Y-%m-%d', time.localtime(e.created_at))}) "
                        f"{e.text}" for e in episodes
                    )
                    result = store.llm.generate_json(_SUMMARY_SYSTEM, listing)
                    summary = (result or {}).get("summary") if isinstance(result, dict) else None
                    if not summary:
                        continue
                    from engram.models import new_memory_id

                    semantic = Memory(
                        id=new_memory_id(store._owner_ns),
                        text=str(summary).strip(),
                        type=MemoryType.SEMANTIC,
                        scope=scope,
                        tags=sorted({t for e in episodes for t in e.tags})[:3],
                        surface="consolidation",
                        importance=max(e.importance for e in episodes),
                        created_at=now,
                        valid_from=now,
                    )
                    store._commit_upserts([semantic], shard)
                    for e in episodes:
                        _soft_invalidate(store, backend, shard, e,
                                         superseded_by=semantic.id)
                    report["summarized"] += 1

    store.journal.mark_flushed(store._applied_seq)
    if not cancelled() and spent() < budget:
        # Only a COMPLETED run advances the daily checkpoint; an exhausted or
        # cancelled run should resume at the next idle window instead.
        store.journal.set_meta("consolidated_at", str(now))
    return report


def last_run(store: MemoryStore) -> float:
    value = store.journal.get_meta("consolidated_at")
    return float(value) if value else 0.0


def _soft_invalidate(store: MemoryStore, backend, shard: str, m: Memory,
                     superseded_by: str | None = None) -> None:
    m.valid_to = now_ts()
    if superseded_by:
        m.superseded_by = superseded_by
    seq = store.journal.append("upsert", m.id, m.to_payload(), shard=shard)
    backend.set_payload(m.id, {"valid_to": m.valid_to, "superseded_by": m.superseded_by})
    store._applied_seq = max(store._applied_seq, seq)
    backend.flush()

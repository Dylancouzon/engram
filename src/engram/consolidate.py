"""Consolidation: the housekeeping that keeps a growing memory clean.

Runs as a well-behaved writer — cancellable between items (so a daemon
shutdown never blocks on a model call), checkpointed via the journal's
meta table, and invoked only when the daemon has been idle (or manually
via `engram consolidate`). Runs are unbounded: at personal-memory scale a
full pass is fast; budgeting can return if dogfood data ever shows a run
long enough to matter.

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
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """One consolidation run. Returns per-pass counts."""
    report = {"pruned": 0, "deduped": 0, "summarized": 0}
    now = now_ts()

    def cancelled() -> bool:
        return stop is not None and stop.is_set()

    # Phase 1 (under the write lock): the fast model-free passes — prune and
    # dedup — plus gathering the episode groups that MIGHT be summarized. The
    # model calls are deferred to phase 2 so the lock is never held across one
    # (a daemon shutdown must not wait on a model call).
    can_summarize = store.llm is not None and store.llm.available()
    plan: list[tuple[str, str, list[Memory]]] = []  # (shard, scope, episodes)
    with store._write_lock:
        # Memories awaiting an owner decision are off-limits: consolidation must
        # not invalidate a review's target or its ADDed twin. Read under the
        # lock so a review created just before phase 1 can't be missed.
        protected: set[str] = set()
        for item in store.pending_reviews():
            protected.add(item.new.id)
            protected.add(item.target.id)
        for shard, backend in list(store.backends.items()):
            if cancelled():
                break
            valid = [
                hit.memory()
                for hit in backend.scroll_all(
                    flt=None  # includes invalidated; filter below (cheap at this scale)
                )
            ]
            valid = [m for m in valid if m.is_valid]

            # -- decay-prune ----------------------------------------------------
            for m in valid:
                if cancelled():
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
                    _soft_invalidate(store, shard, m)
                    report["pruned"] += 1

            # -- dedup ------------------------------------------------------------
            from engram.store import _normalize

            seen: dict[str, Memory] = {}
            for m in sorted(valid, key=lambda m: m.created_at):
                if cancelled():
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
                # Payload-only: prior's text is unchanged, so skip the re-embed.
                store._commit_payload(prior, shard, {
                    "importance": prior.importance,
                    "access_count": prior.access_count,
                })
                _soft_invalidate(store, shard, m, superseded_by=prior.id)
                report["deduped"] += 1

            # -- gather episodic->semantic groups (model calls come in phase 2) --
            if can_summarize:
                groups: dict[tuple[str, str], list[Memory]] = defaultdict(list)
                for m in valid:
                    if (m.is_valid and m.type is MemoryType.EPISODIC
                            and (now - m.created_at) / 86400.0 >= SUMMARIZE_MIN_AGE_DAYS):
                        for tag in m.tags or ["untagged"]:
                            groups[(m.scope, tag)].append(m)
                for (scope, _tag), episodes in groups.items():
                    episodes = [e for e in episodes
                                if e.is_valid and e.id not in protected]
                    if len(episodes) >= SUMMARIZE_MIN_GROUP:
                        plan.append((shard, scope, episodes))
        store._mark_flushed(store._applied_seq)  # prune/dedup durable

    # Phase 2 (NO write lock): the model calls. A stop fired here is honored
    # before touching the store, so shutdown never waits on a call.
    summaries: list[tuple[str, str, list[Memory], str]] = []
    for shard, scope, episodes in plan:
        if cancelled():
            break
        listing = "\n".join(
            f"- ({time.strftime('%Y-%m-%d', time.localtime(e.created_at))}) "
            f"{e.text}" for e in episodes
        )
        result = store.llm.generate_json(_SUMMARY_SYSTEM, listing)
        if cancelled():
            break  # stop fired during the call: discard the result
        summary = (result or {}).get("summary") if isinstance(result, dict) else None
        if summary:
            summaries.append((shard, scope, episodes, str(summary).strip()))

    # Phase 3 (under the write lock): apply summaries, re-validating the exact
    # episodes each summary was built from. The summary text was generated in
    # phase 2 from the episodes as they were in phase 1; if ANY of them has
    # since been forgotten, invalidated, edited, or newly protected, the
    # summary may carry now-stale or now-forgotten content — so discard the
    # whole summary rather than commit a tainted derivative. Consolidation
    # will regenerate it from current state on the next idle run.
    if summaries and not cancelled():
        from engram.models import new_memory_id
        with store._write_lock:
            fresh_protected: set[str] = set()
            for item in store.pending_reviews():
                fresh_protected.add(item.new.id)
                fresh_protected.add(item.target.id)
            for shard, scope, episodes, summary_text in summaries:
                backend = store._backend(shard)
                live: list[Memory] = []
                for e in episodes:
                    found = backend.retrieve([e.id])
                    cur = found[0].memory() if found else None
                    if (cur is None or not cur.is_valid
                            or cur.id in fresh_protected or cur.text != e.text):
                        live = []  # any changed/gone source taints the summary
                        break
                    live.append(cur)
                if len(live) < SUMMARIZE_MIN_GROUP:
                    continue
                semantic = Memory(
                    id=new_memory_id(store._owner_ns),
                    text=summary_text,
                    type=MemoryType.SEMANTIC,
                    scope=scope,
                    tags=sorted({t for e in live for t in e.tags})[:3],
                    surface="consolidation",
                    importance=max(e.importance for e in live),
                    created_at=now,
                    valid_from=now,
                )
                store._commit_upserts([semantic], shard)
                for e in live:
                    _soft_invalidate(store, shard, e, superseded_by=semantic.id)
                report["summarized"] += 1
            store._mark_flushed(store._applied_seq)

    if not cancelled():
        # Only a COMPLETED run advances the daily checkpoint; a cancelled
        # run should resume at the next idle window instead.
        store.journal.set_meta("consolidated_at", str(now))
    return report


def last_run(store: MemoryStore) -> float:
    value = store.journal.get_meta("consolidated_at")
    return float(value) if value else 0.0


def _soft_invalidate(store: MemoryStore, shard: str, m: Memory,
                     superseded_by: str | None = None) -> None:
    m.valid_to = now_ts()
    if superseded_by:
        m.superseded_by = superseded_by
    store._commit_payload(
        m, shard, {"valid_to": m.valid_to, "superseded_by": m.superseded_by})

"""MemoryStore: the write/read/forget pipelines over journal + Edge.

Write path (order is the contract):
    redact -> extract -> conflict-resolve -> JOURNAL APPEND (the ack point)
    -> embed + upsert into Edge -> flush -> advance the journal high-water mark

A crash anywhere after the journal append loses nothing: on next open the
rows past the high-water mark replay into Edge (re-embedding from raw text,
since vectors are never journaled). Edge is a rebuildable index; the journal
is the memory.

Library-mode (M0): one process is the sole writer, enforced by an exclusive
lockfile. The daemon in M1 takes over this seat; the API below is already
the daemon's API.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from types import TracebackType
from typing import TextIO

from engram.backend.edge import EdgeBackend, build_filter
from engram.config import DENSE_MODEL, Config
from engram.embed import Embedder
from engram.extract import ExtractedFact, extract
from engram.journal import Journal, JournalEntry
from engram.llm import LocalLLM
from engram.models import Memory, MemoryType, Op, RecallHit, new_memory_id, now_ts
from engram.redact import redact
from engram.resolve import Verdict, judge


def _normalize(text: str) -> str:
    """Case/punctuation/whitespace-insensitive form for verbatim-dedup."""
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


class StoreLockedError(RuntimeError):
    pass


class WriteRefusedError(ValueError):
    """Stage-0 redaction refused the write (e.g. a private key)."""


@dataclass
class WriteAction:
    """One applied outcome, for reporting and the golden-set harness."""

    op: Op
    memory: Memory | None  # the stored/updated memory (None for NOOP)
    target: Memory | None = None  # the pre-existing memory affected
    confidence: float = 1.0
    redaction_hits: list[str] = field(default_factory=list)


class MemoryStore:
    def __init__(
        self,
        config: Config | None = None,
        *,
        embedder: Embedder | None = None,
        llm: LocalLLM | None | str = "auto",
    ):
        """`embedder` and `llm` exist for injection (tests, future daemon);
        the defaults build the real FastEmbed models and local Ollama probe.
        Pass llm=None to force verbatim/ADD-only mode."""
        self.config = config or Config.load()
        cfg = self.config
        cfg.data_dir.mkdir(parents=True, exist_ok=True)

        self._lock_file = open(cfg.lock_path, "w")  # noqa: SIM115 - held for store lifetime
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self._lock_file.close()
            raise StoreLockedError(
                f"another engram process holds {cfg.lock_path}"
            ) from e

        self._owner_ns = cfg.owner_namespace()
        self.journal = Journal(cfg.journal_path)
        self.backend = EdgeBackend(cfg.shard_dir, dense_dim=cfg.dense_dim)
        self.embedder = embedder or Embedder(cfg.models_dir)
        self.llm = (
            LocalLLM(cfg.ollama_url, cfg.extraction_model) if llm == "auto" else llm
        )
        # Highest journal seq actually applied to Edge in this session. The
        # high-water mark may only ever advance to this — never to last_seq
        # blindly, or a write that crashed between journal-append and
        # Edge-apply would be skipped by replay forever.
        self._applied_seq = self.journal.flushed_seq
        self._replay_pending()

    # -- write ---------------------------------------------------------------

    def remember(
        self,
        text: str,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
        scope: str = "default",
        importance: float | None = None,
        surface: str = "cli",
        source_ref: str | None = None,
    ) -> list[WriteAction]:
        scrubbed = redact(text, enabled=self.config.redaction_enabled)
        if scrubbed.refused:
            raise WriteRefusedError(scrubbed.refusal_reason or "refused by redaction")
        clean_text = scrubbed.text

        facts = extract(clean_text, self.llm, self.config.salience_floor)
        actions: list[WriteAction] = []
        for fact in facts:
            # Explicit caller intent overrides the extractor's guesses.
            if type is not None:
                fact.type = type
            if importance is not None:
                fact.importance = importance
            if tags:
                fact.tags = list(dict.fromkeys(fact.tags + [t.lower() for t in tags]))

            verdict = self._resolve_conflict(fact, scope)
            action = self._apply(fact, verdict, clean_text, scope, surface, source_ref)
            action.redaction_hits = scrubbed.hits
            actions.append(action)
        return actions

    def _resolve_conflict(self, fact: ExtractedFact, scope: str) -> Verdict:
        # Candidates come from a dense-only search: unlike fused scores,
        # cosine similarity is a stable, interpretable gate.
        if self.backend.count() == 0:
            return Verdict(op=Op.ADD, target=None, confidence=1.0)
        query = self.embedder.embed_query(fact.text)
        hits = self.backend.query_dense(
            query.dense,
            k=self.config.conflict_top_k,
            flt=build_filter(scope=scope, valid_at=now_ts()),
        )
        scored = [
            (h.memory(), h.score)
            for h in hits
            if h.score >= self.config.conflict_min_similarity
        ]
        candidates = [m for m, _ in scored]

        # Deterministic dedup before the judge: an (almost) verbatim repeat
        # is a NOOP regardless of what a small model thinks.
        norm = _normalize(fact.text)
        for m, score in scored:
            if _normalize(m.text) == norm:
                return Verdict(op=Op.NOOP, target=m, confidence=1.0,
                               target_similarity=score)

        verdict = judge(fact.text, candidates, self.llm)
        if verdict.target is not None:
            verdict.target_similarity = next(
                (s for m, s in scored if m.id == verdict.target.id), 0.0
            )
        return verdict

    def _apply(
        self,
        fact: ExtractedFact,
        verdict: Verdict,
        source_text: str,
        scope: str,
        surface: str,
        source_ref: str | None,
    ) -> WriteAction:
        confident = verdict.confidence >= self.config.judge_confidence
        # A NOOP the judge is lukewarm on still stands when the retrieval
        # similarity independently says "near-duplicate" — two weak signals
        # agreeing. UPDATE/SUPERSEDE never get this shortcut: a wrongful
        # merge or supersede is the unrecoverable direction.
        if (
            verdict.op is Op.NOOP
            and verdict.target_similarity >= self.config.noop_similarity
        ):
            confident = True
        op = verdict.op if confident else Op.ADD
        now = now_ts()

        if op is Op.NOOP and verdict.target is not None:
            self._reinforce([verdict.target.id])
            return WriteAction(op=Op.NOOP, memory=None, target=verdict.target,
                               confidence=verdict.confidence)

        if op is Op.UPDATE and verdict.target is not None:
            updated = verdict.target
            updated.text = verdict.merged_text or fact.text
            updated.source_text = source_text
            updated.importance = max(updated.importance, fact.importance)
            updated.tags = list(dict.fromkeys(updated.tags + fact.tags))
            updated.embedding_model = DENSE_MODEL
            self._commit_upserts([updated])
            return WriteAction(op=Op.UPDATE, memory=updated, target=verdict.target,
                               confidence=verdict.confidence)

        new = Memory(
            id=new_memory_id(self._owner_ns),
            text=fact.text,
            type=fact.type,
            scope=scope,
            tags=fact.tags,
            surface=surface,
            source_text=None if fact.verbatim else source_text,
            source_ref=source_ref,
            created_at=now,
            valid_from=now,
            importance=fact.importance,
            embedding_model=DENSE_MODEL,
        )

        if op is Op.SUPERSEDE and verdict.target is not None:
            old = verdict.target
            old.valid_to = now
            old.superseded_by = new.id
            # One journal transaction: the correction and the invalidation
            # land together or not at all.
            self._commit_upserts([old, new])
            return WriteAction(op=Op.SUPERSEDE, memory=new, target=old,
                               confidence=verdict.confidence)

        self._commit_upserts([new])
        return WriteAction(op=Op.ADD, memory=new, confidence=verdict.confidence)

    def _commit_upserts(self, memories: list[Memory]) -> None:
        intents = []
        for m in memories:
            payload = m.to_payload()
            key = hashlib.sha256(
                json.dumps(["upsert", m.id, payload], sort_keys=True).encode()
            ).hexdigest()
            intents.append(("upsert", m.id, payload, key))
        last_seq = self.journal.append_many(intents)[-1]  # <- the ack point

        embedded = self.embedder.embed_documents([m.text for m in memories])
        for m, emb in zip(memories, embedded, strict=True):
            self.backend.upsert(m, emb)
        self._applied_seq = max(self._applied_seq, last_seq)
        self.backend.flush()
        self.journal.mark_flushed(self._applied_seq)

    # -- read ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        k: int | None = None,
        scope: str | None = None,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
        as_of: float | None = None,
        reinforce: bool = True,
    ) -> list[RecallHit]:
        k = k or self.config.recall_k
        emb = self.embedder.embed_query(query)
        flt = build_filter(
            scope=scope,
            type=type.value if type else None,
            tags=tags,
            valid_at=as_of if as_of is not None else now_ts(),
        )
        # Over-fetch so the rescore has real headroom to reorder.
        hits = self.backend.query_hybrid(
            emb, k=k * 3, flt=flt, prefetch_limit=self.config.prefetch_limit
        )

        now = now_ts()
        rescored: list[RecallHit] = []
        for h in hits:
            m = h.memory()
            age_days = max(0.0, now - (m.last_accessed or m.created_at)) / 86400.0
            half_life = self.config.half_life_days.get(m.type.value, 90.0)
            recency = math.exp(-math.log(2) * age_days / half_life)
            w_rec, w_imp = self.config.weight_recency, self.config.weight_importance
            blend = (1 - w_rec - w_imp) + w_rec * recency + w_imp * m.importance
            rescored.append(RecallHit(memory=m, score=max(h.score, 0.0) * blend,
                                      similarity=h.score))
        rescored.sort(key=lambda r: r.score, reverse=True)
        top = rescored[:k]

        if reinforce and top:
            self._reinforce([r.memory.id for r in top])
        return top

    def _reinforce(self, memory_ids: list[str]) -> None:
        """Access bumps are journaled (so rebuilds keep them) but not
        flushed — they ride along with the next write's flush or close()."""
        now = now_ts()
        for mid in memory_ids:
            hit = self.backend.retrieve([mid])
            if not hit:
                continue
            count = int(hit[0].payload.get("access_count") or 0) + 1
            partial = {"access_count": count, "last_accessed": now}
            seq = self.journal.append("reinforce", mid, partial)
            self.backend.set_payload(mid, partial)
            # Applied but deliberately not flushed — bumps ride along with
            # the next write's flush or close(). Replay is idempotent
            # (absolute counts), so a crash loses nothing.
            self._applied_seq = max(self._applied_seq, seq)

    # -- forget ------------------------------------------------------------------

    def forget(self, memory_id: str, mode: str = "soft") -> Memory | None:
        """soft: invalidate now (history preserved, excluded from recall).
        hard: purge from Edge and the journal, VACUUM, leave a tombstone."""
        found = self.backend.retrieve([memory_id])
        memory = found[0].memory() if found else None

        if mode == "soft":
            if memory is None:
                return None
            memory.valid_to = now_ts()
            payload = memory.to_payload()
            seq = self.journal.append("upsert", memory.id, payload)
            self.backend.set_payload(memory.id, {"valid_to": memory.valid_to})
            self._applied_seq = max(self._applied_seq, seq)
            self.backend.flush()
            self.journal.mark_flushed(self._applied_seq)
            return memory

        # hard: order matters — Edge first, then the journal purge; a crash
        # in between leaves a journal whose replay no longer resurrects the
        # point (tombstone written in the same transaction as the purge).
        self.backend.delete([memory_id])
        self.backend.flush()
        self.journal.hard_forget(memory_id)
        return memory

    # -- maintenance ---------------------------------------------------------------

    def export_jsonl(self, fp: TextIO) -> int:
        return self.journal.export_jsonl(fp)

    def rebuild(self) -> int:
        """Rebuild the Edge index from the journal (restore/migration path):
        replay every entry in order, re-embedding from raw text."""
        applied = 0
        tombstoned = self.journal.tombstones()
        for entry in self.journal.entries():
            if entry.memory_id in tombstoned:
                continue
            self._apply_entry(entry)
            applied += 1
        self._applied_seq = self.journal.last_seq
        self.backend.flush()
        self.journal.mark_flushed(self._applied_seq)
        return applied

    def stats(self) -> dict:
        info: dict = {
            "points": self.backend.count(),
            "journal_entries": self.journal.last_seq,
            "flushed_seq": self.journal.flushed_seq,
            "tombstones": len(self.journal.tombstones()),
            "data_dir": str(self.config.data_dir),
            "dense_model": DENSE_MODEL,
            "extraction": "ollama" if self.llm.available() else "verbatim fallback",
        }
        return info

    def close(self) -> None:
        try:
            self.backend.flush()
            self.journal.mark_flushed(self._applied_seq)
            self.backend.close()
            self.journal.close()
        finally:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- crash recovery -----------------------------------------------------------

    def _replay_pending(self) -> None:
        pending = self.journal.pending()
        if not pending:
            return
        tombstoned = self.journal.tombstones()
        for entry in pending:
            if entry.memory_id in tombstoned:
                continue
            self._apply_entry(entry)
        self._applied_seq = self.journal.last_seq
        self.backend.flush()
        self.journal.mark_flushed(self._applied_seq)

    def _apply_entry(self, entry: JournalEntry) -> None:
        if entry.op == "upsert" and entry.payload is not None:
            memory = Memory.from_payload(entry.memory_id, entry.payload)
            emb = self.embedder.embed_documents([memory.text])[0]
            self.backend.upsert(memory, emb)
        elif entry.op == "delete":
            self.backend.delete([entry.memory_id])
        elif entry.op == "reinforce" and entry.payload is not None:
            # Only meaningful if the point exists (skip bumps for later-forgotten ids).
            if self.backend.retrieve([entry.memory_id]):
                self.backend.set_payload(entry.memory_id, entry.payload)

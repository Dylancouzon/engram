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
import os
import re
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
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

_SHARD_NAME = re.compile(r"^(private|me-synced|shared:[a-z0-9_-]{1,32})$")


def validate_shard(shard: str) -> str:
    """Trust-boundary shard names are a closed grammar, not free text:
    private (never syncs, default) | me-synced | shared:<group>."""
    if not _SHARD_NAME.match(shard):
        raise ValueError(
            f"invalid shard {shard!r}: use private, me-synced, or shared:<group>"
        )
    return shard


def _normalize(text: str) -> str:
    """Case/punctuation/whitespace-insensitive form for verbatim-dedup."""
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


class _ShardGuard:
    """Readers-writer guard for the Edge backend's *lifetime* (not its data:
    Edge tolerates reads interleaving with writes). Queries take shared
    access; anything that closes or replaces the shard object — hard-forget
    purge, rebuild, close — takes exclusive access, so a lock-free read can
    never hit a closed shard mid-swap."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._exclusive = False

    @contextmanager
    def shared(self):
        with self._cond:
            while self._exclusive:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        with self._cond:
            while self._exclusive:
                self._cond.wait()
            self._exclusive = True
            while self._readers:
                self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._exclusive = False
                self._cond.notify_all()


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
    queued_review: bool = False  # ADDed safely, judged op awaits review


@dataclass
class ReviewItem:
    """An ambiguous verdict waiting for the owner's call: the new fact was
    ADDed (safe), and accepting applies the judged op after the fact."""

    seq: int  # journal seq of the review row = its id
    proposed_op: Op
    new: Memory
    target: Memory
    confidence: float
    merged_text: str | None = None


class MemoryStore:
    def __init__(
        self,
        config: Config | None = None,
        *,
        embedder: Embedder | None = None,
        llm: LocalLLM | None | str = "auto",
        reinforce_mode: str = "sync",
    ):
        """`embedder` and `llm` exist for injection (tests, the daemon);
        the defaults build the real FastEmbed models and local Ollama probe.
        Pass llm=None to force verbatim/ADD-only mode.

        reinforce_mode: "sync" applies access bumps during recall (fine for
        short-lived CLI processes); "buffered" queues them so reads never
        write — the daemon drains the queue on idle and close."""
        self.config = config or Config.load()
        cfg = self.config
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(cfg.data_dir, 0o700)  # the memory folder is private by default

        # Edge provides no locking and SQLite is externally serialized: every
        # mutation goes through this lock. Reads (query/scroll) stay lock-free.
        self._write_lock = threading.RLock()
        self._shard_guard = _ShardGuard()
        self._reinforce_mode = reinforce_mode
        self._reinforce_queue: Counter[str] = Counter()
        self._reinforce_queue_lock = threading.Lock()

        # "a", not "w": never truncate before the lock is ours.
        self._lock_file = open(cfg.lock_path, "a")  # noqa: SIM115 - held for store lifetime
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self._lock_file.close()
            raise StoreLockedError(
                f"another engram process holds {cfg.lock_path}"
            ) from e

        try:
            self._owner_ns = cfg.owner_namespace()
            self.journal = Journal(cfg.journal_path)
            self.embedder = embedder or Embedder(cfg.models_dir)
            self.llm = (
                LocalLLM(cfg.ollama_url, cfg.extraction_model) if llm == "auto" else llm
            )
            self.backends: dict[str, EdgeBackend] = {}
            if self._purge_marker.exists():
                # A hard-forget purge (or its recovery) was interrupted. The
                # journal is already clean, so rebuild the index from it.
                self._recover_interrupted_purge()
            else:
                self._open_existing_shards()
            # Highest journal seq actually applied to Edge in this session.
            # The high-water mark may only ever advance to this — never to
            # last_seq blindly, or a write that crashed between journal-append
            # and Edge-apply would be skipped by replay forever.
            self._applied_seq = self.journal.flushed_seq
            self._replay_pending()
        except BaseException:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            raise

    @property
    def backend(self) -> EdgeBackend:
        """The private shard — the default write target and M0-era surface."""
        return self._backend("private")

    def _backend(self, shard: str) -> EdgeBackend:
        validate_shard(shard)
        if shard not in self.backends:
            self.backends[shard] = EdgeBackend(
                self.config.shard_path(shard), dense_dim=self.config.dense_dim
            )
        return self.backends[shard]

    def _open_existing_shards(self) -> None:
        """Open every shard already on disk (recall fans out across them),
        and always the private one (it's the default write target)."""
        self._backend("private")
        if self.config.shards_root.exists():
            for path in self.config.shards_root.iterdir():
                if path.is_dir() and not path.name.endswith(".purging"):
                    name = path.name.replace("__", ":")
                    if name not in self.backends and _SHARD_NAME.match(name):
                        self.backends[name] = EdgeBackend(
                            path, dense_dim=self.config.dense_dim
                        )

    def shard_of(self, memory_id: str) -> str | None:
        """Which shard holds this id (private wins if somehow duplicated)."""
        for shard in sorted(self.backends, key=lambda n: n != "private"):
            with self._shard_guard.shared():
                if self.backends[shard].retrieve([memory_id]):
                    return shard
        return None

    @property
    def _purge_marker(self) -> Path:
        return self.config.data_dir / "purge.pending"

    def _recover_interrupted_purge(self) -> None:
        import shutil

        if self.config.shards_root.exists():
            shutil.rmtree(self.config.shards_root)
        self.backends = {}
        self._backend("private")
        self.rebuild(wipe=False)  # shards were just wiped
        self._purge_marker.unlink()

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
        shard: str = "private",
    ) -> list[WriteAction]:
        validate_shard(shard)
        scrubbed = redact(text, enabled=self.config.redaction_enabled)
        if scrubbed.refused:
            raise WriteRefusedError(scrubbed.refusal_reason or "refused by redaction")
        clean_text = scrubbed.text
        if source_ref:
            # A provenance pointer can carry credentials too (URLs).
            source_ref = redact(source_ref, enabled=self.config.redaction_enabled).text

        # The whole write is one critical section: two concurrent remembers
        # must not both judge against pre-write state and then both apply.
        with self._write_lock:
            facts = extract(clean_text, self.llm, self.config.salience_floor)
            # source_text is kept only when input and memory are one-to-one:
            # with multiple extracted facts, each would carry the full input,
            # and hard-forgetting one fact must not leave its content living
            # on in a sibling's source_text.
            source_text = clean_text if len(facts) == 1 else None
            actions: list[WriteAction] = []
            for fact in facts:
                # Explicit caller intent overrides the extractor's guesses.
                if type is not None:
                    fact.type = type
                if importance is not None:
                    fact.importance = importance
                if tags:
                    fact.tags = list(dict.fromkeys(fact.tags + [t.lower() for t in tags]))

                verdict = self._resolve_conflict(fact, scope, shard)
                action = self._apply(fact, verdict, source_text, scope, surface,
                                     source_ref, shard)
                action.redaction_hits = scrubbed.hits
                actions.append(action)
            return actions

    def _resolve_conflict(self, fact: ExtractedFact, scope: str, shard: str) -> Verdict:
        # Candidates come from a dense-only search within the target shard:
        # unlike fused scores, cosine similarity is a stable gate, and a
        # private fact must never be judged against (or leak into) a synced
        # shard's contents.
        backend = self._backend(shard)
        if backend.count() == 0:
            return Verdict(op=Op.ADD, target=None, confidence=1.0)
        query = self.embedder.embed_query(fact.text)
        hits = backend.query_dense(
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
        shard: str = "private",
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
            # Audit trail: a NOOP silently drops the incoming statement, so
            # record what was dropped and why. Replay ignores these rows;
            # hard-forgetting the target removes them too (same memory_id).
            noop_seq = self.journal.append(
                "noop",
                verdict.target.id,
                {"dropped_text": fact.text, "confidence": verdict.confidence},
            )
            # A noop has no Edge effect, so it is durable the moment it's in
            # the journal: advance both marks past it immediately.
            self._applied_seq = max(self._applied_seq, noop_seq)
            self.journal.mark_flushed(noop_seq)
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
            self._commit_upserts([updated], shard)
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
            self._commit_upserts([old, new], shard)
            return WriteAction(op=Op.SUPERSEDE, memory=new, target=old,
                               confidence=verdict.confidence)

        self._commit_upserts([new], shard)

        # Ambiguous UPDATE/SUPERSEDE: the ADD above is the safe floor, and
        # the judged op is queued for the owner to accept or reject.
        queue = (
            not confident
            and verdict.op in (Op.UPDATE, Op.SUPERSEDE)
            and verdict.target is not None
            and verdict.confidence >= self.config.review_floor
        )
        if queue:
            seq = self.journal.append(
                "review",
                new.id,
                {
                    "proposed_op": verdict.op.value,
                    "target_id": verdict.target.id,
                    "confidence": verdict.confidence,
                    "merged_text": verdict.merged_text,
                },
                shard=shard,
            )
            self._applied_seq = max(self._applied_seq, seq)
            self.journal.mark_flushed(seq)  # no Edge effect; durable at append
        return WriteAction(op=Op.ADD, memory=new, confidence=verdict.confidence,
                           queued_review=queue)

    def _commit_upserts(self, memories: list[Memory], shard: str = "private") -> None:
        backend = self._backend(shard)
        intents = []
        for m in memories:
            payload = m.to_payload()
            key = hashlib.sha256(
                json.dumps(["upsert", m.id, payload], sort_keys=True).encode()
            ).hexdigest()
            intents.append(("upsert", m.id, payload, key))
        last_seq = self.journal.append_many(intents, shard=shard)[-1]  # <- the ack point

        embedded = self.embedder.embed_documents([m.text for m in memories])
        for m, emb in zip(memories, embedded, strict=True):
            backend.upsert(m, emb)
        self._applied_seq = max(self._applied_seq, last_seq)
        backend.flush()
        self.journal.mark_flushed(self._applied_seq)

    # -- read ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        k: int | None = None,
        scope: str | list[str] | None = None,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
        as_of: float | None = None,
        reinforce: bool = True,
        shard: str | None = None,
    ) -> list[RecallHit]:
        k = k or self.config.recall_k
        emb = self.embedder.embed_query(query)
        flt = build_filter(
            scope=scope,
            type=type.value if type else None,
            tags=tags,
            valid_at=as_of if as_of is not None else now_ts(),
        )
        # Fan out across shards (Edge fuses only within one). Per-shard
        # scores are min-max normalized before merging, ids dedup with
        # private winning ties, and the rescore has over-fetch headroom.
        shards = [shard] if shard else sorted(
            self.backends, key=lambda n: n != "private"
        )
        hits = []
        with self._shard_guard.shared():
            for name in shards:
                shard_hits = self._backend(name).query_hybrid(
                    emb, k=k * 3, flt=flt, prefetch_limit=self.config.prefetch_limit,
                    mmr_lambda=self.config.mmr_lambda,
                )
                if shard_hits:
                    lo = min(h.score for h in shard_hits)
                    hi = max(h.score for h in shard_hits)
                    span = (hi - lo) or 1.0
                    for h in shard_hits:
                        h.score = 0.5 if hi == lo else (h.score - lo) / span
                seen_ids = {h.id for h in hits}
                hits.extend(h for h in shard_hits if h.id not in seen_ids)

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

    def get(self, memory_id: str) -> Memory | None:
        """Fetch one memory by id from whichever shard holds it."""
        with self._shard_guard.shared():
            for name in sorted(self.backends, key=lambda n: n != "private"):
                found = self.backends[name].retrieve([memory_id])
                if found:
                    return found[0].memory()
        return None

    def _reinforce(self, memory_ids: list[str]) -> None:
        if self._reinforce_mode == "buffered":
            with self._reinforce_queue_lock:
                self._reinforce_queue.update(memory_ids)
        else:
            with self._write_lock:
                self._apply_reinforce(Counter(memory_ids))

    def flush_reinforce(self) -> int:
        """Drain queued access bumps (buffered mode). The daemon calls this
        on idle and at shutdown, so reads themselves never write."""
        with self._reinforce_queue_lock:
            drained = self._reinforce_queue
            self._reinforce_queue = Counter()
        if drained:
            with self._write_lock:
                self._apply_reinforce(drained)
        return sum(drained.values())

    def _apply_reinforce(self, bumps: Counter[str]) -> None:
        """Access bumps are journaled (so rebuilds keep them) but not
        flushed — they ride along with the next write's flush or close().
        Replay is idempotent (absolute counts), so a crash loses nothing."""
        now = now_ts()
        for mid, n in bumps.items():
            shard = self.shard_of(mid)
            if shard is None:
                continue
            backend = self._backend(shard)
            hit = backend.retrieve([mid])
            if not hit:
                continue
            count = int(hit[0].payload.get("access_count") or 0) + n
            partial = {"access_count": count, "last_accessed": now}
            seq = self.journal.append("reinforce", mid, partial, shard=shard)
            backend.set_payload(mid, partial)
            self._applied_seq = max(self._applied_seq, seq)

    # -- forget ------------------------------------------------------------------

    def forget(self, memory_id: str, mode: str = "soft") -> Memory | None:
        """soft: invalidate now (history preserved, excluded from recall).
        hard: purge from Edge and the journal, VACUUM, leave a tombstone."""
        with self._write_lock:
            return self._forget_locked(memory_id, mode)

    def _forget_locked(self, memory_id: str, mode: str) -> Memory | None:
        owner_shard = self.shard_of(memory_id)
        if owner_shard is None:
            return None
        backend = self._backend(owner_shard)
        found = backend.retrieve([memory_id])
        memory = found[0].memory() if found else None

        if mode == "soft":
            if memory is None:
                return None
            memory.valid_to = now_ts()
            payload = memory.to_payload()
            seq = self.journal.append("upsert", memory.id, payload, shard=owner_shard)
            backend.set_payload(memory.id, {"valid_to": memory.valid_to})
            self._applied_seq = max(self._applied_seq, seq)
            backend.flush()
            self.journal.mark_flushed(self._applied_seq)
            return memory

        # hard. Deleting the point from Edge is not enough: delete+flush
        # leaves the content readable in the shard's WAL and payload pages
        # (verified empirically). The only byte-level guarantee is a shard
        # rebuild that never contained the memory. Order matters:
        #   1. marker on          — a crash anywhere below leads to a
        #                           rebuild-from-journal on next open
        #   2. journal purge      — the source of truth forgets first
        #                           (tombstone + DELETE + VACUUM)
        #   3. shard purge-rebuild — surviving points move verbatim
        #                           (vectors preserved, no re-embed)
        self._purge_marker.touch()
        self.journal.hard_forget(memory_id)
        self._purge_shard(exclude={memory_id}, shard=owner_shard)
        self._purge_marker.unlink()
        return memory

    def _purge_shard(self, exclude: set[str], shard: str = "private") -> None:
        import shutil

        backend = self._backend(shard)
        survivors = backend.export_raw(exclude=exclude)
        with self._shard_guard.exclusive():
            backend.close()
            shard_dir = self.config.shard_path(shard)
            trash = shard_dir.with_name(shard_dir.name + ".purging")
            shard_dir.rename(trash)
            new_backend = EdgeBackend(shard_dir, dense_dim=self.config.dense_dim)
            self.backends[shard] = new_backend
            for point_id, vector, payload in survivors:
                new_backend.upsert_raw(point_id, vector, payload)
            self._applied_seq = self.journal.last_seq
            new_backend.flush()
            self.journal.mark_flushed(self._applied_seq)
        shutil.rmtree(trash)

    # -- review queue ----------------------------------------------------------

    def pending_reviews(self) -> list[ReviewItem]:
        """Ambiguous verdicts awaiting the owner's call, oldest first.
        Items whose memories have since vanished (forgotten, superseded)
        are silently dropped — the question answered itself."""
        resolved = {
            e.payload["review_seq"]
            for e in self.journal.entries()
            if e.op == "review_resolved" and e.payload
        }
        items: list[ReviewItem] = []
        for e in self.journal.entries():
            if e.op != "review" or e.seq in resolved or e.payload is None:
                continue
            new = self.get(e.memory_id)
            target = self.get(e.payload["target_id"])
            if new is None or target is None or not target.is_valid:
                continue
            items.append(ReviewItem(
                seq=e.seq,
                proposed_op=Op(e.payload["proposed_op"]),
                new=new,
                target=target,
                confidence=e.payload.get("confidence", 0.0),
                merged_text=e.payload.get("merged_text"),
            ))
        return items

    def resolve_review(self, seq: int, accept: bool) -> ReviewItem | None:
        """Apply or dismiss a queued verdict. Accepting a SUPERSEDE
        invalidates the target in favor of the ADDed memory; accepting an
        UPDATE folds the merged text into the target and removes the ADDed
        twin. Rejecting keeps both memories as they are."""
        with self._write_lock:
            item = next((i for i in self.pending_reviews() if i.seq == seq), None)
            if item is None:
                return None
            if accept and item.proposed_op is Op.SUPERSEDE:
                old = item.target
                old.valid_to = now_ts()
                old.superseded_by = item.new.id
                self._commit_upserts([old])
            elif accept and item.proposed_op is Op.UPDATE:
                target = item.target
                target.text = item.merged_text or item.new.text
                target.importance = max(target.importance, item.new.importance)
                target.tags = list(dict.fromkeys(target.tags + item.new.tags))
                # The ADDed twin folds into the target: journal the delete so
                # a rebuild converges to the same state.
                delete_seq = self.journal.append("delete", item.new.id)
                self.backend.delete([item.new.id])
                self._applied_seq = max(self._applied_seq, delete_seq)
                self._commit_upserts([target])
            resolved_seq = self.journal.append(
                "review_resolved", item.new.id,
                {"review_seq": seq, "accepted": accept},
            )
            self._applied_seq = max(self._applied_seq, resolved_seq)
            self.journal.mark_flushed(resolved_seq)
            return item

    # -- maintenance ---------------------------------------------------------------

    def export_jsonl(self, fp: TextIO) -> int:
        with self._write_lock:
            return self.journal.export_jsonl(fp)

    def rebuild(self, wipe: bool = True) -> int:
        """Rebuild the Edge index as a true projection of the journal:
        wipe the shard, then replay every entry in order, re-embedding from
        raw text. Without the wipe, points absent from the journal (stale
        imports, tombstoned ids) would survive in the index."""
        with self._write_lock:
            return self._rebuild_locked(wipe)

    def _rebuild_locked(self, wipe: bool) -> int:
        if wipe:
            import shutil

            with self._shard_guard.exclusive():
                for backend in self.backends.values():
                    backend.close()
                shutil.rmtree(self.config.shards_root)
                self.backends = {}
                self._backend("private")
        applied = 0
        tombstoned = self.journal.tombstones()
        for entry in self.journal.entries():
            if entry.memory_id in tombstoned:
                continue
            self._apply_entry(entry)
            applied += 1
        self._applied_seq = self.journal.last_seq
        for backend in self.backends.values():
            backend.flush()
        self.journal.mark_flushed(self._applied_seq)
        return applied

    def stats(self) -> dict:
        info: dict = {
            "points": sum(b.count() for b in self.backends.values()),
            "shards": {name: b.count() for name, b in sorted(self.backends.items())},
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
            self.flush_reinforce()
            with self._write_lock, self._shard_guard.exclusive():
                for backend in self.backends.values():
                    backend.flush()
                self.journal.mark_flushed(self._applied_seq)
                for backend in self.backends.values():
                    backend.close()
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
        for backend in self.backends.values():
            backend.flush()
        self.journal.mark_flushed(self._applied_seq)

    def _apply_entry(self, entry: JournalEntry) -> None:
        backend = self._backend(entry.shard)
        if entry.op == "upsert" and entry.payload is not None:
            memory = Memory.from_payload(entry.memory_id, entry.payload)
            emb = self.embedder.embed_documents([memory.text])[0]
            backend.upsert(memory, emb)
        elif entry.op == "delete":
            backend.delete([entry.memory_id])
        elif entry.op == "reinforce" and entry.payload is not None:
            # Only meaningful if the point exists (skip bumps for later-forgotten ids).
            if backend.retrieve([entry.memory_id]):
                backend.set_payload(entry.memory_id, entry.payload)

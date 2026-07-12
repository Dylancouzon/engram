"""The SQLite write-intent journal — engram's source of truth.

Qdrant Edge does not replay its WAL on reopen: flush() is the only commit
point, so anything written after the last flush is lost on a crash. The
journal closes that gap and buys four more things with one mechanism:

- crash safety: every write intent lands here (durably) *before* it is
  applied to Edge; on open, rows past the flushed high-water mark replay.
- atomicity: a multi-step operation (supersede = update old + insert new)
  is one journal transaction.
- export/portability: the JSONL export is a dump of this log — the
  engine-agnostic durability guarantee. Restore/migration = replay.
- forgetting: `forget(hard)` deletes the memory's rows and VACUUMs, so the
  content doesn't linger in free pages; a content-free tombstone remains
  and suppresses the id everywhere (including future synced copies).

The Edge shard is a rebuildable index over this log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL,               -- 'upsert' | 'sync-pull' | 'reinforce' | audit rows
    memory_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    payload TEXT,                   -- JSON Memory payload for upsert; counters for reinforce
    ts REAL NOT NULL,
    shard TEXT NOT NULL DEFAULT 'private'
);
CREATE INDEX IF NOT EXISTS journal_memory_id ON journal (memory_id);
-- review_rows() filters on op on every dashboard/serve refresh; without this
-- index it full-scans the whole log, which grows with a year of writes.
CREATE INDEX IF NOT EXISTS journal_op ON journal (op);
CREATE TABLE IF NOT EXISTS tombstones (
    memory_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    shard TEXT NOT NULL DEFAULT 'private'
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- e.g. 'session-start-recall'
    ts REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_details (
    slot INTEGER PRIMARY KEY,        -- fixed-size ring; detail-bearing events only
    event_id INTEGER NOT NULL UNIQUE,
    detail TEXT NOT NULL             -- JSON: prompt + surfaced/saved texts, newest N only
);
"""


@dataclass
class JournalEntry:
    seq: int
    op: str
    memory_id: str
    payload: dict[str, Any] | None
    ts: float
    shard: str = "private"

    def to_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "op": self.op, "memory_id": self.memory_id,
             "payload": self.payload, "ts": self.ts, "shard": self.shard},
            ensure_ascii=False,
        )


class Journal:
    # Keep the prompt/surfaced/saved text of only the newest few events, so the
    # activity view stays useful without the events table growing text forever.
    _DETAIL_KEEP = 50
    _DETAIL_NEXT_KEY = "event_detail_next_slot"

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        existed = path.exists()
        # One shared connection, guarded by an internal lock: SQLite objects
        # must not be used from two threads at once, and callers (daemon
        # handler threads) are not trusted to serialize reads themselves.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        if not existed:
            os.chmod(path, 0o600)  # memories are private by default
        # WAL for concurrency-friendly reads; FULL sync so an acked append
        # survives power loss, not just process death. Writes are human-scale.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        # Pre-shard journals lack the shard column; migrate in place.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(journal)")}
        if "shard" not in cols:
            self._conn.execute(
                "ALTER TABLE journal ADD COLUMN shard TEXT NOT NULL DEFAULT 'private'"
            )
        tcols = {r[1] for r in self._conn.execute("PRAGMA table_info(tombstones)")}
        if "shard" not in tcols:
            # Default 'private' is the safe direction: legacy tombstones are
            # never pushed to a relay.
            self._conn.execute(
                "ALTER TABLE tombstones ADD COLUMN shard TEXT NOT NULL DEFAULT 'private'"
            )
        ecols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
        if "detail" in ecols:
            # Interim builds stored detail inline on the append-only events row.
            # Move the newest kept details into the bounded ring table and clear
            # inline text so future writes do not grow old event pages forever.
            rows = self._conn.execute(
                "SELECT id, detail FROM events WHERE detail IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (self._DETAIL_KEEP,),
            ).fetchall()
            if rows:
                for slot, (event_id, detail) in enumerate(reversed(rows)):
                    self._conn.execute(
                        "INSERT OR REPLACE INTO event_details (slot, event_id, detail) "
                        "VALUES (?, ?, ?)",
                        (slot, event_id, detail),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (self._DETAIL_NEXT_KEY, str(len(rows))),
                )
                self._conn.execute(
                    "UPDATE events SET detail = NULL WHERE detail IS NOT NULL")
        self._conn.commit()

    # -- append (the ack point) -------------------------------------------

    def append(
        self,
        op: str,
        memory_id: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        shard: str = "private",
        ts: float | None = None,
    ) -> int:
        """Durably record one write intent. Returns its seq.
        A duplicate idempotency_key returns the existing seq (retry-safe).
        `ts` defaults to now; sync passes the origin timestamp so LWW clocks
        stay stable across devices."""
        return self.append_many([(op, memory_id, payload, idempotency_key)],
                                shard=shard, ts=ts)[-1]

    def append_many(
        self,
        intents: list[tuple[str, str, dict[str, Any] | None, str | None]],
        shard: str = "private",
        ts: float | None = None,
    ) -> list[int]:
        """Atomically record several intents (e.g. supersede = 2 upserts)."""
        seqs: list[int] = []
        with self._lock, self._conn:
            for op, memory_id, payload, key in intents:
                if key is not None:
                    row = self._conn.execute(
                        "SELECT seq FROM journal WHERE idempotency_key = ?", (key,)
                    ).fetchone()
                    if row:
                        seqs.append(row[0])
                        continue
                cur = self._conn.execute(
                    "INSERT INTO journal (op, memory_id, idempotency_key, payload, ts, shard)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (op, memory_id, key,
                     json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                     ts if ts is not None else time.time(), shard),
                )
                seqs.append(cur.lastrowid)
        return seqs

    def reinforce(self, memory_id: str, payload: dict[str, Any],
                  shard: str = "private") -> int:
        """Record an access bump, collapsing to at most one reinforce row per
        memory. Access counts are absolute, so only the latest matters; keeping
        one row per id (instead of appending per read) means the journal — the
        source of truth this store exports, syncs and replays — grows with
        write volume, not read volume. A memory recalled for years stays one
        row. Returns the new row's seq: because `seq` is AUTOINCREMENT (never
        reused, even after a delete), the replacement always sits above the row
        it replaced, so replay and flush ordering are unaffected. That
        no-reuse guarantee is load-bearing — do not drop AUTOINCREMENT."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM journal WHERE memory_id = ? AND op = 'reinforce'",
                (memory_id,))
            cur = self._conn.execute(
                "INSERT INTO journal (op, memory_id, idempotency_key, payload, ts, shard)"
                " VALUES ('reinforce', ?, NULL, ?, ?, ?)",
                (memory_id, json.dumps(payload, ensure_ascii=False),
                 time.time(), shard))
            return cur.lastrowid

    # -- flush high-water mark --------------------------------------------

    @property
    def flushed_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='flushed_seq'"
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_flushed(self, seq: int) -> None:
        """Record that Edge has durably flushed everything up to `seq`.
        Monotonic by construction: the mark never moves backwards."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('flushed_seq', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(excluded.value AS INTEGER),"
                " CAST(value AS INTEGER))",
                (str(seq),),
            )

    def pending(self) -> list[JournalEntry]:
        """Intents not yet covered by an Edge flush — replayed on open."""
        with self._lock:
            return list(self._iter_rows("WHERE seq > ?", (self.flushed_seq,)))

    @property
    def last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM journal").fetchone()
        return row[0] or 0

    @property
    def row_count(self) -> int:
        """Actual rows in the log (what determines size). Diverges from
        last_seq once reinforce rows collapse: the AUTOINCREMENT keeps
        climbing, the row count does not."""
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    # -- forgetting ---------------------------------------------------------

    def hard_forget(self, memory_id: str, shard: str = "private") -> None:
        """Purge every trace of a memory from the log, then VACUUM so the
        content doesn't survive in free pages. Leaves a content-free
        tombstone (tagged with its shard, so sync knows where it may
        propagate) that suppresses the id."""
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM journal WHERE memory_id = ?", (memory_id,))
                # A review row is keyed by the NEW memory's id but its payload
                # carries the target's id and merged_text (the target's content
                # folded in). Forgetting the target must purge those rows too,
                # or its bytes survive in the log and every JSONL export.
                self._conn.execute(
                    "DELETE FROM journal WHERE json_extract(payload, '$.target_id') = ?",
                    (memory_id,),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO tombstones (memory_id, ts, shard)"
                    " VALUES (?, ?, ?)",
                    (memory_id, time.time(), shard),
                )
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def add_tombstone(self, memory_id: str, shard: str) -> None:
        """Record a tombstone for an id we never held (pulled from sync):
        suppresses any future resurrection, without the VACUUM a local
        hard-forget pays."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tombstones (memory_id, ts, shard)"
                " VALUES (?, ?, ?)",
                (memory_id, time.time(), shard),
            )

    def checkpoint(self) -> None:
        """Fold the SQLite WAL into the main db file — required before a
        snapshot copies journal.db, or the backup misses recent writes."""
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def vacuum(self) -> None:
        """Reclaim freed pages and truncate the WAL. Used by crash recovery
        after an interrupted hard-forget: the rows were deleted but their
        bytes can still linger in free pages / un-checkpointed WAL frames, so
        this re-issues what hard_forget's own VACUUM would have."""
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def is_tombstoned(self, memory_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM tombstones WHERE memory_id = ?", (memory_id,)
                ).fetchone()
                is not None
            )

    def tombstones(self, shard: str | None = None) -> set[str]:
        with self._lock:
            if shard is None:
                rows = self._conn.execute("SELECT memory_id FROM tombstones")
            else:
                rows = self._conn.execute(
                    "SELECT memory_id FROM tombstones WHERE shard = ?", (shard,)
                )
            return {r[0] for r in rows}

    def last_ts_for(self, memory_id: str) -> float:
        """The LWW clock for sync: the last time this memory's CONTENT changed.
        Only content-writing ops count. Read bumps (`reinforce`) and audit rows
        (`noop`, `review`) carry fresh timestamps but change nothing, so
        counting them would let a local recall or a local dedup shadow a genuine
        remote edit (older-but-real) and silently drop it on the next pull."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM journal WHERE memory_id = ?"
                " AND op IN ('upsert', 'sync-pull')",
                (memory_id,),
            ).fetchone()
        return row[0] or 0.0

    # -- generic meta ---------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    # -- trigger measurement ---------------------------------------------------

    def log_event(self, kind: str, hits: int = 0, detail: str | None = None) -> None:
        """Operational counters (proactive-trigger measurement), not memory
        content: excluded from export, ignored by replay. `detail` (a small
        JSON snippet) is retained for the newest `_DETAIL_KEEP` events only."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (kind, ts, hits) VALUES (?, ?, ?)",
                (kind, time.time(), hits),
            )
            if detail is not None:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    (self._DETAIL_NEXT_KEY,),
                ).fetchone()
                next_slot = int(row[0]) if row else 0
                slot = next_slot % self._DETAIL_KEEP
                self._conn.execute(
                    "INSERT INTO event_details (slot, event_id, detail) VALUES (?, ?, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET "
                    "event_id = excluded.event_id, detail = excluded.detail",
                    (slot, cur.lastrowid, detail),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (self._DETAIL_NEXT_KEY, str(next_slot + 1)),
                )
                self._append_activity_log(kind, hits, detail)

    # DEV-ONLY (remove before release): mirror every detail-bearing event to an
    # append-only activity.jsonl next to the journal so we can study offline what
    # prompts came in, what was surfaced, and what was captured — including the
    # older history the bounded event_details ring overwrites. Best-effort: a
    # study log must never break a real write.
    def _append_activity_log(self, kind: str, hits: int, detail: str) -> None:
        try:
            try:
                payload = json.loads(detail)
            except (ValueError, TypeError):
                payload = {"detail": detail}
            record = {"ts": time.time(), "kind": kind, "hits": hits, **payload}
            with open(self._path.with_name("activity.jsonl"), "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def recent_events(self, limit: int = 50) -> list[tuple[str, float, int, str | None]]:
        """Newest firings first: (kind, ts, hits, detail) — feeds `engram log`."""
        with self._lock:
            return self._conn.execute(
                "SELECT e.kind, e.ts, e.hits, d.detail "
                "FROM events e LEFT JOIN event_details d ON d.event_id = e.id "
                "ORDER BY e.id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def event_summary(self) -> dict[str, dict[str, int]]:
        """Per-kind: fired count + how often at least one memory surfaced —
        the M1 recall-at-the-right-moment proxy."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*), SUM(hits > 0) FROM events GROUP BY kind"
            ).fetchall()
        return {k: {"fired": c, "with_hits": h or 0} for k, c, h in rows}

    # -- export / replay -----------------------------------------------------

    def entries(self) -> list[JournalEntry]:
        """Full log in seq order (replay = apply in order, last write wins).
        A consistent snapshot: materialized under the lock, so an export
        never interleaves with a half-applied multi-row transaction."""
        with self._lock:
            return list(self._iter_rows("", ()))

    def entries_after(self, seq: int, shard: str) -> list[JournalEntry]:
        """One shard's rows past `seq`, in seq order — the sync push delta.
        Filters in SQL (seq is the primary key) instead of scanning and
        JSON-decoding the whole log on every push, which grows for years."""
        with self._lock:
            return list(self._iter_rows(
                "WHERE seq > ? AND shard = ?", (seq, shard)))

    def review_rows(self) -> list[JournalEntry]:
        """Just the review / review_resolved rows, in seq order. Scanning the
        whole log (entries()) to find these means json-parsing every upsert
        payload — wasteful on a store with a year of writes; the review queue
        reads this on every dashboard/serve refresh."""
        with self._lock:
            return list(self._iter_rows(
                "WHERE op IN ('review', 'review_resolved')", ()))

    def export_jsonl(self, fp: TextIO) -> int:
        """Dump the whole journal as JSONL. Returns row count."""
        n = 0
        for entry in self.entries():
            fp.write(entry.to_json() + "\n")
            n += 1
        with self._lock:
            rows = sorted(self._conn.execute(
                "SELECT memory_id, shard FROM tombstones"))
        for tid, tshard in rows:
            fp.write(json.dumps(
                {"op": "tombstone", "memory_id": tid, "shard": tshard}) + "\n")
            n += 1
        return n

    def import_jsonl(self, fp: TextIO, scrub: Callable[[str], str] | None = None) -> int:
        """Replay a JSONL export into this journal (restore/migration).
        `scrub` runs over text fields — imported files may not come from a
        store that redacted on write."""
        n = 0
        with self._lock, self._conn:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("op") == "tombstone":
                    self._conn.execute(
                        "INSERT OR REPLACE INTO tombstones (memory_id, ts, shard) "
                        "VALUES (?, ?, ?)",
                        (row["memory_id"], time.time(), row.get("shard", "private")),
                    )
                else:
                    payload = row.get("payload")
                    if payload is not None and scrub is not None:
                        for key in ("text", "source_text", "dropped_text",
                                    "source_ref", "merged_text"):
                            if isinstance(payload.get(key), str):
                                payload[key] = scrub(payload[key])
                    if row["op"] == "reinforce":
                        # Import can run against a non-empty journal; collapse
                        # to one reinforce row per id like reinforce() does, so
                        # overlapping imports don't leak read-volume rows.
                        self._conn.execute(
                            "DELETE FROM journal WHERE memory_id = ? AND op = 'reinforce'",
                            (row["memory_id"],))
                    self._conn.execute(
                        "INSERT INTO journal (op, memory_id, payload, ts, shard)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (row["op"], row["memory_id"],
                         json.dumps(payload, ensure_ascii=False)
                         if payload is not None else None,
                         row.get("ts", time.time()),
                         row.get("shard", "private")),
                    )
                n += 1
        return n

    # -- internals ----------------------------------------------------------

    def _iter_rows(self, where: str, params: tuple) -> Iterator[JournalEntry]:
        cur = self._conn.execute(
            f"SELECT seq, op, memory_id, payload, ts, shard FROM journal {where} ORDER BY seq",
            params,
        )
        for seq, op, memory_id, payload, ts, shard in cur:
            yield JournalEntry(
                seq=seq, op=op, memory_id=memory_id,
                payload=json.loads(payload) if payload else None, ts=ts,
                shard=shard or "private",
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

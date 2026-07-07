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
    op TEXT NOT NULL,               -- 'upsert' | 'delete' | 'reinforce' | audit rows
    memory_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    payload TEXT,                   -- JSON Memory payload for upsert; counters for reinforce
    ts REAL NOT NULL,
    shard TEXT NOT NULL DEFAULT 'private'
);
CREATE INDEX IF NOT EXISTS journal_memory_id ON journal (memory_id);
CREATE TABLE IF NOT EXISTS tombstones (
    memory_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
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
        self._conn.commit()

    # -- append (the ack point) -------------------------------------------

    def append(
        self,
        op: str,
        memory_id: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        shard: str = "private",
    ) -> int:
        """Durably record one write intent. Returns its seq.
        A duplicate idempotency_key returns the existing seq (retry-safe)."""
        return self.append_many([(op, memory_id, payload, idempotency_key)], shard=shard)[-1]

    def append_many(
        self,
        intents: list[tuple[str, str, dict[str, Any] | None, str | None]],
        shard: str = "private",
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
                     time.time(), shard),
                )
                seqs.append(cur.lastrowid)
        return seqs

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

    # -- forgetting ---------------------------------------------------------

    def hard_forget(self, memory_id: str) -> None:
        """Purge every trace of a memory from the log, then VACUUM so the
        content doesn't survive in free pages. Leaves a content-free
        tombstone that suppresses the id."""
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM journal WHERE memory_id = ?", (memory_id,))
                self._conn.execute(
                    "INSERT OR REPLACE INTO tombstones (memory_id, ts) VALUES (?, ?)",
                    (memory_id, time.time()),
                )
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

    def tombstones(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT memory_id FROM tombstones")}

    # -- export / replay -----------------------------------------------------

    def entries(self) -> list[JournalEntry]:
        """Full log in seq order (replay = apply in order, last write wins).
        A consistent snapshot: materialized under the lock, so an export
        never interleaves with a half-applied multi-row transaction."""
        with self._lock:
            return list(self._iter_rows("", ()))

    def export_jsonl(self, fp: TextIO) -> int:
        """Dump the whole journal as JSONL. Returns row count."""
        n = 0
        for entry in self.entries():
            fp.write(entry.to_json() + "\n")
            n += 1
        for tid in sorted(self.tombstones()):
            fp.write(json.dumps({"op": "tombstone", "memory_id": tid}) + "\n")
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
                        "INSERT OR REPLACE INTO tombstones (memory_id, ts) VALUES (?, ?)",
                        (row["memory_id"], time.time()),
                    )
                else:
                    payload = row.get("payload")
                    if payload is not None and scrub is not None:
                        for key in ("text", "source_text", "dropped_text", "source_ref"):
                            if isinstance(payload.get(key), str):
                                payload[key] = scrub(payload[key])
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

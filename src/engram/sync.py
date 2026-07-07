"""Opt-in sync: pool chosen shards through a Qdrant Cloud collection.

Opt-in is structural: the `private` shard has no sync path at all — only
`me-synced` and `shared:<group>` shards can be configured, per shard, with
an explicit `engram sync setup`. The Cloud collection is a relay, not a
search index, and it holds ciphertext:

- each memory travels as {id, encrypted blob, ts, device, op} with a
  1-dim dummy vector — no embeddings, no plaintext leave the device.
- the key (`sync.key`) is generated locally and never uploaded; copy it to
  your other devices yourself (it's one small file).
- pull is a scroll of the union; the MERGE IS OURS: last-write-wins by
  entry timestamp, and a tombstone anywhere suppresses the id everywhere.
- pulled memories re-embed locally, so devices can even run different
  embedding models.

Push is plain upserts; pull is scroll — exactly the app-side pattern Edge
supports against a standard collection, no server cooperation needed.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from engram.config import Config
from engram.models import Memory

if TYPE_CHECKING:
    from engram.store import MemoryStore

VECTOR_NAME = "relay"  # 1-dim dummy; the collection is a relay, not an index


class SyncError(RuntimeError):
    pass


@dataclass
class SyncTarget:
    shard: str
    url: str
    api_key: str | None
    collection: str


def _sync_config_path(config: Config):
    return config.data_dir / "sync.json"


def load_targets(config: Config) -> dict[str, SyncTarget]:
    """Targets are re-validated on every load: a hand-edited or corrupted
    sync.json must never be able to give the private shard a sync path."""
    from engram.store import validate_shard

    path = _sync_config_path(config)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    targets: dict[str, SyncTarget] = {}
    for shard, entry in raw.items():
        validate_shard(shard)
        if shard == "private":
            raise SyncError(
                "sync.json names the private shard — refusing to sync anything"
                " until that entry is removed"
            )
        targets[shard] = SyncTarget(shard=shard, **entry)
    return targets


def save_target(config: Config, target: SyncTarget) -> None:
    import os

    from engram.store import validate_shard

    validate_shard(target.shard)
    if target.shard == "private":
        raise SyncError("the private shard never syncs — that's the point of it")
    path = _sync_config_path(config)
    raw = json.loads(path.read_text()) if path.exists() else {}
    raw[target.shard] = {
        "url": target.url, "api_key": target.api_key, "collection": target.collection,
    }
    path.write_text(json.dumps(raw, indent=2) + "\n")
    os.chmod(path, 0o600)


def _key_bytes(config: Config) -> bytes:
    import os

    path = config.data_dir / "sync.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        os.chmod(path, 0o600)
    return path.read_bytes().strip()


def sync_key(config: Config) -> Fernet:
    """The local encryption key: generated once, copied between devices by
    the owner, never uploaded."""
    return Fernet(_key_bytes(config))


def _tombstone_mac(key: bytes, memory_id: str) -> str:
    """Tombstones are content-free, so they can't carry ciphertext — they
    carry a MAC instead. A relay that can't produce this can't forge
    deletions."""
    return hmac_mod.new(key, f"tombstone:{memory_id}".encode(), hashlib.sha256).hexdigest()


def device_id(config: Config) -> str:
    path = config.data_dir / "device"
    if not path.exists():
        path.write_text(uuid.uuid4().hex + "\n")
    return path.read_text().strip()


class ShardSync:
    def __init__(self, store: MemoryStore, target: SyncTarget, client=None):
        from qdrant_client import QdrantClient

        self.store = store
        self.target = target
        self.fernet = sync_key(store.config)
        self._mac_key = _key_bytes(store.config)
        self.device = device_id(store.config)
        self.client = client or QdrantClient(url=target.url, api_key=target.api_key)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        from qdrant_client import models as qm

        if not self.client.collection_exists(self.target.collection):
            self.client.create_collection(
                self.target.collection,
                vectors_config={VECTOR_NAME: qm.VectorParams(size=1,
                                                             distance=qm.Distance.DOT)},
            )

    # -- push --------------------------------------------------------------------

    def push(self) -> int:
        """Upload journal entries past the per-shard high-water mark, plus
        any tombstones not yet propagated. Normal upserts only."""
        from qdrant_client import models as qm

        mark_key = f"sync_pushed:{self.target.shard}"
        pushed_seq = int(self.store.journal.get_meta(mark_key) or 0)
        points: list[qm.PointStruct] = []
        top = pushed_seq

        for entry in self.store.journal.entries():
            if entry.seq <= pushed_seq or entry.shard != self.target.shard:
                continue
            top = max(top, entry.seq)
            if entry.op == "upsert" and entry.payload is not None:
                # id, ts and shard live INSIDE the ciphertext: a relay that
                # swaps blobs between ids, replays old timestamps, or moves a
                # point across collections fails verification on pull.
                blob = self.fernet.encrypt(json.dumps(
                    {"id": entry.memory_id, "ts": entry.ts,
                     "shard": self.target.shard, "payload": entry.payload},
                    ensure_ascii=False,
                ).encode()).decode()
                points.append(qm.PointStruct(
                    id=entry.memory_id,
                    vector={VECTOR_NAME: [0.0]},
                    payload={"op": "upsert", "blob": blob, "ts": entry.ts,
                             "device": self.device},
                ))
            elif entry.op == "delete":
                points.append(self._tombstone_point(entry.memory_id, entry.ts))

        # Tombstones for THIS shard are content-free and idempotent: always
        # propagate, so a hard forget overwrites any ciphertext still in the
        # relay. Private tombstones never reach any relay (shard filter).
        tombstoned = self.store.journal.tombstones(shard=self.target.shard)
        for mid in tombstoned:
            points.append(self._tombstone_point(mid, None))

        # A hard-forget that raced this scan must not re-upload ciphertext.
        points = [pt for pt in points
                  if pt.payload.get("op") == "tombstone"
                  or str(pt.id) not in tombstoned]

        if points:
            # Last write per id wins within the batch (upsert order).
            self.client.upsert(self.target.collection, points=points)
        self.store.journal.set_meta(mark_key, str(top))
        return len(points)

    def _tombstone_point(self, memory_id: str, ts: float | None):
        from qdrant_client import models as qm

        from engram.models import now_ts

        return qm.PointStruct(
            id=memory_id,
            vector={VECTOR_NAME: [0.0]},
            payload={"op": "tombstone", "ts": ts or now_ts(), "device": self.device,
                     "mac": _tombstone_mac(self._mac_key, memory_id)},
        )

    # -- pull ---------------------------------------------------------------------

    def pull(self) -> dict[str, int]:
        """Scroll the union and merge: LWW by timestamp, tombstones win
        unconditionally, pulled text re-embeds locally through the normal
        journaled write path."""
        report = {"applied": 0, "tombstoned": 0, "skipped": 0}
        offset = None
        while True:
            records, offset = self.client.scroll(
                self.target.collection, limit=256, offset=offset, with_payload=True,
            )
            for record in records:
                self._merge_one(str(record.id), record.payload or {}, report)
            if offset is None or not records:
                break
        return report

    def _merge_one(self, memory_id: str, payload: dict, report: dict) -> None:
        op = payload.get("op")

        if op == "tombstone":
            if not hmac_mod.compare_digest(
                str(payload.get("mac") or ""),
                _tombstone_mac(self._mac_key, memory_id),
            ):
                report["skipped"] += 1  # a relay cannot forge deletions
                return
            if self.store.journal.is_tombstoned(memory_id):
                report["skipped"] += 1
                return
            if self.store.get(memory_id) is not None:
                self.store.forget(memory_id, mode="hard")
            else:
                # Never held it, but record the suppression: a replayed old
                # upsert must not resurrect the id later.
                self.store.journal.add_tombstone(memory_id, self.target.shard)
            report["tombstoned"] += 1
            return

        if op != "upsert" or self.store.journal.is_tombstoned(memory_id):
            report["skipped"] += 1
            return
        try:
            data = json.loads(self.fernet.decrypt(payload["blob"].encode()))
        except (InvalidToken, KeyError, ValueError):
            report["skipped"] += 1  # foreign key or garbage: not ours to apply
            return
        # The authenticated identity lives inside the ciphertext.
        if data.get("id") != memory_id or data.get("shard") != self.target.shard:
            report["skipped"] += 1  # blob moved between ids/collections: reject
            return
        remote_ts = float(data.get("ts") or 0.0)

        owner_shard = self.store.shard_of(memory_id)
        if owner_shard is not None and owner_shard != self.target.shard:
            report["skipped"] += 1  # id already lives in another trust boundary
            return

        memory = Memory.from_payload(memory_id, data["payload"])
        if self.store.apply_synced(memory, self.target.shard, remote_ts):
            report["applied"] += 1
        else:
            report["skipped"] += 1


def sync_shard(store: MemoryStore, shard: str, client=None) -> dict[str, int]:
    targets = load_targets(store.config)
    if shard not in targets:
        raise SyncError(
            f"shard {shard!r} has no sync target; run engram sync setup first"
        )
    s = ShardSync(store, targets[shard], client=client)
    pushed = s.push()
    report = s.pull()
    report["pushed"] = pushed
    return report

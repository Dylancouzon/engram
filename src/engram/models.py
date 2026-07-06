"""Core data model for engram memories.

A Memory is the atomic unit: one self-contained fact, preference, decision,
or event, with provenance and temporal validity. Memories are stored as
points in Qdrant Edge (vectors + payload) and journaled in SQLite (source
of truth). The payload schema here is the durable contract — the JSONL
export is a dump of exactly these fields.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Sentinel for "valid indefinitely" — lets validity checks be a plain
# numeric range filter instead of an is-null special case. Year 9999.
VALID_FOREVER = 253402300800.0


class MemoryType(StrEnum):
    SEMANTIC = "semantic"  # facts/preferences: deduped, superseded, decays slowly
    EPISODIC = "episodic"  # events: timestamped, decays faster
    PROCEDURAL = "procedural"  # how-tos/corrections-of-process: sticky, high importance


class Op(StrEnum):
    """Conflict-resolution verdicts for a new fact vs an existing memory."""

    ADD = "ADD"
    UPDATE = "UPDATE"
    SUPERSEDE = "SUPERSEDE"
    NOOP = "NOOP"


def new_memory_id(owner_ns: uuid.UUID) -> str:
    """Globally-unique, owner-namespaced id: LWW-by-id never collides
    across devices or people sharing a pool."""
    return str(uuid.uuid5(owner_ns, uuid.uuid4().hex))


def now_ts() -> float:
    return time.time()


@dataclass
class Memory:
    id: str
    text: str
    type: MemoryType = MemoryType.SEMANTIC
    scope: str = "default"  # payload scope within the shard: work | personal | project:<x>
    tags: list[str] = field(default_factory=list)
    surface: str = "cli"  # which app wrote it
    source_text: str | None = None  # raw (redacted) excerpt extraction ran on
    source_ref: str | None = None  # provenance pointer (file, chat, url)
    created_at: float = field(default_factory=now_ts)  # when learned (UTC epoch)
    event_time: float | None = None  # when it happened, if different
    valid_from: float = field(default_factory=now_ts)
    valid_to: float = VALID_FOREVER
    superseded_by: str | None = None
    importance: float = 0.5  # salience 0-1
    access_count: int = 0
    last_accessed: float | None = None
    embedding_model: str = ""  # dense model pin, set on embed

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "type": self.type.value,
            "scope": self.scope,
            "tags": self.tags,
            "surface": self.surface,
            "source_text": self.source_text,
            "source_ref": self.source_ref,
            "created_at": self.created_at,
            "event_time": self.event_time,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "superseded_by": self.superseded_by,
            "importance": self.importance,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "embedding_model": self.embedding_model,
        }

    @classmethod
    def from_payload(cls, id: str, payload: dict[str, Any]) -> Memory:
        return cls(
            id=id,
            text=payload["text"],
            type=MemoryType(payload.get("type", "semantic")),
            scope=payload.get("scope", "default"),
            tags=list(payload.get("tags") or []),
            surface=payload.get("surface", "cli"),
            source_text=payload.get("source_text"),
            source_ref=payload.get("source_ref"),
            created_at=payload.get("created_at", 0.0),
            event_time=payload.get("event_time"),
            valid_from=payload.get("valid_from", 0.0),
            valid_to=payload.get("valid_to", VALID_FOREVER),
            superseded_by=payload.get("superseded_by"),
            importance=payload.get("importance", 0.5),
            access_count=int(payload.get("access_count") or 0),
            last_accessed=payload.get("last_accessed"),
            embedding_model=payload.get("embedding_model", ""),
        )

    @property
    def is_valid(self) -> bool:
        now = now_ts()
        return self.valid_from <= now < self.valid_to


@dataclass
class RecallHit:
    memory: Memory
    score: float  # final blended score (similarity x importance x recency)
    similarity: float  # raw retrieval score before rescoring

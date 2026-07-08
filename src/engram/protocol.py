"""The versioned local API — engram's durable "works with everything"
contract. MCP, the CLI, and importers are all thin clients speaking this.

Transport: line-delimited JSON over a Unix domain socket (0600).

    -> {"v": 1, "id": "r1", "client": "claude-code", "method": "recall",
        "params": {"query": "..."}}
    <- {"v": 1, "id": "r1", "ok": true, "result": {...}}
    <- {"v": 1, "id": "r1", "ok": false,
        "error": {"code": "scope_denied", "message": "..."}}

The envelope is deliberately boring: any language can speak it without a
library, and `v` lets the daemon serve old clients after upgrades.
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO

from engram.models import Memory, RecallHit
from engram.store import WriteAction

PROTOCOL_VERSION = 1
# Generous: requests are small, but an export response carries the whole
# journal in one frame until a streaming export lands (M2).
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

# Error codes (stable API surface — clients switch on these)
E_UNREGISTERED = "unregistered_client"
E_SCOPE_DENIED = "scope_denied"
E_BAD_REQUEST = "bad_request"
E_UNSUPPORTED = "unsupported_version"
E_NOT_FOUND = "not_found"
E_REFUSED = "write_refused"
E_INTERNAL = "internal"


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
    stream.flush()


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """One JSON line -> dict. None on clean EOF."""
    line = stream.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError(E_BAD_REQUEST, "message too large")
    try:
        message = json.loads(line)
    except ValueError as e:
        raise ProtocolError(E_BAD_REQUEST, f"invalid JSON: {e}") from e
    if not isinstance(message, dict):
        raise ProtocolError(E_BAD_REQUEST, "message must be a JSON object")
    return message


def ok_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}


def error_response(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


# -- wire forms of the core types ---------------------------------------------


def memory_to_wire(m: Memory) -> dict[str, Any]:
    return {"id": m.id, **m.to_payload()}


def memory_from_wire(data: dict[str, Any]) -> Memory:
    data = dict(data)
    return Memory.from_payload(data.pop("id"), data)


def hit_to_wire(h: RecallHit) -> dict[str, Any]:
    return {
        "memory": memory_to_wire(h.memory),
        "score": h.score,
        "similarity": h.similarity,
    }


def hit_from_wire(data: dict[str, Any]) -> RecallHit:
    return RecallHit(
        memory=memory_from_wire(data["memory"]),
        score=data["score"],
        similarity=data["similarity"],
    )


def review_to_wire(item: Any) -> dict[str, Any]:
    return {
        "seq": item.seq,
        "proposed_op": item.proposed_op.value,
        "new": memory_to_wire(item.new),
        "target": memory_to_wire(item.target),
        "confidence": item.confidence,
        "merged_text": item.merged_text,
        "shard": item.shard,
    }


def review_from_wire(data: dict[str, Any]) -> Any:
    from engram.models import Op
    from engram.store import ReviewItem

    return ReviewItem(
        seq=data["seq"],
        proposed_op=Op(data["proposed_op"]),
        new=memory_from_wire(data["new"]),
        target=memory_from_wire(data["target"]),
        confidence=data.get("confidence", 0.0),
        merged_text=data.get("merged_text"),
        shard=data.get("shard", "private"),
    )


def action_to_wire(a: WriteAction) -> dict[str, Any]:
    return {
        "op": a.op.value,
        "memory": memory_to_wire(a.memory) if a.memory else None,
        "target": memory_to_wire(a.target) if a.target else None,
        "confidence": a.confidence,
        "redaction_hits": a.redaction_hits,
        "queued_review": a.queued_review,
    }


def action_from_wire(data: dict[str, Any]) -> WriteAction:
    from engram.models import Op

    return WriteAction(
        op=Op(data["op"]),
        memory=memory_from_wire(data["memory"]) if data.get("memory") else None,
        target=memory_from_wire(data["target"]) if data.get("target") else None,
        confidence=data.get("confidence", 1.0),
        redaction_hits=list(data.get("redaction_hits") or []),
        queued_review=bool(data.get("queued_review", False)),
    )

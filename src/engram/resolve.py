"""Conflict resolution: what does a new fact do to what's already known?

On every write, the top-k similar existing memories are retrieved and a
local judge classifies the new fact against them:

    ADD        genuinely new -> insert
    UPDATE     same fact, more/better detail -> rewrite target with merged text
    SUPERSEDE  contradicts target; new fact is now true -> invalidate target, insert new
    NOOP       already known, nothing new -> skip (reinforce target)

Only high-confidence UPDATE/SUPERSEDE verdicts are auto-applied; anything
uncertain degrades to ADD — a duplicate memory is recoverable, a wrongful
supersede is not (M1 adds the review queue). Without a local model there is
no judge and every write is an ADD; corrections then need explicit
`engram forget`.
"""

from __future__ import annotations

from dataclasses import dataclass

from engram.llm import LocalLLM
from engram.models import Memory, Op

_SYSTEM = """You maintain a personal long-term memory store. Given a NEW fact and a list of
EXISTING memories (each with an index), decide what the new fact does to the store.

Ops:
- "ADD": the new fact is about something none of the existing memories cover.
- "UPDATE": same underlying fact as one existing memory, adding or refining detail
  without contradicting it. Provide "text": the single merged replacement.
- "SUPERSEDE": directly contradicts or replaces one existing memory; the new fact
  is now true and the old one is stale (corrections, changed preferences, moves).
- "NOOP": the new fact is already fully captured by an existing memory.

Rules:
- Compare meaning, not wording. "Dylan moved to Berlin" supersedes "Dylan lives in Paris".
- Different entities or attributes are never conflicts: pick ADD.
- "target": the index of the affected existing memory (UPDATE/SUPERSEDE/NOOP), else null.
- "confidence": 0.0-1.0 that your op is right. Be conservative: if unsure
  whether facts truly conflict, lower confidence or pick ADD.

Respond with JSON: {"op": "ADD"|"UPDATE"|"SUPERSEDE"|"NOOP", "target": int|null,
"confidence": float, "text": string|null}"""


@dataclass
class Verdict:
    op: Op
    target: Memory | None
    confidence: float
    merged_text: str | None = None


def judge(new_fact: str, candidates: list[Memory], llm: LocalLLM | None) -> Verdict:
    """Classify `new_fact` against similar existing memories. Fail-safe:
    no model, no candidates, or malformed output all resolve to ADD."""
    if not candidates or llm is None or not llm.available():
        return Verdict(op=Op.ADD, target=None, confidence=1.0)

    listing = "\n".join(f"{i}: {m.text}" for i, m in enumerate(candidates))
    result = llm.generate_json(
        _SYSTEM, f"NEW fact:\n{new_fact}\n\nEXISTING memories:\n{listing}"
    )
    if not isinstance(result, dict):
        return Verdict(op=Op.ADD, target=None, confidence=0.0)

    try:
        op = Op(str(result.get("op", "ADD")).upper())
    except ValueError:
        return Verdict(op=Op.ADD, target=None, confidence=0.0)

    target: Memory | None = None
    idx = result.get("target")
    if isinstance(idx, int) and 0 <= idx < len(candidates):
        target = candidates[idx]
    if op is not Op.ADD and target is None:
        # An op that needs a target but names none is not actionable.
        return Verdict(op=Op.ADD, target=None, confidence=0.0)

    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    merged = result.get("text")
    if op is Op.UPDATE and not (isinstance(merged, str) and merged.strip()):
        # UPDATE without replacement text can't be applied safely.
        return Verdict(op=Op.ADD, target=None, confidence=0.0)

    return Verdict(
        op=op,
        target=target,
        confidence=confidence,
        merged_text=merged.strip() if isinstance(merged, str) else None,
    )

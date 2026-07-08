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

from engram.llm import LocalLLM, clamp01
from engram.models import Memory, Op

_SYSTEM = """You maintain a personal long-term memory store. Given a NEW fact and a list of
EXISTING memories (each with an index), decide what the new fact does to the store.

Ops:
- "ADD": the new fact is about something none of the existing memories cover.
- "UPDATE": same underlying fact as one existing memory, adding or refining detail
  without contradicting it. Provide "text": the single merged replacement.
- "SUPERSEDE": directly contradicts or replaces one existing memory; the new fact
  is now true and the old one is stale (corrections, changed values, moves).
- "NOOP": the new fact adds no information beyond an existing memory (duplicate,
  paraphrase, or a vaguer version of something already known).

Rules:
- Compare meaning, not wording. "Dylan moved to Berlin" supersedes "Dylan lives in Paris".
- SUPERSEDE only when the SAME attribute of the SAME entity changed value. Different
  entities or different attributes are never conflicts: pick ADD.
- A one-time event never supersedes a standing fact: "visited Paris last weekend"
  does not touch "lives in Berlin" -> ADD.
- An opinion, plan, or comment ABOUT a fact never supersedes the fact itself:
  "thinks the launch date is too aggressive" does not touch "launch is March 17" -> ADD.
- If the new fact extends a set (a second cat, another team member), UPDATE with a
  merged text or ADD - never SUPERSEDE the sibling.
- NOOP when the existing memory already implies the new statement, even worded
  differently. If the new statement is the more detailed one, that is UPDATE, not NOOP.
- "target": the index of the affected existing memory (UPDATE/SUPERSEDE/NOOP), else null.
- "confidence": 0.0-1.0 that your op is right. If unsure whether facts truly
  conflict, lower confidence or pick ADD.

Examples:
- existing "Sam works at Google" + new "Sam started at Meta" -> SUPERSEDE
- existing "Sam works at Google" + new "Sam interviewed at Meta" -> ADD (event, no change yet)
- existing "Sam has a peanut allergy" + new "Sam is allergic to peanuts" -> NOOP
- existing "the demo runs on a Jetson" + new "the demo runs on a Jetson Orin 8GB"
  -> UPDATE, text: "The demo runs on a Jetson Orin with 8GB of RAM"

Respond with JSON: {"op": "ADD"|"UPDATE"|"SUPERSEDE"|"NOOP", "target": int|null,
"confidence": float, "text": string|null}"""


@dataclass
class Verdict:
    op: Op
    target: Memory | None
    confidence: float
    merged_text: str | None = None
    # Dense cosine similarity of the target candidate, set by the caller.
    # Used to corroborate NOOP verdicts: judge + high similarity agree.
    target_similarity: float = 0.0


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
    # bool is an int subclass: {"target": true} must not select index 1.
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(candidates):
        target = candidates[idx]
    if op is not Op.ADD and target is None:
        # An op that needs a target but names none is not actionable.
        return Verdict(op=Op.ADD, target=None, confidence=0.0)

    confidence = clamp01(result.get("confidence", 0.0), 0.0)

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

"""Extraction: turn raw input into atomic, self-contained memories.

An enhancer, not a dependency: with no local model the raw (redacted) text
is stored verbatim as one semantic memory. With a model, the input is split
into atomic facts, each typed and scored for salience; the raw text is kept
as source_text for audit and future re-extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from engram.llm import LocalLLM
from engram.models import MemoryType

# Unicode word runs of 3+ chars, so grounding works for non-Latin scripts too
# (Cyrillic, Greek, ...). Coarse for scripts without word spacing (CJK), but
# the failure mode there is a safe fall back to verbatim, never a wrong store.
_WORD = re.compile(r"\w{3,}")


def _content_tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))

_SYSTEM = """You extract long-term memories from text for a personal memory system.

Rules:
- Each memory is ONE atomic, self-contained fact. Resolve pronouns; a memory
  must make sense alone months later ("Dylan's cat is named Miso", not "his cat is Miso").
- Only extract things worth remembering long-term: stable facts, preferences,
  decisions, corrections, lessons learned, important events. Skip filler,
  transient state, and anything that will be obviously stale in a week.
- If the message is only a question or a request to do something, with no
  durable fact stated, return {"memories": []}. If it also states a durable
  fact, extract just that fact.
- type: "semantic" for facts/preferences/decisions, "episodic" for dated
  events ("X happened on/when ..."), "procedural" for how-tos and workflows.
- importance: calibrate, do not inflate. MOST facts are 0.3-0.5. Use 0.6-0.7
  for a clear standing preference or decision. Reserve 0.8+ for an explicit
  correction or "remember this". Minor detail -> below 0.3.
- tags: 1-3 short lowercase topic tags.

Respond with JSON: {"memories": [{"text": ..., "type": ..., "importance": ..., "tags": [...]}]}
If nothing is worth remembering, respond {"memories": []}."""


@dataclass
class ExtractedFact:
    text: str
    type: MemoryType = MemoryType.SEMANTIC
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    verbatim: bool = False  # raw text stored as-is: no model, or ungrounded output


def extract(text: str, llm: LocalLLM | None, salience_floor: float = 0.1) -> list[ExtractedFact]:
    """Returns the atomic facts in `text`, or the text itself verbatim when
    no model is available. An empty list means nothing cleared the salience
    floor (the input was heard, judged, and dropped)."""
    if llm is None or not llm.available():
        return [ExtractedFact(text=text.strip(), verbatim=True)]

    result = llm.generate_json(_SYSTEM, text)
    # Small local models are loose about the envelope: accept the documented
    # {"memories": [...]}, a bare list, or a single bare memory object.
    if isinstance(result, dict) and "memories" in result:
        items = result["memories"]
    elif isinstance(result, dict) and result.get("text"):
        items = [result]
    elif isinstance(result, list):
        items = result
    else:
        return [ExtractedFact(text=text.strip(), verbatim=True)]

    facts: list[ExtractedFact] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        try:
            mtype = MemoryType(item.get("type", "semantic"))
        except ValueError:
            mtype = MemoryType.SEMANTIC
        importance = max(0.0, min(1.0, float(item.get("importance", 0.5))))
        if importance < salience_floor:
            continue
        tags = [str(t).lower() for t in item.get("tags", []) if t][:3]
        facts.append(ExtractedFact(text=str(item["text"]).strip(), type=mtype,
                                   importance=importance, tags=tags))

    if not facts:
        return facts
    # Fabrication guard: a weak local model handed contentless or degenerate
    # input can ignore it and emit an unrelated invented memory. If the
    # extraction as a WHOLE shares no content token with the input, it isn't
    # grounded in what the user said, so store the raw text verbatim instead of
    # persisting an invention. Checked across the whole extraction, never
    # per-fact: a legitimate multi-fact split can rephrase one fact past a
    # word-level match, and dropping it would bury that fact in a sibling's
    # source_text.
    src = _content_tokens(text)
    produced = set().union(*(_content_tokens(f.text) for f in facts))
    if not (src & produced):
        return [ExtractedFact(text=text.strip(), verbatim=True)]
    return facts

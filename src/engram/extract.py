"""Extraction: turn raw input into atomic, self-contained memories.

An enhancer, not a dependency: with no local model the raw (redacted) text
is stored verbatim as one semantic memory. With a model, the input is split
into atomic facts, each typed and scored for salience; the raw text is kept
as source_text for audit and future re-extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engram.llm import LocalLLM
from engram.models import MemoryType

_SYSTEM = """You extract long-term memories from text for a personal memory system.

Rules:
- Each memory is ONE atomic, self-contained fact. Resolve pronouns; a memory
  must make sense alone months later ("Dylan's cat is named Miso", not "his cat is Miso").
- Only extract things worth remembering long-term: stable facts, preferences,
  decisions, corrections, lessons learned, important events. Skip filler,
  transient state, and anything that will be obviously stale in a week.
- type: "semantic" for facts/preferences/decisions, "episodic" for dated
  events ("X happened on/when ..."), "procedural" for how-tos and workflows.
- importance: 0.0-1.0. Corrections and explicit "remember this" -> 0.8+.
  Casual details -> 0.3-0.5. Trivia/filler -> below 0.2.
- tags: 1-3 short lowercase topic tags.

Respond with JSON: {"memories": [{"text": ..., "type": ..., "importance": ..., "tags": [...]}]}
If nothing is worth remembering, respond {"memories": []}."""


@dataclass
class ExtractedFact:
    text: str
    type: MemoryType = MemoryType.SEMANTIC
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    verbatim: bool = False  # True when extraction didn't run


def extract(text: str, llm: LocalLLM | None, salience_floor: float = 0.1) -> list[ExtractedFact]:
    """Returns the atomic facts in `text`, or the text itself verbatim when
    no model is available. An empty list means nothing cleared the salience
    floor (the input was heard, judged, and dropped)."""
    if llm is None or not llm.available():
        return [ExtractedFact(text=text.strip(), verbatim=True)]

    result = llm.generate_json(_SYSTEM, text)
    if result is None or not isinstance(result, dict):
        return [ExtractedFact(text=text.strip(), verbatim=True)]

    facts: list[ExtractedFact] = []
    for item in result.get("memories", []):
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
    return facts

"""Extraction: turn raw input into atomic, self-contained memories.

An enhancer, not a dependency: with no local model the raw (redacted) text
is stored verbatim as one semantic memory. With a model, the input is split
into atomic facts, each typed and scored for salience; the raw text is kept
as source_text for audit and future re-extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from engram.llm import LocalLLM, clamp01
from engram.models import MemoryType

# Unicode word runs of 3+ chars, so grounding works for non-Latin scripts too
# (Cyrillic, Greek, ...). Coarse for scripts without word spacing (CJK), but
# the failure mode there is a safe fall back to verbatim, never a wrong store.
_WORD = re.compile(r"\w{3,}")
_REDACTION = re.compile(r"\[REDACTED:[^\]]*\]")

# Transient status/breakage language: a bug or in-progress state ("X is broken",
# "still failing", "works for now"), not a durable fact. A weak local model often
# stores these as semantic, so they linger long after the fix. Force them to
# episodic — the 14-day half-life decays them fast, dream decay-prunes them once
# never recalled, and a later "X fixed" supersedes via the normal conflict judge.
# ponytail: keyword heuristic, not intent detection; widen the vocabulary if
# transient notes still slip through as semantic.
_TRANSIENT = re.compile(
    r"(?i)\b(broken|not working|doesn'?t work|does not work|failing|crashing|"
    r"erroring|throws?|throwing|exception|\w*error|hangs|stuck|currently|"
    r"right now|at the moment|for now|not yet (?:working|fixed|done))\b"
)
_TRANSIENT_MAX_IMPORTANCE = 0.4  # keep transient notes out of the sticky high band

# A chat-timestamp marker ("Luis [3:39 PM]", "[15:04]") inside an extracted
# fact means the model quoted a transcript line instead of stating a fact —
# the dogfood store collected dozens of these. Only model output is filtered;
# the no-model verbatim path stores the user's text untouched.
_CHAT_LINE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?\]", re.IGNORECASE)


def _content_tokens(text: str) -> set[str]:
    # Drop redaction placeholders first: their generic words ("redacted",
    # "secret", "token") must never be what grounds a fact against the input,
    # or a fabricated memory passes the guard on any input that had a secret.
    return set(_WORD.findall(_REDACTION.sub(" ", text).lower()))

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
- A statement of intent ("I will update the README") is a transient plan,
  not a fact — skip it, but still extract any durable fact stated alongside.
  A decision that sets standing state ("we chose SQLite") is worth keeping.
- A bare title or heading with no claim ("Key Considerations for X") states
  nothing — skip it, and never expand it into facts it does not state.
- A pasted chat or log transcript: never copy lines verbatim. Extract only
  durable facts stated in it, rephrased self-contained with the speaker
  named ("Andrey said Edge has no async methods").
- type: "semantic" for facts/preferences/decisions, "episodic" for dated
  events ("X happened on/when ..."), "procedural" for how-tos and workflows.
- importance: calibrate, do not inflate. MOST facts are 0.3-0.5. Use 0.6-0.7
  for a clear standing preference or decision. Reserve 0.8+ for an explicit
  correction or "remember this". Minor detail -> below 0.3.
- tags: 1-3 short lowercase topic tags.
- general: true ONLY for a durable fact about the user themselves (a
  preference, habit, or way of working) that would hold in any project —
  e.g. "prefers concise READMEs", "always uses cheaper sub-agent models".
  false for anything about the current project's code, content, infra,
  bugs, or decisions. When unsure, false.
- general_confidence: 0.0-1.0 that `general` is right. A fact only escapes
  the current project's scope at high confidence, so if you are unsure
  whether it is truly project-independent, lower this or set general false.

Respond with JSON: {"memories": [{"text": ..., "type": ..., "importance": ...,
"tags": [...], "general": ..., "general_confidence": ...}]}
If nothing is worth remembering, respond {"memories": []}."""


@dataclass
class ExtractedFact:
    text: str
    type: MemoryType = MemoryType.SEMANTIC
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    verbatim: bool = False  # raw text stored as-is: no model, or ungrounded output
    general: bool = False  # durable fact about the user, not the current project
    general_confidence: float = 0.0  # model's confidence in `general`; gates the scope escape


def extract(text: str, llm: LocalLLM | None, salience_floor: float = 0.1) -> list[ExtractedFact]:
    """Returns the atomic facts in `text`, or the text itself verbatim when
    no model is available. An empty list means nothing cleared the salience
    floor (the input was heard, judged, and dropped)."""
    if llm is None or not llm.available():
        return [ExtractedFact(text=text.strip(), verbatim=True)]

    # Extraction often neutralizes transient wording ("X is currently broken" ->
    # "X throws a SyntaxError"), so the demotion below also checks the raw input,
    # but only demotes when it maps 1:1 to a single fact — a multi-fact session
    # tail that merely mentions a bug must not drag durable facts down with it.
    input_transient = bool(_TRANSIENT.search(text))

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

    raw = items if isinstance(items, list) else []
    facts: list[ExtractedFact] = []
    had_usable = False  # at least one item the model gave usable text for
    chat_quoted = False  # model quoted transcript lines instead of extracting
    for item in raw:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        had_usable = True
        try:
            mtype = MemoryType(item.get("type", "semantic"))
        except ValueError:
            mtype = MemoryType.SEMANTIC
        # The model may hand back null/non-numeric fields; parse defensively
        # (like resolve.py) so a loose envelope degrades to verbatim, never
        # crashes the write — extraction is an enhancer, not a dependency.
        importance = clamp01(item.get("importance") or 0.5, 0.5)
        if importance < salience_floor:
            continue
        tags = [str(t).lower() for t in (item.get("tags") or []) if t][:3]
        fact_text = str(item["text"]).strip()
        if _CHAT_LINE.search(fact_text):
            chat_quoted = True
            continue  # quoted transcript line, not an extracted fact
        # A transient/breakage note is never a durable semantic or procedural
        # fact, whatever the model guessed — demote so it decays and can't
        # outlive the fix.
        if _TRANSIENT.search(fact_text):
            mtype = MemoryType.EPISODIC
            importance = min(importance, _TRANSIENT_MAX_IMPORTANCE)
        gen_conf = clamp01(item.get("general_confidence") or 0.0, 0.0)
        facts.append(ExtractedFact(text=fact_text, type=mtype,
                                   importance=importance, tags=tags,
                                   general=item.get("general") is True,
                                   general_confidence=gen_conf))

    # Raw input was transient but the extractor washed the wording out of the
    # one fact it produced — demote that fact too. Only for a clean 1:1 mapping.
    if input_transient and len(facts) == 1 and facts[0].type is not MemoryType.EPISODIC:
        facts[0].type = MemoryType.EPISODIC
        facts[0].importance = min(facts[0].importance, _TRANSIENT_MAX_IMPORTANCE)

    if not facts:
        # A chat paste the model only quoted from was never really extracted —
        # keep it whole as one verbatim memory rather than lose its content.
        if chat_quoted:
            return [ExtractedFact(text=text.strip(), verbatim=True)]
        # Honor a real "nothing salient" judgment (the model gave usable items
        # and they all fell below the floor, or it returned an empty list) as
        # []. Only fall back to verbatim when NO usable item came back — a
        # malformed envelope means the input was never really judged, so keep
        # the user's text rather than silently dropping it.
        return [] if (had_usable or not raw) else [ExtractedFact(text=text.strip(), verbatim=True)]
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

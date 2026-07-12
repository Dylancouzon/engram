"""Extraction envelope tolerance: small local models are loose about the
JSON shape, and none of their variants may cause a fact to be dropped."""

from engram.extract import extract
from engram.models import MemoryType


class EnvelopeLLM:
    def __init__(self, response):
        self.response = response

    def available(self):
        return True

    def generate_json(self, system, prompt):
        return self.response


def test_documented_envelope():
    llm = EnvelopeLLM({"memories": [{"text": "Dylan lives in Paris",
                                     "type": "semantic", "importance": 0.7}]})
    [fact] = extract("Dylan lives in Paris", llm)
    assert fact.text == "Dylan lives in Paris" and not fact.verbatim


def test_bare_object_envelope():
    llm = EnvelopeLLM({"text": "Dylan lives in Paris", "importance": 0.7})
    [fact] = extract("Dylan lives in Paris", llm)
    assert fact.text == "Dylan lives in Paris" and not fact.verbatim


def test_bare_list_envelope():
    llm = EnvelopeLLM([{"text": "the demo is tomorrow", "type": "episodic",
                        "importance": 0.5}])
    [fact] = extract("the demo is tomorrow", llm)
    assert fact.type is MemoryType.EPISODIC


def test_ungrounded_extraction_falls_back_to_verbatim():
    # A weak model can invent a fact unrelated to the input. If the extraction
    # shares no content token with the source, store the raw text instead.
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan decided to learn Python", "importance": 0.7}]})
    [fact] = extract("test fact", llm)
    assert fact.verbatim and fact.text == "test fact"


def test_grounded_extraction_survives():
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan's cat is named Miso", "importance": 0.7}]})
    [fact] = extract("his cat is Miso", llm)
    assert not fact.verbatim and fact.text == "Dylan's cat is named Miso"


def test_grounding_is_whole_extraction_not_per_fact():
    # A legitimate multi-fact split can rephrase one fact past a word-level
    # match ("prefer" vs "prefers", "JS" too short). As long as the extraction
    # as a whole is grounded, no individual fact is dropped — else the lost
    # fact would be buried in a sibling's source_text.
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan prefers JavaScript", "importance": 0.6},
        {"text": "Dylan's dog is named Miso", "importance": 0.6}]})
    facts = extract("I prefer JS; my dog is Miso", llm)
    assert {f.text for f in facts} == {
        "Dylan prefers JavaScript", "Dylan's dog is named Miso"}
    assert not any(f.verbatim for f in facts)


def test_contentless_input_cannot_smuggle_a_fabrication():
    # Input with no content tokens can't ground anything: a model that invents
    # an unrelated fact must not have it accepted — fall back to verbatim.
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan decided to learn Python", "importance": 0.7}]})
    [fact] = extract("ok", llm)
    assert fact.verbatim and fact.text == "ok"


def test_redaction_placeholder_does_not_ground_a_fabrication():
    # The input's only "content" is a redaction placeholder. A fabricated fact
    # that merely echoes placeholder words ("secret", "token") must NOT pass
    # the grounding check — placeholders are stripped before tokenizing, so
    # they can't be what grounds a memory against the input.
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan's API secret token rotates weekly", "importance": 0.7}]})
    [fact] = extract("[REDACTED:assigned-secret]", llm)
    assert fact.verbatim


def test_garbage_falls_back_to_verbatim():
    [fact] = extract("keep me", EnvelopeLLM("not json at all"))
    assert fact.verbatim and fact.text == "keep me"


def test_none_falls_back_to_verbatim():
    [fact] = extract("keep me", EnvelopeLLM(None))
    assert fact.verbatim


def test_salience_floor_drops_trivia():
    llm = EnvelopeLLM({"memories": [
        {"text": "important thing", "importance": 0.8},
        {"text": "filler", "importance": 0.05},
    ]})
    facts = extract("the important thing plus some filler", llm, salience_floor=0.1)
    assert [f.text for f in facts] == ["important thing"]


def test_all_below_salience_floor_is_honored_not_verbatim():
    # Well-formed items that ALL fall below the floor is a real "nothing
    # salient" judgment — return [], don't overwrite it with the raw text
    # verbatim (which would defeat the floor the model just applied).
    llm = EnvelopeLLM({"memories": [
        {"text": "minor detail", "importance": 0.05},
        {"text": "another triviality", "importance": 0.02},
    ]})
    assert extract("a minor detail and another triviality", llm,
                   salience_floor=0.1) == []


def test_invalid_type_defaults_semantic():
    llm = EnvelopeLLM({"memories": [{"text": "x", "type": "banana", "importance": 0.5}]})
    [fact] = extract("x", llm)
    assert fact.type is MemoryType.SEMANTIC


def test_no_llm_verbatim():
    [fact] = extract("remember me", None)
    assert fact.verbatim and fact.text == "remember me"


def test_malformed_nonempty_memories_degrades_to_verbatim():
    # A non-empty memories list whose entries don't parse (bare strings, a
    # classic loose shape) must not be conflated with "nothing salient" — the
    # input was never really judged, so keep it verbatim rather than drop it.
    llm = EnvelopeLLM({"memories": ["Dylan's cat is named Miso"]})
    [fact] = extract("Dylan's cat is named Miso", llm)
    assert fact.verbatim and fact.text == "Dylan's cat is named Miso"


def test_empty_memories_list_is_honored():
    # A genuinely empty list means the model judged and dropped the input.
    assert extract("is it going to rain?", EnvelopeLLM({"memories": []})) == []


def test_null_importance_and_tags_do_not_crash():
    # The model may emit null fields; parsing must degrade, not raise.
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan likes tea", "importance": None, "tags": None}]})
    [fact] = extract("Dylan likes tea", llm)
    assert fact.importance == 0.5 and fact.tags == [] and not fact.verbatim


def test_non_numeric_importance_defaults():
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan likes tea", "importance": "high"}]})
    [fact] = extract("Dylan likes tea", llm)
    assert fact.importance == 0.5


def test_missing_general_key_defaults_false():
    llm = EnvelopeLLM({"memories": [{"text": "Dylan likes tea"}]})
    [fact] = extract("Dylan likes tea", llm)
    assert fact.general is False


def test_malformed_general_value_defaults_false():
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan likes tea", "general": "yes"}]})
    [fact] = extract("Dylan likes tea", llm)
    assert fact.general is False


def test_general_true_is_parsed():
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan prefers tea over coffee", "general": True}]})
    [fact] = extract("Dylan prefers tea over coffee", llm)
    assert fact.general is True


def test_transient_state_forced_episodic():
    # The model labels a bug report as a durable semantic fact; the guard
    # demotes it to episodic so it decays fast and can't outlive the fix.
    llm = EnvelopeLLM({"memories": [
        {"text": "the reshard endpoint is broken", "type": "semantic",
         "importance": 0.8}]})
    [fact] = extract("the reshard endpoint is broken", llm)
    assert fact.type is MemoryType.EPISODIC
    assert fact.importance <= 0.4


def test_durable_fact_not_demoted():
    # No transient language -> the model's semantic classification stands.
    llm = EnvelopeLLM({"memories": [
        {"text": "Dylan prefers concise READMEs", "type": "semantic",
         "importance": 0.7}]})
    [fact] = extract("Dylan prefers concise READMEs", llm)
    assert fact.type is MemoryType.SEMANTIC and fact.importance == 0.7


def test_transient_input_demotes_even_when_wording_washed_out():
    # Raw input is a bug report; the model rewrites it into a neutral fact with
    # no transient keyword. The 1:1 input-level guard still demotes it.
    llm = EnvelopeLLM({"memories": [
        {"text": "the autoplay route returns a 500", "type": "semantic",
         "importance": 0.8}]})
    [fact] = extract("the autoplay route is currently broken", llm)
    assert fact.type is MemoryType.EPISODIC and fact.importance <= 0.4


def test_transient_mention_does_not_demote_multi_fact_split():
    # A session tail that mentions a bug but also states a durable fact must
    # not drag the durable fact down — input-level demotion is 1:1 only.
    llm = EnvelopeLLM({"memories": [
        {"text": "the build is currently broken", "type": "semantic", "importance": 0.5},
        {"text": "Dylan prefers concise READMEs", "type": "semantic", "importance": 0.7}]})
    facts = extract("the build is currently broken. also Dylan prefers concise READMEs", llm)
    durable = [f for f in facts if "READMEs" in f.text][0]
    assert durable.type is MemoryType.SEMANTIC and durable.importance == 0.7

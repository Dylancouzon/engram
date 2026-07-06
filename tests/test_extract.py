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
    llm = EnvelopeLLM([{"text": "a fact", "type": "episodic", "importance": 0.5}])
    [fact] = extract("stuff", llm)
    assert fact.type is MemoryType.EPISODIC


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
    facts = extract("...", llm, salience_floor=0.1)
    assert [f.text for f in facts] == ["important thing"]


def test_invalid_type_defaults_semantic():
    llm = EnvelopeLLM({"memories": [{"text": "x", "type": "banana", "importance": 0.5}]})
    [fact] = extract("x", llm)
    assert fact.type is MemoryType.SEMANTIC


def test_no_llm_verbatim():
    [fact] = extract("remember me", None)
    assert fact.verbatim and fact.text == "remember me"

"""Trust-boundary shards: routing, fan-out recall with merge rules, and
the privacy invariants that make opt-in structural."""

import pytest
from conftest import make_store

from engram.store import validate_shard


def test_cross_shard_reinforce_flushed_before_mark_advances(config):
    # Deferred reinforcement leaves a shard unflushed; the flush mark is global
    # while flush() is per-shard. A write to a DIFFERENT shard must flush the
    # dirty shard before advancing the mark, or a crash strands the bump.
    store = make_store(config)  # sync reinforce mode
    try:
        [a] = store.remember("Dylan's cat is named Miso", shard="me-synced")
        store._reinforce([a.memory.id])  # bumps me-synced, defers its flush
        assert "me-synced" in store._dirty_shards
        store.remember("Dylan uses uv for Python", shard="private")
        assert store._dirty_shards == set()  # me-synced was flushed first
    finally:
        store.close()


def test_shard_names_are_a_closed_grammar():
    for good in ("private", "me-synced", "shared:family", "shared:team-a"):
        assert validate_shard(good) == good
    for bad in ("public", "shared:", "shared:UPPER", "../etc", "me synced"):
        with pytest.raises(ValueError):
            validate_shard(bad)


def test_writes_land_in_their_shard(config):
    store = make_store(config)
    try:
        store.remember("Dylan's cat is named Miso")  # private by default
        store.remember("The team standup is at 9:30", shard="shared:team")
        assert store.backends["private"].count() == 1
        assert store.backends["shared:team"].count() == 1
        # and the journal knows where each row belongs (sync needs this)
        shards = {e.shard for e in store.journal.entries()}
        assert shards == {"private", "shared:team"}
    finally:
        store.close()


def test_recall_fans_out_across_shards(config):
    store = make_store(config)
    try:
        store.remember("Dylan's cat is named Miso")
        store.remember("The team standup is at 9:30 every day", shard="shared:team")
        texts = [h.memory.text for h in store.recall("standup cat", k=5)]
        assert any("Miso" in t for t in texts)
        assert any("standup" in t for t in texts)
        # single-shard recall stays confined
        team_only = [h.memory.text for h in store.recall("standup cat", k=5,
                                                         shard="shared:team")]
        assert all("Miso" not in t for t in team_only)
    finally:
        store.close()


def test_conflicts_never_cross_shards(config):
    """A private fact must not be superseded by (or judged against) a
    shared write — opt-in is structural."""
    from conftest import FakeLLM

    llm = FakeLLM(judge_responses=[
        {"op": "SUPERSEDE", "target": 0, "confidence": 0.99},
    ])
    store = make_store(config, llm=llm)
    try:
        store.remember("Dylan lives in Paris")  # private
        [action] = store.remember("Dylan lives in Berlin", shard="shared:family")
        # fresh shard had no candidates: the judge never even ran, so the
        # scripted SUPERSEDE response was not consumed
        assert action.op.value == "ADD"
        assert llm.judge_responses  # unconsumed
        private_texts = [h.payload["text"]
                         for h in store.backends["private"].scroll_all()]
        assert private_texts == ["Dylan lives in Paris"]
    finally:
        store.close()


def test_shards_reopen_and_rebuild(config):
    store = make_store(config)
    store.remember("Dylan's cat is named Miso")
    store.remember("standup at 9:30", shard="me-synced")
    store.close()

    reopened = make_store(config)
    try:
        assert set(reopened.backends) == {"private", "me-synced"}
        reopened.rebuild()
        assert reopened.backends["private"].count() == 1
        assert reopened.backends["me-synced"].count() == 1
    finally:
        reopened.close()


def test_hard_forget_routes_to_owning_shard(config):
    store = make_store(config)
    try:
        [a] = store.remember("the secret retro note", shard="shared:team")
        store.remember("Dylan's cat is named Miso")
        store.forget(a.memory.id, mode="hard")
        assert store.backends["shared:team"].count() == 0
        assert store.backends["private"].count() == 1
    finally:
        store.close()
    grep = [p for p in config.data_dir.rglob("*")
            if p.is_file() and b"secret retro note" in p.read_bytes()]
    assert grep == []

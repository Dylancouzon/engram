"""Review queue: ambiguous verdicts land as safe ADDs + a queued decision;
accepting applies the judged op late; rejecting keeps both."""

from conftest import FakeLLM, make_store

from engram.models import Op


def _ambiguous_supersede(config):
    llm = FakeLLM(judge_responses=[
        {"op": "SUPERSEDE", "target": 0, "confidence": 0.65},  # in review band
    ])
    store = make_store(config, llm=llm)
    [first] = store.remember("Dylan lives in Paris")
    [second] = store.remember("Dylan lives in Berlin")
    return store, first, second


def test_ambiguous_supersede_adds_and_queues(config):
    store, first, second = _ambiguous_supersede(config)
    try:
        assert second.op is Op.ADD and second.queued_review
        assert store.backend.count() == 2  # nothing was destroyed
        [item] = store.pending_reviews()
        assert item.proposed_op is Op.SUPERSEDE
        assert item.target.id == first.memory.id
        assert item.new.id == second.memory.id
    finally:
        store.close()


def test_accept_supersede_applies_late(config):
    store, first, second = _ambiguous_supersede(config)
    try:
        [item] = store.pending_reviews()
        assert store.resolve_review(item.seq, accept=True)
        old = store.get(first.memory.id)
        assert not old.is_valid and old.superseded_by == second.memory.id
        texts = [h.memory.text for h in store.recall("where does Dylan live")]
        assert "Dylan lives in Berlin" in texts
        assert "Dylan lives in Paris" not in texts
        assert store.pending_reviews() == []
    finally:
        store.close()


def test_reject_keeps_both(config):
    store, first, second = _ambiguous_supersede(config)
    try:
        [item] = store.pending_reviews()
        store.resolve_review(item.seq, accept=False)
        assert store.get(first.memory.id).is_valid
        assert store.get(second.memory.id).is_valid
        assert store.pending_reviews() == []  # decision is remembered
    finally:
        store.close()


def test_accept_update_folds_twin_into_target(config):
    llm = FakeLLM(judge_responses=[
        {"op": "UPDATE", "target": 0, "confidence": 0.6,
         "text": "Dylan's cat Miso is a grey british shorthair"},
    ])
    store = make_store(config, llm=llm)
    try:
        [first] = store.remember("Dylan's cat is named Miso")
        [second] = store.remember("Miso is a grey british shorthair")
        assert second.queued_review and store.backend.count() == 2
        [item] = store.pending_reviews()
        store.resolve_review(item.seq, accept=True)
        assert store.backend.count() == 1
        target = store.get(first.memory.id)
        assert "british shorthair" in target.text
        assert store.get(second.memory.id) is None  # twin folded away
    finally:
        store.close()


def test_review_survives_rebuild(config):
    """The queue and its resolution live in the journal: a rebuild
    converges to the resolved state."""
    store, first, second = _ambiguous_supersede(config)
    [item] = store.pending_reviews()
    store.resolve_review(item.seq, accept=True)
    store.rebuild()
    try:
        old = store.get(first.memory.id)
        assert not old.is_valid and old.superseded_by == second.memory.id
        assert store.pending_reviews() == []
    finally:
        store.close()


def test_below_floor_is_plain_add(config):
    llm = FakeLLM(judge_responses=[
        {"op": "SUPERSEDE", "target": 0, "confidence": 0.3},  # below review floor
    ])
    store = make_store(config, llm=llm)
    try:
        store.remember("Dylan lives in Paris")
        [second] = store.remember("Dylan lives in Berlin")
        assert second.op is Op.ADD and not second.queued_review
        assert store.pending_reviews() == []
    finally:
        store.close()


def test_stale_review_disappears_when_target_forgotten(config):
    store, first, second = _ambiguous_supersede(config)
    try:
        store.forget(first.memory.id, mode="hard")
        assert store.pending_reviews() == []  # question answered itself
    finally:
        store.close()

"""Store pipeline + the three M0 exit tests (spec §9)."""

import pytest
from conftest import FakeLLM, make_store

from engram.models import Op
from engram.store import StoreLockedError, WriteRefusedError


def test_remember_and_recall(store):
    store.remember("Dylan's cat is named Miso", scope="personal")
    store.remember("The deploy pipeline uses GitHub Actions", scope="work")
    hits = store.recall("what is the cat called", scope="personal")
    assert hits and hits[0].memory.text == "Dylan's cat is named Miso"


def test_scope_prefilter(store):
    store.remember("Dylan's cat is named Miso", scope="personal")
    assert store.recall("cat named", scope="work") == []


def test_recall_reinforces(store):
    store.remember("Dylan drinks oat-milk flat whites")
    first = store.recall("coffee oat milk drinks")[0]
    assert first.memory.access_count == 0
    second = store.recall("coffee oat milk drinks")[0]
    assert second.memory.access_count == 1
    assert second.memory.last_accessed is not None


def test_second_writer_locked_out(config):
    s1 = make_store(config)
    try:
        with pytest.raises(StoreLockedError):
            make_store(config)
    finally:
        s1.close()
    s2 = make_store(config)  # released cleanly
    s2.close()


def test_secret_redacted_before_persistence(config):
    store = make_store(config)
    fake_secret = "".join(["zX9v2", "Lq8Tr", "W4bN6m"])  # runtime-assembled: not a real secret
    try:
        actions = store.remember(f"staging api_key = {fake_secret} no one must lose this")
        stored = actions[0].memory
        assert fake_secret not in stored.text
        for entry in store.journal.entries():
            assert fake_secret not in (entry.payload or {}).get("text", "")
    finally:
        store.close()
    raw = config.journal_path.read_bytes()
    assert fake_secret.encode() not in raw


def test_private_key_refused_entirely(store):
    with pytest.raises(WriteRefusedError):
        store.remember("backup: -----BEGIN OPENSSH PRIVATE KEY----- b3BlbnNzaA")
    assert store.backend.count() == 0
    assert store.journal.last_seq == 0


# --- judge behaviors ---------------------------------------------------------


def test_high_confidence_supersede(config):
    """M0 exit test 1: a correction supersedes the stale fact."""
    llm = FakeLLM(judge_responses=[
        {"op": "SUPERSEDE", "target": 0, "confidence": 0.95},
    ])
    store = make_store(config, llm=llm)
    try:
        [first] = store.remember("Dylan lives in Paris")
        [second] = store.remember("Correction: Dylan lives in Berlin now")
        assert second.op is Op.SUPERSEDE
        assert second.target.id == first.memory.id

        hits = store.recall("where does Dylan live")
        texts = [h.memory.text for h in hits]
        assert "Correction: Dylan lives in Berlin now" in texts
        assert "Dylan lives in Paris" not in texts  # invalidated, filtered out

        old = store.backend.retrieve([first.memory.id])[0].memory()
        assert old.superseded_by == second.memory.id
        assert not old.is_valid
    finally:
        store.close()


def test_low_confidence_degrades_to_add(config):
    llm = FakeLLM(judge_responses=[
        {"op": "SUPERSEDE", "target": 0, "confidence": 0.5},  # under threshold
    ])
    store = make_store(config, llm=llm)
    try:
        store.remember("Dylan lives in Paris")
        [second] = store.remember("Dylan lives in Berlin")
        assert second.op is Op.ADD  # uncertain -> keep both, never wrongly supersede
        assert store.backend.count() == 2
    finally:
        store.close()


def test_update_merges_in_place(config):
    llm = FakeLLM(judge_responses=[
        {"op": "UPDATE", "target": 0, "confidence": 0.9,
         "text": "Dylan's cat Miso is a grey british shorthair"},
    ])
    store = make_store(config, llm=llm)
    try:
        [first] = store.remember("Dylan's cat is named Miso")
        [second] = store.remember("the cat Miso is a grey british shorthair")
        assert second.op is Op.UPDATE
        assert second.memory.id == first.memory.id  # same identity, refined text
        assert store.backend.count() == 1
        hit = store.recall("Miso cat breed")[0]
        assert "british shorthair" in hit.memory.text
    finally:
        store.close()


def test_noop_reinforces_instead_of_duplicating(config):
    llm = FakeLLM(judge_responses=[
        {"op": "NOOP", "target": 0, "confidence": 0.95},
    ])
    store = make_store(config, llm=llm)
    try:
        store.remember("Dylan's cat is named Miso")
        [second] = store.remember("Dylan has a cat called Miso")
        assert second.op is Op.NOOP
        assert store.backend.count() == 1
        m = store.recall("cat", reinforce=False)[0].memory
        assert m.access_count == 1  # the NOOP reinforced the original
    finally:
        store.close()


def test_no_llm_is_add_only(store):
    store.remember("Dylan lives in Paris")
    store.remember("Dylan lives in Berlin")  # no judge -> both kept
    assert store.backend.count() == 2


# --- forgetting ----------------------------------------------------------------


def test_soft_forget_excludes_from_recall(store):
    [a] = store.remember("Dylan is allergic to peanuts")
    assert store.recall("allergies peanuts")
    store.forget(a.memory.id, mode="soft")
    assert store.recall("allergies peanuts") == []
    # but history is preserved
    assert store.backend.retrieve([a.memory.id])


def test_hard_forget_is_gone_everywhere(config):
    """M0 exit test 2: a hard-forgotten fact is gone, including the journal."""
    store = make_store(config)
    try:
        [a] = store.remember("Dylan's SSN ends in 1234")
        store.remember("Dylan prefers window seats")
        store.forget(a.memory.id, mode="hard")

        assert all(h.memory.id != a.memory.id
                   for h in store.recall("SSN social security"))
        assert store.backend.retrieve([a.memory.id]) == []
        assert store.journal.is_tombstoned(a.memory.id)
        for entry in store.journal.entries():
            assert "SSN" not in ((entry.payload or {}).get("text") or "")

        import io

        buf = io.StringIO()
        store.export_jsonl(buf)
        assert "SSN" not in buf.getvalue()
        assert "window seats" in buf.getvalue()
    finally:
        store.close()
    assert b"SSN ends in" not in config.journal_path.read_bytes()


# --- durability -------------------------------------------------------------------


def test_crash_after_ack_before_flush_replays(config):
    """M0 exit test 3: a store killed after an acked write but before flush
    reopens with the write intact."""
    store = make_store(config)
    [a] = store.remember("Dylan uses uv for Python packaging")
    # Simulate the crash window: the intent is journaled (acked) but never
    # reached Edge. This is exactly the state a kill -9 inside
    # _commit_upserts leaves behind.
    from engram.models import Memory, now_ts

    ghost = Memory(id="9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                   text="Dylan's flight to Berlin is on Friday",
                   created_at=now_ts(), valid_from=now_ts())
    store.journal.append("upsert", ghost.id, ghost.to_payload())
    # Abandon without close(): release the flock the way a dead process would.
    store.journal.close()
    store.backend.close()
    import fcntl

    fcntl.flock(store._lock_file, fcntl.LOCK_UN)
    store._lock_file.close()

    reopened = make_store(config)
    try:
        # Replay drained the journal before any new activity.
        assert reopened.journal.pending() == []
        hits = reopened.recall("when is the flight to Berlin")
        assert any(h.memory.id == ghost.id for h in hits)
        # and the pre-crash memory is still there too
        assert any(h.memory.id == a.memory.id
                   for h in reopened.recall("python packaging tool"))
    finally:
        reopened.close()


def test_rebuild_from_journal_alone(config):
    """The journal really is the source of truth: wipe the index, replay."""
    import shutil

    store = make_store(config)
    store.remember("Dylan's cat is named Miso")
    [b] = store.remember("Dylan lives in Paris")
    store.forget(b.memory.id, mode="hard")
    store.close()

    shutil.rmtree(config.shard_dir)
    reopened = make_store(config)
    try:
        reopened.rebuild()
        assert reopened.backend.count() == 1
        assert reopened.recall("cat name")[0].memory.text == "Dylan's cat is named Miso"
        assert all(h.memory.id != b.memory.id for h in reopened.recall("lives in Paris"))
    finally:
        reopened.close()

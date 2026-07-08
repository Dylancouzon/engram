"""M2: snapshot/restore round-trip (encrypted), consolidation passes."""

import time

import pytest
from conftest import FakeEmbedder, FakeLLM, make_store

from engram.archive import read_snapshot, restore_snapshot
from engram.config import Config
from engram.models import MemoryType, now_ts
from engram.store import MemoryStore


def test_snapshot_restore_roundtrip_encrypted(config, tmp_path):
    store = make_store(config)
    store.remember("Dylan's cat is named Miso", scope="personal")
    store.remember("standup at 9:30", shard="me-synced")
    snap = tmp_path / "backup.engram"
    size = store.snapshot(snap, passphrase="correct horse")
    store.close()
    assert size > 0
    # Encrypted: the artifact is a Fernet blob, not a readable tar. (A
    # plaintext-substring check would be flaky — base64 ciphertext collides
    # with short needles — so assert the security property structurally.)
    raw = snap.read_bytes()
    assert raw.startswith(b"ENGRAM1")  # encrypted-snapshot magic, not gzip
    with pytest.raises(ValueError):  # cannot be opened without the passphrase
        read_snapshot(snap, None)

    dest = Config(data_dir=tmp_path / "second-machine", dense_dim=config.dense_dim)
    with pytest.raises(ValueError):  # wrong passphrase refused
        restore_snapshot(dest, snap, "wrong")
    restore_snapshot(dest, snap, "correct horse")
    restored = MemoryStore(dest, embedder=FakeEmbedder(), llm=None)
    try:
        assert restored.recall("cat name")[0].memory.text == "Dylan's cat is named Miso"
        assert set(restored.backends) == {"private", "me-synced"}
    finally:
        restored.close()


def test_restore_refuses_nonempty(config, tmp_path):
    store = make_store(config)
    store.remember("a fact")
    snap = tmp_path / "b.engram"
    store.snapshot(snap, passphrase=None)
    store.close()
    with pytest.raises(ValueError):
        restore_snapshot(config, snap, None)  # same dir already has a journal


def _aged(store, text, days, **kw):
    [action] = store.remember(text, **kw)
    m = action.memory
    m.created_at = m.valid_from = now_ts() - days * 86400
    store._commit_upserts([m])
    return m


def test_consolidate_prunes_stale_episodes(config):
    store = make_store(config)
    try:
        _aged(store, "grabbed a coffee with Sam", 90,
              type=MemoryType.EPISODIC, importance=0.2)
        keep = _aged(store, "signed the apartment lease in Berlin", 90,
                     type=MemoryType.EPISODIC, importance=0.9)
        store.remember("Dylan's cat is named Miso")  # fresh semantic: untouched
        report = store.consolidate()
        assert report["pruned"] == 1
        texts = [h.memory.text for h in store.recall("coffee lease cat", k=10)]
        assert "grabbed a coffee with Sam" not in texts
        assert store.get(keep.id).is_valid
    finally:
        store.close()


def test_consolidate_flushes_reinforcement_before_pruning(config):
    """Buffered mode (the daemon's mode): a recall's access bump only queues.
    consolidate() must drain it first, or decay-prune reads access_count=0 and
    prunes a memory that was recalled seconds ago."""
    store = MemoryStore(config, embedder=FakeEmbedder(), llm=None,
                        reinforce_mode="buffered")
    try:
        m = _aged(store, "grabbed a coffee with Sam", 90,
                  type=MemoryType.EPISODIC, importance=0.2)
        store.recall("coffee with Sam", reinforce=True)  # queues a bump only
        assert store.get(m.id).access_count == 0  # not yet applied to Edge
        report = store.consolidate()
        assert report["pruned"] == 0  # the drained bump spared it
        assert store.get(m.id).is_valid
    finally:
        store.close()


def test_consolidate_dedups_identical(config):
    store = make_store(config)
    try:
        # Bypass write-time dedup (imports/sync can inject twins directly).
        from engram.models import Memory, new_memory_id

        for text in ("Dylan prefers window seats", "Dylan prefers window seats!"):
            m = Memory(id=new_memory_id(store._owner_ns), text=text)
            store._commit_upserts([m])
            time.sleep(0.01)
        report = store.consolidate()
        assert report["deduped"] == 1
        valid = [h.memory() for h in store.backend.scroll_all()]
        assert sum(m.is_valid for m in valid) == 1
    finally:
        store.close()


def test_consolidate_summarizes_old_episode_clusters(config):
    llm = FakeLLM(extract_response=None)
    llm.generate_json = lambda system, prompt: (
        {"summary": "Dylan runs the Tuesday community call"}
        if "compress" in system else None
    )
    store = make_store(config, llm=llm)
    try:
        for week in range(3):
            _aged(store, f"hosted the community call, week {week}", 40 + week * 7,
                  type=MemoryType.EPISODIC, tags=["community"], importance=0.5)
        report = store.consolidate()
        assert report["summarized"] == 1
        hits = store.recall("community call", k=5)
        texts = [h.memory.text for h in hits]
        assert "Dylan runs the Tuesday community call" in texts
        assert all("week 0" not in t for t in texts)  # episodes retired
        # and the summary survives a rebuild (it went through the journal)
        store.rebuild()
        assert any("Tuesday community call" in h.memory.text
                   for h in store.recall("community call", k=5))
    finally:
        store.close()


def test_consolidate_does_not_hold_write_lock_during_model_call(config):
    # The summarization model call runs in phase 2, OUTSIDE the write lock, so
    # a daemon shutdown never blocks ~60s on an in-flight Ollama call.
    import threading

    entered = threading.Event()
    proceed = threading.Event()

    class BlockingLLM:
        def available(self):
            return True

        def generate_json(self, system, prompt):
            if "compress" not in system:
                return None  # extraction/judge during setup: no-op (verbatim/ADD)
            entered.set()
            assert proceed.wait(5), "model call was never released"
            return {"summary": "Dylan runs the Tuesday community call"}

    store = make_store(config, llm=BlockingLLM())
    try:
        for week in range(3):
            _aged(store, f"hosted the community call, week {week}", 40 + week * 7,
                  type=MemoryType.EPISODIC, tags=["community"], importance=0.5)
        t = threading.Thread(target=store.consolidate)
        t.start()
        assert entered.wait(5), "consolidation never reached the model call"
        # Model call in flight: the write lock MUST be free.
        got = store._write_lock.acquire(timeout=2)
        assert got, "write lock held across the model call"
        store._write_lock.release()
        proceed.set()
        t.join(timeout=5)
        assert not t.is_alive()
        assert any("Tuesday community call" in h.memory.text
                   for h in store.recall("community call", k=5))
    finally:
        proceed.set()
        store.close()


def test_consolidate_discards_summary_when_source_forgotten_midrun(config):
    # A summary is generated (phase 2) from episodes as of phase 1. If a source
    # episode is hard-forgotten while the model runs, committing the summary
    # would reintroduce forgotten content — so the whole summary is discarded.
    import threading

    entered = threading.Event()
    proceed = threading.Event()

    class BlockingLLM:
        def available(self):
            return True

        def generate_json(self, system, prompt):
            if "compress" not in system:
                return None
            entered.set()
            assert proceed.wait(5), "model call was never released"
            return {"summary": "community summary mentioning secret venue XYZ"}

    store = make_store(config, llm=BlockingLLM())
    episodes = []
    try:
        for week in range(3):
            episodes.append(_aged(
                store, f"community call week {week} at venue XYZ", 40 + week * 7,
                type=MemoryType.EPISODIC, tags=["community"], importance=0.5))
        t = threading.Thread(target=store.consolidate)
        t.start()
        assert entered.wait(5), "consolidation never reached the model call"
        store.forget(episodes[0].id, mode="hard")  # forget a source mid-run
        proceed.set()
        t.join(timeout=5)
        assert not t.is_alive()
        hits = store.recall("community call venue", k=10)
        assert all(h.memory.surface != "consolidation" for h in hits)  # no summary
        assert all("secret venue" not in h.memory.text for h in hits)
        assert store.get(episodes[1].id).is_valid  # survivors not invalidated
    finally:
        proceed.set()
        store.close()


def test_consolidate_cancel_skips_work_and_checkpoint(config):
    import threading

    from engram.consolidate import last_run

    store = make_store(config)
    try:
        for i in range(6):
            _aged(store, f"forgettable moment number {i} at place {i}", 90,
                  type=MemoryType.EPISODIC, importance=0.1)
        stop = threading.Event()
        stop.set()  # cancelled up front: nothing touched, no daily checkpoint
        report = store.consolidate(stop=stop)
        assert sum(report.values()) == 0
        assert last_run(store) == 0.0
        # A completed run does the work and advances the checkpoint.
        report = store.consolidate()
        assert report["pruned"] == 6
        assert last_run(store) > 0.0
    finally:
        store.close()

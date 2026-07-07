"""M2: snapshot/restore round-trip (encrypted), consolidation passes."""

import time

import pytest
from conftest import FakeEmbedder, FakeLLM, make_store

from engram.archive import restore_snapshot
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
    # Encrypted: memory text must not appear in the artifact bytes.
    assert b"Miso" not in snap.read_bytes()

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


def test_consolidate_respects_budget(config):
    store = make_store(config)
    try:
        for i in range(6):
            _aged(store, f"forgettable moment number {i} at place {i}", 90,
                  type=MemoryType.EPISODIC, importance=0.1)
        report = store.consolidate(budget=3)
        assert sum(report.values()) == 3  # bounded, resumes next run
    finally:
        store.close()

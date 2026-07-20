"""Privacy invariants from the Codex review: byte-level purge of the shard,
no source_text bleed across sibling facts, purge crash recovery, scrubbed
imports, and judge-output hardening."""

import io

from conftest import FakeLLM, make_store

from engram.models import Memory, Op
from engram.resolve import judge

SENTINEL = "XyzzyPurgeSentinel12345"


def _grep_tree(root, needle: bytes) -> list[str]:
    return [
        str(p) for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()
    ]


def test_hard_forget_purges_shard_bytes(config):
    """Edge's delete+flush leaves content in WAL/payload pages; the
    purge-rebuild must not. Nothing under the data dir may contain the
    forgotten text afterwards."""
    store = make_store(config)
    try:
        [a] = store.remember(f"the secret meeting is at {SENTINEL}")
        store.remember("Dylan prefers window seats")
        store.forget(a.memory.id, mode="hard")
        assert store.backend.count() == 1
        assert store.recall("window seats")  # survivors still recallable
    finally:
        store.close()
    assert _grep_tree(config.data_dir, SENTINEL.encode()) == []


def test_multi_fact_input_keeps_no_shared_source_text(config):
    llm = FakeLLM(extract_response={"memories": [
        {"text": f"the code word is {SENTINEL}", "importance": 0.8},
        {"text": "Dylan flies to Berlin on Friday", "importance": 0.7},
    ]})
    store = make_store(config, llm=llm)
    try:
        actions = store.remember(f"code word {SENTINEL}; also flying to Berlin Friday")
        assert len(actions) == 2
        assert all(a.memory.source_text is None for a in actions)
        [secret_action] = [a for a in actions if SENTINEL in a.memory.text]
        store.forget(secret_action.memory.id, mode="hard")
    finally:
        store.close()
    assert _grep_tree(config.data_dir, SENTINEL.encode()) == []


def test_interrupted_purge_recovers_on_open(config):
    store = make_store(config)
    store.remember("Dylan's cat is named Miso")
    store.remember("Dylan prefers window seats")
    store.close()

    (config.data_dir / "purge.pending").touch()
    reopened = make_store(config)
    try:
        assert not (config.data_dir / "purge.pending").exists()
        assert reopened.backend.count() == 2
        assert reopened.recall("cat name", reinforce=False)
    finally:
        reopened.close()


def test_interrupted_hard_forget_reclaims_bytes_on_open(config):
    """A crash between hard_forget's DELETE-commit and its VACUUM leaves the
    forgotten plaintext in journal.db free pages. Recovery rebuilds the shard
    AND re-VACUUMs, so the bytes don't survive on exactly this crash window."""
    store = make_store(config)
    [a] = store.remember(f"the secret is {SENTINEL}")
    store.remember("Dylan prefers window seats")
    # Simulate the crash: the DELETE + tombstone committed, but the process
    # died before VACUUM and before the shard purge-rebuild.
    with store.journal._conn:
        store.journal._conn.execute(
            "DELETE FROM journal WHERE memory_id = ?", (a.memory.id,))
        store.journal._conn.execute(
            "INSERT OR REPLACE INTO tombstones (memory_id, ts, shard)"
            " VALUES (?, 1.0, 'private')", (a.memory.id,))
    (config.data_dir / "purge.pending").touch()
    store.close()
    # Precondition: the bytes are still on disk (the shard still holds it).
    assert _grep_tree(config.data_dir, SENTINEL.encode())

    reopened = make_store(config)
    try:
        assert not (config.data_dir / "purge.pending").exists()
        assert reopened.backend.count() == 1  # only the survivor
    finally:
        reopened.close()
    assert _grep_tree(config.data_dir, SENTINEL.encode()) == []


def test_import_scrubs_unredacted_payloads(config):
    fake_key = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
    line = (
        '{"seq": 1, "op": "upsert", "memory_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",'
        f' "payload": {{"text": "aws key is {fake_key}"}}, "ts": 1.0}}'
    )
    store = make_store(config)
    try:
        from engram.redact import redact

        store.journal.import_jsonl(io.StringIO(line), scrub=lambda t: redact(t).text)
        [entry] = store.journal.entries()
        assert fake_key not in entry.payload["text"]
        assert "[REDACTED:aws-access-key]" in entry.payload["text"]
    finally:
        store.close()


def test_noop_leaves_audit_row(config):
    llm = FakeLLM(judge_responses=[{"op": "NOOP", "target": 0, "confidence": 0.95}])
    store = make_store(config, llm=llm)
    try:
        store.remember("Dylan's cat is named Miso")
        store.remember("Dylan has a cat called Miso")
        noops = [e for e in store.journal.entries() if e.op == "noop"]
        assert len(noops) == 1
        assert noops[0].payload["dropped_text"] == "Dylan has a cat called Miso"
        # The audit row itself never wedges the high-water mark; only the
        # deliberately-lazy reinforce bump may ride until the next flush.
        assert all(e.op == "reinforce" for e in store.journal.pending())
    finally:
        store.close()


class _BoolTargetLLM:
    def available(self):
        return True

    def generate_json(self, system, prompt):
        return {"op": "SUPERSEDE", "target": True, "confidence": 0.99}


def test_judge_bool_target_is_not_an_index():
    candidates = [
        Memory(id="6f1a4b1e-0000-4000-8000-000000000001", text="a"),
        Memory(id="6f1a4b1e-0000-4000-8000-000000000002", text="b"),
    ]
    verdict = judge("new fact", candidates, _BoolTargetLLM())
    assert verdict.op is Op.ADD and verdict.target is None


def test_noop_without_target_defaults_to_sole_candidate():
    # qwen3 reliably returns NOOP with target=null; a single candidate makes
    # the target unambiguous, so the duplicate must not degrade into an ADD.
    only = Memory(id="6f1a4b1e-0000-4000-8000-000000000001", text="a")
    llm = FakeLLM(judge_responses=[{"op": "NOOP", "target": None, "confidence": 1.0}])
    verdict = judge("a restated", [only], llm)
    assert verdict.op is Op.NOOP and verdict.target is only


def test_noop_without_target_stays_add_when_ambiguous():
    # Multiple candidates and no named target: which duplicate is genuinely
    # ambiguous, so fail toward ADD rather than guess.
    candidates = [
        Memory(id="6f1a4b1e-0000-4000-8000-000000000001", text="a"),
        Memory(id="6f1a4b1e-0000-4000-8000-000000000002", text="b"),
    ]
    llm = FakeLLM(judge_responses=[{"op": "NOOP", "target": None, "confidence": 1.0}])
    verdict = judge("new fact", candidates, llm)
    assert verdict.op is Op.ADD and verdict.target is None

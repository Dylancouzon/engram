"""Store pipeline + the three M0 exit tests (spec §9)."""

import pytest
from conftest import FakeEmbedder, FakeLLM, make_store

from engram.models import Op
from engram.store import MemoryStore, StoreLockedError, WriteRefusedError


def test_remember_and_recall(store):
    store.remember("Dylan's cat is named Miso", scope="personal")
    store.remember("The deploy pipeline uses GitHub Actions", scope="work")
    hits = store.recall("what is the cat called", scope="personal")
    assert hits and hits[0].memory.text == "Dylan's cat is named Miso"


def test_scope_prefilter(store):
    store.remember("Dylan's cat is named Miso", scope="personal")
    assert store.recall("cat named", scope="work") == []


def test_general_fact_routes_out_of_project_scope(config):
    # A fact about the user follows them into every repo, but only when the
    # classifier is confident; a project-specific sibling stays put.
    llm = FakeLLM(extract_response={"memories": [
        {"text": "Dylan prefers concise READMEs", "general": True,
         "general_confidence": 0.9},
        {"text": "This repo's CI uses GitHub Actions", "general": False},
    ]})
    store = make_store(config, llm=llm)
    try:
        [general, project] = store.remember(
            "Dylan prefers concise READMEs. This repo's CI uses GitHub Actions.",
            scope="project:engram",
        )
        assert general.memory.scope == "default"
        assert project.memory.scope == "project:engram"
    finally:
        store.close()


def test_low_confidence_general_stays_in_project_scope(config):
    # general=True but below the confidence gate: keep the project scope. A
    # wrongly-generalized fact follows the user everywhere, so escaping the
    # project scope requires the same confidence bar as the conflict judge.
    llm = FakeLLM(extract_response={"memories": [
        {"text": "Walt prefers jeans over flannels", "general": True,
         "general_confidence": 0.5},
    ]})
    store = make_store(config, llm=llm)
    try:
        [action] = store.remember("Walt prefers jeans over flannels",
                                  scope="project:ecommerce-demo")
        assert action.memory.scope == "project:ecommerce-demo"
    finally:
        store.close()


def test_general_flag_ignored_outside_project_scope(config):
    # The flag only demotes an inferred project scope; an explicit
    # non-project scope is the caller's own choice.
    llm = FakeLLM(extract_response={"memories": [
        {"text": "Dylan prefers concise READMEs", "general": True},
    ]})
    store = make_store(config, llm=llm)
    try:
        [action] = store.remember("Dylan prefers concise READMEs", scope="work")
        assert action.memory.scope == "work"
    finally:
        store.close()


def test_recall_reinforces(store):
    store.remember("Dylan drinks oat-milk flat whites")
    first = store.recall("coffee oat milk drinks")[0]
    assert first.memory.access_count == 0
    second = store.recall("coffee oat milk drinks")[0]
    assert second.memory.access_count == 1
    assert second.memory.last_accessed is not None


def test_reinforcement_does_not_grow_journal(config):
    # A store meant to last years must not let reads inflate the source of
    # truth: many recalls collapse to one reinforce row, and the accumulated
    # count survives a restart.
    store = make_store(config)
    store.remember("Dylan drinks oat-milk flat whites")
    for _ in range(20):
        store.recall("coffee oat milk drinks")
    assert store.journal.row_count == 2  # one upsert + one collapsed reinforce
    mid = store.recall("coffee oat milk drinks", reinforce=False)[0].memory.id
    store.close()

    reopened = make_store(config)
    try:
        m = reopened.get(mid)
        assert m.access_count >= 20  # count preserved across restart
        assert reopened.journal.row_count == 2
    finally:
        reopened.close()


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


def test_hard_forget_purges_target_content_in_review_rows(config):
    """A review row is keyed by the NEW memory's id but its merged_text carries
    the TARGET's content. Hard-forgetting the target must purge those rows too,
    or the bytes survive in the log."""
    llm = FakeLLM(judge_responses=[
        {"op": "UPDATE", "target": 0, "confidence": 0.6,
         "text": "Dylan lives in Berlin ZZSECRETZZ"}])
    store = make_store(config, llm=llm)
    try:
        [a] = store.remember("Dylan lives in Paris")
        store.remember("Dylan lives in Berlin now")  # judged UPDATE, low conf -> queued
        assert store.pending_reviews(), "expected an ambiguous UPDATE to queue"
        store.forget(a.memory.id, mode="hard")
    finally:
        store.close()
    assert b"ZZSECRETZZ" not in config.journal_path.read_bytes()


# --- durability -------------------------------------------------------------------


def test_mid_write_apply_failure_not_sealed_by_later_write(config):
    """A write acked to the journal whose Edge apply then raises must not be
    sealed under the flush high-water mark by a later successful write — Edge
    never replays its WAL, so replay-on-open is the only thing that refills the
    gap, and an advanced mark would hide it forever."""

    class FailOnPoison(FakeEmbedder):
        def embed_documents(self, texts):
            if any("POISON" in t for t in texts):
                raise RuntimeError("edge apply blew up")
            return super().embed_documents(texts)

    store = MemoryStore(config, embedder=FailOnPoison(), llm=None)
    with pytest.raises(RuntimeError):
        store.remember("POISON pill about the Berlin flight on Friday")
    assert store._flush_damaged  # the gap is recorded
    # A later write succeeds but must NOT advance the mark past the gap.
    store.remember("Dylan uses uv for Python packaging")
    # Abandon the way a dead process would (no clean close/rebuild).
    store.journal.close()
    for b in store.backends.values():
        b.close()
    import fcntl

    fcntl.flock(store._lock_file, fcntl.LOCK_UN)
    store._lock_file.close()

    reopened = make_store(config)  # healthy embedder; replay refills the gap
    try:
        assert any("POISON" in h.memory.text
                   for h in reopened.recall("Berlin flight Friday"))
        assert any("uv" in h.memory.text
                   for h in reopened.recall("python packaging"))
    finally:
        reopened.close()


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

    shutil.rmtree(config.shard_path("private"))
    reopened = make_store(config)
    try:
        reopened.rebuild()
        assert reopened.backend.count() == 1
        assert reopened.recall("cat name")[0].memory.text == "Dylan's cat is named Miso"
        assert all(h.memory.id != b.memory.id for h in reopened.recall("lives in Paris"))
    finally:
        reopened.close()


def test_resolve_target_hex_never_falls_through_to_search(store):
    """A short id (what the CLI prints) resolves by prefix; an unknown
    hex string resolves to NOTHING — never to a semantic search hit,
    which once let `forget <typo> --yes` purge the wrong memory."""
    from engram.cli import _resolve_target

    [action] = store.remember("Dylan's cat is named Miso")
    short = action.memory.id.split("-")[0]
    assert _resolve_target(store, short).id == action.memory.id
    assert _resolve_target(store, action.memory.id).id == action.memory.id
    assert _resolve_target(store, "deadbeef") is None
    # Non-hex text still resolves by search (documented behavior).
    assert _resolve_target(store, "cat named Miso").id == action.memory.id


def test_list_browses_without_query(store):
    store.remember("Dylan's cat is named Miso", scope="personal")
    store.remember("Correction: the cat is named Mochi", scope="personal")
    listed = store.list()
    assert all(m.is_valid for m in listed)
    everything = store.list(include_invalid=True)
    assert len(everything) >= len(listed)
    assert store.list(limit=1) and len(store.list(limit=1)) == 1


def test_dashboard_renders_memories_and_events(store):
    from engram.dashboard import render_dashboard

    store.remember("Dylan's cat is named Miso")
    store.log_event("prompt-recall", hits=1)
    memories = [
        {"id": m.id, "text": m.text, "type": m.type.value, "scope": m.scope,
         "tags": m.tags, "created_at": m.created_at,
         "access_count": m.access_count, "valid": m.is_valid}
        for m in store.list(include_invalid=True)
    ]
    html = render_dashboard(memories, store.recent_events(10), store.stats())
    assert "Miso" in html and "prompt-recall" in html
    # The embedded JSON escapes "</" so memory text can never break out of
    # the <script type="application/json"> element.
    payload = html.split('id="data">')[1].split("</script>")[0]
    assert "</" not in payload


def test_event_detail_roundtrips_and_prunes_to_newest(store):
    import json

    from engram.journal import Journal

    store.log_event("prompt-recall", hits=1,
                    detail=json.dumps({"prompt": "hi", "surfaced": ["a fact"]}))
    got = store.recent_events(1)[0]
    assert got["detail"] == {"prompt": "hi", "surfaced": ["a fact"]}

    # Only the newest _DETAIL_KEEP events retain their detail text; older ones
    # keep the row (the counter) but drop the text so storage stays bounded.
    keep = Journal._DETAIL_KEEP
    for i in range(keep + 5):
        store.log_event("prompt-recall", hits=1, detail=json.dumps({"prompt": f"p{i}"}))
    with_detail = [e for e in store.recent_events(keep + 20) if e["detail"]]
    assert len(with_detail) == keep
    # And events without detail still record (the metric is intact).
    store.log_event("prompt-recall", hits=0)
    assert store.recent_events(1)[0]["detail"] is None


def test_event_detail_malformed_degrades_not_crashes(store):
    # A non-cli client could store a non-JSON detail string; one bad row must
    # not crash the whole events read (serve /api/state, `engram log`).
    store.journal.log_event("prompt-recall", hits=1, detail="{not json")
    store.log_event("prompt-recall", hits=1, detail='{"prompt": "ok"}')
    events = store.recent_events(10)  # must not raise
    assert {"prompt": "ok"} in [e["detail"] for e in events]
    assert None in [e["detail"] for e in events]


def test_event_detail_prune_counts_only_detail_rows(store):
    import json

    store.log_event("prompt-recall", hits=1, detail=json.dumps({"prompt": "first"}))
    for _ in range(49):
        store.log_event("prompt-recall", hits=0)
    store.log_event("prompt-recall", hits=1, detail=json.dumps({"prompt": "second"}))

    got = [e["detail"]["prompt"] for e in store.recent_events(60) if e["detail"]]
    assert got == ["second", "first"]


def test_event_detail_inline_migration_does_not_reset_ring(tmp_path):
    import json
    import sqlite3

    from engram.journal import Journal

    path = tmp_path / "journal.db"
    j = Journal(path)
    j.close()

    conn = sqlite3.connect(path)
    with conn:
        conn.execute("ALTER TABLE events ADD COLUMN detail TEXT")
        conn.execute(
            "INSERT INTO events (kind, ts, hits, detail) VALUES (?, ?, ?, ?)",
            ("prompt-recall", 1.0, 1, json.dumps({"prompt": "first"})),
        )
        conn.execute(
            "INSERT INTO events (kind, ts, hits, detail) VALUES (?, ?, ?, ?)",
            ("prompt-recall", 2.0, 1, json.dumps({"prompt": "second"})),
        )
    conn.close()

    migrated = Journal(path)
    migrated.close()
    reopened = Journal(path)
    try:
        reopened.log_event("prompt-recall", hits=1,
                           detail=json.dumps({"prompt": "third"}))
        got = [
            json.loads(detail)["prompt"]
            for _kind, _ts, _hits, detail in reopened.recent_events(10)
            if detail
        ]
        assert got == ["third", "second", "first"]
    finally:
        reopened.close()


def test_flush_damaged_surfaces_in_stats(store):
    """A post-ack Edge-apply failure freezes the flush mark; stats() must
    surface it so a silent durability freeze isn't invisible."""
    assert "flush_damaged" not in store.stats()
    store._flush_damaged = True
    assert "flush_damaged" in store.stats()

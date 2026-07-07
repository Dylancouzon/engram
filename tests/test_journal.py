import io

from engram.journal import Journal


def test_append_and_pending(tmp_path):
    j = Journal(tmp_path / "j.db")
    s1 = j.append("upsert", "id-1", {"text": "hello"})
    s2 = j.append("upsert", "id-2", {"text": "world"})
    assert [e.seq for e in j.pending()] == [s1, s2]
    j.mark_flushed(s1)
    assert [e.memory_id for e in j.pending()] == ["id-2"]
    j.mark_flushed(s2)
    assert j.pending() == []


def test_idempotency_key_dedups(tmp_path):
    j = Journal(tmp_path / "j.db")
    s1 = j.append("upsert", "id-1", {"text": "x"}, idempotency_key="k1")
    s2 = j.append("upsert", "id-1", {"text": "x"}, idempotency_key="k1")
    assert s1 == s2
    assert j.last_seq == s1


def test_append_many_is_atomic(tmp_path):
    j = Journal(tmp_path / "j.db")
    seqs = j.append_many([
        ("upsert", "old", {"text": "a", "valid_to": 5.0}, None),
        ("upsert", "new", {"text": "b"}, None),
    ])
    assert len(seqs) == 2 and seqs[1] == seqs[0] + 1


def test_hard_forget_scrubs_and_tombstones(tmp_path):
    path = tmp_path / "j.db"
    j = Journal(path)
    j.append("upsert", "id-1", {"text": "the secret meeting location"})
    j.append("upsert", "id-2", {"text": "innocuous"})
    j.hard_forget("id-1")

    assert j.is_tombstoned("id-1")
    assert [e.memory_id for e in j.entries()] == ["id-2"]
    # The content must not survive anywhere in the db file (VACUUM ran).
    j.close()
    raw = path.read_bytes()
    assert b"secret meeting location" not in raw
    assert b"innocuous" in raw


def test_reinforce_collapses_to_one_row(tmp_path):
    # Reads must not grow the source of truth: a memory recalled a thousand
    # times keeps exactly one reinforce row, holding the latest absolute count.
    j = Journal(tmp_path / "j.db")
    up = j.append("upsert", "id-1", {"text": "fact"})
    last = up
    for count in range(1, 1001):
        last = j.reinforce("id-1", {"access_count": count, "last_accessed": 1.0})
    rows = j.entries()
    assert [e.op for e in rows] == ["upsert", "reinforce"]  # not 1001 rows
    assert j.row_count == 2
    assert rows[-1].payload["access_count"] == 1000  # latest count preserved
    assert last > up  # new seq stays above what it replaced (replay ordering)


def test_lww_clock_ignores_reads_and_audit_rows(tmp_path):
    # The sync LWW clock must track the last CONTENT change, not activity.
    # A later read bump (reinforce) and a later dedup no-op both carry fresh
    # timestamps; if they moved the clock, a local recall or dedup would shadow
    # a genuine (older but real) remote edit and silently drop it on pull.
    j = Journal(tmp_path / "j.db")
    j.append("upsert", "id-1", {"text": "x"}, ts=100.0)
    j.reinforce("id-1", {"access_count": 1})           # ts = now, far above 100
    j.append("noop", "id-1", {"dropped_text": "dup"})  # ts = now, far above 100
    assert j.last_ts_for("id-1") == 100.0
    j.append("sync-pull", "id-1", {"text": "y"}, ts=200.0)  # a real content write
    assert j.last_ts_for("id-1") == 200.0


def test_export_import_roundtrip(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.append("upsert", "id-1", {"text": "fact one"})
    j.append("upsert", "id-2", {"text": "fact two"})
    j.hard_forget("id-2")
    buf = io.StringIO()
    n = j.export_jsonl(buf)
    assert n == 2  # one surviving entry + one tombstone line

    j2 = Journal(tmp_path / "j2.db")
    buf.seek(0)
    j2.import_jsonl(buf)
    assert [e.memory_id for e in j2.entries()] == ["id-1"]
    assert j2.is_tombstoned("id-2")

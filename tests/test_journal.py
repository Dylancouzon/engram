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

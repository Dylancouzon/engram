"""_transcript_tail must feed the extractor only what the user actually
entered. Claude Code injects skill bodies, hook notices, and reference text
as user-role turns; those were being stored as "memories". The promptSource
gate is the fix.

Also covers the project-scope derivation and the capture high-water mark:
a second capture of an unchanged transcript must be a no-op, and appending
new turns must extract only the delta."""
from __future__ import annotations

import json
from pathlib import Path

from engram.cli import (
    _activity_detail,
    _best_rejected,
    _hook_recall_scope,
    _load_marks,
    _project_scope,
    _recall_usefulness,
    _save_marks,
    _transcript_tail,
)


def test_tail_keeps_only_user_entered_turns(tmp_path):
    entries = [
        {"promptSource": "typed", "message": {"role": "user",
         "content": "I decided to pin the project to Python 3.12 for Edge wheels."}},
        {"promptSource": "sdk", "message": {"role": "user",
         "content": "You have access to this Qdrant skill:\n---\nname: qdrant-edge"}},
        {"promptSource": "system", "message": {"role": "user",
         "content": "<task-notification>done</task-notification>"}},
        {"promptSource": None, "message": {"role": "user",
         "content": "Base directory for this skill: /Users/x/.claude/skills/foo"}},
        {"promptSource": "queued", "message": {"role": "user", "content": [
            {"type": "text", "text": "Also, always use gpt-5.5 for code."}]}},
        {"message": {"role": "assistant", "content": "sure"}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))

    tail, mark = _transcript_tail(str(p), 8000)

    assert "Python 3.12" in tail            # typed
    assert "gpt-5.5" in tail                # queued, list-form content
    assert "qdrant-edge" not in tail        # sdk skill body
    assert "task-notification" not in tail  # system notice
    assert "Base directory" not in tail     # None-source skill path
    assert "sure" not in tail               # assistant turn
    assert mark == 2                        # only 2 entries qualified


def test_tail_second_call_at_the_same_mark_is_a_noop(tmp_path):
    entries = [{"promptSource": "typed", "message": {"role": "user",
                "content": "the project uses uv, not pip"}}]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))

    tail, mark = _transcript_tail(str(p), 8000)
    assert tail and mark == 1

    tail2, mark2 = _transcript_tail(str(p), 8000, mark)
    assert tail2 == ""
    assert mark2 == mark


def test_tail_appended_entries_capture_only_the_delta(tmp_path):
    entries = [{"promptSource": "typed", "message": {"role": "user",
                "content": "the project uses uv, not pip"}}]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    _, mark = _transcript_tail(str(p), 8000)

    entries.append({"promptSource": "typed", "message": {"role": "user",
                     "content": "correction: always run tests with rtk proxy"}})
    p.write_text("\n".join(json.dumps(e) for e in entries))
    tail2, mark2 = _transcript_tail(str(p), 8000, mark)

    assert "rtk proxy" in tail2
    assert "not pip" not in tail2  # already processed at the prior mark
    assert mark2 == 2


def test_marks_round_trip_and_bound_to_50(tmp_path):
    data_dir = tmp_path / "home"
    data_dir.mkdir()
    marks = {f"/t{i}.jsonl": i for i in range(60)}

    _save_marks(str(data_dir), marks)
    loaded = _load_marks(str(data_dir))

    assert len(loaded) == 50
    assert "/t59.jsonl" in loaded   # newest kept
    assert "/t0.jsonl" not in loaded  # oldest dropped


def test_marks_missing_or_corrupt_file_degrades_to_empty(tmp_path):
    assert _load_marks(str(tmp_path / "nonexistent")) == {}

    data_dir = tmp_path / "home"
    data_dir.mkdir()
    (data_dir / "capture-marks.json").write_text("{not json")
    assert _load_marks(str(data_dir)) == {}


def test_project_scope_derived_from_payload_cwd():
    assert _project_scope({"cwd": "/Users/dylan/Projects/Engram"}) == "project:engram"
    # Missing cwd falls back to the process cwd — never None, or the scope
    # filter would silently disable and recall across every project.
    assert _project_scope({}) == f"project:{Path.cwd().name.lower()}"


def test_hook_recall_scope_prefers_explicit_then_falls_back_to_project_and_default():
    payload = {"cwd": "/Users/dylan/Projects/Engram"}
    assert _hook_recall_scope("work", payload) == "work"           # explicit wins
    assert _hook_recall_scope(None, payload) == ["project:engram", "default"]
    # No cwd: falls back to a real project scope, never None (no unfiltered recall).
    assert _hook_recall_scope(None, {}) == [f"project:{Path.cwd().name.lower()}", "default"]


def test_activity_detail_records_study_fields():
    # scope = §3 self-healing attribution; ids/latency/best_rejected feed the
    # offline dogfood report. All omitted when absent (kept small).
    detail = json.loads(_activity_detail(
        surfaced=["x"], ids=["abc"], scope=["project:engram", "default"],
        latency_ms=12.3, best_rejected=0.42))
    assert detail["scope"] == ["project:engram", "default"]
    assert detail["ids"] == ["abc"]
    assert detail["latency_ms"] == 12.3
    assert detail["best_rejected"] == 0.42
    bare = json.loads(_activity_detail(surfaced=["x"]))
    assert not ({"scope", "ids", "latency_ms", "best_rejected"} & bare.keys())


class _Hit:
    def __init__(self, similarity):
        self.similarity = similarity


def test_best_rejected_is_top_uninjected_similarity():
    raw = [_Hit(0.9), _Hit(0.42), _Hit(0.3)]
    assert _best_rejected(raw, raw[:1]) == 0.42       # best of the two not injected
    assert _best_rejected(raw, raw) is None            # everything made the cut


def _transcript(tmp_path, entries):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return str(p)


def test_recall_usefulness_matches_injected_to_replies(tmp_path):
    entries = [
        {"attachment": {"content": "<engram-memories>\n"
                        "- Dylan prefers oat-milk lattes every single morning\n"
                        "- The deployment pipeline uses github actions workflow scripts"}},
        {"message": {"role": "assistant", "content":
                     "I'll note that you prefers oat-milk lattes every single morning."}},
    ]
    u = _recall_usefulness(_transcript(tmp_path, entries))
    assert u["injected"] == 2
    assert u["used"] == 1
    assert any("oat-milk" in t for t in u["used_texts"])
    assert any("deployment pipeline" in t for t in u["unused_texts"])


def test_recall_usefulness_none_without_injection(tmp_path):
    entries = [{"message": {"role": "assistant", "content": "no memories here"}}]
    assert _recall_usefulness(_transcript(tmp_path, entries)) is None


def test_spawn_background_capture_detaches_and_pipes_payload(monkeypatch):
    # The Stop hook must detach: a new session (so Claude Code's hook-group
    # signal can't kill it), the payload piped to the child's stdin, and the
    # re-spawn guard set. This is what stops the 2-min interactive stall.
    from engram import cli

    class FakeStdin:
        data = b""

        def write(self, b):
            self.data += b

        def close(self):
            pass

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()

    seen = {}

    def fake_popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
        seen["proc"] = FakeProc()
        return seen["proc"]

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    payload = {"transcript_path": "/x", "session_id": "s"}
    cli._spawn_background_capture("/tmp/dd", payload, "project:foo", 4000)

    a = seen["args"]
    assert a[:3] == [cli.sys.argv[0], "--data-dir", "/tmp/dd"]
    assert a[3:5] == ["hook", "capture"] and "--scope" in a and "project:foo" in a
    assert "4000" in a
    assert seen["kw"]["start_new_session"] is True
    assert seen["kw"]["env"]["ENGRAM_CAPTURE_BG"] == "1"
    assert json.loads(seen["proc"].stdin.data) == payload


def test_capture_parent_gates_before_spawning(tmp_path, monkeypatch):
    # Regression: the parent must NOT spawn a child on a turn-end with no new
    # content — that per-Stop Python spawn was the process churn. Only spawn
    # when there is actually something to extract.
    from click.testing import CliRunner

    from engram import cli

    calls = []
    monkeypatch.setattr(cli, "_spawn_background_capture",
                        lambda *a, **k: calls.append(a))
    runner = CliRunner()

    substantial = tmp_path / "big.jsonl"
    substantial.write_text(json.dumps(
        {"message": {"role": "user", "content": "x" * 200}, "promptSource": "typed"}))
    r = runner.invoke(cli.main, ["--data-dir", str(tmp_path), "hook", "capture"],
                      input=json.dumps({"transcript_path": str(substantial)}))
    assert r.exit_code == 0 and len(calls) == 1  # new content -> spawn

    calls.clear()
    nothing_new = tmp_path / "tiny.jsonl"
    nothing_new.write_text(json.dumps({"message": {"role": "assistant", "content": "hi"}}))
    r = runner.invoke(cli.main, ["--data-dir", str(tmp_path), "hook", "capture"],
                      input=json.dumps({"transcript_path": str(nothing_new)}))
    assert r.exit_code == 0 and calls == []  # nothing new -> no spawn, no churn


def test_capture_debounce(tmp_path):
    # First capture of a conversation is allowed and stamps the time; an
    # immediate repeat is debounced so rapid turns batch into one extraction.
    from engram import cli

    dd = str(tmp_path)
    assert cli._capture_debounced(dd, "/x/sess.jsonl") is False   # first: allowed
    assert cli._capture_debounced(dd, "/x/sess.jsonl") is True    # right after: skip
    assert cli._capture_debounced(dd, "/x/other.jsonl") is False  # other convo independent


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""_transcript_tail must feed the extractor only what the user actually
entered. Claude Code injects skill bodies, hook notices, and reference text
as user-role turns; those were being stored as "memories". The promptSource
gate is the fix.

Also covers the project-scope derivation and the capture high-water mark:
a second capture of an unchanged transcript must be a no-op, and appending
new turns must extract only the delta."""
from __future__ import annotations

import json

from engram.cli import (
    _hook_recall_scope,
    _load_marks,
    _project_scope,
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
    assert _project_scope({}) is None


def test_hook_recall_scope_prefers_explicit_then_falls_back_to_project_and_default():
    payload = {"cwd": "/Users/dylan/Projects/Engram"}
    assert _hook_recall_scope("work", payload) == "work"           # explicit wins
    assert _hook_recall_scope(None, payload) == ["project:engram", "default"]
    assert _hook_recall_scope(None, {}) is None                    # no cwd, no filter


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

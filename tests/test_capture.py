"""_transcript_tail must feed the extractor only what the user actually
entered. Claude Code injects skill bodies, hook notices, and reference text
as user-role turns; those were being stored as "memories". The promptSource
gate is the fix."""
from __future__ import annotations

import json

from engram.cli import _transcript_tail


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

    tail = _transcript_tail(str(p), 8000)

    assert "Python 3.12" in tail            # typed
    assert "gpt-5.5" in tail                # queued, list-form content
    assert "qdrant-edge" not in tail        # sdk skill body
    assert "task-notification" not in tail  # system notice
    assert "Base directory" not in tail     # None-source skill path
    assert "sure" not in tail               # assistant turn


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""Checks for the dev-only dogfood report (tools/report.py). Loaded by path
since tools/ is a scripts dir, not a package."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "dogfood_report", Path(__file__).resolve().parent.parent / "tools" / "report.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def test_percentile():
    assert rp._pct([], 50) == 0.0
    assert rp._pct([10, 20, 30], 50) == 20
    assert rp._pct([10, 20, 30, 40], 100) == 40


def test_usefulness_dedups_by_session_keeping_last():
    # Two Stops of one session recompute cumulatively; the report must count
    # only the later one, not sum both.
    rows = [
        {"ts": 1, "kind": "recall-usefulness", "session_id": "s", "used": 1, "judged": 5},
        {"ts": 2, "kind": "recall-usefulness", "session_id": "s", "used": 3, "judged": 5},
    ]
    text = "\n".join(rp.report(rows))
    assert "3/5 injected memories echoed" in text


def test_entrenchment_counts_ids_and_maps_text():
    # id->text recovered from the parallel ids/surfaced arrays; one id twice.
    rows = [
        {"ts": 1, "kind": "prompt-recall", "hits": 1, "ids": ["a", "b"],
         "surfaced": ["fact A", "fact B"]},
        {"ts": 2, "kind": "prompt-recall", "hits": 1, "ids": ["a"],
         "surfaced": ["fact A"]},
    ]
    text = "\n".join(rp.report(rows))
    assert "3 total injections" in text
    assert "fact A" in text


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

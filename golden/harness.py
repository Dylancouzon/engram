"""Golden-set harness: grades the real write model (real embeddings, real
local judge) against golden/cases.json. This is the fixture the M0 conflict
thresholds are tuned with (spec §10).

Usage:
    uv run python golden/harness.py [cases.json] [--verbose]

Each case gets a fresh throwaway store. `existing` memories are seeded with
the judge bypassed (their presence is the fixture, not the thing under
test); the `input` write then runs the full pipeline. Scored:
  - op accuracy: did the write model pick the expected op?
  - recall: after the write, does the probe query surface the right memory
    in the top-k (and never a memory that should be gone)?

Requires the FastEmbed models (downloaded on first run) and, for judge ops
other than ADD, a local Ollama with the configured model. Without Ollama
everything degrades to ADD and the report says so honestly.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engram.config import Config  # noqa: E402
from engram.models import Op  # noqa: E402
from engram.resolve import Verdict  # noqa: E402
from engram.store import MemoryStore  # noqa: E402


def run_case(case: dict, base_dir: Path, verbose: bool) -> dict:
    store = MemoryStore(Config(data_dir=base_dir / case["name"]))
    try:
        # Seed fixture memories without judging them against each other.
        original_resolve = store._resolve_conflict
        store._resolve_conflict = lambda *args, **kwargs: Verdict(Op.ADD, None, 1.0)
        for text in case["existing"]:
            store.remember(text)
        store._resolve_conflict = original_resolve

        actions = store.remember(case["input"])
        got_ops = [a.op.value for a in actions] or ["(salience-dropped)"]
        expected = case.get("expect_op")
        expected = [expected] if isinstance(expected, str) else (expected or [])
        op_ok = any(op in expected for op in got_ops) if expected else True

        recall_ok = True
        recall_detail = ""
        if case.get("recall_query"):
            hits = store.recall(case["recall_query"], reinforce=False)
            texts = [h.memory.text for h in hits]
            if case.get("expect_recalled"):
                recall_ok = any(case["expect_recalled"].lower() in t.lower() for t in texts)
            if recall_ok and case.get("expect_not_recalled"):
                recall_ok = all(case["expect_not_recalled"].lower() not in t.lower()
                                for t in texts)
            if recall_ok and case.get("expect_not_recalled_first") and texts:
                recall_ok = case["expect_not_recalled_first"].lower() not in texts[0].lower()
            recall_detail = f" top: {texts[0][:60]!r}" if texts else " (no hits)"

        if verbose or not (op_ok and recall_ok):
            print(f"  {'PASS' if op_ok and recall_ok else 'FAIL'} {case['name']}: "
                  f"got {got_ops} want {expected or 'any'}{recall_detail}")
        return {"name": case["name"], "op_ok": op_ok, "recall_ok": recall_ok,
                "got": got_ops}
    finally:
        store.close()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    cases_path = Path(args[0]) if args else Path(__file__).parent / "cases.json"
    cases = json.loads(cases_path.read_text())["cases"]

    with tempfile.TemporaryDirectory(prefix="engram-golden-") as tmp:
        base = Path(tmp)
        probe = MemoryStore(Config(data_dir=base / "_probe"))
        judge_live = probe.llm is not None and probe.llm.available()
        probe.close()
        if not judge_live:
            print("WARNING: no local judge model reachable — every op degrades "
                  "to ADD; op accuracy below measures the fallback, not the judge.\n")

        results = [run_case(c, base, verbose) for c in cases]

    op_acc = sum(r["op_ok"] for r in results) / len(results)
    recall_acc = sum(r["recall_ok"] for r in results) / len(results)
    both = sum(r["op_ok"] and r["recall_ok"] for r in results) / len(results)
    print(f"\n{len(results)} cases | op accuracy {op_acc:.0%} | "
          f"recall accuracy {recall_acc:.0%} | both {both:.0%}")
    return 0 if both >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())

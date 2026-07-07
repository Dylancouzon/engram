"""Retrieval eval: recall@k, MRR, and distinct-coverage@k (real embedder).

harness.py grades op accuracy; this grades ranking. It seeds one corpus so
distractors exist, then scores whether each query surfaces its relevant
memories and at what rank.

Relevance is labeled by CORPUS INDEX, not substring: a near-duplicate
distractor shares vocabulary with the answer, so a substring match would
credit a hit on the distractor as a hit on the target. Seeding runs with
llm=None (verbatim, ADD-only) so each corpus line becomes exactly one
memory and ids align to corpus order.

Metrics:
  recall@k    — fraction of queries with any relevant memory in the top-k
  MRR         — mean reciprocal rank of the first relevant hit
  coverage@k  — mean fraction of a query's relevant memories found in top-k
                (== recall@k for single-answer queries; shows diversity on
                multi-answer queries, which is where MMR earns its keep)

Usage:
    uv run python golden/recall.py [recall_cases.json] [--verbose] [--mmr=0.7|none]

--mmr overrides config.mmr_lambda for the MMR-vs-fusion A/B (Change 1).
Requires the FastEmbed models; the judge is irrelevant here.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engram.config import Config  # noqa: E402
from engram.store import MemoryStore  # noqa: E402

KS = (3, 5, 8)


def parse_mmr(argv: list[str]) -> tuple[bool, float | None]:
    """Returns (override?, value). --mmr=none/off disables MMR (DBSF fusion)."""
    for a in argv:
        if a.startswith("--mmr="):
            v = a.split("=", 1)[1].lower()
            return True, (None if v in ("none", "off") else float(v))
    return False, None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    override, mmr = parse_mmr(sys.argv)
    cases_path = Path(args[0]) if args else Path(__file__).parent / "recall_cases.json"
    fixture = json.loads(cases_path.read_text())
    corpus, queries = fixture["corpus"], fixture["queries"]

    with tempfile.TemporaryDirectory(prefix="engram-recall-") as tmp:
        cfg = Config(data_dir=Path(tmp) / "store")
        if override:
            cfg.mmr_lambda = mmr
        # llm=None: verbatim, ADD-only — one memory per corpus line, ids in order.
        store = MemoryStore(cfg, llm=None)
        try:
            id_index: dict[str, int] = {}
            for i, text in enumerate(corpus):
                actions = store.remember(text)
                id_index[actions[0].memory.id] = i

            max_k = max(KS)
            ranks: list[int | None] = []
            covers: list[list[float]] = []  # per query, coverage at each k in KS
            for q in queries:
                relevant = set(q["relevant"])
                hits = store.recall(q["query"], k=max_k, reinforce=False)
                hit_idx = [id_index.get(h.memory.id) for h in hits]
                rank = next((r for r, i in enumerate(hit_idx, 1) if i in relevant), None)
                ranks.append(rank)
                covers.append([
                    len(relevant & set(hit_idx[:k])) / len(relevant) for k in KS
                ])
                if verbose:
                    where = f"@{rank}" if rank else "MISS"
                    print(f"  {where:>5}  {q['query']}")
        finally:
            store.close()

    n = len(queries)
    mrr = sum(1.0 / r for r in ranks if r) / n
    mmr_label = "off (DBSF)" if override and mmr is None else (
        f"{mmr}" if override else "default")
    print(f"\n{n} queries | mmr_lambda={mmr_label} | MRR {mrr:.3f}")
    for j, k in enumerate(KS):
        recall_k = sum(1 for r in ranks if r and r <= k) / n
        coverage_k = sum(c[j] for c in covers) / n
        print(f"  recall@{k} {recall_k:.0%}  |  coverage@{k} {coverage_k:.0%}")
    # Gate: a relevant memory in the top-5 for most queries.
    return 0 if sum(1 for r in ranks if r and r <= 5) / n >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Throwaway experiment (not part of CI): does a nearest-centroid classifier
on existing dense embeddings beat the current extraction-time `general` LLM
call at separating general vs project-scoped facts?

Data: the live store's own hand-corrected scopes (`engram list -n 0 --json`),
not the golden set — cases.json carries no scope labels. Ground truth is
scope != "default" -> project, scope == "default" -> general.

Usage:
    uv run engram list -n 0 --json > /tmp/live_memories.json
    uv run python golden/scope_eval.py /tmp/live_memories.json
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engram.config import Config  # noqa: E402
from engram.embed import Embedder  # noqa: E402
from engram.extract import extract  # noqa: E402
from engram.llm import LocalLLM  # noqa: E402


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def kfold_indices(n: int, k: int, seed: int = 0) -> list[tuple[list[int], list[int]]]:
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    folds = [idx[i::k] for i in range(k)]
    out = []
    for i in range(k):
        test = folds[i]
        train = [j for f in folds if f is not folds[i] for j in f]
        out.append((train, test))
    return out


def prf(tp: int, fp: int, fn: int) -> tuple[float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return precision, recall


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/live_memories.json")
    memories = json.loads(path.read_text())
    memories = [m for m in memories if m.get("valid", True)]
    labels = [0 if m["scope"] == "default" else 1 for m in memories]  # 1 = project
    texts = [m["text"] for m in memories]
    print(f"{len(memories)} memories | general={labels.count(0)} project={labels.count(1)}")

    cfg = Config()
    embedder = Embedder(cfg.models_dir)
    vecs = [e.dense for e in embedder.embed_documents(texts)]

    # --- Arm A: nearest-centroid on dense embeddings ---
    tp = fp = fn = tn = 0
    for train_idx, test_idx in kfold_indices(len(memories), k=5):
        train_general = [vecs[i] for i in train_idx if labels[i] == 0]
        train_project = [vecs[i] for i in train_idx if labels[i] == 1]
        if not train_general or not train_project:
            continue
        cg = [sum(v[d] for v in train_general) / len(train_general) for d in range(len(vecs[0]))]
        cp = [sum(v[d] for v in train_project) / len(train_project) for d in range(len(vecs[0]))]
        for i in test_idx:
            pred = 0 if cosine(vecs[i], cg) >= cosine(vecs[i], cp) else 1
            actual = labels[i]
            if pred == 0 and actual == 0:
                tn += 1
            elif pred == 0 and actual == 1:
                fn += 1
            elif pred == 1 and actual == 1:
                tp += 1
            else:
                fp += 1
    # Report precision/recall on the GENERAL class (the asymmetric-risk class:
    # a wrongly-generalized fact follows the user everywhere).
    gen_tp, gen_fp, gen_fn = tn, fn, fp  # general is the "0" class
    gen_p, gen_r = prf(gen_tp, gen_fp, gen_fn)
    print(f"\nArm A — nearest centroid, 5-fold CV:")
    print(f"  general-class precision {gen_p:.0%}  recall {gen_r:.0%}  "
          f"(tp={gen_tp} fp={gen_fp} fn={gen_fn})")
    print(f"  project-class precision {tp / (tp + fn) if (tp + fn) else 0:.0%}  "
          f"recall {tp / (tp + fp) if (tp + fp) else 0:.0%}")

    # --- Arm A2: logistic regression (numpy, no sklearn dep) on dense embeddings ---
    import numpy as np

    X = np.array(vecs)
    y = np.array(labels, dtype=float)

    def train_logreg(train_idx: list[int], epochs: int = 400, lr: float = 0.1, l2: float = 0.01):
        xt, yt = X[train_idx], y[train_idx]
        # Balance classes so the 14:1 imbalance doesn't just learn "always project".
        w_pos = 1.0
        w_neg = (yt == 1).sum() / max((yt == 0).sum(), 1)
        sample_w = np.where(yt == 0, w_neg, w_pos)
        w = np.zeros(xt.shape[1])
        b = 0.0
        for _ in range(epochs):
            z = xt @ w + b
            p = 1 / (1 + np.exp(-z))
            grad_w = xt.T @ ((p - yt) * sample_w) / len(yt) + l2 * w
            grad_b = float(((p - yt) * sample_w).mean())
            w -= lr * grad_w
            b -= lr * grad_b
        return w, b

    l_tp = l_fp = l_fn = l_tn = 0
    for train_idx, test_idx in kfold_indices(len(memories), k=5):
        w, b = train_logreg(train_idx)
        for i in test_idx:
            p = 1 / (1 + np.exp(-(X[i] @ w + b)))
            pred = 1 if p >= 0.5 else 0
            actual = labels[i]
            if pred == 0 and actual == 0:
                l_tn += 1
            elif pred == 0 and actual == 1:
                l_fn += 1
            elif pred == 1 and actual == 1:
                l_tp += 1
            else:
                l_fp += 1
    l_gen_p, l_gen_r = prf(l_tn, l_fn, l_fp)
    print(f"\nArm A2 — class-balanced logistic regression, 5-fold CV:")
    print(f"  general-class precision {l_gen_p:.0%}  recall {l_gen_r:.0%}  "
          f"(tp={l_tn} fp={l_fn} fn={l_fp})")

    # --- Arm B: replay the current extraction-time `general` LLM call ---
    llm = LocalLLM(cfg.ollama_url, cfg.extraction_model)
    if not llm.available():
        print(f"\nArm B skipped — {cfg.extraction_model} unreachable at {cfg.ollama_url}.")
        return 0
    b_tp = b_fp = b_fn = b_tn = 0
    unusable = 0
    for text, actual in zip(texts, labels, strict=True):
        facts = extract(text, llm)
        if not facts:
            unusable += 1
            continue
        pred = 0 if facts[0].general else 1
        if pred == 0 and actual == 0:
            b_tn += 1
        elif pred == 0 and actual == 1:
            b_fn += 1
        elif pred == 1 and actual == 1:
            b_tp += 1
        else:
            b_fp += 1
    b_gen_p, b_gen_r = prf(b_tn, b_fn, b_fp)
    print(f"\nArm B — {cfg.extraction_model} general-call replay "
          f"({unusable} skipped, no fact returned):")
    print(f"  general-class precision {b_gen_p:.0%}  recall {b_gen_r:.0%}  "
          f"(tp={b_tn} fp={b_fn} fn={b_fp})")

    print("\nShip bar: Arm A general-precision >= Arm B + 5pts, "
          "with Arm A general-recall no more than 10pts below Arm B.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

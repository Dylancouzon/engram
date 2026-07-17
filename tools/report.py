#!/usr/bin/env python3
"""Dogfood self-diagnosis: turn ~/.engram/activity.jsonl into a compact health
report so a session reasons over findings instead of hand-parsing raw JSONL.

DEV-ONLY (remove with activity.jsonl before release). Reads ONE file, no daemon,
no Edge, no embedder — safe to run while the daemon is live (append-only log).

    uv run python tools/report.py                 # ~/.engram/activity.jsonl
    uv run python tools/report.py --days 7         # only the last 7 days
    ENGRAM_HOME=/path uv run python tools/report.py

The numbers are leads, not verdicts: every flag is a hypothesis to confirm in
the store before acting. See docs/self-improve.md for how to act on each.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

# Gates the recall hooks inject at (cli.py). A candidate scoring just under its
# gate is a false-negative lead — a memory that keeps almost-surfacing.
_GATES = {"prompt-recall": 0.5, "session-start-recall": 0.35}
_NEAR = 0.05           # "just under the gate" band width
_SLOW_MS = 500.0       # warm recall over this blocks generation noticeably
_ENTRENCH_PCT = 15.0   # one memory over this share of all injections = rich-get-richer
_MIN_N = 30            # below this a metric is noise — report the number, suppress the FLAG


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def _age(ts: float, now: float) -> str:
    h = (now - ts) / 3600
    return f"{h:.1f}h ago" if h < 48 else f"{h / 24:.1f}d ago"


def load(path: Path, days: float | None) -> list[dict]:
    now = time.time()
    cutoff = now - days * 86400 if days else 0
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) >= cutoff:
            rows.append(r)
    return rows


def report(rows: list[dict]) -> list[str]:
    now = time.time()
    out: list[str] = []
    if not rows:
        return ["No activity records in range."]
    span_d = (rows[-1]["ts"] - rows[0]["ts"]) / 86400
    kinds = Counter(r["kind"] for r in rows)
    out.append("# engram dogfood report")
    out.append(f"{len(rows)} events over {span_d:.1f} days · {dict(kinds)}\n")

    # -- capture health: is the store still growing, or silently degraded? -----
    caps = [r for r in rows if r["kind"] == "auto-capture"]
    degraded = [r for r in rows if r["kind"] == "capture-degraded"]
    out.append("## Capture health")
    if caps:
        out.append(f"- auto-capture: {len(caps)} runs, last {_age(caps[-1]['ts'], now)}")
        gaps = [(b["ts"] - a["ts"]) / 3600 for a, b in zip(caps, caps[1:], strict=False)]
        if gaps and max(gaps) > 24:
            out.append(f"- FLAG longest silent gap between captures: {max(gaps) / 24:.1f}d")
    else:
        out.append("- FLAG no auto-capture events — capture never succeeded in range")
    if degraded:
        out.append(f"- FLAG capture-degraded fired {len(degraded)}x "
                   f"(no extraction model) — last {_age(degraded[-1]['ts'], now)}")

    # -- recall latency: proactive recall blocks generation on the hot path -----
    lat = [r["latency_ms"] for r in rows if "latency_ms" in r]
    out.append("\n## Recall latency (ms)")
    if lat:
        out.append(f"- p50 {_pct(lat, 50):.0f} · p95 {_pct(lat, 95):.0f} · max {max(lat):.0f}"
                   f" (n={len(lat)}){' — small sample' if len(lat) < _MIN_N else ''}")
        slow = [x for x in lat if x > _SLOW_MS]
        if _pct(lat, 95) > _SLOW_MS and len(lat) >= _MIN_N:
            out.append(f"- FLAG p95 over {_SLOW_MS:.0f}ms — {len(slow)} slow recalls "
                       f"(first-after-restart cold-load is expected; steady-state is not)")
    else:
        out.append("- no latency data (pre-instrumentation events)")

    # -- recall hit-rate --------------------------------------------------------
    pr = [r for r in rows if r["kind"] == "prompt-recall"]
    if pr:
        with_hits = sum(1 for r in pr if r.get("hits", 0) > 0)
        out.append(f"\n## Recall hit-rate\n- prompt-recall injected something in "
                   f"{with_hits}/{len(pr)} prompts ({with_hits / len(pr):.0%})")

    # -- usefulness proxy: did injected memories show up in a reply? -----------
    #    Dedup by session_id (later Stops recompute cumulatively — keep the last).
    latest: dict[str, dict] = {}
    for r in rows:
        if r["kind"] == "recall-usefulness":
            key = r.get("session_id") or str(r["ts"])
            if r["ts"] >= latest.get(key, {}).get("ts", 0):
                latest[key] = r
    if latest:
        used = sum(r.get("used", 0) for r in latest.values())
        judged = sum(r.get("judged", 0) for r in latest.values())
        small = " — small sample" if judged < _MIN_N else ""
        out.append("\n## Recall usefulness (weak proxy — overlap, under-counts)")
        out.append(f"- {used}/{judged} injected memories echoed in a reply "
                   f"({used / max(judged, 1):.0%}) across {len(latest)} sessions{small}")
        never = Counter()
        for r in latest.values():
            never.update(t for t in r.get("unused_texts", []))
        top_never = [(t, c) for t, c in never.most_common(8) if c > 1]
        if top_never:
            out.append("- most-injected-never-used (demotion/forget candidates):")
            out.extend(f"    {c}x  {t[:90]}" for t, c in top_never)

    # -- entrenchment: is one memory winning every injection? (rich-get-richer) -
    #    id->text recovered from the parallel `ids`/`surfaced` arrays, no db.
    freq: Counter = Counter()
    text_of: dict[str, str] = {}
    for r in rows:
        ids, surf = r.get("ids") or [], r.get("surfaced") or []
        for i, mid in enumerate(ids):
            freq[mid] += 1
            if i < len(surf):
                text_of.setdefault(mid, surf[i])
    total = sum(freq.values())
    if total:
        low = total < _MIN_N
        out.append(f"\n## Entrenchment ({total} total injections"
                   f"{' — small sample, flags suppressed' if low else ''})")
        for mid, c in freq.most_common(8):
            share = 100 * c / total
            flag = "  <-- FLAG" if share > _ENTRENCH_PCT and not low else ""
            out.append(f"- {c:>4} ({share:4.1f}%)  {text_of.get(mid, mid)[:80]}{flag}")

    # -- false negatives: candidates that kept almost-surfacing -----------------
    near: dict[str, list[float]] = {}
    for r in rows:
        gate = _GATES.get(r["kind"])
        br = r.get("best_rejected")
        if gate and br is not None and gate - _NEAR <= br < gate:
            near.setdefault(r["kind"], []).append(br)
    if near:
        out.append("\n## False-negative leads (best rejected just under the gate)")
        for kind, vals in near.items():
            out.append(f"- {kind}: {len(vals)} near-misses "
                       f"(gate {_GATES[kind]}, e.g. {sorted(vals, reverse=True)[:4]})")
            out.append("    -> lower --min-score, widen scope, or improve embeddings/tags")

    # -- scope health (§3): deeper join needs current scopes (run separately) ---
    out.append("\n## Scope health (§3)")
    out.append("- Needs the id->scope join from the live store; not in this log-only")
    out.append("  pass. See docs/self-improve.md for the `engram list` query.")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=float, default=None, help="only the last N days")
    ap.add_argument("--file", type=Path, default=None, help="activity.jsonl path")
    args = ap.parse_args()
    home = Path(os.environ.get("ENGRAM_HOME", Path.home() / ".engram"))
    path = args.file or home / "activity.jsonl"
    if not path.exists():
        raise SystemExit(f"no activity log at {path} (dogfood first, or set ENGRAM_HOME)")
    print("\n".join(report(load(path, args.days))))


if __name__ == "__main__":
    main()

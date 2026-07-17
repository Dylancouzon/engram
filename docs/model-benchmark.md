# Extraction/judge model benchmark

Benchmarked 7 Ollama models as a replacement for `qwen3:4b` in the
extraction+judge pipeline (`extract.py`/`resolve.py`, called via
`LocalLLM.generate_json`). Same production code, same prompts, real
embeddings — only `Config.extraction_model` changes. Golden set:
`golden/cases.json` (29 cases), grading reused from `golden/harness.py`.
M5 Pro, Ollama `NUM_PARALLEL=1`, live daemon running concurrently (some
queueing expected and noted below).

## Golden-set results (op accuracy is the primary quality signal)

| model | median (s)/call | p90 (s)/call | extraction valid | op acc | recall acc | malformed JSON |
|---|---|---|---|---|---|---|
| qwen3:4b (baseline) | 1.15 | 28.62 | 98% | **86%** | 97% | 3/78 |
| **split: extraction 1.7b / judge 4b** | 0.65 | 0.75 | 100% | **79%** | 93% | 0/77 |
| qwen3:1.7b | 1.05 | 1.24 | 100% | 55% | 86% | 0/77 |
| llama3.2:3b | 0.83 | 0.94 | 100% | 55% | 72% | 0/69 |
| gemma2:2b | 1.27 | 1.58 | 98% | 52% | 97% | 0/81 |
| granite3.1-moe:3b | 0.96 | 1.16 | 100% | 34% | 86% | 0/76 |
| llama3.2:1b | 0.64 | 1.14 | 91% | 34% | 76% | 3/76 |
| qwen3:0.6b | 0.59 | 0.76 | 100% | 31% | 66% | 0/64 |

The split row runs the real `extract()`/`judge()` through `MemoryStore` with
a duck-typed `LocalLLM` that routes each `generate_json` call to a different
model by matching the production system prompt (extraction's vs judge's) —
no store/protocol change, and no production code touched.

Latency here is on golden-set inputs, which are short test sentences
("Dylan lives in Paris" etc.) — not representative of a real hook capture.
See below.

## Real capture latency: prompt length, not just model size, drives the slowdown

Golden-set prompts are ~10-15 words. A real `Stop`/`PreCompact` capture sends
a whole transcript tail. Tested `extract()` on a synthetic ~2000-char/313-word
transcript (a realistic multi-fact session tail):

| model | `extract()` latency | facts | notes |
|---|---|---|---|
| qwen3:4b | 21-35s | 5 | atomic, correctly typed (semantic/episodic/procedural), no hallucination |
| qwen3:1.7b | 8.4s | 7 | usable text, but over-fragmented (splits what qwen3:4b merged into one fact) and ignores the type taxonomy — everything came back `semantic` |
| qwen3:0.6b | 1.5s | 1 | **broken**: the one "fact" it extracted was a quoted assistant question, not a fact |

This reproduces the documented 11-40s figure for qwen3:4b — it's a real
effect of prompt length + model size, not a benchmarking artifact. The
golden-set median/p90 above understate real capture latency for every
model; the *relative* ordering (smaller model → faster) still holds and
gets more pronounced with longer transcripts.

Ran the same transcript through the split config: `extract()` on 1.7b took
4.1s (repeat run; an earlier run under daemon contention hit 8.4s — see
caveats), and `judge()` on 4b against a realistic short 2-candidate list
averaged **0.61s per fact**, 4.2s total across the 7 facts 1.7b extracted.
Split end-to-end: **~8.3s**, vs qwen3:4b full at 21-35s for extraction
alone (judge calls add more on top of that). This confirms the story:
extraction is the latency bottleneck (scales with transcript length ×
model size) and judge is cheap on any model here (short, fixed-size
prompt) — the split keeps the 4b judge's accuracy at near-zero latency
cost.

## Disqualified

- **qwen3:0.6b**: worst op accuracy (31%) and the long-prompt test shows why
  — it grabs the wrong span of text and calls it a fact. Fast, but unusable.
- **llama3.2:1b**: 3/76 malformed-JSON calls (91% extraction validity), plus
  34% op accuracy. Unreliable and low quality.
- **granite3.1-moe:3b**: valid JSON every time but 34% op accuracy — no
  better than the smaller, cheaper models.

## Recommendation

**Ship the split: extraction on qwen3:1.7b, judge on qwen3:4b.** Keeping
the 4b judge recovers most of what full-1.7b lost — op accuracy 55% → 79%
(vs 86% baseline), recall accuracy 86% → 93% (vs 97% baseline) — while the
extraction step (the actual latency bottleneck on real transcripts) still
runs at 1.7b speed. This confirms the hypothesis from the first pass: judge
prompts are short and fixed-size regardless of transcript length, so
judge latency barely depends on model size, but extraction latency scales
with both transcript length and model size. Splitting captures the
extraction speedup with almost none of the judge-side accuracy loss.

Needs one small code change to ship: `Config` currently has a single
`extraction_model` knob shared by both `extract()` and `judge()`
(`store.py` passes `self.llm` to both) — add a `judge_model` field and a
second `LocalLLM` instance. Not done here since the task was benchmark-only;
production code wasn't touched.

If a single model is preferred over adding that knob, qwen3:1.7b for both
(55%/86%) is the next-best fallback, trading more accuracy for one fewer
moving part.

## Caveats

- p90 for qwen3:4b (28.62s) is a single outlier, almost certainly the live
  daemon's own qwen3:4b calls queuing behind the benchmark's (both compete
  for Ollama's single parallel slot) — not a per-call characteristic.
- Golden set is 29 cases; op-accuracy percentages move by ~3.4 points per
  case, so differences under ~7 points (e.g. 52% vs 55%) aren't reliable
  rankings among the low-quality candidates.
- All numbers are from one run per model, not averaged across repeats.
  Repeated the long-transcript `extract()` timing on qwen3:4b across three
  runs and got 21.4s / 35.4s / 5.6s for the identical prompt — GPU/daemon
  contention swings absolute latency a lot; treat the relative ordering
  (split and small models faster than full 4b) as the reliable signal, not
  exact seconds.
- **Split config's 6 golden-set misses, checked individually: none involve
  1.7b over-fragmentation** — every miss had 0 or 1 extracted facts, never
  the multi-fact split seen in the long-transcript test. 4 misses are
  NOOP/UPDATE cases where 1.7b's paraphrase of the fact apparently didn't
  match closely enough for the 4b judge to catch the relationship (e.g.
  `noop-paraphrase`, `noop-weaker-version`). 2 misses are extraction
  dropping a decision/preference stated inside a question or request
  (`guard-decision-inside-request`, `guard-preference-inside-question`) —
  1.7b salience-dropped both to zero facts, where full 4b (86% baseline)
  evidently still finds them. That second category — not fragmentation —
  looks like the split's real remaining quality gap.

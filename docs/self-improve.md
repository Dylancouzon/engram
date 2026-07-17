# Self-troubleshoot and self-improve — runbook

The loop: **dogfood a few days → run the report → confirm each lead in the
store → fix the highest-leverage one → add a regression test → ship → dogfood
again.** This file is the exact procedure. A fresh session can run it end to
end.

The data comes from the dev-only study log `~/.engram/activity.jsonl` (every
recall/capture event with its detail) plus the journal. Both are DEV-ONLY and
get removed before release, along with `tools/report.py`.

## 1. Test — is the code healthy?

Run before and after any change. All fake-model, no downloads except the
golden set.

```
uv run pytest tests/ -q            # full suite (currently 173 green)
uv run ruff check src tests tools  # lint
uv run python golden/harness.py -v # write-model accuracy — needs Ollama running
```

Live smoke test against the real store (read-only):

```
engram stats     # daemon: running · extraction: ollama (NOT "verbatim fallback")
engram log       # recent hook events; scan for capture-degraded
```

If `extraction: verbatim fallback`, the model is down — capture stores
nothing. Fix: `brew services start ollama` (launchd keeps it alive across
reboots; the daemon deliberately does NOT supervise Ollama).

## 2. Diagnose — what is the live store telling us?

```
uv run python tools/report.py            # whole log
uv run python tools/report.py --days 7   # just the recent window
```

Read each section as a **lead, not a verdict** — confirm in the store before
acting. Flags are suppressed below ~30 samples, so early runs show numbers
without alarms.

- **Capture health** — `capture-degraded` firing or a multi-day gap means the
  store stopped growing. Check Ollama (§1).
- **Recall latency** — p95 over 500ms on the hot path is felt. The first
  recall after a daemon restart is a cold embedder load (~4s); steady-state
  should be tens of ms. Only worry if p95 stays high with a real sample.
- **Recall hit-rate** — prompts that injected nothing. Very low means the
  gate or scope is too tight; near 100% is fine.
- **Recall usefulness** — a weak overlap proxy (under-counts; morphology).
  Watch the *trend* and the **most-injected-never-used** list: a memory
  surfaced repeatedly and never echoed is a demotion/forget candidate.
- **Entrenchment** — one memory dominating injections is the rich-get-richer
  smell. Recency now tracks `created_at`, not `last_accessed`, so this should
  stay flat; a spike means the fix regressed or a memory is genuinely central.
- **False-negative leads** — candidates that scored just under the gate. A
  recurring near-miss means lower `--min-score`, widen scope, or fix tags.

### Scope health (§3) — the one query the report punts on

The report can't see a memory's own scope from the log alone. To find
`default` memories only ever surfaced inside one project (mis-scoped):

```
engram list --scope default --all   # note the ids of default memories
```

Then, in the log, collect the recall `scope` filters each of those ids
appeared under. An id that only ever shows up under `[project:x, "default"]`
for a single `x` is a mis-scope lead. Re-scope it in `engram serve` (the web
app manages memories; there is no CLI re-scope verb — the daemon `edit`
method backs it), or leave it if it is a genuinely general fact.

## 3. Fix — prioritize by blast radius

Order: **data-loss / correctness > recall quality > scope hygiene > cleanup.**
A wrongful forget or supersede is unrecoverable; a duplicate or a missed
recall is not. Fail toward keeping data.

- Every non-trivial fix gets a **regression test** in `tests/`.
- A classifier miss (wrong op, wrong scope, wrong general/default) becomes a
  labeled case in `golden/cases.json` — that is how classifier quality gets
  measured against real failures instead of synthetic ones. Format: the input,
  the expected op/scope, and a one-line note on why.
- A tuning change (a gate, a weight, a half-life) is a one-liner — do it alone
  so the before/after is measurable in the next report against this one.

## 4. Ship to the live daemon (the trap)

`uv run engram` from the repo talks to the OLD daemon. The launchd daemon and
hooks run the `uv tool install` copy, so a code change is not live until:

```
uv tool install --force --reinstall .
launchctl kickstart -k gui/$(id -u)/tech.qdrant.engram
engram stats     # confirm daemon: running on the new binary
```

Never `git commit`/`push` without an explicit ask in the same turn.

## 5. Guardrails — what NOT to build

- **No Ollama supervision in the daemon.** launchd owns it (§1). Ollama is a
  shared service; engram starting/killing it breaks other tools.
- **No new unbounded telemetry** without a removal plan. The study log and
  report are DEV-ONLY; per-event rows in the `events` table grow forever.
- **Adversarial review at milestones** — a Codex pass on correctness-critical
  or auth-path changes (`codex:rescue`), briefed to find defects, not summarize.
- **Don't act on a single noisy signal.** The proxies are weak by design;
  cross-check a lead against the store, and prefer a trend over one report.

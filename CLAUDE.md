# engram — working notes for agents

Personal, local-first, long-term memory for AI assistants, built on **Qdrant
Edge** (in-process vector search) and exposed over **MCP**. Apache-2.0, in the
Qdrant org. Track B: an OSS product built for adoption, polished from v1.

This file is the handoff for a fresh session: the architecture, every
load-bearing decision and *why*, the milestone state, the invariants you must
not break, and the traps already discovered. Full original spec:
`INSTRUCTIONS.md`. Verified Edge 0.7.2 API facts: `docs/edge-api-notes.md`
(read before touching `backend/edge.py`). Ingestion-adapter contract:
`docs/ingestion.md`. **Self-troubleshoot + dogfood-improvement loop:
`docs/self-improve.md`** — the exact test/diagnose/fix/ship procedure; run
`uv run python tools/report.py` to turn `~/.engram/activity.jsonl` into a
health report before deciding what to improve.

## Status: M0–M3 complete

All four milestones built, reviewed (Codex + andrey-review adversarial passes,
all findings fixed), and validated with real models. **177 tests green**, ruff
clean. Golden set: **84% op accuracy, 100% recall accuracy** (misses are the
safe direction: mostly degrade-to-ADD, plus a couple NOOP→UPDATE — never a
wrongful supersede or a lost recall). ~4500 LOC source.

A reliability/dogfood pass hardened four things (all reviewed, tests added):
reads no longer grow the source of truth (`journal.reinforce` collapses to
one row per memory); a fabrication guard degrades ungrounded LLM output to
verbatim; the sync LWW clock counts only content ops (a local read/dedup can
no longer shadow a remote edit); `engram hook install` offers the daemon so
the proactive-recall path isn't slow by default.

An adoption/simplification pass (July 2026, three-agent audit + fixes)
changed the surface significantly — see "Adoption pass" in the decision log.
Highlights: fixed a data-loss bug in `forget <short-id>` under the daemon;
added the transparency surface (`list`/`log`/`dashboard`, disk usage +
pending reviews in `stats`); hook events now log through the protocol (they
were silently dropped in daemon mode — the dogfood metric depends on this);
cut per-method grants, the consolidation budget machinery, the `quantize`
knob, and `hook print-config`. Distribution name is now **qdrant-engram**
(PyPI `engram` is squatted and the name collides with several AI-memory
projects); the CLI/module stay `engram`. Packaging (uvx/plugin-marketplace/
.mcpb) is deliberately parked until Dylan has dogfooded.

A serve + durability pass added `engram serve` (an interactive local web app
over the store — manage, chat, and trigger consolidation, surfaced there as
**Dream**; the dashboard map was deduped into shared `webui.init_map`), real
disk-usage in `stats`, and dropped unused payload indexes. It also closed an
apply-failure durability gap: if a journaled write's Edge-apply raises after
the ack, `_apply_guard` sets `_flush_damaged` and `_mark_flushed` freezes the
high-water mark, so replay-on-open (or a rebuild) refills the gap instead of
skipping it forever.

A stress-test hardening pass (Sonnet bug-hunt across four subsystems + two
Codex adversarial passes) fixed eight issues in two batches. **Robustness:**
(1) redaction leaked a secret value's tail (or missed it entirely) when it
held `#`, `@`, a space, or non-ASCII — the `assigned-secret` value class is
now non-whitespace/non-quote, plus a `Bearer <token>` rule; (2) `hook capture`
stored raw transcript tails as memories in daemon mode when no model was
reachable (the old `isinstance(store, MemoryStore)` guard is always false for a
`Client` — now gates on `stats()["extraction"]`, symmetric across the daemon);
(3) a malformed relay point (non-string blob, ciphertext decrypting to a
non-dict, bad ts) crashed and aborted a whole `sync now` pull — now degrades to
skipped; (4) export/import collapsed every tombstone's shard to `private`,
breaking sync re-propagation after a migrate; (5) `noop`/`review_resolved`
rows were mistagged `private`. **Durability (Codex-reviewed twice):**
(6) accepting an ambiguous UPDATE review left the ADDed twin's verbatim text in
an exportable, un-forgettable journal row — now the merged target commits
first, then the twin is byte-purged (its `review` row goes with it, so no
`review_resolved` marker is written under the tombstoned twin id); (7) the
global flush high-water mark could advance past a deferred reinforcement only
one shard had flushed (`_dirty_shards` are now flushed before `_mark_flushed`
advances); (8) consolidation held the write lock across its summarization LLM
call, so daemon shutdown could block ~60s — consolidation is now two-phase
(gather under the lock, model calls lock-free, apply under the lock with a
strict re-validation that discards any summary whose source episodes were
changed or forgotten mid-run, so forgotten content can't reappear).

A refactor/logic-review pass (10 Sonnet subsystem reviewers, findings
consolidated + ponytail-filtered to "clean code + bugs a solo dogfooder
hits", multi-user/adversarial-relay/scale-over-years cases dropped) fixed 16
things across 17 files (net −104 LOC), 7 new tests. **Bugs:** shard-exists now
keys on `edge_config.json` not "any file" (a `.DS_Store` from opening
`~/.engram` in Finder crashed the first write); `seed` routed through
`_open_surface` (was library-only, hard-failed under the daemon); `serve`
`do_POST` returns JSON 400 on a malformed body instead of dropping the
connection; MCP `recall`/`forget` catch `DaemonUnavailable` (was a raw
traceback after a retry); `engram consolidate` client socket timeout raised to
600s (the "caller passes a larger value" comment described a caller that never
existed); `pending_reviews` also checks the twin's validity (soft-forgetting
the ADDed twin then accepting resurrected deleted content); extraction honors
all-below-salience-floor as `[]` instead of overwriting verbatim, and strips
`[REDACTED:…]` placeholders before the fabrication-grounding check; embedder
lazy-load is locked (was double-loading ~600MB under daemon threads);
`config.toml` numeric overrides coerce or fail loudly; the redaction entropy
gate loosened to ≥2-of-3 char classes (a single-case token skipped scoring
entirely). **Cleanups:** `_shard_order`/`_commit_payload`/`_replay` helpers in
store (the last batch-embeds on replay instead of one call per row);
`to_payload` → `asdict` minus id; `_parse_tags`/`_hook_payload` in the CLI;
the `isinstance(Client)` branches for sync/snapshot collapsed via a thin
`MemoryStore.sync` + `snapshot(Path|str)`; `_clamp01`/`_post` shared in the
write model; the dead `delete` journal op removed across journal/store/sync; a
`journal(op)` index so `review_rows()` (every dashboard/serve refresh) stops
full-scanning. **Flagged, not fixed** (needs Dylan): `shared:<group>` pools
reuse one sync key across all shards, so they aren't isolated between people —
irrelevant solo, a real design decision before inviting anyone.

A cross-cutting/seam pass (10 Sonnet reviewers scoped to whole-flow
correctness, concurrency, perf-under-age, and honesty gaps — deliberately NOT
the per-file work the prior pass covered; findings ponytail-filtered) fixed 16
things, 5 new tests, all green (**145 tests**). **Correctness/durability:**
`_purge_shard` was the one Edge-mutating path not wrapped in `_apply_guard` —
a rebuild that failed mid-way (ENOSPC right after the hard-forget VACUUM) left
the live daemon serving a partial shard and let the flush mark advance past
the gap; now guarded, and a leftover `.purging` dir is cleared before rename
(a second same-shard forget used to wedge on it). `apply_synced` now re-checks
`is_tombstoned` under the write lock (a hard-forget racing a sync-pull could
resurrect: the purge deletes the rows, so `last_ts_for` reads 0 and the LWW
guard alone waved it back in). `_recover_interrupted_purge` now re-VACUUMs the
journal — a crash between `hard_forget`'s DELETE-commit and its VACUUM left the
forgotten plaintext in free pages, and recovery rebuilt Edge but never
reclaimed them (byte-forget privacy gap). `consolidate()` drains buffered
reinforcement first, or the manual RPC path decay-prunes a memory recalled
seconds ago (stale `access_count=0`; the idle path masked it). **Concurrency:**
a dedicated `_backends_lock` guards the lazy check-then-create of
`self.backends` and the `_shard_order`/`stats` snapshots — reached under
`_write_lock` OR `_shard_guard.shared()` with no common lock, two threads
first-touching a new shard could double-open it or crash mid-sort; `stats()`
also now takes the shard guard (use-after-close vs a purge); daemon shutdown
joins the flusher before `store.close()` so a phase-3 consolidation can't
touch a closed journal. **Recall quality:** both hooks over-fetch, gate on RAW
similarity, then cap (filtering the already-truncated top-k let a fresh-but-
off-topic memory crowd out an on-topic one), and `session-start` gained the
noise gate it never had (`--min-score`, default 0.35 — the synthetic project
query scores lower than a real prompt). **Honesty:** the daemon flusher logs
failures to stderr instead of swallowing them, and a frozen flush mark
(`_flush_damaged`) surfaces in `stats()`; `config.toml` warns on an unknown
key; `seed` reports `refused: N`; serve's chat emits a "model stopped
responding" frame instead of an empty stream. **Perf:** `sync push` uses an
indexed `entries_after(seq, shard)` instead of deserializing the whole journal
each push; `stats()` memoizes the static ~600MB models-cache size (was an
rglob on every serve UI click). **Cleanups:** dead `_commit_payload(flush=)`
param removed; `review_to_wire` now round-trips `shard`; stale "consolidate
holds the write lock through summarization" comment corrected. **Left as-is
(Dylan's call):** `snapshot` still omits `sync.key`/`sync.json`, so restoring
onto a fresh device needs the key re-copied by hand.

A dogfood relevance pass (July 2026, driven by Dylan's real-use complaints,
tested against the live store) fixed the two biggest quality gaps. Measured
first: 27% of hook-injected memories were low-value or wrong-project (all
captures landed in scope `default`, recall never filtered), and 32% of the
store was near-duplicates (every Stop/PreCompact re-extracted the same
transcript tail with fresh wording; lukewarm judge verdicts degraded each to
ADD). Fixes (**159 tests**): hooks derive `project:<cwd-dirname-lowercased>`
— capture stores there, recall filters to `[project:x, "default"]`, explicit
`--scope` wins (no protocol change needed: `build_filter` already MatchAny's
a list scope); `hook capture` keeps a per-transcript high-water mark
(`capture-marks.json` in the data dir, hook-side, advances only after
`remember` succeeds) so only new transcript entries are ever extracted;
consolidation gained a near-dedup pass (cosine ≥ `noop_similarity` over the
vectors already in Edge, same shard+scope+type, union-find, oldest survives,
soft-invalidate only) and reports `examined`/`too_young` so the serve Dream
toast explains an all-zero run instead of looking broken. The live store was
rescoped (347 of 422 memories moved to `project:*` scopes via daemon `edit`;
one 42-memory drafter/post-scheduler group left in `default`, no matching
repo dir) and a live Dream run collapsed 125 near-dups (one Vercel-email fact
had 9 copies). Trap fixed in README: when the CLI is installed via `uv tool
install`, the launchd daemon and hooks run THAT copy, not the repo —
`git pull` alone upgrades nothing; `uv tool install --force --reinstall .`
then restart the daemon. Follow-up (Dylan-approved): extraction classifies
each fact `general: bool` — a durable fact about the user themselves routes
to scope `default` even when captured under a `project:*` scope (including
its conflict judging); unsure defaults to false and explicit non-project
scopes are never overridden, because wrongly-generalized follows the user
everywhere while wrongly-project-scoped is merely less visible.

A dogfood reliability + instrumentation pass (July 17, driven by a live
`~/.engram` health check) fixed write-time scope/quality bugs and made the
store self-diagnosing (**177 tests**). **Fixes:** hook recall never resolves
scope to `None` again (a missing `cwd` fell through to *unfiltered* recall
across every project — `_project_scope` falls back to `Path.cwd()`); the
general/default classifier gained a confidence gate (`general_confidence`,
reuses `judge_confidence` 0.8) so a low-confidence "general" fact stays
project-scoped; recall recency tracks `created_at`, not `last_accessed` —
killing a day-one rich-get-richer loop where being surfaced kept a memory
looking fresh; MCP `recall` restricts to `[project, "default"]` by default
(mirrors the hooks, matches `remember`), which exposed and fixed a daemon bug
where a *list* scope hit the string-only `_check_scope` and wrongly denied
non-`*` clients (now confined to the allowlist). **Dogfood instrumentation**
(all into the dev-only `activity.jsonl`): recall events record surfaced ids,
latency, best-rejected similarity, and the session scope (the §3 self-healing
attribution — logged now, the demote/flag check deferred until data
accumulates); `hook capture` logs a `recall-usefulness` proxy (does an
injected memory show up in a later assistant reply — weak, under-counts,
validated on 100 real transcripts) and a deduped `capture-degraded` event when
no extraction model is reachable (a stopped Ollama was silently dropping
captures for days). `tools/report.py` turns `activity.jsonl` into a compact
health report; `docs/self-improve.md` is the exact test→diagnose→fix→ship
runbook (pointed to from the top of this file). **Then a live bug the dogfood
surfaced:** the Stop hook ran extraction synchronously and froze Claude Code
~2min (Ollama `NUM_PARALLEL=1` serializes concurrent sessions' captures) —
capture now DETACHES (parent returns ~0.07s, child extracts in the
background). Ollama put under launchd (`brew services start ollama`) so it
survives reboots; the daemon deliberately does not supervise it.

A capture-load pass followed (the detached captures still pegged the GPU on
every substantive turn). Two levers, both shipped: (1) a per-transcript
**debounce** (`capture_debounce_s`, default 90s) so rapid turns batch into one
extraction instead of one 10-40s model run each — the parent gates cheaply and
only spawns when there is new content past the window; (2) **split models**,
after a Sonnet benchmark (`docs/model-benchmark.md`, 7 models × golden set):
extraction sends long transcript prompts (the latency cost) so it moved to
`qwen3:1.7b`, while the conflict judge (short fixed-size prompts) keeps
`qwen3:4b` to protect op accuracy. Benchmarked split = 79% op / 93% recall vs
86/97 for 4b-both, at ~2.6x faster extraction; the misses mostly fail toward
ADD (safe). Config: `extraction_model` + new `judge_model` (set both the same
to un-split); `store.judge_llm` is a second `LocalLLM`, and an injected llm
(tests) is used for both roles. **Known cost of the split:** 1.7b is weaker at
the extraction-time `general`/project scope call, so more project facts
misroute to `default` (undermining the §1.3 confidence gate) — the report's
scope-health signal is how to catch them.

A classification-alternatives evaluation (five parallel research passes —
GLiNER2, SetFit/embedding heads, constrained decoding + LoRA, embedding-only
heuristics, small encoder cross-encoders — followed by real experiments
against the live store and an expanded golden set) closed with a different
result than any of the five candidates: a real bug, not a model swap.
**Findings:** an embedding-only scope classifier (nearest-centroid and a
class-balanced logistic-regression head, both on the existing nomic-embed
vectors) lost badly to the current qwen3:1.7b baseline on the live store's
225 hand-labeled memories (13%/20% and 0%/0% general-class precision/recall
vs. the LLM's 32%/60%) — only 15 of 225 are scope `default`, too little data
in 768 dimensions, and "project" is 12 different sub-topics, not one
cluster; don't revisit without materially more `default`-scoped labels.
Extraction's JSON-envelope reliability was re-measured (77 inputs incl. 20
adversarial) at 0 parse failures against the current model — the multi-shape
parser in `extract.py` stays untouched, nothing to fix. Relevance-formula
tuning is blocked: `activity.jsonl` never logged candidate-level data
(ids/raw scores/rejected candidates) to replay offline, and the
`recall-usefulness` proxy (`cli.py::_recall_usefulness`) shows a flat 0
used / 1074 judged across the store's whole history — undetermined whether
that's the proxy's documented weakness or a real bug; needs a manual trace
against one real transcript before Experiment 4 is revisited.
**The actual fix:** growing the golden set (29 -> 53 cases, adding easy-ADD/
paraphrase-NOOP/adversarial-near-miss cases) surfaced that the judge model
(qwen3:4b) reliably returns a correct, confident NOOP verdict but omits
`target` (it doesn't think "this is a duplicate" needs to name which memory)
— and `resolve.py`'s `judge()` blanket-degraded any targetless non-ADD op to
ADD, silently turning recognized duplicates into fresh ADDs. Fixed: NOOP
with no target defaults to the sole candidate when there is exactly one
(unambiguous); the multi-candidate case still degrades to ADD (genuinely
ambiguous which memory NOOP'd); UPDATE/SUPERSEDE untouched (still require an
explicit target — a wrongful merge/supersede is the unrecoverable
direction, unlike a missed NOOP). Op accuracy on the 53-case golden set:
68% -> 85% (measured before the fix, after the golden-set expansion exposed
it) from this one change alone — bigger than anything the five researched
alternatives would have delivered. `golden/cases.json` now has 53 cases;
`golden/scope_eval.py` is the throwaway embedding-classifier probe (kept for
reference, not part of CI). Two remaining golden-set failures are a genuine,
minor judge weakness on hard adversarial near-misses (surface lexical
overlap across different attributes, e.g. "oat milk in his tea, not his
coffee" merges as UPDATE instead of ADD) — documented, not investigated
further; the confidence gate already bounds the damage.

Not built (deliberate cuts, not omissions): Wave-C/D ingestion adapters
(email/messages/voice/CLIP) — the adapter *contract* is documented in
`docs/ingestion.md` and the write path is proven, so these are per-source
adapters, not new architecture. Also skipped: streaming export, a systemd
unit (launchd only), and a Rust single-binary (a stated v1-adoption goal, not
started).

## Positioning (drives every product decision)

Compete on **ownership**, not algorithms. Do NOT try to out-engineer the
funded memory players (Mem0, Zep, Letta, cognee) on extraction quality —
concede that. engram competes with the **cloud built-ins** (ChatGPT/Claude/
Gemini memory) on ownership / privacy / portability — a lane their business
model can't follow into. Verified mid-2026: all three built-ins are
personal-only (no team/shared memory), capacity-capped, and export-hostile —
the wedge is real. Nearest OSS neighbors have moved, though: **Basic
Memory** is no longer "local but not capable" (it now has hybrid semantic
search, a knowledge graph, Obsidian sync) and **claude-mem** (~65k stars)
owns the Claude-Code-hooks lane with a 2-command plugin install. Do a fresh
feature diff before writing public comparisons. Target user: ownership/
privacy-motivated multi-tool power users, not the mass market. When a design
choice trades capability for ownership/portability/privacy, take ownership.

Qdrant is a **vector search engine**, never a "vector database", in all copy.
Public copy (README, docs) goes through the `qdrant-messaging` skill.

## The one architectural idea

**The SQLite journal is the source of truth; every Edge shard is a
rebuildable index over it.** This falls out of a hard Edge constraint
(verified against the engine, see `docs/edge-api-notes.md`): Edge does NOT
replay its WAL on reopen — `flush()` is the only durability point, so anything
written after the last flush is lost on reopen. So:

- Every write intent is appended to the journal (the **ack point**) *before*
  it is applied to Edge. A crash anywhere after the ack loses nothing: on open,
  journal rows past the flushed high-water mark replay into Edge, re-embedding
  from raw text (vectors are never journaled).
- `_applied_seq` tracks the highest journal seq actually applied to Edge this
  session. The high-water mark may advance ONLY to `_applied_seq`, never
  blindly to `last_seq` — else a write that crashed between journal-append and
  Edge-apply would be skipped by replay forever.
- One mechanism buys: crash-safety, atomic multi-step writes (supersede = 2
  rows in one txn), JSONL export (dump the log), restore/migrate (replay), and
  byte-level forget.

Everything else is a consequence of protecting that invariant.

## Decision log (what we chose and why)

**Write model (M0).**
- Pipeline order is the contract: `redact → extract → conflict-resolve →
  JOURNAL APPEND → embed + upsert → flush → advance high-water mark`.
- Stage-0 redaction (`redact.py`) runs before *anything* persists. Regex
  deny-list + Shannon-entropy detection; hex-only strings (git SHAs) exempt.
  Known secrets → redact-and-continue; private-key blocks → refuse the whole
  write. Secrets never land, even transiently. Tests grep raw db bytes.
- Conflict resolution: retrieve top-k similar (dense-only — cosine is a stable,
  interpretable gate, unlike fused scores), a local Qwen3 judge classifies
  ADD/UPDATE/SUPERSEDE/NOOP + confidence. **Auto-apply only high-confidence
  (≥0.8) UPDATE/SUPERSEDE; else degrade to ADD.** Rationale: a duplicate is
  recoverable, a wrongful supersede is not — always fail toward ADD.
- Deterministic verbatim-dedup short-circuits NOOP before the judge; a NOOP the
  judge is lukewarm on still stands if dense similarity ≥0.9 (two weak signals
  agreeing). These never apply to UPDATE/SUPERSEDE.
- Extraction + judge (`llm.py`, Ollama qwen3:4b) are an **enhancer, not a
  dependency**: with no model, `remember` stores verbatim and is ADD-only.
  Every code path must preserve this.
- `source_text` (the raw redacted input) is kept ONLY when one input yields
  one memory. Multi-fact inputs store no shared source_text — else
  hard-forgetting one fact leaves its content alive in a sibling's source_text.

**forget = gone (M0, hardened after Codex).**
- Codex + an empirical probe proved Edge `delete()+flush()` does NOT remove
  bytes — content survives in the WAL and payload pages, and `optimize()`
  doesn't reclaim it. So `forget --hard` cannot just delete the point.
- Real mechanism: (1) drop a `purge.pending` crash marker, (2) purge the
  journal (tombstone + DELETE + VACUUM), (3) rebuild the shard WITHOUT the
  memory by moving survivors to a fresh shard verbatim (scroll→upsert, vectors
  preserved, no re-embed). A crash anywhere → rebuild-from-journal on next open.
  Tests grep the whole data dir for the forgotten bytes.
- `soft` forget just sets `valid_to=now` (history preserved, filtered out).

**Daemon + protocol (M1).**
- Edge is single-writer (WAL takes an exclusive flock) with no background
  threads. So one long-lived daemon owns the shard(s) and is the sole writer;
  every surface (CLI, MCP, importers) is a thin client over a **versioned
  local API** (`protocol.py`, line-delimited JSON over a 0600 Unix socket).
  That API — not MCP — is the durable "works with everything" contract.
- M0 runs library-mode (CLI opens the store directly, sole writer via an
  exclusive lockfile). The daemon takes that seat in M1; the CLI is daemon-first
  with library-mode fallback so zero-setup still works.
- Auth: socket is 0600 (OS excludes other users). Client-name registration +
  per-client scope allowlists, **default-deny** except an implicit `cli`
  client. Capability tokens (hashed in clients.json, shown once) added in
  M3. Client names are self-declared — this is consent bookkeeping, NOT a
  security boundary against a hostile same-user process (documented; real
  defense is the token). Per-method grants existed briefly and were cut in
  the adoption pass: nothing wired them up, nobody asked, scopes + tokens
  cover the realistic cases.
- Reads are lock-free; writes serialize through `store._write_lock`.
  Reinforcement (access bumps) is **buffered** in the daemon and drained on
  idle/close, so reads never write; and each bump **collapses** to one journal
  row per memory (`journal.reinforce`), so read volume never grows the source
  of truth — load-bearing for a store meant to last years. `_ShardGuard` is a
  readers-writer lock on the shard *lifetime* (not its data): recall takes
  shared, anything that closes/replaces a shard object (purge, rebuild, close)
  takes exclusive.

**Proactive recall — the adoption bet (M1, expanded after research).**
- MCP is pull-only; the field-wide finding (see research below) is that
  anything gated on the model deciding to call a memory tool is unreliable.
  engram's floor is deterministic via Claude Code hooks (`engram hook install
  claude-code`): `UserPromptSubmit` recalls against the prompt and injects
  confident hits BEFORE the model generates; `Stop`+`PreCompact` capture the
  transcript tail through the full write pipeline; `SessionStart` injects
  project context. MCP tools remain the model-initiated supplement.
- For hookless surfaces (Cursor, Windsurf): `engram rules <surface>` emits a
  paste-in rules block — the weaker tier, honestly labeled.
- Injection gates on RAW similarity (absolute scale), not the rescored score —
  a bug fixed this session (see traps).

**Retrieval (M1/M2).**
- Hybrid dense (nomic-embed-text-v1.5, 768d) + sparse (miniCOIL, IDF) via
  FastEmbed, DBSF fusion over Prefetch branches, payload pre-filters
  (scope/type/tags/temporal-validity) applied *inside* each branch (true
  pre-filter). App-side rescore blends similarity × importance × recency-decay.
- MMR diversification is ON by default (`mmr_lambda=0.7`) and composes with the
  hybrid prefetches (probed working).
- **FormulaQuery decay is impossible on Edge 0.7.2** — probed: `Variable
  ("score")` never binds the real fused/prefetch score (flat or nested), only
  payload fields or defaults. So recency/importance rescoring is app-side by
  design, not by omission. Do not try to move it server-side.

**Trust-boundary shards (M3 foundation, built in the M1→M2 seam).**
- Shards are the sync/privacy boundary: `private` (never syncs, default),
  `me-synced` (your devices), `shared:<group>` (opt-in pools). Names are a
  closed grammar (`store.validate_shard`), not free text.
- Opt-in is **structural**: `private` has no sync code path at all. Promotion
  out of private is a deliberate act.
- Recall fans out per shard (Edge fuses only within a shard), dedups by id with
  **private winning ties**, merges. Scores are compared RAW across shards
  (same embedder → comparable); an earlier per-shard min-max normalization was
  removed because it let a weak shard's best hit outrank a strong shard's real
  match.
- Conflicts never cross shards: a private fact is judged only against private
  candidates.

**Consolidation (M2).** Idle-only daemon job (or `engram consolidate`),
cancellable/checkpointed, all through journaled writes: decay-prune
stale never-recalled episodes (60d half-life, slower than recall ranking),
normalized-text dedup (keep oldest), episodic→semantic LLM summarization (≥3
old episodes sharing scope+tag). Protects memories with a pending review.
Only a COMPLETED run advances the daily checkpoint. The per-run `budget`
accounting was cut in the adoption pass (a scale problem personal stores
don't have); the `stop` event stays — daemon shutdown must not block on a
model call.

**Snapshot/restore (M2).** `archive.py`: quiesced tar.gz of the durable state,
scrypt→Fernet passphrase encryption by default (magic `ENGRAM1` + salt header).
Snapshot checkpoints the SQLite WAL first (else the tar misses recent writes).
Restore refuses a non-empty dir. (An int8 quantization knob existed and was
cut in the adoption pass — premature at personal-memory scale.)

**Adoption pass (July 2026).** Three sub-agent audits (over-engineering,
competitive landscape, adoption funnel) → one fix batch, Dylan-approved:
- **forget-by-short-id bug (data loss class):** `_resolve_target` fell
  through to semantic search for hex prefixes in daemon mode; `--yes` could
  hard-purge the wrong memory. Now: hex-like targets resolve by exact id or
  unambiguous prefix (`store.find_by_prefix`, prefix-aware daemon `get`) and
  NEVER fall through to search. Test coverage both modes.
- **Transparency surface** (the ownership pitch, made visible): `engram
  list` (browse, `--all`), `engram log` (hook audit trail), `engram
  dashboard` (static self-contained HTML written into the 0700 data dir —
  deliberately not a served app: no port, no new attack surface), `stats`
  grew disk usage + pending_reviews. New protocol methods: `list`,
  `log_event`, `events`.
- **Hook events now go through the store surface** (`store.log_event`,
  client passthrough): they were journal-direct and silently dropped in
  daemon mode, which would have starved the events-table dogfood metric.
- **Review queue surfaced** instead of cut: session-start hook injects a
  "N conflicts await `engram review`" line so the assistant relays it.
- **First-run UX:** embed.py announces the ~600 MB model download on stderr
  (stderr because hook stdout is injected into model context); `hook
  install` warns when Ollama is missing (capture would silently no-op).
- **Naming:** distribution = `qdrant-engram` (PyPI `engram` is squatted;
  several unrelated "Engram" AI-memory projects exist, incl. engram.fyi).
  CLI, module, and `~/.engram` stay `engram`.
- **Cuts:** per-method grants, consolidation budget, `quantize`, `hook
  print-config`. LICENSE file added (GitHub license detection needs the
  file, not just pyproject metadata). README: real clone URL
  (Dylancouzon/engram until the org transfer), 3.12-pin explanation,
  uninstall section, See What It Knows section, cloud built-ins wedge.

**Sync (M3).** `sync.py`: push chosen shards to a standard Qdrant Cloud
collection used as a **dumb ciphertext relay** (1-dim dummy vector). Each
memory travels as a Fernet blob; the encryption key (`sync.key`) is generated
locally and NEVER uploaded (copy it between devices by hand). Merge is OURS:
push per-shard past a high-water mark, pull = scroll union, LWW by journal
timestamp, tombstone anywhere suppresses everywhere. Pulled rows journal as
`sync-pull` (never re-pushed → no cross-device ping-pong) and re-embed locally.
Post-Codex hardening: id+ts+shard are bound INSIDE the ciphertext (a relay
can't swap blobs between ids or replay old ts); tombstones carry an HMAC (a
relay can't forge deletions); unknown-id tombstones are recorded (no
resurrection); LWW re-checks atomically under the write lock in `apply_synced`.

## Data model (`models.py`)

Memory payload: `id` (owner-namespaced UUID so LWW never collides across
devices), `text`, `type` (semantic|episodic|procedural), `scope` (payload
multitenancy: work|personal|project:x), `tags`, `surface`, `source_text`/
`source_ref`, `created_at`/`event_time`, `valid_from`/`valid_to`/
`superseded_by` (temporal validity — invalidate, don't delete; `VALID_FOREVER`
sentinel = year 9999 so validity is a plain range filter), `importance`,
`access_count`/`last_accessed`, `embedding_model`. Named vectors: `dense`
(nomic) + `sparse` (miniCOIL).

## Layout

`src/engram/`: `store.py` (MemoryStore — all pipelines, multi-shard routing,
locks, review queue, sync/consolidate/snapshot entry points) · `journal.py`
(source of truth; `journal`/`tombstones`/`meta`/`events` tables, shard column,
WAL-mode, internal RLock) · `backend/edge.py` (per-shard EdgeBackend) ·
`embed.py` (FastEmbed, lazy-load) · `extract.py`/`resolve.py`/`llm.py` (local
model) · `redact.py` (stage-0) · `protocol.py` (local API v1, wire forms) ·
`daemon.py` (ThreadingUnixStreamServer, ClientRegistry, idle consolidation,
drain-on-shutdown) · `client.py` (thin client, daemon auto-spawn) ·
`mcp_server.py` (FastMCP stdio) · `consolidate.py` · `archive.py` · `sync.py`
· `cli.py` (Click; daemon-first, library fallback) · `models.py`/`config.py`.

Tests inject `FakeEmbedder`/`FakeLLM` (`tests/conftest.py`) so CI needs no
model downloads; sync tests use `QdrantClient(":memory:")` as the relay.
`golden/` grades the write model against real models.

## Commands

- `uv run pytest tests/ -q` — full suite (fake models, no downloads)
- `uv run python golden/harness.py -v` — write-model accuracy (real models;
  needs Ollama for non-ADD ops)
- `uv run ruff check src tests`
- `uv run engram --help` — CLI. Key verbs: `remember/recall/forget`,
  `list/log/dashboard` (transparency surface), `serve` (interactive local web
  app), `review`, `seed <files|dirs>`,
  `export/import`, `snapshot/restore`, `sync setup/now`, `consolidate`,
  `daemon [--install]`, `clients allow/revoke/list`, `hook
  install|session-start|user-prompt|capture`, `rules <surface>`, `mcp
  --client`, `stats/rebuild`.
- `ENGRAM_HOME` overrides `~/.engram`. `ENGRAM_SOCKET` overrides the socket
  path. `ENGRAM_TOKEN` supplies a client capability token.

## Environment notes

- Ollama runs under launchd (`brew services start ollama`, survives reboots).
  Pull BOTH `qwen3:1.7b` (extraction default) and `qwen3:4b` (judge +
  summarize). Extraction unreachable → verbatim/ADD-only; judge model
  unreachable → judge degrades to ADD (both by design). No docker: engram is
  local-first (Qdrant Edge in-process), the daemon is the only process.
- Embedding models (~600 MB) cache in `~/.cache/engram/models` (NOT in the
  data dir — the memory folder stays small and portable).
- Reference: `~/Documents/GitHub/edge-mission-control` has the qdrant_edge
  0.7.2 venv and real usage patterns.
- `rtk` (Rust Token Killer) wraps shell commands; `rtk proxy <cmd>` runs raw.
  pytest under the plain wrapper collected 0 tests once — use `rtk proxy uv run
  pytest`.

## Traps already hit (don't re-discover these)

- **Edge `retrieve()` needs `with_vector` positionally** despite the stub
  marking it optional. Edge flushes on GC-drop and panics if the shard dir was
  already deleted — always `close()` before removing a temp dir.
- **AF_UNIX path cap** ~104 bytes on macOS; deep `ENGRAM_HOME` (pytest tmp
  dirs) overflows it — hence `ENGRAM_SOCKET`/`socket_override`.
- **Non-UUID point ids** raise in Edge — ids are always UUID strings.
- **Cross-shard score normalization** (min-max per shard) silently wrecks
  ranking — removed; raw scores are comparable under one embedder.
- **Injection thresholds must gate on raw similarity**, not the
  recency/importance-rescored score, or a stale-but-relevant memory slips
  past the noise gate. `RecallHit.similarity` carries the raw value.
- **Small models are loose about JSON envelopes** — `extract.py` accepts
  `{"memories":[...]}`, a bare list, or a single bare object before falling
  back to verbatim.
- **Small models fabricate on contentless input** — "test fact" once yielded
  an invented "I decided to learn Python". `extract.py` guards this: if the
  WHOLE extraction shares no content token with the input, degrade to verbatim.
  Grounding is checked across the whole extraction, NEVER per-fact (a per-fact
  drop would bury a legit rephrased fact in a sibling's `source_text`).
- **The sync LWW clock (`journal.last_ts_for`) must count only content ops**
  (`upsert`/`delete`/`sync-pull`), never `reinforce`/`noop`/`review`. Those
  audit/read rows carry fresh timestamps but change nothing; counting them let
  a local recall or dedup shadow a genuine older-but-real remote edit and drop
  it on pull. Found by andrey-review + Codex.
- **Reinforcement must not grow the journal** — `journal.reinforce` collapses
  to one row per memory (delete + reinsert at a fresh seq). Relies on `seq`
  being AUTOINCREMENT (no reuse) so the replacement stays above what it
  replaced; don't drop AUTOINCREMENT. `stats` reports `row_count`, not
  `last_seq` (which keeps climbing past deleted rows).
- **The live daemon/hooks may not run repo code.** Dylan's install is `uv
  tool install` + launchd: `~/.local/bin/engram` is a frozen copy, launchd
  respawns it after a pkill, and `ps` shows which binary is live. Verify a
  change against the real store only after `uv tool install --force
  --reinstall .` + daemon restart — `uv run engram` from the repo talks to
  the OLD daemon otherwise.
- **Test secret fixtures** must be runtime-assembled from fragments, or
  GitGuardian flags them (it did, on commit ff5790d — those are false
  positives; assembly since 71fd366).
- **The `assigned-secret` value class must stay broad** (non-whitespace,
  non-quote). A narrow character class leaks a secret's tail after the first
  `#`/`@`/space/non-ASCII char — or fails to match at all, storing the whole
  value in cleartext. The keyword (`password:`/`token=`/`Bearer`) is the
  signal; capture the value greedily to whitespace.
- **Never gate on `isinstance(store, MemoryStore)` to detect library vs daemon
  mode** — a daemon `Client` is not a `MemoryStore`, so the branch silently
  flips. To read the daemon's model/config state from either surface, call
  `store.stats()` (the `Client` proxies it through the daemon).
- **The flush high-water mark is global; `flush()` is per-shard.** Any write
  that mutates a shard without flushing it (only deferred reinforcement, today)
  must register the shard in `_dirty_shards` so `_mark_flushed` flushes it
  before advancing the mark — else a later write on another shard strands the
  bump past the mark and replay skips it. Discard a shard from `_dirty_shards`
  only after its flush succeeds; a failed flush freezes the mark.
- **Accepting an UPDATE review byte-purges the twin** (commit the merged
  target first, then `_forget_locked(twin, "hard")`). Do NOT append a
  `review_resolved` row for the UPDATE case — the `review` row is keyed under
  the twin id and is purged with it, so writing one would re-add a journal row
  under a now-tombstoned id.
- **Consolidation must not hold `_write_lock` across a model call** — a daemon
  shutdown waits on that lock and would block a full Ollama timeout. It runs in
  three phases (gather under the lock, model calls lock-free, apply under the
  lock). On apply, re-validate each source episode against its phase-1 text: if
  any was forgotten, invalidated, edited, or newly protected mid-run, discard
  the whole summary — a summary built from now-forgotten text would otherwise
  re-journal that content after `forget` returned.
- **`hook capture` must stay DETACHED.** Extraction runs a local model that
  Ollama serializes (`NUM_PARALLEL=1`); inline in the Stop hook it froze the
  interactive session for minutes when sessions overlapped. The hook re-spawns
  itself in a new session with `ENGRAM_CAPTURE_BG=1`, pipes the payload to the
  child's stdin, and returns immediately. Do NOT collapse it back to a
  synchronous call. Capture-marks advance in the child (after the write), so a
  child that dies just re-picks the tail next Stop — no optimistic-mark loss.
- **The Stop hook's cost is invisible to the user but real.** The slow work —
  extraction, a serialized Ollama call — MUST run in the detached child, never
  inline. The parent runs one bounded gate before spawning: a `_transcript_tail`
  parse (tens of ms typically; ~0.36s worst-case on a 66 MB transcript) plus a
  cheap debounce read, so a turn-end with no new content costs a parse instead
  of a wasted child spawn (per-Stop subprocess churn is worse than the parse for
  the common case). Keep the gate cheap and bounded; anything model-touching or
  unbounded (the `recall-usefulness` full parse, etc.) stays in the child.

## Working rules for this repo

- Adversarial review is default at milestones: run a Codex pass
  (`codex:codex-rescue` subagent or `/codex:*`), brief it with goal + change +
  "find defects, not a summary", triage findings with the user. Three passes
  so far each caught a real load-bearing bug (byte-purge gap, scope leak, sync
  auth). The wrapper subagent often goes idle after backgrounding Codex —
  fetch results from the main thread via the companion `status`/`result`.
- Sub-agents: fine and preferred for research/probes (keep raw material out of
  context); avoid for codebase edits (do those in main context).
- Git: never commit/push without explicit ask in the same turn (this session
  had standing commit-freely authority — do NOT assume that carries to a fresh
  session). Commit messages subject-only, imperative, 5–10 words, keep the
  `Co-Authored-By` footer. **Local `main` has never been pushed** — 17 commits
  ahead of origin as of this handoff.
- Repo currently lives at `Dylancouzon/engram`; spec says Qdrant org —
  transfer before public is cheaper than after.

## What a fresh session might do next

Dogfood-driven, need-order not spec-order: measure recall-at-the-right-moment
from the `events` table under real use; tune the golden set with Dylan's real
correction cases (drafts in `golden/cases.json`, he reviews); Wave-C/D
ingestion adapters when a source is actually wanted; Rust single-binary for
the v1-adoption install story; the public-release gate (one-command install,
managed models, in-place upgrade, clean uninstall).

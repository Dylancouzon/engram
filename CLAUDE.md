# engram — working notes for agents

Personal, local-first, long-term memory for AI assistants, built on **Qdrant
Edge** (in-process vector search) and exposed over **MCP**. Apache-2.0, in the
Qdrant org. Track B: an OSS product built for adoption, polished from v1.

This file is the handoff for a fresh session: the architecture, every
load-bearing decision and *why*, the milestone state, the invariants you must
not break, and the traps already discovered. Full original spec:
`INSTRUCTIONS.md`. Verified Edge 0.7.2 API facts: `docs/edge-api-notes.md`
(read before touching `backend/edge.py`). Ingestion-adapter contract:
`docs/ingestion.md`.

## Status: M0–M3 complete

All four milestones built, reviewed (Codex + andrey-review adversarial passes,
all findings fixed), and validated with real models. **102 tests green**, ruff
clean. Golden set: **84% op accuracy, 100% recall accuracy** (misses are the
safe direction: mostly degrade-to-ADD, plus a couple NOOP→UPDATE — never a
wrongful supersede or a lost recall). ~4500 LOC source.

A reliability/dogfood pass hardened four things (all reviewed, tests added):
reads no longer grow the source of truth (`journal.reinforce` collapses to
one row per memory); a fabrication guard degrades ungrounded LLM output to
verbatim; the sync LWW clock counts only content ops (a local read/dedup can
no longer shadow a remote edit); `engram hook install` offers the daemon so
the proactive-recall path isn't slow by default.

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
model can't follow into — and with Basic Memory (local but not capable) /
the niche OpenMemory is vacating. Target user: ownership/privacy-motivated
multi-tool power users, not the mass market. When a design choice trades
capability for ownership/portability/privacy, take ownership.

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
  client. Capability tokens (hashed in clients.json, shown once) + per-method
  grants added in M3. Client names are self-declared — this is consent
  bookkeeping, NOT a security boundary against a hostile same-user process
  (documented; real defense is the token).
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
bounded/cancellable/checkpointed, all through journaled writes: decay-prune
stale never-recalled episodes (60d half-life, slower than recall ranking),
normalized-text dedup (keep oldest), episodic→semantic LLM summarization (≥3
old episodes sharing scope+tag). Protects memories with a pending review.
Only a COMPLETED run advances the daily checkpoint.

**Snapshot/restore (M2).** `archive.py`: quiesced tar.gz of the durable state,
scrypt→Fernet passphrase encryption by default (magic `ENGRAM1` + salt header).
Snapshot checkpoints the SQLite WAL first (else the tar misses recent writes).
Restore refuses a non-empty dir. Quantization: optional int8 scalar
(`config.quantize`, ~4× smaller index, `engram rebuild` after changing).

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
- `uv run engram --help` — CLI. Key verbs: `remember/recall/forget`, `review`,
  `seed <files|dirs>`, `export/import`, `snapshot/restore`, `sync setup/now`,
  `consolidate`, `daemon [--install]`, `clients allow/revoke/list`, `hook
  install|session-start|user-prompt|capture`, `rules <surface>`, `mcp
  --client`, `stats/rebuild`.
- `ENGRAM_HOME` overrides `~/.engram`. `ENGRAM_SOCKET` overrides the socket
  path. `ENGRAM_TOKEN` supplies a client capability token.

## Environment notes

- Ollama installed via brew this session; `ollama serve` must be running and
  `qwen3:4b` pulled for the judge/extraction/summarize paths. Without it
  everything degrades to verbatim/ADD-only (by design).
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
- **Test secret fixtures** must be runtime-assembled from fragments, or
  GitGuardian flags them (it did, on commit ff5790d — those are false
  positives; assembly since 71fd366).

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

# engram — Build Spec

> **engram**: a personal, local-first, long-term memory for AI assistants, built on **Qdrant Edge** and
> exposed over **MCP**. Repo lives in the Qdrant org (Apache-2.0). **Track B:** an OSS product built for
> broad adoption, polished from v1. Decisions below are final (hardened by three adversarial reviews + Edge
> repo/team verification); build against them.

**Stack at a glance:** Qdrant Edge (in-process) · dense `nomic-embed-text-v1.5` + sparse **miniCOIL**
(`Qdrant/minicoil-v1`, IDF), both via FastEmbed, local · local extraction LLM (small Qwen3-class via Ollama; an *enhancer* — verbatim fallback if absent) · MCP over a
single-writer daemon · Python + `uv` (Rust single-binary a v1-adoption goal) · Apache-2.0 · fully offline.

---

## 1. What we're building

One general-purpose personal memory — **a folder you own** — that any assistant can read and write:
corrections, preferences, decisions, the dead ends you only had to hit once. It **plugs into everything**
(Claude Code, Desktop, Cursor, any MCP client, plus non-agent sources), runs **fully offline**, and travels
with you. Not the webcam/object demo (`edge-mission-control`) — this is the assistant memory.

**Verbs:** `write` / `retrieve` / `forget`. **Mechanism:** retrieval (surface the right memory at the right
moment, decayed by recency/frequency, filtered by topic) — not prompt-stuffing, not a markdown folder.
General from day one (work + life); only *ingestion sources* phase in.

---

## 2. Positioning (our focus + how to present it)

**Core message:** *Your memory shouldn't be rented from one vendor's cloud.* engram is the capable AI memory
that's **yours** — local, portable, private-by-default, plugs into every assistant.

**Compete on ownership, not algorithms.** Do **not** try to out-engineer the funded memory players (Mem0,
Zep, Letta, cognee) on extraction quality or memory-graph sophistication — concede those. They're
cloud/infra layers for app builders (different buyer; some run on Qdrant). We compete with the **cloud
built-ins** (ChatGPT/Claude/Gemini memory) on ownership/privacy/portability — a lane their business model
can't follow us into — and with **Basic Memory** (local but not capable) / the local-cross-client niche
**OpenMemory** is vacating.

**Our edge (true; say these):** (1) ownership as a portable artifact — no vendor can read/throttle/delete it;
(2) one memory across every surface via MCP; (3) server-class retrieval on-device (hybrid dense+miniCOIL,
recency decay, filterable pre-filter, quantization, in-process); (4) private-by-default trust boundaries;
(5) built to outlive any app or model (raw-text + JSONL export, engine-agnostic rebuild).

**Target audience:** ownership/privacy-motivated multi-tool power users — not the mass market the zero-setup
cloud built-ins own. **Adoption levers to lead with:** the import "aha" (reclaim your existing cloud memory),
the portability trick (correct once → sticks; copy folder → your mind moves), cross-app payoff, one-command
install, transparency (Apache-2.0, plain-file data).

**Brand/voice:** Qdrant is a **vector search engine**, never a "vector database." All public copy (README,
landing, blog, social) must run through the **qdrant-messaging** skill.

---

## 3. Architecture

**Process model.** Edge is in-process and **single-writer** (its WAL takes an exclusive `flock`), with **no
background threads**. So: **one long-lived local daemon owns the shard(s) and is the sole writer** — it runs
the write pipeline, retrieval (fan-out + merge across shards), the flush/optimize lifecycle, and
consolidation. Every surface (MCP server, CLI, importers) is a **thin client of the daemon** over local IPC;
none opens the shard directly. **In M0 this runs library-mode** (the CLI calls the core directly, sole writer
via our lockfile); the daemon + Unix-socket + auth land in **M1** when multiple clients connect — but the
core-lib/daemon-API boundary is drawn day one so the promotion is clean.

**Pipeline.**
```
WRITE:  redact(stage-0) → extract(local LLM, atomic + keep raw) → salience gate → type
        → conflict-resolve → embed(dense + miniCOIL) → journal-append → upsert(Edge) → flush(checkpoint)
READ:   pre-filter(topic/type/scope/validity) → per-shard hybrid(dense+miniCOIL, DBSF) + FormulaQuery
        score + MMR  → daemon merges across shards → top-k w/ provenance → (batched) reinforce
CONSOLIDATE (daemon, idle-only): dedup · merge · resolve conflicts · episodic→semantic summarize · decay-prune
```

**Write-path integrity (build this before the Edge backend):**
- **Redaction is stage 0** — a deterministic local scrubber (regex deny-list for common secret patterns +
  high-entropy detection) runs before extraction, embedding, `source_text` persistence, and any sync; on a hit
  it redacts the span and continues (drop/refuse only for high-confidence secrets like private keys),
  configurable. Secrets never land, even transiently.
- **The journal is a local SQLite log and the source of truth; Edge is a rebuildable index.** Edge does
  **not** replay its WAL on reopen — `flush()` is the only commit point — so every write intent (atomic text +
  payload + idempotency key) is written to SQLite *before* it's applied to Edge; we `flush()` Edge on a short
  cadence/idle and record the flushed high-water mark; on restart we replay journal rows past it. One mechanism
  gives crash-safety, atomic multi-step writes, the **JSONL export** (dump the log), **restore** (replay), and
  **migration** (re-embed from the log). `forget(hard)` scrubs via `DELETE` + `VACUUM` — no compaction machinery.
- **Forget = gone.** `forget(hard)` purges the point, `source_text`, and audit-entry content, **scrubs the
  intent from the journal**, and **expires old snapshots**, leaving a **content-free tombstone** that
  suppresses the id and propagates to synced copies. (Backups you already exported off-device are documented
  as out of scope.) Corrections use `soft-invalidate`.
- **Reinforcement never blocks reads** — access-count/last-seen bumps are buffered, coalesced, idle-flushed;
  skipped for speculative recalls.
- **Consolidation is a well-behaved writer** — idle-only, cancellable, checkpointed, bounded, yields to
  foreground.

**Conflict resolution (the M0 core — what the exit test grades).** On write: retrieve top-k similar existing
memories (hybrid, scoped); a Qwen3-class judge classifies the new fact vs each candidate → **ADD / UPDATE /
SUPERSEDE / NOOP** with a confidence. **Auto-apply only high-confidence UPDATE/SUPERSEDE; low confidence → ADD**
(M0 has no review queue — that arrives in M1). Thresholds are tuned against the golden set (§10). Extraction and
this judge share the local model; if Ollama is absent, `remember` stores verbatim and is ADD-only — extraction
is an enhancer, not a dependency.

**Cross-shard merge rules** (recall fans out per shard; Edge fuses only *within* a shard): normalize
per-shard scores before merging, dedup by our stable id, **`private` wins ties over synced**, and a
**tombstone in any shard suppresses the id everywhere**.

**Layers (keeps it swappable):** a **core library** with a `MemoryStore` interface + pipeline logic, backed by
`EdgeBackend`; **adapters** (MCP server, CLI, importers) are thin clients of the daemon; **config** (models,
scopes, decay params, flush/optimize cadence, sync). The daemon exposes a small **versioned local API** —
*that* API, not MCP, is the durable "works-with-everything" contract.

---

## 4. Data model

**Memory point (payload):**

| Field | Purpose |
|---|---|
| `id` | stable, **globally-unique, owner-namespaced** UUID (so LWW-by-id never collides across devices/people) |
| `text` | the atomic, self-contained memory |
| `entity`/`attribute`/`value`/`confidence` *(M1, deferred)* | optional lightweight structure for assertion-type memories. **M0 uses free-text + similarity + the judge**; add these only if M1 accuracy measurement demands it. Not a triple-store; episodic skips them |
| `source_text`/`source_ref` | raw excerpt + provenance — kept for re-embedding and audit |
| `type` | `semantic` \| `episodic` \| `procedural` |
| `scope` | `work` \| `personal` \| `project:<x>` … (payload multitenancy) |
| `topics`/`tags[]` | topical metadata for pre-filtering |
| `surface` | which app wrote it |
| `created_at`, `event_time` | when learned vs when it happened |
| `valid_from`, `valid_to`, `superseded_by` | temporal validity (invalidate, don't delete) |
| `importance` | salience 0–1 |
| `access_count`, `last_accessed` | reinforcement signals |
| `embedding_model` | so a migration knows what to re-embed |
| vectors | named: `dense` (nomic) + `sparse` (miniCOIL) |

**Types drive behavior:** semantic = deduped/superseded, decays slowly; episodic = timestamped, decays
faster, summarized into semantic during consolidation; procedural = sticky, high-importance.

**Temporal semantics:** default recall returns only *currently-valid* memories (`valid_to` unset/future); one
explicit **valid-time `as_of`** recalls what was true at a past time; timestamps stored UTC; on overlap the
higher-confidence/more-recent `valid_from` wins, the other soft-invalidated. (Both time axes are stored;
transaction-time travel is derivable later, not a v1 query.)

**Store layout — trust-boundary shards** (the sync/privacy boundary), payload scopes within each:
- **`private`** — never syncs. Local-only. **Default write target.**
- **`me-synced`** — your own memories across your devices.
- **`shared:<group>`** — opt-in pooled memories (family/team).

`me-synced` and `shared` use the **same sync mechanism** (§6) — normal upserts + `scroll`-union pull + our
merge to a Cloud collection — differing only in who can access that collection. Promotion out of `private` is
a deliberate, consent-gated action, so **opt-in is structural** and secrets *physically* can't sit in a synced
shard. Snapshot copy is backup/migration, **not** live continuity.

---

## 5. Retrieval & how we use Qdrant (all in-process on Edge)

| Need | Qdrant/Edge feature | Use |
|---|---|---|
| Pre-filter by topic/type/scope/validity | Filterable HNSW + payload indexes | filters applied *during* traversal (true pre-filter) |
| Recency/frequency decay | **FormulaQuery** (`Expression.Decay`, gauss/exp/lin) | blend similarity × importance + time-decay(last_seen) + log(access_count). **Apply per prefetch branch (or via DBSF), not on the RRF-fused score**; generous prefetch limits |
| Lexical + semantic | Hybrid dense + **miniCOIL** sparse, RRF/DBSF over `Prefetch` | miniCOIL via FastEmbed (`Qdrant/minicoil-v1`, IDF) upserted as a sparse vector (Edge's bundled encoder is BM25; it stores any sparse vector) |
| Non-redundant results | MMR | avoid near-duplicate hits |
| Migration | named/multi vectors + retained raw text | change model = **rebuild into a new store from raw text** (no in-place vector add); version-pin models |
| Footprint | Quantization (scalar/binary/PQ/TurboQuant) | shrink the index for small devices |
| Portability | Snapshots / partial snapshots | backup/move via snapshot API or a quiesced `flush()`+`close()` copy — never a live `cp -r` of an open shard |
| Multi-device / family | opt-in Cloud sync (§6) | pool chosen shards, opt-in |
| Later: "index your day" | multivector + multimodal (CLIP) | images/audio become retrievable |

---

## 6. Interface, auth & sync

**MCP server + CLI**, both thin clients of the daemon. MCP is the neutral cross-surface waist; keep the tool
surface small:
- `remember(text, type?, tags?, scope?, importance?)` — write (extraction + conflict-resolution behind it)
- `recall(query, scope?, filters?, k?, as_of?)` — hybrid + scored retrieval
- `forget(query|id, mode=soft|hard)` — `soft` invalidates; `hard` purges + tombstones (§3)
- `reinforce(id)` / `update(id, …)` — optional
- `stats()` / `scopes()`

**Auth (at the daemon):** the Unix socket is 0600 (owner-only), so **M1 = client-name registration + per-client
scope allowlists** (default-deny); full **capability tokens + per-tool grants land with `shared` shards (M3)**.
Thin clients never open the shard.

**Retrieval-at-the-right-moment:** MCP is pull-only, so implement **proactive triggers** where the surface
allows (Claude Code hooks / session-start auto-recall) and **measure the recall rate**. "Works with
everything" is claimed only where such triggers exist and are measured; elsewhere recall is best-effort — say
so.

**Sync (app-side; Cloud target is a normal Qdrant collection):** each device **pushes its own points via
normal upserts** and **pulls the union via `scroll`**; *we* merge (provenance + temporal validity + dedup).
Do **not** use partial-snapshot pull for the multi-writer `shared` pool — it's a one-way convergence to the
server's segment state and can clobber another device's points. No first-party push client exists; app-side
push is the intended pattern.

---

## 7. Edge constraints the build MUST respect

Verified against the Edge source / team:
- **Single-writer:** the WAL takes an exclusive `flock` on `wal/` — a 2nd full-shard open is *refused* (not
  corruption), released cleanly on crash (no stale lock). Also hold our own app lockfile for a clean error +
  client coordination. `ReadOnlyEdgeShard` takes no lock (lockless readers).
- **No background optimizer and no auto-flush** (`flush_interval_sec` is ignored) — the daemon owns
  flush/optimize cadence.
- **`load()` does NOT replay the WAL** — `flush()` is the only commit/durability point; unflushed writes are
  lost on reopen (data loss, not corruption). → our command journal is the replay log (§3). `load()` runs
  per-segment `check_consistency_and_repair()`.
- Set a **small WAL `segment_capacity` (~4 MiB)** for the embedded store (`EdgeConfig.wal_options`).
- **Cross-shard fusion is app-side** (Edge fuses only within a shard).
- **Sync is app-side** to a standard Cloud collection (upsert + `scroll`); partial-snapshot *pull* = one-way
  convergence (server-authoritative), not a merge. Partial-snapshot *create* needs a newer server; plain
  upsert/scroll needs none.
- Platform: macOS arm64 wheel exists; `pip install qdrant-edge-py` + `fastembed`. Beta but near-GA + maintained.

---

## 8. Privacy, portability & packaging

- **Fully local/offline.** Extraction, embedding, retrieval all on-device; no cloud path in the loop.
- **Privacy:** stage-0 redaction (§3); scoped IPC — client-name + per-client scope allowlists (§6); `forget` =
  true purge (§3). **At-rest:** working store relies on FileVault; store-level encryption optional; but
  **artifacts that leave the device (exported snapshots, synced points) are encrypted by default** — lands with
  export/sync (M1+/M3), not M0, since nothing leaves the device before then.
- **Portability (three tiers):** (1) **memory data** — the `private`/`me-synced` shard folders travel as a
  small **snapshot**, copied device-to-device with no Cloud, or via opt-in sync; (2) **models** —
  re-provisioned from a **pinned manifest** per machine (embedding models must match versions, or re-embed
  from raw text); (3) **the app** — installed per machine. The **raw JSONL export** is a dump of the SQLite journal
  (§3) — the engine-agnostic durability guarantee; rebuild on any engine/version by replaying it.
- **Packaging:** Python + `uv` daemon (launchd) to build fast. Public v1 = **one-command install + managed
  local models + a single MCP line**; a **Rust single-binary** is a real v1-adoption goal (Edge's core is Rust).

---

## 9. Milestones

Sequenced so the **write/update model is proven first**, dogfooding early. Ingestion sources arrive in waves
(A→D), each just an adapter over the shared write-path.

- **M0 — Core + write model + durability (library-mode).** Core lib (`MemoryStore` + pipeline) + `EdgeBackend`,
  driven by a **CLI** (library-mode — the CLI is the sole writer via our lockfile; daemon + socket + auth land
  in M1, but the core-lib/daemon-API boundary is drawn now). `remember`/`recall`/`forget` over a **single
  `private` shard**. **Write model:** stage-0 redaction → (optional) Qwen3 extraction → **SQLite journal
  (source of truth)** → upsert to Edge → flush-as-commit; **conflict resolution** (retrieve similar → judge →
  high-confidence UPDATE/SUPERSEDE, else ADD); `forget(hard)` = DELETE+VACUUM + tombstone; **JSONL export =
  journal dump**. Plain hybrid retrieval (dense + miniCOIL) + payload pre-filters + a simple app-side
  recency/importance rescore; **verbatim fallback if Ollama is absent**. Seed from `CLAUDE.md` +
  `.claude/memory/*.md` + a few life facts + the **golden set** (§10). *Exit:* a correction supersedes the
  stale fact from the CLI; a `hard`-forgotten fact is gone (incl. journal); a store killed after an acked write
  but before flush reopens with it intact (journal replay). **Deferred:** daemon/socket/auth (M1), MMR,
  FormulaQuery tuning, quantization, snapshot/restore (M2), `as_of`, entity/av fields.
- **M1 — Daemon + MCP everywhere + retrieval polish + first ingestion.** Promote to the **shard-owning daemon**
  (WAL-flock + our lockfile) with a **versioned local API over a 0600 Unix socket**; ship the **MCP server**
  (thin client) → Claude Code, Desktop, Cursor, any client, with **client-name registration + scope allowlists**
  (default-deny). **Proactive retrieval triggers** (hooks / session-start); **aggressive auto-capture** (low
  threshold) kept clean by dedup-on-write + a lightweight decay-prune + a **review queue for ambiguous
  supersedes**; **undoable audit log**; retrieval polish (**FormulaQuery decay + MMR**). **Create the
  trust-boundary shards** (`private` / `me-synced` / `shared`) + cross-shard merge. **Wave-A ingestion:** import
  existing ChatGPT/Claude/Gemini memory + Obsidian/notes + a global quick-capture hotkey. *Exit:* daily use
  across >1 app; a correction made anywhere sticks and is recalled later everywhere; conflict-resolution
  accuracy + recall-at-the-right-moment measured & acceptable.
- **M2 — Consolidation & scale.** Daemon-run consolidation (idle-only, cancellable, checkpointed, bounded):
  dedup, merge, decay-prune, episodic→semantic summarization; scoring tuning; quantization; **snapshot backup +
  tested restore**; entity/av assertion fields *if* M1 accuracy demanded them. **Wave-B:** calendar + browser.
  *Exit:* memory stays clean and fast as it grows; portable folder proven across two machines.
- **M3 — Broad, multimodal, shared.** Opt-in Cloud sync of the `shared` shard (family/team) — **capability
  tokens + per-tool grants + encrypted export/sync artifacts** land here. **Wave-C:** messages/email (now that
  redaction + purge are proven). **Wave-D:** voice/ambient (local Whisper) + multimodal (CLIP). *Exit:* the full
  vision, on your terms.
- **Release gate — Public v1.** Independent of M3; ships once the core is solid (≈ end of M1, hardened through
  M2): one-command install, managed models, single MCP line, target platforms, model provisioning, in-place
  upgrade, clean uninstall; import + portability demos as onboarding; docs/README; all public copy through
  qdrant-messaging.

---

## 10. Quality / acceptance

Don't chase contested leaderboards (LoCoMo). Measure our own:
- **Golden set (~25 cases)** — the fixture the M0 write-model + conflict thresholds are tuned/graded against:
  real correction / supersede / dedup / forget cases, each `(context, existing memory, new input) → expected op
  (ADD/UPDATE/SUPERSEDE/NOOP) + expected recall`. Dylan provides/reviews the real cases; the harness + examples
  are drafted with M0.
- **Recall probes:** given context X, is the right memory in top-k across real seeded data?
- **Recall-at-the-right-moment rate** (M1 gate — the pull-only-MCP risk).
- **Conflict-resolution accuracy** (M1 gate; the audit log makes it checkable — current fact wins, no wrongful
  supersede).
- Retrieval precision + latency (scope "sub-ms" to the raw vector query; measure read and write separately).
- Noise/hoarding rate under auto-capture.
- **Product acceptance (Track B):** clean install, import, permission/consent flows, crash-recovery + journal
  replay, purge-really-purges, cold-start — all green before public v1.

---

## Sources

- Edge facts: `research/N-qdrant-edge-corrected.md` + Edge repo (`qdrant/qdrant` `lib/edge`, `qdrant_edge.pyi`,
  `qdrant-edge-demo`) verified 2026-07. Memory landscape: `research/I-memory-landscape.md`.
- Edge Python reference: `~/Documents/GitHub/edge-mission-control`.
- Talk (source of the thesis): Claude Design project `e19d820d-…`, `v7 - The Brilliant Stranger, Shared`.

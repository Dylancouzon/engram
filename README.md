# engram

**A personal, long-term memory for AI assistants. Local, portable, and yours.**

Every AI assistant you use is building a memory of you right now, and none of
them share it. Your preferences live in ChatGPT's cloud, your corrections in
Claude's, your project context in Cursor's. Switch tools and you start over.
Cancel a subscription and that memory is gone. You can't read it, move it, or
truly delete it.

engram puts that memory in a folder you own. Assistants write to it and
recall from it; you decide which apps see what, which parts sync, and what
gets forgotten. It runs fully offline: embedding, retrieval, and extraction
all happen on your machine, with no cloud in the loop.

> **Status: beta.** The full engine works today: write model with conflict
> resolution, hybrid retrieval, daemon + MCP, consolidation, encrypted
> snapshots, and multi-device sync. Built on Qdrant Edge, which is itself in
> beta. APIs may still change.

## How It Works

Three verbs, one folder (`~/.engram`):

```console
$ engram remember "Sarah is allergic to peanuts"
[added] Sarah is allergic to peanuts (9a95135e)

$ engram remember "Correction: Sarah's allergy is tree nuts, not peanuts"
[superseded] Sarah's allergy is tree nuts, not peanuts (fb80d689)
  ↳ replaces: Sarah is allergic to peanuts

$ engram recall "what should I check before cooking for Sarah"
  fb80d689 [semantic·default] 0.912
    Sarah's allergy is tree nuts, not peanuts

$ engram forget fb80d689 --hard
purged: index, journal, and future exports.
```

Writing is not appending. Each new memory is checked against what's already
known: corrections supersede the stale fact, refinements update it,
duplicates reinforce it, and anything the local judge is unsure about is
kept safe and queued for your call (`engram review`). A small local model
(Qwen3 via [Ollama](https://ollama.com)) makes those calls; without it,
engram still works and stores verbatim.

Recall is retrieval, not keyword grep: hybrid dense + sparse search
([nomic-embed](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) +
[miniCOIL](https://huggingface.co/Qdrant/minicoil-v1), both local via
[FastEmbed](https://github.com/qdrant/fastembed)), MMR diversification,
payload pre-filtering by scope and temporal validity, decayed by recency,
weighted by importance. The engine is [Qdrant Edge](https://qdrant.tech/edge/),
the in-process build of the Qdrant vector search engine: server-class
retrieval running inside the process, like SQLite.

## Plug It Into Your Assistants

A long-lived daemon owns your memory folder; the CLI and MCP servers are
thin clients of it. Each app must be granted access first (default-deny),
and you control which scopes it sees:

```bash
engram clients allow claude-code --scopes '*'
claude mcp add engram -- engram mcp --client claude-code
```

For Claude Desktop or Cursor, register a client name the same way and add
to the app's MCP config:

```json
{"mcpServers": {"engram": {"command": "engram", "args": ["mcp", "--client", "cursor"]}}}
```

The assistant gets three tools: `remember`, `recall`, and `forget`. A
correction made in one app supersedes the stale fact everywhere, because
there is only one memory. Apps granted `--scopes work` never see `personal`
memories; add `--token --methods remember` for least-privilege ingestion
adapters (see `docs/ingestion.md`). Claude Code can also surface relevant
memories at session start:

```bash
engram hook print-config   # paste into ~/.claude/settings.json
```

## Trust Boundaries and Sync

Memories live in shards that set their blast radius:

- **`private`** (default): never syncs. There is no code path that uploads
  it. Moving a memory out of `private` is a deliberate act.
- **`me-synced`**: your own memories, across your devices.
- **`shared:<group>`**: opt-in pools (family, team).

Sync uses a standard Qdrant Cloud collection as a dumb relay: memories
travel as ciphertext (encrypted with a key that never leaves your devices),
with no embeddings and no plaintext uploaded. Devices merge locally:
last-write-wins by timestamp, and a hard forget propagates as a
content-free tombstone that purges the memory everywhere.

```bash
engram sync setup --shard me-synced --url https://<cluster>.qdrant.io --api-key ...
engram sync now
```

## What Ownership Means Here

- **Local by default.** No account, no telemetry, no network calls. The
  only network paths are localhost Ollama and the sync you explicitly set up.
- **Secrets never land.** A deterministic scrubber runs before anything is
  extracted, embedded, or persisted. API keys and tokens are redacted;
  private keys refuse the whole write.
- **Forgotten means gone.** `forget --hard` purges the journal (delete +
  VACUUM) and rebuilds the search index without the memory, because a plain
  index delete leaves content readable in storage pages. The tests grep raw
  bytes across the whole folder to prove it, and the tombstone purges
  synced copies on their next sync.
- **Built to outlive any app.** `engram export` dumps the write journal as
  JSONL: plain text, no vectors, no lock-in. Replaying it rebuilds your
  memory on any machine, any future version, or any other engine.
  `engram snapshot` backs up the whole folder as one encrypted file.
- **Crash-safe.** Every write is journaled (SQLite) before it touches the
  index. Kill the process at any point; nothing acknowledged is lost.
- **Memory stays clean as it grows.** When idle, the daemon prunes stale
  never-recalled episodes, collapses duplicates, and summarizes old event
  clusters into durable facts (locally, via the same small model).

## Install

Requires Python 3.12 on macOS (Apple Silicon), Linux x86_64/aarch64, or
Windows. From source for now:

```bash
git clone https://github.com/qdrant/engram && cd engram
uv sync
uv run engram remember "engram works"
```

Optional pieces:

```bash
ollama pull qwen3:4b            # extraction + conflict resolution
uv run engram daemon --install  # start at login (macOS launchd)
```

Embedding models (~600 MB) download on first use and are cached per
machine, not inside your memory folder.

## Reclaim Your Cloud Memory

Your existing assistant memories import in one command. Copy them from
ChatGPT (Settings → Personalization → Manage memories) or Claude into a
markdown file, or point engram at a notes folder:

```bash
engram seed chatgpt-memories.md
engram seed ~/ObsidianVault --scope personal
engram seed ~/.claude/CLAUDE.md
```

Every chunk goes through the full write pipeline: redaction, extraction,
dedup, conflict resolution.

## Commands

| Command | What it does |
|---|---|
| `engram remember / recall / forget` | The three verbs (`--shard`, `--scope` to target) |
| `engram review` | Decide queued conflicts the judge wasn't sure about |
| `engram seed <files\|dirs>` | Import markdown through the write pipeline |
| `engram export` / `import` | JSONL journal dump / replay (the no-lock-in guarantee) |
| `engram snapshot` / `restore` | Encrypted one-file backup / verified restore |
| `engram sync setup / now` | Encrypted multi-device / shared-pool sync |
| `engram consolidate` | Run housekeeping now instead of waiting for idle |
| `engram daemon [--install]` | The memory-owning daemon (launchd install) |
| `engram clients allow/revoke/list` | Per-app scopes, capability tokens, method grants |
| `engram mcp --client <name>` | MCP server for one registered app |
| `engram stats` / `rebuild` | Store health / re-index from the journal |

## License

Apache-2.0.

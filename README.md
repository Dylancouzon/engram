# engram

**A personal, long-term memory for AI assistants. Local, portable, and yours.**

Every AI assistant you use is building a memory of you right now, and none of
them share it. Your preferences live in ChatGPT's cloud, your corrections in
Claude's, your project context in Cursor's. Switch tools and you start over.
Cancel a subscription and that memory is gone. You can't read it, move it, or
truly delete it.

engram puts that memory in a folder you own. Assistants write to it and recall
from it; you decide what stays, what syncs, and what gets forgotten. It runs
fully offline: embedding, retrieval, and extraction all happen on your machine,
with no cloud in the loop.

> **Status: early.** The core write/recall/forget engine and CLI work today
> (M0). The MCP server that plugs engram into Claude Code, Claude Desktop,
> Cursor, and other assistants is the next milestone. APIs may change.

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
known: corrections supersede the stale fact, refinements update it, duplicates
reinforce it. A small local model (Qwen3 via [Ollama](https://ollama.com))
makes those calls; without it, engram still works and stores verbatim.

Recall is retrieval, not keyword grep: hybrid dense + sparse search
([nomic-embed](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) +
[miniCOIL](https://huggingface.co/Qdrant/minicoil-v1), both local via
[FastEmbed](https://github.com/qdrant/fastembed)), filtered by scope and
validity, decayed by recency, weighted by importance. The engine is
[Qdrant Edge](https://qdrant.tech/edge/), the in-process build of the Qdrant
vector search engine: server-class hybrid retrieval and payload filtering,
running inside the process like SQLite.

## What Ownership Means Here

- **Local by default.** No account, no telemetry, no network calls. The only
  optional network dependency is localhost Ollama.
- **Secrets never land.** A deterministic scrubber runs before anything is
  extracted, embedded, or persisted. API keys and tokens are redacted;
  private keys refuse the whole write.
- **Forgotten means gone.** `forget --hard` purges the memory from the
  journal (delete + VACUUM) and rebuilds the search index without it, because
  a plain index delete leaves content readable in storage pages. Afterwards
  the content exists in no file in your memory folder: the tests grep raw
  bytes to prove it. Only a content-free tombstone remains.
- **Built to outlive any app.** `engram export` dumps the write journal as
  JSONL: plain text, no vectors, no lock-in. Replaying it rebuilds your
  memory on any machine, any future version, or any other engine.
- **Crash-safe.** Every write is journaled (SQLite) before it touches the
  index. Kill the process at any point; nothing acknowledged is lost.

## Install

Requires Python 3.12 on macOS (Apple Silicon), Linux x86_64/aarch64, or
Windows. From source for now:

```bash
git clone https://github.com/qdrant/engram && cd engram
uv sync
uv run engram remember "engram works"
```

Optional, for extraction and conflict resolution:

```bash
ollama pull qwen3:4b
```

Embedding models (~600 MB) download on first use and are cached per machine,
not inside your memory folder.

## Commands

| Command | What it does |
|---|---|
| `engram remember <text>` | Store a memory (redact, extract, resolve conflicts) |
| `engram recall <query>` | Hybrid search with recency decay and importance weighting |
| `engram forget <id\|query>` | Soft (invalidate, keep history) or `--hard` (purge) |
| `engram seed <files>` | Import memories from markdown (CLAUDE.md, notes) |
| `engram export` / `import` | JSONL journal dump / replay |
| `engram stats` / `rebuild` | Store health / re-index from the journal |

## Roadmap

- **M1:** daemon + MCP server (one memory across Claude Code, Desktop, Cursor,
  and any MCP client), proactive recall triggers, import from ChatGPT/Claude
  memory exports, review queue for ambiguous conflicts.
- **M2:** background consolidation (dedup, episodic summaries, decay pruning),
  snapshot backup and restore.
- **M3:** opt-in encrypted sync for multi-device and shared (family/team)
  memory pools.

## License

Apache-2.0.

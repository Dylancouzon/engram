# Qdrant Engram

**A personal, long-term memory for AI assistants. Local, portable, and yours.**

Every AI assistant you use is building a memory of you right now, and none of
them share it. Your preferences live in ChatGPT's cloud, your corrections in
Claude's, your project context in Cursor's. Switch tools and you start over;
cancel a subscription and that memory is gone.

engram puts that memory in a folder you own. Assistants write to it and
recall from it; you decide which apps see what, which parts sync, and what
gets forgotten. Embedding, retrieval, and extraction all run offline on your
machine.

> **Status: beta**, built on Qdrant Edge (itself in beta). APIs may change;
> your data doesn't depend on them: every memory lives in a plain-text
> journal that `engram export` dumps and any future version replays.

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

Writing is not appending: a small local model (Qwen3 via
[Ollama](https://ollama.com)) checks each new memory against what's known.
Corrections supersede, refinements update, duplicates reinforce; anything
uncertain is stored safely and queued for `engram review`. Without Ollama,
engram still works and stores verbatim. On a 25-case golden set (`golden/`),
the write model picks the right operation 84% of the time and surfaces the
right memory on every recall.

Recall is hybrid dense + sparse search
([nomic-embed](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) +
[miniCOIL](https://huggingface.co/Qdrant/minicoil-v1) via
[FastEmbed](https://github.com/qdrant/fastembed)), pre-filtered by scope and
temporal validity, decayed by recency, weighted by importance. The engine is
[Qdrant Edge](https://qdrant.tech/edge/), the in-process build of the Qdrant
vector search engine: like SQLite, but for search.

## Claude Code, Wired In

One command puts engram inside the assistant's loop:

```bash
engram hook install claude-code   # installs the hooks, offers the daemon
```

Every session then recalls against each prompt (confident matches are
injected before the model answers), loads project context at session start,
and captures durable facts through the full write pipeline when the session
ends. Recall gates on similarity, so unrelated prompts inject nothing;
capture needs the local model. Editors without hooks (Cursor, Windsurf) get
a paste-in rules block: `engram rules cursor`.

## Plug In Other Assistants

A daemon owns your memory folder; the CLI and MCP servers are thin clients.
Apps are default-deny and scope-limited:

```bash
engram clients allow claude-code --scopes '*'
claude mcp add engram -- engram mcp --client claude-code
```

For Claude Desktop or Cursor, register a client name and add:

```json
{"mcpServers": {"engram": {"command": "engram", "args": ["mcp", "--client", "cursor"]}}}
```

The assistant gets `remember`, `recall`, and `forget`. One memory, every
app: a correction made anywhere supersedes the stale fact everywhere. Apps
granted `--scopes work` never see `personal`; add `--token` to require a
capability token (see `docs/ingestion.md`).

## Trust Boundaries and Sync

Memories live in shards that set their blast radius:

- **`private`** (default): never syncs. There is no code path that uploads it.
- **`me-synced`**: your own memories, across your devices.
- **`shared:<group>`**: opt-in pools (family, team).

Sync uses a Qdrant Cloud collection as a dumb relay: memories travel as
ciphertext, encrypted with a key that never leaves your devices. The relay
sees only routing metadata (ids, timestamps, device names). Merge happens
locally, last-write-wins; a hard forget propagates as a content-free
tombstone that purges the memory everywhere.

```bash
engram sync setup --shard me-synced --url https://<cluster>.qdrant.io --api-key ...
engram sync now
```

Cloud built-ins offer none of this: ChatGPT, Claude, and Gemini memories are
personal-only, locked to one assistant, and export as copy-paste at best.

## See What It Knows

A memory you can't inspect is one you're trusting, not owning:

```console
$ engram list        # browse every memory, newest first
$ engram log         # what the hooks injected or captured, and when
$ engram dashboard   # the same in your browser: search, filter, history
$ engram stats       # counts, disk usage, pending reviews
```

The dashboard is one static HTML file in your memory folder: no server,
nothing leaves the machine.

### Study What Passed Through (dev-only)

`engram log` and the ring behind it keep only the newest ~50 events. For
offline study of the full history — every prompt seen, what it surfaced, what
was captured — the daemon also appends each event to `~/.engram/activity.jsonl`
(append-only, never pruned; prompts are secret-redacted, truncated to 500
chars). Memory writes are covered separately by `engram export`.

```console
# prompts where nothing was recalled — the misses worth studying
$ jq 'select(.kind=="prompt-recall" and (.surfaced|length)==0)' ~/.engram/activity.jsonl
# what extraction actually kept from sessions
$ jq 'select(.kind=="auto-capture") | .saved' ~/.engram/activity.jsonl
# the full memory lifecycle: adds, updates, supersessions
$ engram export
```

This is a temporary study aid (`_append_activity_log` in `journal.py`) and is
removed before release — it is not covered by `forget --hard`.

## What Ownership Means Here

- **Local by default.** No account, no telemetry; the only network paths are
  localhost Ollama and the sync you explicitly set up.
- **Secrets never land.** A deterministic scrubber runs before anything is
  extracted, embedded, or persisted; private keys refuse the whole write.
- **Forgotten means gone.** `forget --hard` purges the journal (delete +
  VACUUM) and rebuilds the index without the memory. Tests grep raw bytes
  across the whole folder to prove it.
- **No lock-in.** `engram export` dumps plain-text JSONL; replaying it
  rebuilds your memory on any machine or engine. `engram snapshot` is the
  encrypted one-file backup.
- **Crash-safe.** Every write is journaled (SQLite) before it touches the
  index; nothing acknowledged is lost.
- **Stays clean as it grows.** When idle, the daemon prunes stale episodes,
  collapses duplicates, and summarizes old event clusters into facts.

## Install

Requires Python 3.12 exactly (the Qdrant Edge beta ships wheels for 3.12
only; `uv sync` fails on 3.13) on macOS (Apple Silicon) or Linux
(x86_64/aarch64). From source for now:

```bash
git clone https://github.com/Dylancouzon/engram && cd engram
uv sync
uv run engram remember "engram works"
```

The first command downloads the embedding models (~600 MB, once, to
`~/.cache/engram`) and says so. Everything after runs offline.

Optional:

```bash
ollama pull qwen3:4b            # extraction + conflict resolution
uv run engram daemon --install  # start at login (macOS launchd)
```

The daemon keeps recall warm for the hooks. On Linux, run `engram daemon`
yourself (or wrap it in a systemd user unit).

### Upgrade

```bash
git pull
uv sync
```

If you installed the CLI as a tool (`uv tool install .`), the daemon and hooks
run that installed copy, not the repo — refresh it too:

```bash
uv tool install --force --reinstall .
```

The daemon doesn't hot-reload code — if one is already running, restart it:

```bash
pkill -TERM -f "engram daemon"
```

It respawns on the next command (or at login, if installed with `--install`).

### Uninstall

```bash
launchctl unload -w ~/Library/LaunchAgents/tech.qdrant.engram.plist \
  && rm ~/Library/LaunchAgents/tech.qdrant.engram.plist   # if daemon installed
rm -rf ~/.engram          # your memories (snapshot first to keep them)
rm -rf ~/.cache/engram    # the downloaded models
```

If you installed hooks, remove the `engram hook` entries from
`~/.claude/settings.json` (the install saved a backup next to it).

## Reclaim Your Cloud Memory

Copy your saved memories from ChatGPT (Settings → Personalization) or Claude
into a markdown file, or point engram at a notes folder. Every chunk goes
through the full write pipeline:

```bash
engram seed chatgpt-memories.md
engram seed ~/ObsidianVault --scope personal
engram seed ~/.claude/CLAUDE.md
```

## Commands

| Command | What it does |
|---|---|
| `engram remember / recall / forget` | The three verbs (`--shard`, `--scope` to target) |
| `engram list` | Browse every memory, newest first (`--all` includes superseded) |
| `engram log` | The hook audit trail: what was injected or captured, when |
| `engram dashboard` | Browse and search everything in a local HTML file |
| `engram review` | Decide queued conflicts the judge wasn't sure about |
| `engram seed <files\|dirs>` | Import markdown through the write pipeline |
| `engram export` / `import` | JSONL journal dump / replay (the no-lock-in guarantee) |
| `engram snapshot` / `restore` | Encrypted one-file backup / verified restore |
| `engram sync setup / now` | Encrypted multi-device / shared-pool sync |
| `engram consolidate` | Run housekeeping now instead of waiting for idle |
| `engram daemon [--install]` | The memory-owning daemon (launchd install) |
| `engram clients allow/revoke/list` | Per-app scopes and capability tokens |
| `engram mcp --client <name>` | MCP server for one registered app |
| `engram stats` / `rebuild` | Store health / re-index from the journal |

## License

Apache-2.0.

# engram — working notes for agents

Personal, local-first, long-term memory for AI assistants. Qdrant Edge
(in-process) + MCP. Full build spec: `INSTRUCTIONS.md`. Distilled Edge API
facts (verified against 0.7.2): `docs/edge-api-notes.md` — read it before
touching `backend/edge.py`; the .pyi stub lies about some defaults.

## Ground rules

- Qdrant is a **vector search engine**, never a "vector database", in all copy.
- The SQLite journal is the source of truth; Edge is a rebuildable index.
  Never apply a write to Edge before its journal append (the ack point).
- Edge's `flush()` is the only durability point (no WAL replay on reopen);
  the journal high-water mark may only advance to seqs actually applied.
- `forget --hard` must leave no content anywhere: Edge point, journal rows
  (DELETE + VACUUM), exports. Tests assert on raw db-file bytes.
- Extraction/judge (Ollama qwen3) is an enhancer: everything must work
  verbatim/ADD-only without it.
- Public copy (README, docs) goes through the `qdrant-messaging` skill.

## Commands

- `uv run pytest tests/ -q` — unit + exit tests (fake embedder, no downloads)
- `uv run engram daemon` — the shard-owning daemon (local API on a 0600
  Unix socket; `ENGRAM_SOCKET` overrides the path — AF_UNIX caps ~104 bytes)
- `uv run engram mcp --client <name>` — MCP stdio server (thin daemon client)
- `uv run python golden/harness.py -v` — write-model accuracy vs golden set
  (real models; needs Ollama for non-ADD ops)
- `uv run engram --help` — the CLI (`ENGRAM_HOME` overrides `~/.engram`)
- `uv run ruff check src tests`

## Layout

`src/engram/`: `store.py` (pipelines, write lock, buffered reinforce) ·
`journal.py` (source of truth) · `backend/edge.py` (shard) · `embed.py`
(nomic + miniCOIL via FastEmbed) · `extract.py`/`resolve.py`/`llm.py`
(local model) · `redact.py` (stage-0) · `protocol.py` (versioned local API,
line-JSON over Unix socket) · `daemon.py` (shard owner; client registry =
clients.json, default-deny except cli) · `client.py` (thin client, daemon
auto-spawn) · `mcp_server.py` (FastMCP stdio) · `cli.py` (daemon-first,
library fallback) · `models.py`/`config.py`. Tests inject
`FakeEmbedder`/`FakeLLM` (`tests/conftest.py`) — no model downloads in CI.

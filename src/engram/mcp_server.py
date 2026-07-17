"""MCP server: the neutral waist that plugs engram into every assistant.

A thin client of the daemon — it holds no store state. One instance runs
per MCP client (stdio), identified by --client, so the daemon can enforce
that client's scope allowlist. Setup is one line, e.g. for Claude Code:

    claude mcp add engram -- engram mcp --client claude-code
"""

from __future__ import annotations

import datetime as dt

from mcp.server.fastmcp import FastMCP

from engram.client import Client, DaemonUnavailable
from engram.config import Config
from engram.models import Op
from engram.protocol import ProtocolError
from engram.store import WriteRefusedError

_INSTRUCTIONS = """engram is the user's personal, long-term memory. It persists across
sessions and across every assistant they use.

Call `recall` at the start of a task to check what is already known about
the people, projects, and preferences involved. Call `remember` when the
user states a stable fact, preference, decision, or correction worth
keeping ("remember this", "actually it's X not Y", "we decided...").
Corrections are handled automatically: remembering a fact that contradicts
an old one supersedes it."""


def run_mcp(config: Config, client_name: str, token: str | None = None) -> None:
    client = Client(config, client_name, token=token)
    client.connect(spawn=True)

    def call(fn, *args, retry: bool = False, **kwargs):
        """Reconnect if the daemon restarted under us. Only idempotent calls
        (recall) are replayed: a lost response to remember/forget may mean
        the daemon already applied it, and a blind retry would duplicate."""
        try:
            return fn(*args, **kwargs)
        except DaemonUnavailable:
            client.close()
            client.connect(spawn=True)
            if not retry:
                raise
            return fn(*args, **kwargs)

    mcp = FastMCP("engram", instructions=_INSTRUCTIONS)

    @mcp.tool()
    def remember(
        text: str,
        type: str | None = None,
        tags: list[str] | None = None,
        scope: str = "default",
        importance: float | None = None,
    ) -> str:
        """Store a fact, preference, decision, or correction in the user's
        long-term memory. Write ONE atomic, self-contained statement that
        will make sense months from now (resolve pronouns; include names).
        Contradictions with existing memories are resolved automatically:
        a correction supersedes the stale fact.

        type: semantic (facts/preferences), episodic (dated events), or
        procedural (how-tos). Omit to let engram classify.
        importance: 0-1; use 0.8+ only for explicit "remember this" asks.
        """
        try:
            actions = call(client.remember, text, tags=tags, scope=scope,
                           importance=importance,
                           type=_parse_type(type), source_ref=None)
        except WriteRefusedError as e:
            return f"Not stored: {e}."
        except DaemonUnavailable:
            return ("The memory daemon restarted while storing; the memory may or "
                    "may not have been saved. Use recall to check before retrying.")
        except ProtocolError as e:
            return _protocol_help(e, client_name)
        if not actions:
            return "Nothing stored: judged not worth keeping long-term."
        lines = []
        for a in actions:
            if a.op is Op.NOOP and a.target:
                lines.append(f"Already known: {a.target.text}")
            elif a.op is Op.SUPERSEDE and a.memory and a.target:
                lines.append(f"Stored (supersedes \"{a.target.text}\"): "
                             f"{a.memory.text} [id {a.memory.id}]")
            elif a.memory:
                lines.append(f"Stored ({a.op.value.lower()}): {a.memory.text} "
                             f"[id {a.memory.id}]")
            if a.redaction_hits:
                lines.append(f"Note: secrets were redacted before storing "
                             f"({', '.join(sorted(set(a.redaction_hits)))}).")
        return "\n".join(lines)

    @mcp.tool()
    def recall(query: str, k: int = 6, scope: str | None = None) -> str:
        """Search the user's long-term memory. Use at the start of tasks to
        check what is already known about the people, projects, tools, or
        preferences involved. Returns the most relevant memories, weighted
        by recency and importance. Query with natural language."""
        # Restrict to this project + "default" when the caller gives no scope,
        # mirroring the hooks and matching remember's "default" — an unscoped
        # recall must not fan out across every project. The server runs as a
        # subprocess of the client in the project dir, so its cwd IS the
        # project. Same contract as cli._project_scope. Pass an explicit scope
        # to search broadly.
        if scope is None:
            from pathlib import Path
            scope = [f"project:{Path.cwd().name.lower()}", "default"]
        try:
            hits = call(client.recall, query, k=k, scope=scope, retry=True)
        except DaemonUnavailable:
            return "The memory daemon is unavailable; recall could not run."
        except ProtocolError as e:
            return _protocol_help(e, client_name)
        if not hits:
            return "No memories match."
        lines = []
        for h in hits:
            created = dt.datetime.fromtimestamp(h.memory.created_at).strftime("%Y-%m-%d")
            lines.append(f"- {h.memory.text} (id {h.memory.id}, {h.memory.type.value},"
                         f" learned {created})")
        return "\n".join(lines)

    @mcp.tool()
    def forget(memory_id: str, hard: bool = False) -> str:
        """Forget a memory by id (get ids from recall). Default is soft:
        the memory stops being recalled but history is kept. hard=True
        purges it completely and irreversibly — only when the user
        explicitly asks for permanent deletion."""
        try:
            # Idempotent by id (forgetting twice converges), so retry is safe.
            done = call(client.forget, memory_id, mode="hard" if hard else "soft",
                        retry=True)
        except DaemonUnavailable:
            return ("The memory daemon is unavailable; nothing was forgotten. "
                    "Try again once it is running.")
        except ProtocolError as e:
            return _protocol_help(e, client_name)
        if not done:
            return f"No memory with id {memory_id}."
        return ("Purged permanently (index, journal, and future exports)."
                if hard else "Forgotten (soft): no longer recalled, history kept.")

    mcp.run()


def _parse_type(value: str | None):
    from engram.models import MemoryType

    if not value:
        return None
    try:
        return MemoryType(value.lower())
    except ValueError:
        return None


def _protocol_help(e: ProtocolError, client_name: str) -> str:
    if e.code == "unregistered_client":
        return (f"engram has not been granted access for {client_name!r} yet. "
                f"Ask the user to run: engram clients allow {client_name} --scopes '*'")
    if e.code == "scope_denied":
        return f"Access denied by the user's scope settings: {e}"
    return f"engram error ({e.code}): {e}"

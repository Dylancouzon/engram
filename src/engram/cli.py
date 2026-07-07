"""engram CLI.

Daemon-first: when the daemon is running, every command is a thin client of
it over the local API. Otherwise commands fall back to library mode (open
the store directly as the sole writer), so engram works with zero setup.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import uuid
from pathlib import Path

import click

from engram.client import Client, DaemonUnavailable
from engram.config import Config
from engram.models import VALID_FOREVER, Memory, MemoryType, Op
from engram.store import MemoryStore, StoreLockedError, WriteRefusedError

_OP_STYLES = {
    Op.ADD: ("added", "green"),
    Op.UPDATE: ("updated", "yellow"),
    Op.SUPERSEDE: ("superseded", "magenta"),
    Op.NOOP: ("already known", "blue"),
}


def _config(data_dir: str | None) -> Config:
    return Config.load(Path(data_dir) if data_dir else None)


def _open_store(data_dir: str | None) -> MemoryStore:
    try:
        return MemoryStore(_config(data_dir))
    except StoreLockedError as e:
        raise click.ClickException(
            f"{e} — if that's the daemon, this command talks to it automatically;"
            " otherwise remove the stale lock"
        ) from e


def _open_surface(data_dir: str | None) -> Client | MemoryStore:
    """Daemon when it's running, library mode when it's not."""
    cfg = _config(data_dir)
    client = Client(cfg, client_name="cli")
    try:
        client.connect(spawn=False)
        return client
    except DaemonUnavailable:
        return _open_store(data_dir)


def _fmt_ts(ts: float | None) -> str:
    if not ts or ts >= VALID_FOREVER:
        return "-"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _short(mid: str) -> str:
    return mid.split("-")[0]


def _print_memory(m: Memory, score: float | None = None) -> None:
    line = click.style(f"  {_short(m.id)}", fg="cyan")
    line += click.style(f" [{m.type.value}·{m.scope}]", dim=True)
    if score is not None:
        line += click.style(f" {score:.3f}", fg="green")
    click.echo(line)
    click.echo(f"    {m.text}")
    meta = f"    created {_fmt_ts(m.created_at)}"
    if m.tags:
        meta += f" · tags: {', '.join(m.tags)}"
    if m.access_count:
        meta += f" · recalled {m.access_count}x"
    click.echo(click.style(meta, dim=True))


@click.group()
@click.option("--data-dir", envvar="ENGRAM_HOME", default=None,
              help="Memory folder (default ~/.engram).")
@click.version_option(package_name="engram")
@click.pass_context
def main(ctx: click.Context, data_dir: str | None) -> None:
    """Your memory, in a folder you own.

    engram is a local, portable, long-term memory for AI assistants —
    stored on your machine, private by default.
    """
    ctx.obj = data_dir


@main.command()
@click.argument("text")
@click.option("--type", "mtype", type=click.Choice([t.value for t in MemoryType]),
              default=None, help="Force the memory type.")
@click.option("--tags", default=None, help="Comma-separated topic tags.")
@click.option("--scope", default="default", help="Payload scope (work, personal, ...).")
@click.option("--importance", type=click.FloatRange(0.0, 1.0), default=None)
@click.option("--source-ref", default=None, help="Provenance pointer (file, url, chat).")
@click.option("--shard", default="private",
              help="Trust boundary: private (default, never syncs), me-synced, shared:<group>.")
@click.pass_obj
def remember(data_dir: str | None, text: str, mtype: str | None, tags: str | None,
             scope: str, importance: float | None, source_ref: str | None,
             shard: str) -> None:
    """Store something worth keeping. Conflicts with existing memories are
    resolved on write: corrections supersede, refinements update."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    with _open_surface(data_dir) as store:
        try:
            actions = store.remember(
                text,
                type=MemoryType(mtype) if mtype else None,
                tags=tag_list,
                scope=scope,
                importance=importance,
                source_ref=source_ref,
                shard=shard,
            )
        except WriteRefusedError as e:
            raise click.ClickException(f"not stored: {e}") from e

    if not actions:
        click.echo("nothing worth remembering in that (salience gate).")
        return
    for action in actions:
        label, color = _OP_STYLES[action.op]
        click.echo(click.style(f"[{label}]", fg=color, bold=True), nl=False)
        if action.op is Op.NOOP and action.target:
            click.echo(f" {action.target.text}")
        elif action.memory:
            click.echo(f" {action.memory.text} " +
                       click.style(f"({_short(action.memory.id)})", fg="cyan"))
        if action.op is Op.SUPERSEDE and action.target:
            click.echo(click.style(f"  ↳ replaces: {action.target.text}", dim=True))
        if action.queued_review:
            click.echo(click.style(
                "  ? may conflict with an existing memory — run `engram review`",
                fg="yellow"))
        if action.redaction_hits:
            click.echo(click.style(
                f"  ⚠ redacted before storing: {', '.join(sorted(set(action.redaction_hits)))}",
                fg="red"))


@main.command()
@click.argument("query")
@click.option("-k", type=int, default=None, help="How many memories to return.")
@click.option("--scope", default=None)
@click.option("--type", "mtype", type=click.Choice([t.value for t in MemoryType]), default=None)
@click.option("--tags", default=None, help="Comma-separated tag filter (any match).")
@click.option("--shard", default=None, help="Search one trust boundary only.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_obj
def recall(data_dir: str | None, query: str, k: int | None, scope: str | None,
           mtype: str | None, tags: str | None, shard: str | None, as_json: bool) -> None:
    """Surface the memories relevant to a query (hybrid search, decayed by
    recency, weighted by importance)."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    with _open_surface(data_dir) as store:
        hits = store.recall(
            query, k=k, scope=scope,
            type=MemoryType(mtype) if mtype else None, tags=tag_list, shard=shard,
        )
    if as_json:
        click.echo(json.dumps(
            [{"id": h.memory.id, "text": h.memory.text, "type": h.memory.type.value,
              "scope": h.memory.scope, "tags": h.memory.tags, "score": h.score,
              "created_at": h.memory.created_at} for h in hits],
            ensure_ascii=False, indent=2))
        return
    if not hits:
        click.echo("no memories match.")
        return
    for hit in hits:
        _print_memory(hit.memory, hit.score)


@main.command()
@click.argument("target")
@click.option("--hard", is_flag=True,
              help="Purge completely (index, journal, exports) and tombstone the id. "
                   "Default is soft: kept for history, excluded from recall.")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.pass_obj
def forget(data_dir: str | None, target: str, hard: bool, yes: bool) -> None:
    """Forget a memory, by id (or id prefix) or by search query."""
    with _open_surface(data_dir) as store:
        memory = _resolve_target(store, target)
        if memory is None:
            raise click.ClickException(f"no memory matches {target!r}")
        _print_memory(memory)
        mode = "hard" if hard else "soft"
        if not yes and not click.confirm(f"{mode}-forget this memory?"):
            click.echo("kept.")
            return
        store.forget(memory.id, mode=mode)
    if hard:
        click.echo(click.style("purged: index, journal, and future exports.", fg="red"))
    else:
        click.echo("invalidated: no longer recalled (history preserved).")


def _resolve_target(store: Client | MemoryStore, target: str) -> Memory | None:
    if re.fullmatch(r"[0-9a-fA-F-]{4,36}", target):
        try:
            uuid.UUID(target)
            if isinstance(store, Client):
                return store.get(target)
            found = store.backend.retrieve([target])
            return found[0].memory() if found else None
        except ValueError:
            if isinstance(store, MemoryStore):  # prefix scan needs the shard
                for hit in store.backend.scroll_all():
                    if hit.id.startswith(target.lower()):
                        return hit.memory()
                return None
    hits = store.recall(target, k=1, reinforce=False)
    return hits[0].memory if hits else None


@main.command()
@click.option("-o", "--output", type=click.File("w"), default=sys.stdout,
              help="Destination file (default stdout).")
@click.pass_obj
def export(data_dir: str | None, output) -> None:
    """Dump the journal as JSONL — the engine-agnostic export. Rebuild your
    memory anywhere by replaying it (`engram import`)."""
    with _open_surface(data_dir) as store:
        if isinstance(store, Client):
            data = store.export_jsonl()
            output.write(data)
            n = data.count("\n")
        else:
            n = store.export_jsonl(output)
    click.echo(f"exported {n} journal entries.", err=True)


@main.command(name="import")
@click.argument("source", type=click.File("r"))
@click.pass_obj
def import_(data_dir: str | None, source) -> None:
    """Replay a JSONL export into this store (restore / migration)."""
    from engram.redact import redact

    with _open_store(data_dir) as store:
        # Imported files may not come from a store that redacted on write.
        scrub = (lambda t: redact(t).text) if store.config.redaction_enabled else None
        n = store.journal.import_jsonl(source, scrub=scrub)
        applied = store.rebuild()
    click.echo(f"imported {n} entries, rebuilt index with {applied} operations.")


@main.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--scope", default="default")
@click.pass_obj
def seed(data_dir: str | None, paths: tuple[Path, ...], scope: str) -> None:
    """Seed memories from markdown files (CLAUDE.md, notes, ...). Each
    paragraph or bullet goes through the full write pipeline."""
    if not paths:
        raise click.ClickException("give at least one file to seed from")
    chunks: list[tuple[str, str]] = []
    for path in paths:
        for chunk in _split_markdown(path.read_text()):
            chunks.append((chunk, str(path)))
    click.echo(f"seeding {len(chunks)} chunks from {len(paths)} file(s)...")
    counts: dict[Op, int] = {}
    with _open_store(data_dir) as store, click.progressbar(chunks, label="remembering") as bar:
        for text, ref in bar:
            try:
                for action in store.remember(text, scope=scope, source_ref=ref,
                                             surface="seed"):
                    counts[action.op] = counts.get(action.op, 0) + 1
            except WriteRefusedError:
                continue
    summary = ", ".join(f"{op.value.lower()} {n}" for op, n in sorted(counts.items()))
    click.echo(f"done: {summary or 'nothing stored'}")


def _split_markdown(text: str) -> list[str]:
    """Paragraphs and top-level bullets become candidate chunks; headings
    are dropped (structure, not facts)."""
    chunks: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        bullets = re.findall(r"^[-*]\s+(.+(?:\n(?![-*]\s).+)*)", block, re.MULTILINE)
        if bullets:
            chunks.extend(re.sub(r"\s+", " ", b).strip() for b in bullets)
        else:
            chunks.append(re.sub(r"\s+", " ", block))
    return [c for c in chunks if len(c) > 15]


@main.command()
@click.pass_obj
def stats(data_dir: str | None) -> None:
    """Store health: counts, journal state, models."""
    with _open_surface(data_dir) as store:
        info = store.stats()
        if isinstance(store, Client):
            info["daemon"] = "running"
    for key, value in info.items():
        click.echo(f"{key:>16}: {value}")


@main.command()
@click.pass_obj
def rebuild(data_dir: str | None) -> None:
    """Re-index everything from the journal (after model changes or to
    verify the journal really is the source of truth)."""
    with _open_store(data_dir) as store:
        applied = store.rebuild()
    click.echo(f"rebuilt index from journal: {applied} operations replayed.")


@main.command()
@click.option("--list", "list_only", is_flag=True, help="List without prompting.")
@click.pass_obj
def review(data_dir: str | None, list_only: bool) -> None:
    """Decide queued conflicts: writes the judge wasn't sure about were
    stored as separate memories; accept to apply the suspected
    update/supersede, reject to keep both."""
    with _open_surface(data_dir) as store:
        items = store.pending_reviews()
        if not items:
            click.echo("nothing to review.")
            return
        for item in items:
            op = item.proposed_op.value.lower()
            click.echo(click.style(f"\n[{op}? {item.confidence:.0%}]", fg="yellow",
                                   bold=True) + f" (review {item.seq})")
            click.echo(f"  existing: {item.target.text}")
            click.echo(f"       new: {item.new.text}")
            if item.merged_text:
                click.echo(click.style(f"    merged: {item.merged_text}", dim=True))
            if list_only:
                continue
            choice = click.prompt(
                f"  apply {op}? [a]ccept / [r]eject / [s]kip", default="s",
                show_default=False)
            if choice.lower().startswith("a"):
                store.resolve_review(item.seq, accept=True)
                click.echo(click.style(f"  applied {op}.", fg="green"))
            elif choice.lower().startswith("r"):
                store.resolve_review(item.seq, accept=False)
                click.echo("  kept both.")


@main.command()
@click.pass_obj
def daemon(data_dir: str | None) -> None:
    """Run the engram daemon: the single owner of your memory, serving the
    local API every other surface (CLI, MCP, importers) talks to."""
    from engram.daemon import run_daemon

    cfg = _config(data_dir)
    click.echo(f"engram daemon starting on {cfg.socket_path}")
    try:
        run_daemon(cfg)
    except StoreLockedError as e:
        raise click.ClickException(str(e)) from e


@main.group()
def clients() -> None:
    """Which apps may read or write which scopes (default-deny)."""


@clients.command("allow")
@click.argument("name")
@click.option("--scopes", default="*",
              help="Comma-separated scope allowlist, or * for everything.")
@click.pass_obj
def clients_allow(data_dir: str | None, name: str, scopes: str) -> None:
    """Register a client (e.g. claude-code, cursor) and grant it scopes."""
    from engram.daemon import ClientRegistry

    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    ClientRegistry(_config(data_dir)).allow(name, scope_list)
    click.echo(f"{name}: allowed scopes {', '.join(scope_list)}")


@clients.command("revoke")
@click.argument("name")
@click.pass_obj
def clients_revoke(data_dir: str | None, name: str) -> None:
    """Remove a client's access."""
    from engram.daemon import ClientRegistry

    if ClientRegistry(_config(data_dir)).revoke(name):
        click.echo(f"{name}: revoked")
    else:
        raise click.ClickException(f"no registered client {name!r}")


@clients.command("list")
@click.pass_obj
def clients_list(data_dir: str | None) -> None:
    """List registered clients and their scopes."""
    from engram.daemon import ClientRegistry

    for name, entry in ClientRegistry(_config(data_dir)).list().items():
        click.echo(f"{name:>16}: {', '.join(entry['scopes'])}")


@main.command()
@click.option("--client", "client_name", required=True,
              help="Client name this MCP server represents (must be registered).")
@click.pass_obj
def mcp(data_dir: str | None, client_name: str) -> None:
    """Run the MCP server (stdio) — plugs engram into Claude Code, Claude
    Desktop, Cursor, or any MCP client. Starts the daemon if needed."""
    from engram.mcp_server import run_mcp

    run_mcp(_config(data_dir), client_name)


if __name__ == "__main__":
    main()

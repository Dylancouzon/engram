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
@click.option("--shard", default="private")
@click.pass_obj
def seed(data_dir: str | None, paths: tuple[Path, ...], scope: str, shard: str) -> None:
    """Seed memories from markdown files or directories (CLAUDE.md, notes,
    an Obsidian vault, a folder of pasted ChatGPT/Claude memories). Each
    paragraph or bullet goes through the full write pipeline."""
    if not paths:
        raise click.ClickException("give at least one file or directory to seed from")
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.md") if p.is_file()))
        else:
            files.append(path)
    if not files:
        raise click.ClickException("no markdown files found")
    chunks: list[tuple[str, str]] = []
    for path in files:
        for chunk in _split_markdown(path.read_text(errors="replace")):
            chunks.append((chunk, str(path)))
    click.echo(f"seeding {len(chunks)} chunks from {len(files)} file(s)...")
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
@click.option("--install", is_flag=True,
              help="Install as a launchd agent (macOS) so the daemon starts"
                   " at login, then exit.")
@click.pass_obj
def daemon(data_dir: str | None, install: bool) -> None:
    """Run the engram daemon: the single owner of your memory, serving the
    local API every other surface (CLI, MCP, importers) talks to."""
    from engram.daemon import run_daemon

    cfg = _config(data_dir)
    if install:
        _install_launchd(cfg)
        return
    click.echo(f"engram daemon starting on {cfg.socket_path}")
    try:
        run_daemon(cfg)
    except StoreLockedError as e:
        raise click.ClickException(str(e)) from e


def _install_launchd(cfg: Config) -> None:
    import plistlib
    import shutil as _shutil
    import subprocess
    import sys as _sys

    if _sys.platform != "darwin":
        raise click.ClickException("--install supports macOS (launchd) for now;"
                                   " use a systemd user unit on Linux")
    binary = _shutil.which("engram")
    args = ([binary, "daemon"] if binary
            else [_sys.executable, "-m", "engram.cli", "daemon"])
    plist = {
        "Label": "tech.qdrant.engram",
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"ENGRAM_HOME": str(cfg.data_dir)},
        "StandardErrorPath": str(cfg.data_dir / "daemon.log"),
        "StandardOutPath": str(cfg.data_dir / "daemon.log"),
    }
    dest = Path.home() / "Library" / "LaunchAgents" / "tech.qdrant.engram.plist"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(dest)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise click.ClickException(f"launchctl load failed: {result.stderr.strip()}")
    click.echo(f"installed and started: {dest}")
    click.echo("uninstall with: launchctl unload -w "
               f"{dest} && rm {dest}")


@main.group()
def clients() -> None:
    """Which apps may read or write which scopes (default-deny)."""


@clients.command("allow")
@click.argument("name")
@click.option("--scopes", default="*",
              help="Comma-separated scope allowlist, or * for everything.")
@click.option("--token", "with_token", is_flag=True,
              help="Require a capability token (printed once, store it in the"
                   " client's config as ENGRAM_TOKEN).")
@click.option("--methods", default=None,
              help="Comma-separated method grants (e.g. recall,remember)."
                   " Default: all methods.")
@click.pass_obj
def clients_allow(data_dir: str | None, name: str, scopes: str,
                  with_token: bool, methods: str | None) -> None:
    """Register a client (e.g. claude-code, cursor) and grant it scopes."""
    from engram.daemon import ClientRegistry

    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    method_list = ([m.strip() for m in methods.split(",") if m.strip()]
                   if methods else None)
    token = ClientRegistry(_config(data_dir)).allow(
        name, scope_list, token=with_token, methods=method_list)
    click.echo(f"{name}: allowed scopes {', '.join(scope_list)}"
               + (f", methods {', '.join(method_list)}" if method_list else ""))
    if token:
        click.echo(click.style(f"capability token (shown once): {token}", fg="yellow"))


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
@click.option("-o", "--output", required=True, type=click.Path(path_type=Path))
@click.option("--passphrase", default=None,
              help="Encryption passphrase (prompted if omitted).")
@click.option("--no-encrypt", is_flag=True,
              help="Write an unencrypted snapshot (not recommended off-device).")
@click.pass_obj
def snapshot(data_dir: str | None, output: Path, passphrase: str | None,
             no_encrypt: bool) -> None:
    """Back up your memory folder to one portable file (encrypted by
    default). Restore anywhere with `engram restore`."""
    if not no_encrypt and passphrase is None:
        passphrase = click.prompt("snapshot passphrase", hide_input=True,
                                  confirmation_prompt=True)
    with _open_surface(data_dir) as store:
        if isinstance(store, Client):
            size = store.snapshot(str(output.resolve()),
                                  None if no_encrypt else passphrase)
        else:
            size = store.snapshot(output, None if no_encrypt else passphrase)
    enc = "unencrypted" if no_encrypt else "encrypted"
    click.echo(f"wrote {enc} snapshot: {output} ({size / 1024:.0f} KiB)")


@main.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option("--passphrase", default=None)
@click.pass_obj
def restore(data_dir: str | None, source: Path, passphrase: str | None) -> None:
    """Restore a snapshot into an empty memory folder, then verify it."""
    from engram.archive import restore_snapshot

    cfg = _config(data_dir)
    try:
        restore_snapshot(cfg, source,
                         passphrase or click.prompt("snapshot passphrase",
                                                    hide_input=True, default="",
                                                    show_default=False) or None)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    with _open_store(data_dir) as store:
        info = store.stats()
    click.echo(f"restored: {info['points']} memories across "
               f"{len(info.get('shards', {}))} shard(s).")


@main.command()
@click.option("--budget", type=int, default=50, help="Max operations this run.")
@click.pass_obj
def consolidate(data_dir: str | None, budget: int) -> None:
    """Housekeeping now instead of waiting for the daemon's idle run:
    prune stale episodes, dedup, summarize old episodes into facts."""
    with _open_surface(data_dir) as store:
        report = store.consolidate(budget=budget)
    click.echo(", ".join(f"{k} {v}" for k, v in report.items()) or "nothing to do")


@main.group()
def hook() -> None:
    """Proactive recall triggers for assistant surfaces."""


@hook.command("session-start")
@click.option("--scope", default=None)
@click.option("-k", type=int, default=5)
@click.pass_obj
def hook_session_start(data_dir: str | None, scope: str | None, k: int) -> None:
    """Claude Code SessionStart hook: surface memories relevant to the
    project being opened. Reads the hook payload on stdin, prints a context
    block on stdout. Speculative recall: never reinforces."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    cwd = Path(payload.get("cwd") or Path.cwd())
    query = f"project {cwd.name} preferences conventions decisions corrections"
    with _open_surface(data_dir) as store:
        hits = store.recall(query, k=k, scope=scope, reinforce=False)
        journal = getattr(store, "journal", None)
        if journal is not None:  # library mode; daemon logs server-side later
            journal.log_event("session-start-recall", hits=len(hits))
    if not hits:
        return
    click.echo(f"## Relevant long-term memories (engram, project {cwd.name})")
    for h in hits:
        click.echo(f"- {h.memory.text}")
    click.echo("\n(Use the engram MCP tools to recall more or remember new facts.)")


@hook.command("user-prompt")
@click.option("--scope", default=None)
@click.option("-k", type=int, default=4)
@click.option("--min-score", type=float, default=0.5,
              help="Minimum absolute similarity to inject (noise gate).")
@click.pass_obj
def hook_user_prompt(data_dir: str | None, scope: str | None, k: int,
                     min_score: float) -> None:
    """Claude Code UserPromptSubmit hook: recall against the prompt itself,
    inject only confident hits. Deterministic recall-at-the-right-moment —
    the model never has to remember to ask."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    prompt = (payload.get("prompt") or "").strip()
    if len(prompt) < 12:  # nothing to match against
        return
    with _open_surface(data_dir) as store:
        hits = [h for h in store.recall(prompt, k=k, scope=scope, reinforce=False)
                if h.similarity >= min_score]
        journal = getattr(store, "journal", None)
        if journal is not None:
            journal.log_event("prompt-recall", hits=len(hits))
    if not hits:
        return
    click.echo("<engram-memories>")
    for h in hits:
        click.echo(f"- {h.memory.text}")
    click.echo("</engram-memories>")


@hook.command("capture")
@click.option("--scope", default="default")
@click.option("--max-chars", type=int, default=8000)
@click.pass_obj
def hook_capture(data_dir: str | None, scope: str, max_chars: int) -> None:
    """Claude Code Stop hook: harvest memories from the conversation that
    just ended. Runs the tail of the transcript through the full write
    pipeline; extraction's salience gate + dedup keep it clean. No-ops
    without a local extraction model (verbatim transcripts are not memories)."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        return
    texts: list[str] = []
    for line in Path(transcript_path).read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        if message.get("role") != "user":
            continue  # the user's words carry the facts worth keeping
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    tail = "\n".join(t for t in texts if t and not t.startswith("<"))[-max_chars:]
    if len(tail) < 40:
        return
    with _open_surface(data_dir) as store:
        llm = getattr(store, "llm", None)
        if (llm is None or not llm.available()) and isinstance(store, MemoryStore):
            return  # library mode without a model: skip, never store blobs
        actions = store.remember(tail, scope=scope, surface="auto-capture",
                                 source_ref=str(transcript_path))
        journal = getattr(store, "journal", None)
        if journal is not None:
            journal.log_event("auto-capture", hits=len(actions))


@hook.command("install")
@click.argument("surface", type=click.Choice(["claude-code"]))
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.pass_obj
def hook_install(data_dir: str | None, surface: str, yes: bool) -> None:
    """Wire the hooks into ~/.claude/settings.json (backs it up first)."""
    settings_path = Path.home() / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text() or "{}")
    hooks = settings.setdefault("hooks", {})
    wanted = {
        "SessionStart": "engram hook session-start",
        "UserPromptSubmit": "engram hook user-prompt",
        "Stop": "engram hook capture",
        # Long sessions compact before they Stop; capture there too so a
        # day-long session's facts aren't lost to the context window.
        "PreCompact": "engram hook capture",
    }
    added = []
    for event, command in wanted.items():
        matchers = hooks.setdefault(event, [])
        flat = json.dumps(matchers)
        if command not in flat:
            matchers.append({"hooks": [{"type": "command", "command": command}]})
            added.append(event)
    if not added:
        click.echo("all engram hooks already installed.")
        _offer_daemon(data_dir, yes)  # hooks may be set but the daemon may not
        return
    click.echo(f"will add hooks to {settings_path}: {', '.join(added)}")
    if not yes and not click.confirm("proceed?"):
        return
    if settings_path.exists():
        backup = settings_path.with_suffix(".json.engram-backup")
        backup.write_text(settings_path.read_text())
        click.echo(f"backed up existing settings to {backup}")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    click.echo("installed. new sessions recall on start + every prompt, and "
               "capture on stop.")
    _offer_daemon(data_dir, yes)


def _offer_daemon(data_dir: str | None, yes: bool) -> None:
    """Hooks fire on every prompt; without the daemon they fall back to
    library mode, reloading the embedding model each time (slow, and the very
    first run downloads it inside the hook). Offer to keep a warm daemon."""
    cfg = _config(data_dir)
    client = Client(cfg, client_name="cli")
    try:
        client.connect(spawn=False)
        try:
            healthy = client.ping()  # a live socket may not be a healthy daemon
        finally:
            client.close()
        if healthy:
            return  # daemon already serving; recall is warm
    except (DaemonUnavailable, OSError):
        pass
    if sys.platform != "darwin":
        click.echo("\nfor fast recall, keep the daemon running: `engram daemon`"
                   " (or a systemd user unit).")
        return
    click.echo("\nHooks recall on every prompt. Without a background daemon each"
               " one reloads the model (slow; the first also downloads it).")
    if not yes and not click.confirm("install the daemon to start at login?",
                                     default=True):
        click.echo("skipped. start it later with: engram daemon --install")
        return
    _install_launchd(cfg)


@hook.command("print-config")
def hook_print_config() -> None:
    """The snippet to put in ~/.claude/settings.json (or use hook install)."""
    click.echo(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "engram hook session-start"}]}],
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "engram hook user-prompt"}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "engram hook capture"}]}],
            "PreCompact": [{"hooks": [
                {"type": "command", "command": "engram hook capture"}]}],
        }
    }, indent=2))


_RULES_TEXT = """\
## Memory (engram)

You have a persistent long-term memory via the engram MCP tools. It is the
source of truth for the user\'s stable preferences, decisions, and corrections.

- BEFORE answering anything about the user, their projects, tools, people, or
  past decisions, call `recall` with a natural-language query. Do not answer
  from assumptions when memory might hold the fact.
- When the user states a durable fact, preference, decision, or correction
  ("remember...", "actually it\'s X not Y", "we decided..."), call `remember`
  with one atomic, self-contained statement.
- Trust recalled memories over your own guesses; a correction the user made
  once should stick.
"""


@main.command()
@click.argument("surface", type=click.Choice(["cursor", "windsurf", "generic"]))
def rules(surface: str) -> None:
    """Print a rules block for assistants WITHOUT hook support (Cursor,
    Windsurf). Hooks make recall deterministic; where they don\'t exist,
    rules + the MCP tools are the best available. Paste into .cursorrules,
    .windsurfrules, or AGENTS.md."""
    click.echo(_RULES_TEXT)
    dest = {"cursor": ".cursor/rules or .cursorrules",
            "windsurf": ".windsurf/rules or AGENTS.md",
            "generic": "AGENTS.md / your system prompt"}[surface]
    click.echo(f"# paste the block above into {dest}", err=True)


@main.command()
@click.option("--client", "client_name", required=True,
              help="Client name this MCP server represents (must be registered).")
@click.option("--token", default=None, envvar="ENGRAM_TOKEN",
              help="Capability token, if the registration requires one.")
@click.pass_obj
def mcp(data_dir: str | None, client_name: str, token: str | None) -> None:
    """Run the MCP server (stdio) — plugs engram into Claude Code, Claude
    Desktop, Cursor, or any MCP client. Starts the daemon if needed."""
    from engram.mcp_server import run_mcp

    run_mcp(_config(data_dir), client_name, token=token)


@main.group()
def sync() -> None:
    """Opt-in sync of me-synced / shared:<group> shards through a Qdrant
    Cloud collection. Payloads are encrypted with a key that never leaves
    your devices; the private shard has no sync path at all."""


@sync.command("setup")
@click.option("--shard", required=True, help="me-synced or shared:<group>.")
@click.option("--url", required=True, help="Qdrant Cloud cluster URL.")
@click.option("--api-key", default=None, envvar="QDRANT_API_KEY")
@click.option("--collection", default=None,
              help="Collection name (default engram-<shard>).")
@click.pass_obj
def sync_setup(data_dir: str | None, shard: str, url: str,
               api_key: str | None, collection: str | None) -> None:
    """Point a shard at its relay collection and mint the local key."""
    from engram.sync import SyncError, SyncTarget, save_target, sync_key

    cfg = _config(data_dir)
    try:
        save_target(cfg, SyncTarget(
            shard=shard, url=url, api_key=api_key,
            collection=collection or f"engram-{shard.replace(':', '-')}"))
    except (SyncError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    sync_key(cfg)
    click.echo(f"{shard}: sync target saved.")
    click.echo(f"copy {cfg.data_dir / 'sync.key'} to your other devices "
               "(it never syncs itself).")


@sync.command("now")
@click.option("--shard", default=None, help="One shard (default: all configured).")
@click.pass_obj
def sync_now(data_dir: str | None, shard: str | None) -> None:
    """Push local changes, pull the union, merge (LWW + tombstones)."""
    from engram.sync import SyncError, load_targets, sync_shard

    cfg = _config(data_dir)
    shards = [shard] if shard else list(load_targets(cfg))
    if not shards:
        raise click.ClickException("no shards configured; run engram sync setup")
    with _open_surface(data_dir) as store:
        for name in shards:
            try:
                report = (store.sync(name) if isinstance(store, Client)
                          else sync_shard(store, name))
            except SyncError as e:
                raise click.ClickException(str(e)) from e
            summary = ", ".join(f"{k} {v}" for k, v in report.items())
            click.echo(f"{name}: {summary}")


if __name__ == "__main__":
    main()

"""engram CLI — library-mode surface for M0.

Every command opens the store as the sole writer (exclusive lockfile),
does its work, and closes cleanly. The daemon takes over this seat in M1;
the CLI then becomes a thin client.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import uuid
from pathlib import Path

import click

from engram.config import Config
from engram.models import VALID_FOREVER, Memory, MemoryType, Op
from engram.store import MemoryStore, StoreLockedError, WriteRefusedError

_OP_STYLES = {
    Op.ADD: ("added", "green"),
    Op.UPDATE: ("updated", "yellow"),
    Op.SUPERSEDE: ("superseded", "magenta"),
    Op.NOOP: ("already known", "blue"),
}


def _open_store(data_dir: str | None) -> MemoryStore:
    try:
        return MemoryStore(Config.load(Path(data_dir) if data_dir else None))
    except StoreLockedError as e:
        raise click.ClickException(str(e)) from e


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
@click.pass_obj
def remember(data_dir: str | None, text: str, mtype: str | None, tags: str | None,
             scope: str, importance: float | None, source_ref: str | None) -> None:
    """Store something worth keeping. Conflicts with existing memories are
    resolved on write: corrections supersede, refinements update."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    with _open_store(data_dir) as store:
        try:
            actions = store.remember(
                text,
                type=MemoryType(mtype) if mtype else None,
                tags=tag_list,
                scope=scope,
                importance=importance,
                source_ref=source_ref,
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
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_obj
def recall(data_dir: str | None, query: str, k: int | None, scope: str | None,
           mtype: str | None, tags: str | None, as_json: bool) -> None:
    """Surface the memories relevant to a query (hybrid search, decayed by
    recency, weighted by importance)."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    with _open_store(data_dir) as store:
        hits = store.recall(
            query, k=k, scope=scope,
            type=MemoryType(mtype) if mtype else None, tags=tag_list,
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
    with _open_store(data_dir) as store:
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
        click.echo(click.style("purged — index, journal and future exports.", fg="red"))
    else:
        click.echo("invalidated — no longer recalled (history preserved).")


def _resolve_target(store: MemoryStore, target: str) -> Memory | None:
    if re.fullmatch(r"[0-9a-fA-F-]{4,36}", target):
        try:
            uuid.UUID(target)
            found = store.backend.retrieve([target])
            if found:
                return found[0].memory()
        except ValueError:
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
    with _open_store(data_dir) as store:
        n = store.export_jsonl(output)
    click.echo(f"exported {n} journal entries.", err=True)


@main.command(name="import")
@click.argument("source", type=click.File("r"))
@click.pass_obj
def import_(data_dir: str | None, source) -> None:
    """Replay a JSONL export into this store (restore / migration)."""
    with _open_store(data_dir) as store:
        n = store.journal.import_jsonl(source)
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
    with _open_store(data_dir) as store:
        info = store.stats()
    for key, value in info.items():
        click.echo(f"{key:>16}: {value}")


@main.command()
@click.pass_obj
def rebuild(data_dir: str | None) -> None:
    """Re-index everything from the journal (after model changes or to
    verify the journal really is the source of truth)."""
    import shutil

    cfg = Config.load(Path(data_dir) if data_dir else None)
    if cfg.shard_dir.exists():
        shutil.rmtree(cfg.shard_dir)
    with _open_store(data_dir) as store:
        applied = store.rebuild()
    click.echo(f"rebuilt index from journal: {applied} operations replayed.")


if __name__ == "__main__":
    main()

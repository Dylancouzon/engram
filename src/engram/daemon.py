"""The engram daemon: the single long-lived owner of the shard.

Every surface (MCP server, CLI, importers) is a thin client of this process
over the versioned local API (protocol.py) on a 0600 Unix socket. Writes
serialize through the store's write lock; reads run concurrently; access
bumps buffer and drain on idle, so reads never write.

Auth model (M1): the socket is owner-only, so the OS already excludes other
users. Client-name registration + per-scope allowlists (default-deny for
anything that isn't the CLI) are consent bookkeeping — which app may touch
which part of your memory. Capability tokens land with shared pools (M3).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socketserver
import sys
import threading
import time as _time
from time import monotonic as _monotonic
from typing import Any

from engram.config import Config
from engram.models import MemoryType
from engram.protocol import (
    E_BAD_REQUEST,
    E_INTERNAL,
    E_NOT_FOUND,
    E_REFUSED,
    E_SCOPE_DENIED,
    E_UNREGISTERED,
    E_UNSUPPORTED,
    PROTOCOL_VERSION,
    ProtocolError,
    action_to_wire,
    error_response,
    hit_to_wire,
    memory_to_wire,
    ok_response,
    read_message,
    review_to_wire,
    write_message,
)
from engram.store import MemoryStore, WriteRefusedError

REINFORCE_FLUSH_SECONDS = 30.0
IDLE_FOR_CONSOLIDATION = 600.0  # no requests for 10 min
CONSOLIDATE_EVERY = 86400.0  # at most daily

# The CLI is the owner's own hands: implicitly registered, all scopes.
_IMPLICIT_CLIENTS = {"cli": ["*"]}


class ClientRegistry:
    """clients.json: {"claude-code": {"scopes": ["*"], ...}}. Default-deny —
    an unknown client gets a registration hint, not data."""

    def __init__(self, config: Config):
        self._path = config.clients_path

    def _load(self) -> dict[str, dict]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def scopes_for(self, client: str) -> list[str] | None:
        entry = self.entry_for(client)
        return list(entry.get("scopes", [])) if entry else None

    def entry_for(self, client: str) -> dict | None:
        if client in _IMPLICIT_CLIENTS:
            return {"scopes": _IMPLICIT_CLIENTS[client]}
        return self._load().get(client)

    def allow(self, client: str, scopes: list[str], token: bool = False) -> str | None:
        """Register a client. With token=True a capability token is
        generated and its hash stored; the plaintext is returned ONCE and
        never persisted."""
        if not scopes:
            raise ValueError("scope allowlist cannot be empty (use '*' for everything)")
        import hashlib
        import secrets

        entry: dict = {"scopes": scopes}
        plaintext: str | None = None
        if token:
            plaintext = "egt_" + secrets.token_urlsafe(24)
            entry["token_hash"] = hashlib.sha256(plaintext.encode()).hexdigest()
        data = self._load()
        data[client] = entry
        self._write(data)
        return plaintext

    def revoke(self, client: str) -> bool:
        data = self._load()
        if client not in data:
            return False
        del data[client]
        self._write(data)
        return True

    def _write(self, data: dict) -> None:
        # Atomic replace: a reader never sees a partially-written registry.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def list(self) -> dict[str, dict]:
        return {**{c: {"scopes": s} for c, s in _IMPLICIT_CLIENTS.items()}, **self._load()}


def _check_scope(scopes: list[str], scope: str, code: str = E_SCOPE_DENIED) -> None:
    if "*" not in scopes and scope not in scopes:
        raise ProtocolError(code, f"client is not allowed scope {scope!r}")


class Daemon:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.registry = ClientRegistry(store.config)
        self._stop = threading.Event()
        self._inflight = 0
        self._inflight_cond = threading.Condition()
        self._last_request = 0.0

    def _track_request(self):
        @contextlib.contextmanager
        def tracked():
            with self._inflight_cond:
                self._inflight += 1
            try:
                yield
            finally:
                with self._inflight_cond:
                    self._inflight -= 1
                    self._inflight_cond.notify_all()

        return tracked()

    def _drain(self, timeout: float = 10.0) -> None:
        """Wait for in-flight requests before closing the store. Stragglers
        past the timeout get errors — safe, because durability lives in the
        journal ack, not the connection."""
        deadline = _monotonic() + timeout
        with self._inflight_cond:
            while self._inflight and _monotonic() < deadline:
                self._inflight_cond.wait(timeout=0.5)

    # -- request dispatch ------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        try:
            if message.get("v") != PROTOCOL_VERSION:
                raise ProtocolError(
                    E_UNSUPPORTED,
                    f"daemon speaks protocol v{PROTOCOL_VERSION}, got {message.get('v')!r}",
                )
            client = message.get("client")
            if not isinstance(client, str) or not client:
                raise ProtocolError(E_BAD_REQUEST, "missing client name")
            entry = self.registry.entry_for(client)
            if entry is None:
                raise ProtocolError(
                    E_UNREGISTERED,
                    f"client {client!r} is not registered; run: "
                    f"engram clients allow {client} --scopes <scopes|*>",
                )
            scopes = list(entry.get("scopes", []))
            if entry.get("token_hash"):
                import hashlib
                import hmac as hmac_mod

                supplied = message.get("token")
                if (not isinstance(supplied, str)
                        or not hmac_mod.compare_digest(
                            hashlib.sha256(supplied.encode()).hexdigest(),
                            entry["token_hash"])):
                    raise ProtocolError(
                        E_UNREGISTERED,
                        f"client {client!r} requires a valid capability token",
                    )
            method = message.get("method")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ProtocolError(E_BAD_REQUEST, "params must be an object")
            handler = getattr(self, f"_m_{method}", None)
            if handler is None:
                raise ProtocolError(E_BAD_REQUEST, f"unknown method {method!r}")
            return ok_response(request_id, handler(params, scopes, client))
        except ProtocolError as e:
            return error_response(request_id, e.code, str(e))
        except WriteRefusedError as e:
            return error_response(request_id, E_REFUSED, str(e))
        except (KeyError, TypeError, ValueError) as e:
            # Missing/mistyped params are the client's fault; keep the code
            # stable so clients can distinguish their bugs from ours.
            return error_response(request_id, E_BAD_REQUEST, f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - the daemon must not die on a bad request
            return error_response(request_id, E_INTERNAL, f"{type(e).__name__}: {e}")

    # -- methods (the versioned API) ---------------------------------------------

    def _m_ping(self, params: dict, scopes: list[str], client: str) -> dict:
        return {"pong": True, "protocol": PROTOCOL_VERSION}

    def _m_remember(self, params: dict, scopes: list[str], client: str) -> dict:
        scope = params.get("scope") or "default"
        _check_scope(scopes, scope)
        mtype = params.get("type")
        actions = self.store.remember(
            text=str(params["text"]),
            type=MemoryType(mtype) if mtype else None,
            tags=params.get("tags"),
            scope=scope,
            importance=params.get("importance"),
            surface=client,
            source_ref=params.get("source_ref"),
            shard=params.get("shard") or "private",
        )
        return {"actions": [action_to_wire(a) for a in actions]}

    def _m_recall(self, params: dict, scopes: list[str], client: str) -> dict:
        if not scopes:
            raise ProtocolError(E_SCOPE_DENIED, "client has an empty scope allowlist")
        scope = params.get("scope")
        if scope:
            _check_scope(scopes, scope)
        elif "*" not in scopes:
            # No scope requested: the client sees only what it's allowed.
            scope = scopes  # build_filter accepts a list
        mtype = params.get("type")
        hits = self.store.recall(
            query=str(params["query"]),
            k=params.get("k"),
            scope=scope,
            type=MemoryType(mtype) if mtype else None,
            tags=params.get("tags"),
            as_of=params.get("as_of"),
            reinforce=bool(params.get("reinforce", True)),
            shard=params.get("shard"),
        )
        return {"hits": [hit_to_wire(h) for h in hits]}

    def _m_forget(self, params: dict, scopes: list[str], client: str) -> dict:
        memory_id = str(params["id"])
        memory = self.store.get(memory_id)
        if memory is None:
            raise ProtocolError(E_NOT_FOUND, f"no memory {memory_id}")
        _check_scope(scopes, memory.scope)
        mode = params.get("mode", "soft")
        forgotten = self.store.forget(memory_id, mode=mode)
        return {"forgotten": forgotten is not None, "mode": mode}

    def _m_get(self, params: dict, scopes: list[str], client: str) -> dict:
        import uuid as _uuid

        ref = str(params["id"])
        try:
            _uuid.UUID(ref)
            memory = self.store.get(ref)
        except ValueError:
            # A short id prefix, as the CLI prints. Edge raises on non-UUID
            # ids, so never pass a prefix to retrieve.
            memory = self.store.find_by_prefix(ref)
        if memory is None:
            raise ProtocolError(E_NOT_FOUND, f"no memory {params['id']}")
        _check_scope(scopes, memory.scope)
        return {"memory": memory_to_wire(memory)}

    def _m_list(self, params: dict, scopes: list[str], client: str) -> dict:
        scope = params.get("scope")
        if scope:
            _check_scope(scopes, scope)
        elif "*" not in scopes:
            scope = scopes  # the client browses only what it's allowed
        mtype = params.get("type")
        memories = self.store.list(
            scope=scope,
            type=MemoryType(mtype) if mtype else None,
            shard=params.get("shard"),
            include_invalid=bool(params.get("include_invalid", False)),
            limit=params.get("limit"),
        )
        return {"memories": [memory_to_wire(m) for m in memories]}

    def _m_stats(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*")  # shard names/counts are owner information
        return self.store.stats()

    def _m_map_points(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*")  # a projection over every memory is owner-wide
        return {"points": self.store.map_points(int(params.get("neighbors", 3)))}

    def _m_edit(self, params: dict, scopes: list[str], client: str) -> dict:
        current = self.store.get(str(params["id"]))
        if current is None:
            raise ProtocolError(E_NOT_FOUND, f"no memory {params['id']}")
        new_scope = params.get("scope")

        def authorize(located):  # runs under the write lock, on the current state
            _check_scope(scopes, located.scope)  # must be allowed the source scope
            if new_scope is not None:
                _check_scope(scopes, new_scope)  # ...and the destination on a move

        updated = self.store.edit_metadata(
            current.id, scope=new_scope, tags=params.get("tags"),
            importance=params.get("importance"), authorize=authorize,
        )
        if updated is None:
            raise ProtocolError(E_NOT_FOUND, f"no memory {params['id']}")
        return {"memory": memory_to_wire(updated)}

    def _m_reviews(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*")  # owner decision, full access only
        return {"reviews": [review_to_wire(i) for i in self.store.pending_reviews()]}

    def _m_resolve_review(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*")
        item = self.store.resolve_review(int(params["seq"]), bool(params["accept"]))
        if item is None:
            raise ProtocolError(E_NOT_FOUND, f"no pending review {params['seq']}")
        return {"resolved": True, "accepted": bool(params["accept"])}

    def _m_export(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*", code=E_SCOPE_DENIED)  # full-store op: * only
        import io

        buf = io.StringIO()
        n = self.store.export_jsonl(buf)
        return {"jsonl": buf.getvalue(), "entries": n}

    def _m_consolidate(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*")
        return self.store.consolidate(stop=self._stop)  # cancellable on shutdown

    def _m_log_event(self, params: dict, scopes: list[str], client: str) -> dict:
        detail = params.get("detail")
        self.store.log_event(str(params["kind"]), int(params.get("hits", 0)),
                             str(detail) if detail is not None else None)
        return {"logged": True}

    def _m_events(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*")  # trigger history is owner information
        return {"events": self.store.recent_events(int(params.get("limit", 50)))}

    def _m_snapshot(self, params: dict, scopes: list[str], client: str) -> dict:
        from pathlib import Path

        _check_scope(scopes, "*")
        size = self.store.snapshot(Path(params["path"]), params.get("passphrase"))
        return {"bytes": size, "path": params["path"]}

    def _m_sync(self, params: dict, scopes: list[str], client: str) -> dict:
        from engram.sync import sync_shard

        _check_scope(scopes, "*")
        return sync_shard(self.store, str(params["shard"]))

    # -- server loop --------------------------------------------------------------

    def serve(self, ready: threading.Event | None = None,
              install_signals: bool = True) -> None:
        cfg = self.store.config
        sock_path = cfg.socket_path
        if sock_path.exists():
            # The store's flock guarantees we're the only daemon; anything
            # at this path is a stale socket from an unclean shutdown.
            sock_path.unlink()

        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            # A stuck or idle client releases its thread instead of holding
            # it forever; clients reconnect transparently.
            timeout = 600.0

            def handle(self) -> None:
                while not daemon._stop.is_set():
                    try:
                        message = read_message(self.rfile)
                    except ProtocolError as e:
                        # Same guard as the response write below: a client that
                        # already hung up must not raise in the handler thread.
                        with contextlib.suppress(ConnectionError, BrokenPipeError):
                            write_message(self.wfile, error_response(None, e.code, str(e)))
                        return
                    except (ConnectionError, ValueError, TimeoutError, OSError):
                        return
                    if message is None:
                        return
                    daemon._last_request = _monotonic()
                    with daemon._track_request():
                        response = daemon.handle(message)
                    try:
                        write_message(self.wfile, response)
                    except (ConnectionError, BrokenPipeError):
                        return

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            request_queue_size = 64  # default 5 refuses concurrent clients

        with Server(str(sock_path), Handler) as server:
            os.chmod(sock_path, 0o600)
            self._server = server
            flusher = threading.Thread(target=self._flush_loop, daemon=True)
            flusher.start()

            def shutdown(*_: object) -> None:
                self.stop()

            if install_signals:  # only possible from the main thread
                signal.signal(signal.SIGTERM, shutdown)
                signal.signal(signal.SIGINT, shutdown)
            if ready is not None:
                ready.set()
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                self._stop.set()
                sock_path.unlink(missing_ok=True)
                # Let the flusher notice _stop and finish its current
                # flush/consolidate before we close the store — otherwise a
                # phase-3 consolidation could touch the journal after close()
                # shut it. The flusher is idle-waiting in the common case, so
                # this returns at once; the timeout bounds a stuck model call.
                flusher.join(timeout=5.0)
                self._drain()
                self.store.close()  # flushes buffered reinforcements too

    def stop(self) -> None:
        self._stop.set()
        if getattr(self, "_server", None) is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _flush_loop(self) -> None:
        from engram.consolidate import last_run

        while not self._stop.wait(REINFORCE_FLUSH_SECONDS):
            # The flusher must stay alive across a failure, but a swallowed one
            # is a silent durability/housekeeping freeze — log it to stderr
            # (the daemon's own log, never a hook's stdout) so it's visible.
            try:
                self.store.flush_reinforce()
            except Exception as e:  # noqa: BLE001 - keep the flusher alive
                print(f"engram daemon: flush_reinforce failed: {e!r}",
                      file=sys.stderr, flush=True)
            try:
                idle = _monotonic() - self._last_request
                if (self._last_request and idle >= IDLE_FOR_CONSOLIDATION
                        and _time.time() - last_run(self.store) >= CONSOLIDATE_EVERY):
                    self.store.consolidate(stop=self._stop)
            except Exception as e:  # noqa: BLE001 - keep the flusher alive
                print(f"engram daemon: consolidation failed: {e!r}",
                      file=sys.stderr, flush=True)


def run_daemon(config: Config | None = None) -> None:
    store = MemoryStore(config, reinforce_mode="buffered")
    Daemon(store).serve()

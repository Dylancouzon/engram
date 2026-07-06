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

import json
import os
import signal
import socketserver
import threading
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
    ok_response,
    read_message,
    write_message,
)
from engram.store import MemoryStore, WriteRefusedError

REINFORCE_FLUSH_SECONDS = 30.0

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
        if client in _IMPLICIT_CLIENTS:
            return _IMPLICIT_CLIENTS[client]
        entry = self._load().get(client)
        return list(entry.get("scopes", [])) if entry else None

    def allow(self, client: str, scopes: list[str]) -> None:
        data = self._load()
        data[client] = {"scopes": scopes}
        self._path.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(self._path, 0o600)

    def revoke(self, client: str) -> bool:
        data = self._load()
        if client not in data:
            return False
        del data[client]
        self._path.write_text(json.dumps(data, indent=2) + "\n")
        return True

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
            scopes = self.registry.scopes_for(client)
            if scopes is None:
                raise ProtocolError(
                    E_UNREGISTERED,
                    f"client {client!r} is not registered; run: "
                    f"engram clients allow {client} --scopes <scopes|*>",
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
        )
        return {"actions": [action_to_wire(a) for a in actions]}

    def _m_recall(self, params: dict, scopes: list[str], client: str) -> dict:
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
        )
        return {"hits": [hit_to_wire(h) for h in hits]}

    def _m_forget(self, params: dict, scopes: list[str], client: str) -> dict:
        memory_id = str(params["id"])
        found = self.store.backend.retrieve([memory_id])
        if not found:
            raise ProtocolError(E_NOT_FOUND, f"no memory {memory_id}")
        _check_scope(scopes, found[0].payload.get("scope", "default"))
        memory = self.store.forget(memory_id, mode=params.get("mode", "soft"))
        return {"forgotten": memory is not None, "mode": params.get("mode", "soft")}

    def _m_get(self, params: dict, scopes: list[str], client: str) -> dict:
        from engram.protocol import memory_to_wire

        found = self.store.backend.retrieve([str(params["id"])])
        if not found:
            raise ProtocolError(E_NOT_FOUND, f"no memory {params['id']}")
        memory = found[0].memory()
        _check_scope(scopes, memory.scope)
        return {"memory": memory_to_wire(memory)}

    def _m_stats(self, params: dict, scopes: list[str], client: str) -> dict:
        return self.store.stats()

    def _m_export(self, params: dict, scopes: list[str], client: str) -> dict:
        _check_scope(scopes, "*", code=E_SCOPE_DENIED)  # full-store op: * only
        import io

        buf = io.StringIO()
        n = self.store.export_jsonl(buf)
        return {"jsonl": buf.getvalue(), "entries": n}

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
            def handle(self) -> None:
                while True:
                    try:
                        message = read_message(self.rfile)
                    except ProtocolError as e:
                        write_message(self.wfile, error_response(None, e.code, str(e)))
                        return
                    except (ConnectionError, ValueError):
                        return
                    if message is None:
                        return
                    try:
                        write_message(self.wfile, daemon.handle(message))
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
                self.store.close()  # flushes buffered reinforcements too

    def stop(self) -> None:
        self._stop.set()
        if getattr(self, "_server", None) is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _flush_loop(self) -> None:
        import contextlib

        while not self._stop.wait(REINFORCE_FLUSH_SECONDS):
            with contextlib.suppress(Exception):  # keep the flusher alive
                self.store.flush_reinforce()


def run_daemon(config: Config | None = None) -> None:
    store = MemoryStore(config, reinforce_mode="buffered")
    Daemon(store).serve()

"""Thin client of the daemon over the versioned local API.

Exposes the same surface as MemoryStore (remember/recall/forget/stats), so
the CLI and MCP server can hold either without caring which. If no daemon
is running, `connect()` can spawn one (detached) and wait for the socket.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from engram.config import Config
from engram.models import Memory, MemoryType, RecallHit
from engram.protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    action_from_wire,
    hit_from_wire,
    memory_from_wire,
    read_message,
    write_message,
)
from engram.store import WriteAction, WriteRefusedError

SPAWN_WAIT_SECONDS = 15.0  # first daemon start may load embedding models


class DaemonUnavailable(RuntimeError):
    pass


class Client:
    def __init__(self, config: Config, client_name: str):
        self.config = config
        self.client_name = client_name
        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None

    # -- connection ----------------------------------------------------------

    def connect(self, spawn: bool = False) -> Client:
        path = self.config.socket_path
        if not self._try_connect(path):
            if not spawn:
                raise DaemonUnavailable(f"no daemon at {path}")
            self._spawn_daemon()
            deadline = time.monotonic() + SPAWN_WAIT_SECONDS
            while time.monotonic() < deadline:
                if self._try_connect(path):
                    break
                time.sleep(0.2)
            else:
                raise DaemonUnavailable(
                    f"daemon did not come up at {path}; try `engram daemon` in a"
                    " terminal to see why"
                )
        return self

    def _try_connect(self, path: Path) -> bool:
        if not path.exists():
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(120.0)  # remember() with extraction can take a while
            sock.connect(str(path))
        except OSError:
            sock.close()
            return False
        self._sock = sock
        self._rfile = sock.makefile("rb")
        self._wfile = sock.makefile("wb")
        return True

    def _spawn_daemon(self) -> None:
        env = dict(os.environ, ENGRAM_HOME=str(self.config.data_dir))
        subprocess.Popen(
            [sys.executable, "-m", "engram.cli", "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survives this process exiting
            env=env,
        )

    def close(self) -> None:
        for f in (self._rfile, self._wfile, self._sock):
            if f is not None:
                f.close()
        self._sock = self._rfile = self._wfile = None

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        if self._sock is None:
            raise DaemonUnavailable("not connected")
        write_message(self._wfile, {
            "v": PROTOCOL_VERSION,
            "id": uuid.uuid4().hex[:8],
            "client": self.client_name,
            "method": method,
            "params": params,
        })
        response = read_message(self._rfile)
        if response is None:
            raise DaemonUnavailable("daemon closed the connection")
        if response.get("ok"):
            return response.get("result")
        error = response.get("error") or {}
        code, message = error.get("code", "internal"), error.get("message", "")
        if code == "write_refused":
            raise WriteRefusedError(message)
        raise ProtocolError(code, message)

    # -- MemoryStore-shaped surface ----------------------------------------------

    def remember(self, text: str, type: MemoryType | None = None,
                 tags: list[str] | None = None, scope: str = "default",
                 importance: float | None = None, surface: str | None = None,
                 source_ref: str | None = None) -> list[WriteAction]:
        result = self.call(
            "remember", text=text, type=type.value if type else None,
            tags=tags, scope=scope, importance=importance, source_ref=source_ref,
        )
        return [action_from_wire(a) for a in result["actions"]]

    def recall(self, query: str, k: int | None = None,
               scope: str | None = None, type: MemoryType | None = None,
               tags: list[str] | None = None, as_of: float | None = None,
               reinforce: bool = True) -> list[RecallHit]:
        result = self.call(
            "recall", query=query, k=k, scope=scope,
            type=type.value if type else None, tags=tags, as_of=as_of,
        )
        return [hit_from_wire(h) for h in result["hits"]]

    def forget(self, memory_id: str, mode: str = "soft") -> bool:
        return bool(self.call("forget", id=memory_id, mode=mode)["forgotten"])

    def get(self, memory_id: str) -> Memory | None:
        try:
            return memory_from_wire(self.call("get", id=memory_id)["memory"])
        except ProtocolError as e:
            if e.code == "not_found":
                return None
            raise

    def stats(self) -> dict:
        return dict(self.call("stats"))

    def export_jsonl(self) -> str:
        return self.call("export")["jsonl"]

    def ping(self) -> bool:
        try:
            return bool(self.call("ping").get("pong"))
        except (ProtocolError, DaemonUnavailable):
            return False

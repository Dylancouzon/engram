"""Daemon + client integration: the versioned local API, auth, scope
enforcement, buffered reinforcement, and concurrent writes."""

import threading

import pytest
from conftest import FakeEmbedder

from engram.client import Client, DaemonUnavailable
from engram.daemon import ClientRegistry, Daemon
from engram.models import Op
from engram.protocol import ProtocolError
from engram.store import MemoryStore, WriteRefusedError


@pytest.fixture
def daemon(config, tmp_path_factory):
    # pytest tmp dirs overflow the AF_UNIX sun_path limit (~104 bytes on
    # macOS) — the same reason Config grew socket_override.
    import tempfile
    from pathlib import Path

    config.socket_override = Path(tempfile.mkdtemp(prefix="eng", dir="/tmp")) / "d.sock"
    store = MemoryStore(config, embedder=FakeEmbedder(), llm=None,
                        reinforce_mode="buffered")
    d = Daemon(store)
    ready = threading.Event()
    thread = threading.Thread(
        target=d.serve, kwargs={"ready": ready, "install_signals": False}, daemon=True
    )
    thread.start()
    assert ready.wait(5.0), "daemon did not start"
    yield d
    d.stop()
    thread.join(timeout=5.0)


def _client(config, name: str) -> Client:
    return Client(config, client_name=name).connect(spawn=False)


def test_ping_and_socket_perms(config, daemon):
    with _client(config, "cli") as c:
        assert c.ping()
    assert (config.socket_path.stat().st_mode & 0o777) == 0o600


def test_cli_is_implicitly_trusted(config, daemon):
    with _client(config, "cli") as c:
        [action] = c.remember("Dylan's cat is named Miso", scope="personal")
        assert action.op is Op.ADD and action.memory is not None
        hits = c.recall("what is the cat called")
        assert hits and hits[0].memory.text == "Dylan's cat is named Miso"


def test_unregistered_client_denied_with_hint(config, daemon):
    with _client(config, "surprise-app") as c:
        with pytest.raises(ProtocolError) as exc:
            c.recall("anything")
        assert exc.value.code == "unregistered_client"
        assert "engram clients allow surprise-app" in str(exc.value)


def test_scope_allowlist_enforced(config, daemon):
    ClientRegistry(config).allow("workbot", ["work"])
    with _client(config, "cli") as c:
        c.remember("Dylan's cat is named Miso", scope="personal")
        c.remember("The deploy uses GitHub Actions", scope="work")

    with _client(config, "workbot") as c:
        # Writing outside the allowlist is refused.
        with pytest.raises(ProtocolError) as exc:
            c.remember("sneaky", scope="personal")
        assert exc.value.code == "scope_denied"
        # Unscoped recall is silently confined to allowed scopes.
        texts = [h.memory.text for h in c.recall("cat Miso deploy actions", k=10)]
        assert any("GitHub Actions" in t for t in texts)
        assert all("Miso" not in t for t in texts)
        # Forgetting a memory in a denied scope is refused too.
        with _client(config, "cli") as owner:
            personal_id = owner.recall("cat Miso")[0].memory.id
        with pytest.raises(ProtocolError) as exc:
            c.forget(personal_id)
        assert exc.value.code == "scope_denied"


def test_revoked_client_loses_access(config, daemon):
    registry = ClientRegistry(config)
    registry.allow("tempbot", ["*"])
    with _client(config, "tempbot") as c:
        assert c.ping()
        registry.revoke("tempbot")
        with pytest.raises(ProtocolError) as exc:
            c.recall("anything")
        assert exc.value.code == "unregistered_client"


def test_write_refusal_travels_the_wire(config, daemon):
    with _client(config, "cli") as c, pytest.raises(WriteRefusedError):
        c.remember("-----BEGIN EC PRIVATE KEY----- abc")


def test_protocol_version_mismatch(config, daemon):
    with _client(config, "cli") as c:
        from engram.protocol import read_message, write_message

        write_message(c._wfile, {"v": 99, "id": "x", "client": "cli",
                                 "method": "ping", "params": {}})
        response = read_message(c._rfile)
        assert response["ok"] is False
        assert response["error"]["code"] == "unsupported_version"


def test_reinforce_is_buffered_not_inline(config, daemon):
    with _client(config, "cli") as c:
        c.remember("Dylan drinks flat whites")
        c.recall("coffee flat whites")
        # The read did not write: access_count is still 0 until a flush.
        assert c.recall("coffee flat whites")[0].memory.access_count == 0
    flushed = daemon.store.flush_reinforce()
    assert flushed >= 2
    with _client(config, "cli") as c:
        assert c.recall("coffee flat whites")[0].memory.access_count >= 2


def test_concurrent_writes_serialize(config, daemon):
    errors: list[Exception] = []

    def write(n: int) -> None:
        try:
            with _client(config, "cli") as c:
                c.remember(f"parallel fact number {n} about topic {n}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)
    assert not errors
    assert daemon.store.backend.count() == 8


def test_export_requires_full_access(config, daemon):
    ClientRegistry(config).allow("narrow", ["work"])
    with _client(config, "narrow") as c:
        with pytest.raises(ProtocolError) as exc:
            c.export_jsonl()
        assert exc.value.code == "scope_denied"
    with _client(config, "cli") as c:
        c.remember("exportable fact")
        assert "exportable fact" in c.export_jsonl()


def test_no_daemon_raises_cleanly(config):
    with pytest.raises(DaemonUnavailable):
        Client(config, client_name="cli").connect(spawn=False)


def test_mcp_module_imports():
    import engram.mcp_server  # noqa: F401

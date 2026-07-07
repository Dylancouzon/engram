"""Regressions for the M1-spine Codex findings: the empty-allowlist leak,
reads racing hard-forget's shard swap, wire-level reinforce control, and
bad-request error mapping."""

import threading

import pytest
from conftest import make_store
from test_daemon import _client, daemon  # noqa: F401 - fixture reuse

from engram.backend.edge import build_filter
from engram.daemon import ClientRegistry
from engram.protocol import PROTOCOL_VERSION, ProtocolError, read_message, write_message


def test_empty_allowlist_never_means_no_filter(config, daemon):  # noqa: F811
    # Registry refuses to create the hazardous state...
    with pytest.raises(ValueError):
        ClientRegistry(config).allow("broken", [])
    # ...and if it exists anyway (hand-edited clients.json), recall denies.
    config.clients_path.write_text('{"broken": {"scopes": []}}\n')
    with _client(config, "cli") as c:
        c.remember("Dylan's cat is named Miso", scope="personal")
    with _client(config, "broken") as c:
        with pytest.raises(ProtocolError) as exc:
            c.recall("cat")
        assert exc.value.code == "scope_denied"


def test_build_filter_empty_scope_list_matches_nothing():
    flt = build_filter(scope=[])
    assert flt is not None  # an empty allowlist must NOT collapse to "no filter"


def test_recall_reinforce_flag_respected_over_wire(config, daemon):  # noqa: F811
    with _client(config, "cli") as c:
        c.remember("Dylan drinks flat whites")
        for _ in range(3):
            c.recall("flat whites", reinforce=False)
    assert daemon.store.flush_reinforce() == 0  # nothing was enqueued


def test_bad_params_map_to_bad_request(config, daemon):  # noqa: F811
    with _client(config, "cli") as c:
        write_message(c._wfile, {"v": PROTOCOL_VERSION, "id": "x", "client": "cli",
                                 "method": "recall", "params": {}})  # missing query
        response = read_message(c._rfile)
        assert response["ok"] is False
        assert response["error"]["code"] == "bad_request"


def test_reads_survive_concurrent_hard_forget(config):
    """Recalls hammering the store while hard-forget swaps the shard out
    must neither crash nor touch a closed shard."""
    store = make_store(config)
    try:
        ids = [store.remember(f"stress fact number {i} topic {i}")[0].memory.id
               for i in range(6)]
        errors: list[Exception] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    store.recall("stress fact topic", k=3, reinforce=False)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        try:
            for mid in ids[:3]:
                store.forget(mid, mode="hard")  # three shard swaps under load
        finally:
            stop.set()
            for t in readers:
                t.join(timeout=10.0)
        assert not errors, errors[:1]
        assert store.backend.count() == 3
    finally:
        store.close()


def test_export_consistent_under_concurrent_writes(config):
    import io

    store = make_store(config, llm=None)
    try:
        store.remember("seed fact zero")
        stop = threading.Event()

        def writer() -> None:
            i = 0
            while not stop.is_set():
                store.remember(f"background fact {i}")
                i += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            for _ in range(5):
                buf = io.StringIO()
                store.export_jsonl(buf)  # must never see a torn journal
        finally:
            stop.set()
            t.join(timeout=10.0)
    finally:
        store.close()

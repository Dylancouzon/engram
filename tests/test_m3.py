"""M3: two-device encrypted sync (LWW merge, tombstone suppression,
private-never-syncs) and capability tokens + per-method grants."""

import shutil

import pytest
from conftest import make_store
from test_daemon import _client, daemon  # noqa: F401 - fixture reuse

from engram.config import Config
from engram.daemon import ClientRegistry
from engram.protocol import ProtocolError
from engram.store import MemoryStore
from engram.sync import SyncError, SyncTarget, save_target, sync_shard


@pytest.fixture
def cloud():
    from qdrant_client import QdrantClient

    return QdrantClient(":memory:")  # the "Cloud" collection, shared by devices


def _device(tmp_path, name, key_from=None) -> MemoryStore:
    cfg = Config(data_dir=tmp_path / name, dense_dim=64)
    cfg.conflict_min_similarity = 0.2
    store = make_store(cfg)
    save_target(cfg, SyncTarget(shard="me-synced", url="unused", api_key=None,
                                collection="engram-me-synced"))
    if key_from is not None:
        # The owner copies sync.key between devices by hand (documented).
        from engram.sync import sync_key

        sync_key(key_from)  # ensure it exists
        shutil.copy(key_from.data_dir / "sync.key", cfg.data_dir / "sync.key")
    return store


def test_two_device_sync_roundtrip(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b", key_from=a.config)
    try:
        a.remember("Dylan's cat is named Miso", shard="me-synced")
        assert sync_shard(a, "me-synced", client=cloud)["pushed"] >= 1

        report = sync_shard(b, "me-synced", client=cloud)
        assert report["applied"] == 1
        assert b.recall("cat name")[0].memory.text == "Dylan's cat is named Miso"

        # B adds; A picks it up on its next sync.
        b.remember("Dylan flies to Berlin on Friday", shard="me-synced")
        sync_shard(b, "me-synced", client=cloud)
        assert sync_shard(a, "me-synced", client=cloud)["applied"] == 1
        assert any("Berlin" in h.memory.text for h in a.recall("flight Berlin"))

        # Idempotent: a third sync applies nothing new anywhere.
        assert sync_shard(a, "me-synced", client=cloud)["applied"] == 0
        assert sync_shard(b, "me-synced", client=cloud)["applied"] == 0
    finally:
        a.close()
        b.close()


def test_relay_holds_only_ciphertext(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    try:
        a.remember("Dylan's cat is named Miso", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        records, _ = cloud.scroll("engram-me-synced", limit=10, with_payload=True)
        assert records
        for r in records:
            blob = str(r.payload)
            assert "Miso" not in blob and "cat" not in blob
    finally:
        a.close()


def test_tombstone_propagates_and_purges(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b", key_from=a.config)
    try:
        [action] = a.remember("the secret plan is XYZZY42", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        sync_shard(b, "me-synced", client=cloud)
        assert any("XYZZY42" in h.memory.text for h in b.recall("secret plan"))

        a.forget(action.memory.id, mode="hard")
        sync_shard(a, "me-synced", client=cloud)
        report = sync_shard(b, "me-synced", client=cloud)
        assert report["tombstoned"] == 1
        assert b.journal.is_tombstoned(action.memory.id)
        # gone from B's disk too (hard forget ran the purge path)
        leaks = [p for p in b.config.data_dir.rglob("*")
                 if p.is_file() and b"XYZZY42" in p.read_bytes()]
        assert leaks == []
        # and the relay now holds a content-free tombstone, not ciphertext
        records, _ = cloud.scroll("engram-me-synced", limit=10, with_payload=True)
        entry = next(r for r in records if str(r.id) == action.memory.id)
        assert entry.payload["op"] == "tombstone" and "blob" not in entry.payload
    finally:
        a.close()
        b.close()


def test_private_shard_refuses_sync_config(config):
    with pytest.raises(SyncError):
        save_target(config, SyncTarget(shard="private", url="x", api_key=None,
                                       collection="c"))


def test_lww_prefers_newer_write(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b", key_from=a.config)
    try:
        [act] = a.remember("status: draft", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        sync_shard(b, "me-synced", client=cloud)

        # B updates the same memory later (same id, newer journal ts).
        m = b.get(act.memory.id)
        m.text = "status: shipped"
        b._commit_upserts([m], "me-synced")
        sync_shard(b, "me-synced", client=cloud)
        assert sync_shard(a, "me-synced", client=cloud)["applied"] == 1
        assert a.get(act.memory.id).text == "status: shipped"
        # A's older state does not resurrect on B.
        assert sync_shard(b, "me-synced", client=cloud)["applied"] == 0
        assert b.get(act.memory.id).text == "status: shipped"
    finally:
        a.close()
        b.close()


def test_wrong_key_is_skipped_not_crashed(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b")  # generates its OWN key: can't decrypt A
    try:
        a.remember("Dylan's cat is named Miso", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        report = sync_shard(b, "me-synced", client=cloud)
        assert report["applied"] == 0 and report["skipped"] >= 1
    finally:
        a.close()
        b.close()


# --- capability tokens ---------------------------------------------------------


def test_token_required_and_verified(config, daemon):  # noqa: F811
    token = ClientRegistry(config).allow("phone", ["*"], token=True)
    assert token and token.startswith("egt_")

    from engram.client import Client

    with Client(config, "phone").connect() as c:  # no token
        with pytest.raises(ProtocolError) as exc:
            c.recall("anything")
        assert exc.value.code == "unregistered_client"
    with Client(config, "phone", token="egt_wrong").connect() as c:
        with pytest.raises(ProtocolError):
            c.recall("anything")
    with Client(config, "phone", token=token).connect() as c:
        assert c.ping()
        c.remember("token-authed write")
        assert any("token-authed" in h.memory.text for h in c.recall("token"))


def test_method_grants_enforced(config, daemon):  # noqa: F811
    ClientRegistry(config).allow("readonly", ["*"], methods=["recall"])
    with _client(config, "cli") as owner:
        owner.remember("Dylan's cat is named Miso")
    with _client(config, "readonly") as c:
        assert c.recall("cat")  # granted
        with pytest.raises(ProtocolError) as exc:
            c.remember("sneaky write")
        assert exc.value.code == "scope_denied"

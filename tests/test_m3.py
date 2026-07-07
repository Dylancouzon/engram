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


def test_reinforce_churn_does_not_drop_content_from_sync(tmp_path, cloud):
    # Reinforce collapse churns the journal's max seq (delete+reinsert at a
    # fresh seq on every recall). A content upsert written after that churn
    # must still be pushed: read bumps must never advance the sync high-water
    # mark past an unpushed content write.
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b", key_from=a.config)
    try:
        a.remember("Dylan's cat is named Miso", shard="me-synced")
        for _ in range(5):
            a.recall("cat name")  # reinforce churn between the two content writes
        a.remember("Dylan flies to Berlin on Friday", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        assert sync_shard(b, "me-synced", client=cloud)["applied"] == 2
        assert any("Miso" in h.memory.text for h in b.recall("cat name"))
        assert any("Berlin" in h.memory.text for h in b.recall("flight Berlin"))
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


def test_hand_edited_private_target_refused_on_load(config):
    # A corrupt/manual sync.json naming private must not open a sync path.
    (config.data_dir).mkdir(parents=True, exist_ok=True)
    (config.data_dir / "sync.json").write_text(
        '{"private": {"url": "x", "api_key": null, "collection": "c"}}')
    from engram.sync import load_targets
    with pytest.raises(SyncError):
        load_targets(config)


def test_relay_cannot_forge_a_tombstone(tmp_path, cloud):
    from qdrant_client import models as qm
    a = _device(tmp_path, "device-a")
    try:
        [act] = a.remember("Dylan's cat is named Miso", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        # Attacker injects a tombstone for a real id with no valid MAC.
        cloud.upsert("engram-me-synced", points=[qm.PointStruct(
            id=act.memory.id, vector={"relay": [0.0]},
            payload={"op": "tombstone", "ts": 9e9, "device": "evil"})])
        report = sync_shard(a, "me-synced", client=cloud)
        assert report["tombstoned"] == 0  # forgery rejected
        assert a.get(act.memory.id) is not None  # memory survives
    finally:
        a.close()


def test_relay_cannot_swap_blob_between_ids(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b", key_from=a.config)
    try:
        [act] = a.remember("Dylan's cat is named Miso", shard="me-synced")
        sync_shard(a, "me-synced", client=cloud)
        # Take A's legit ciphertext and re-file it under a different id.
        records, _ = cloud.scroll("engram-me-synced", limit=10, with_payload=True)
        blob = next(r.payload["blob"] for r in records
                    if r.payload.get("op") == "upsert")
        from qdrant_client import models as qm
        cloud.upsert("engram-me-synced", points=[qm.PointStruct(
            id="00000000-0000-4000-8000-000000000999",
            vector={"relay": [0.0]},
            payload={"op": "upsert", "blob": blob, "ts": 9e9, "device": "evil"})])
        sync_shard(b, "me-synced", client=cloud)
        # B applies the genuine point but rejects the swapped one (id mismatch
        # inside the authenticated blob).
        assert b.get(act.memory.id) is not None
        assert b.get("00000000-0000-4000-8000-000000000999") is None
    finally:
        a.close()
        b.close()


def test_unknown_tombstone_is_recorded(tmp_path, cloud):
    a = _device(tmp_path, "device-a")
    b = _device(tmp_path, "device-b", key_from=a.config)
    try:
        [act] = a.remember("ephemeral note", shard="me-synced")
        a.forget(act.memory.id, mode="hard")  # tombstone before B ever syncs
        sync_shard(a, "me-synced", client=cloud)
        # B sees only the tombstone (never had the upsert).
        sync_shard(b, "me-synced", client=cloud)
        assert b.journal.is_tombstoned(act.memory.id)
        # A later replayed upsert cannot resurrect it on B.
        import json as _json

        from cryptography.fernet import Fernet
        from qdrant_client import models as qm
        key = (a.config.data_dir / "sync.key").read_bytes().strip()
        blob = Fernet(key).encrypt(_json.dumps(
            {"id": act.memory.id, "ts": 1.0, "shard": "me-synced",
             "payload": {"text": "ephemeral note"}}).encode()).decode()
        cloud.upsert("engram-me-synced", points=[qm.PointStruct(
            id=act.memory.id, vector={"relay": [0.0]},
            payload={"op": "upsert", "blob": blob, "ts": 1.0, "device": "old"})])
        sync_shard(b, "me-synced", client=cloud)
        assert b.get(act.memory.id) is None  # suppressed
    finally:
        a.close()
        b.close()


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
    with Client(config, "phone", token="egt_wrong").connect() as c, \
            pytest.raises(ProtocolError):
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

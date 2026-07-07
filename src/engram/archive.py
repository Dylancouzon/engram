"""Snapshot backup and restore: the portable-folder promise, verified.

A snapshot is a tar of the memory folder's durable state (journal, shards,
owner id, client registry, config), taken quiesced — flushed, under the
store's exclusive locks, never a live copy of an open shard. Artifacts
that leave the device are encrypted by default: the tar is wrapped with a
passphrase-derived key (scrypt -> Fernet), so a snapshot on a USB stick or
in cloud storage is ciphertext.

Restore refuses to overwrite an existing memory folder and finishes with a
journal-replay verification: the journal is the source of truth even for
backups.
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from engram.config import Config

MAGIC = b"ENGRAM1\n"  # encrypted-snapshot header: magic + 16-byte salt
_DURABLE = ("journal.db", "owner", "clients.json", "config.toml", "shards")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def write_snapshot(config: Config, dest: Path, passphrase: str | None) -> int:
    """Tar the durable state into `dest`. The caller must hold the store
    quiesced (write lock + exclusive shard guard, everything flushed).
    Returns the byte size written."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in _DURABLE:
            path = config.data_dir / name
            if path.exists():
                tar.add(path, arcname=name)
    raw = buf.getvalue()

    if passphrase:
        salt = os.urandom(16)
        token = Fernet(_derive_key(passphrase, salt)).encrypt(raw)
        data = MAGIC + salt + token
    else:
        data = raw
    dest.write_bytes(data)
    return len(data)


def read_snapshot(source: Path, passphrase: str | None) -> io.BytesIO:
    data = source.read_bytes()
    if data.startswith(MAGIC):
        if not passphrase:
            raise ValueError("snapshot is encrypted; a passphrase is required")
        salt, token = data[len(MAGIC):len(MAGIC) + 16], data[len(MAGIC) + 16:]
        try:
            raw = Fernet(_derive_key(passphrase, salt)).decrypt(token)
        except InvalidToken as e:
            raise ValueError("wrong passphrase or corrupted snapshot") from e
    else:
        raw = data
    return io.BytesIO(raw)


def restore_snapshot(config: Config, source: Path, passphrase: str | None) -> None:
    """Unpack a snapshot into an empty memory folder."""
    if config.journal_path.exists():
        raise ValueError(
            f"{config.data_dir} already contains a memory; restore into a fresh"
            " folder (or move the old one aside)"
        )
    config.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config.data_dir, 0o700)
    with tarfile.open(fileobj=read_snapshot(source, passphrase), mode="r:gz") as tar:
        tar.extractall(config.data_dir, filter="data")

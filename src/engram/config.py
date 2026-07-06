"""Configuration: where the memory folder lives and the knobs that tune it.

The data dir is the product — "a folder you own". Everything engram knows
lives under it, and copying it to another machine moves your memory.

Layout:
    ~/.engram/
        config.toml      # user overrides (optional)
        owner            # owner namespace UUID (id-namespacing across devices)
        journal.db       # SQLite write-intent journal — the source of truth
        shards/private/  # Qdrant Edge shard — rebuildable index
        models/          # FastEmbed model cache (re-provisionable, pinned below)
"""

from __future__ import annotations

import os
import tomllib
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

DENSE_MODEL = "nomic-ai/nomic-embed-text-v1.5"
DENSE_DIM = 768
SPARSE_MODEL = "Qdrant/minicoil-v1"


def default_data_dir() -> Path:
    return Path(os.environ.get("ENGRAM_HOME", Path.home() / ".engram"))


@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    dense_dim: int = DENSE_DIM  # overridden only by tests/alt models

    # Write model
    salience_floor: float = 0.1  # extraction results below this are dropped
    judge_confidence: float = 0.8  # auto-apply UPDATE/SUPERSEDE at or above this
    conflict_top_k: int = 5  # similar memories retrieved for the judge
    conflict_min_similarity: float = 0.6  # candidates below this aren't conflicts

    # Retrieval
    recall_k: int = 8
    prefetch_limit: int = 40  # generous per-branch prefetch before fusion
    half_life_days: dict[str, float] = field(
        default_factory=lambda: {"semantic": 180.0, "episodic": 14.0, "procedural": 365.0}
    )
    # Blend weights for the app-side rescore:
    # score = similarity * (base + w_rec*recency + w_imp*importance)
    weight_recency: float = 0.25
    weight_importance: float = 0.25

    # Extraction (enhancer — verbatim fallback if unreachable)
    ollama_url: str = "http://localhost:11434"
    extraction_model: str = "qwen3:4b"

    # Redaction
    redaction_enabled: bool = True

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def shard_dir(self) -> Path:
        return self.data_dir / "shards" / "private"

    @property
    def models_dir(self) -> Path:
        # Deliberately NOT inside data_dir: the memory folder stays small and
        # portable; models are a per-machine tier, re-provisioned from the
        # pinned names above (or re-downloaded on first use).
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return cache_root / "engram" / "models"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "writer.lock"

    def owner_namespace(self) -> uuid.UUID:
        """Stable per-owner UUID namespace, created on first use."""
        owner_file = self.data_dir / "owner"
        if owner_file.exists():
            return uuid.UUID(owner_file.read_text().strip())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ns = uuid.uuid4()
        owner_file.write_text(str(ns) + "\n")
        return ns

    @classmethod
    def load(cls, data_dir: Path | None = None) -> Config:
        cfg = cls(data_dir=data_dir) if data_dir else cls()
        toml_path = cfg.data_dir / "config.toml"
        if toml_path.exists():
            overrides = tomllib.loads(toml_path.read_text())
            valid = {f.name for f in fields(cls)}
            for key, value in overrides.items():
                if key in valid and key != "data_dir":
                    setattr(cfg, key, value)
        return cfg

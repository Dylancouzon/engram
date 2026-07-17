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
import sys
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
    review_floor: float = 0.5  # UPDATE/SUPERSEDE between floor and judge_confidence
    #   are applied as ADD and queued for review (below the floor: plain ADD)
    conflict_top_k: int = 5  # similar memories retrieved for the judge
    conflict_min_similarity: float = 0.6  # candidates below this aren't conflicts
    noop_similarity: float = 0.9  # NOOP verdicts stand at any confidence above this

    # Retrieval
    recall_k: int = 8
    prefetch_limit: int = 40  # generous per-branch prefetch before fusion
    half_life_days: dict[str, float] = field(
        default_factory=lambda: {"semantic": 180.0, "episodic": 14.0, "procedural": 365.0}
    )
    # MMR diversification of recall results (None disables). Server-side
    # decay via FormulaQuery is impossible on Edge 0.7.2 (probed: Formula
    # never sees the fused score), hence the app-side rescore below.
    mmr_lambda: float | None = 0.7

    # Blend weights for the app-side rescore:
    # score = similarity * (base + w_rec*recency + w_imp*importance)
    weight_recency: float = 0.25
    weight_importance: float = 0.25

    # Extraction (enhancer — verbatim fallback if unreachable). Split models:
    # extraction sends long transcript prompts (the latency cost), so it runs a
    # smaller/faster model; the conflict judge sends short fixed-size prompts, so
    # it keeps the stronger model to protect op accuracy. Benchmarked: the split
    # holds 79% op / 93% recall (vs 86/97 for 4b-both) at ~2.6x faster
    # extraction — see docs/model-benchmark.md. Set both the same to un-split.
    ollama_url: str = "http://localhost:11434"
    extraction_model: str = "qwen3:1.7b"
    judge_model: str = "qwen3:4b"
    # Min seconds between captures of the SAME conversation. Each capture is a
    # 10-40s local-model burst; without this, every turn-end fires one. Rapid
    # turns batch — the transcript tail accumulates and is captured once per
    # window (nothing lost). Tune in config.toml; 0 disables.
    capture_debounce_s: float = 90.0

    # Redaction
    redaction_enabled: bool = True

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def shards_root(self) -> Path:
        return self.data_dir / "shards"

    def shard_path(self, shard: str) -> Path:
        # "shared:family" -> shards/shared__family (':' is awkward on disk)
        return self.shards_root / shard.replace(":", "__")

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

    # AF_UNIX paths are capped (~104 bytes on macOS); a deep ENGRAM_HOME
    # needs a short override. Set via config.toml or ENGRAM_SOCKET.
    socket_override: Path | None = None

    @property
    def socket_path(self) -> Path:
        if env := os.environ.get("ENGRAM_SOCKET"):
            return Path(env)
        # socket_override may arrive as a str from config.toml; wrap it so
        # every consumer can treat socket_path as a Path.
        return Path(self.socket_override) if self.socket_override else self.data_dir / "daemon.sock"

    @property
    def clients_path(self) -> Path:
        """Registered clients + their scope allowlists (daemon state, not
        user config): {"claude-code": {"scopes": ["*"]}}"""
        return self.data_dir / "clients.json"

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
                if key == "data_dir":
                    continue  # set via ENGRAM_HOME / constructor, not the file
                if key not in valid:
                    # A typo'd or since-removed knob does nothing; say so rather
                    # than let the user believe an override took (stderr, so a
                    # hook's stdout context stays clean).
                    print(f"engram: config.toml: ignoring unknown key {key!r}",
                          file=sys.stderr)
                    continue
                current = getattr(cfg, key)
                # config.toml is hand-edited: coerce a mistyped number
                # (salience_floor = "0.8") to the field's type instead of
                # silently storing a string that blows up on the first write.
                # bool is an int subclass, so guard it first and leave it be.
                if not isinstance(current, bool) and isinstance(current, (int, float)) \
                        and not isinstance(value, type(current)):
                    try:
                        value = type(current)(value)
                    except (TypeError, ValueError) as e:
                        raise ValueError(
                            f"config.toml: {key}={value!r} is not a "
                            f"{type(current).__name__}"
                        ) from e
                setattr(cfg, key, value)
        return cfg

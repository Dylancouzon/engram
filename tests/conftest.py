"""Test doubles: a deterministic bag-of-words embedder (no model downloads,
real cosine overlap between texts sharing words) and a scriptable judge."""

from __future__ import annotations

import hashlib
import math
import re

import pytest

from engram.config import Config
from engram.embed import Embedded
from engram.store import MemoryStore

DIM = 64


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class FakeEmbedder:
    """Hashed bag-of-words: texts sharing words get high cosine similarity,
    which is exactly what conflict-candidate retrieval needs to exercise."""

    def _embed(self, text: str) -> Embedded:
        dense = [0.0] * DIM
        counts: dict[int, float] = {}
        for tok in _tokens(text):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            dense[h % DIM] += 1.0
            counts[h % 100_000] = counts.get(h % 100_000, 0.0) + 1.0
        norm = math.sqrt(sum(v * v for v in dense)) or 1.0
        dense = [v / norm for v in dense]
        indices = sorted(counts)
        return Embedded(
            dense=dense,
            sparse_indices=indices,
            sparse_values=[counts[i] for i in indices],
        )

    def embed_documents(self, texts: list[str]) -> list[Embedded]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> Embedded:
        return self._embed(text)


class FakeLLM:
    """Scripted local model. `judge_responses` are handed out in order for
    judge calls; extraction returns verbatim unless `extract_response` set."""

    def __init__(self, judge_responses: list[dict] | None = None,
                 extract_response: dict | None = None,
                 general_response: dict | None = None):
        self.judge_responses = list(judge_responses or [])
        self.extract_response = extract_response
        self.general_response = general_response or {"general": True}
        self.judge_prompts: list[str] = []

    def available(self) -> bool:
        return True

    def generate_json(self, system: str, prompt: str):
        if "You extract long-term memories" in system:
            return self.extract_response  # None -> verbatim fallback
        if "about the user themselves" in system:
            return self.general_response
        self.judge_prompts.append(prompt)
        if self.judge_responses:
            return self.judge_responses.pop(0)
        return {"op": "ADD", "target": None, "confidence": 1.0, "text": None}


@pytest.fixture
def config(tmp_path):
    cfg = Config(data_dir=tmp_path / "engram-home", dense_dim=DIM)
    # The fake embedder has crude similarity; keep the candidate gate low so
    # related texts qualify as conflict candidates.
    cfg.conflict_min_similarity = 0.2
    return cfg


def make_store(config: Config, llm=None) -> MemoryStore:
    return MemoryStore(config, embedder=FakeEmbedder(), llm=llm)


@pytest.fixture
def store(config):
    s = make_store(config)
    yield s
    s.close()

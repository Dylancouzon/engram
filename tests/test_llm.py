"""LocalLLM reachability probe. The split-model gate depends on available()
matching the EXACT tag, not the family — a pulled qwen3:4b must not make an
unpulled qwen3:1.7b look reachable (that lets the capture gate pass and
extraction 404s into verbatim transcript tails)."""

from __future__ import annotations

import contextlib
import json

from engram import llm


def _fake_tags(monkeypatch, names):
    @contextlib.contextmanager
    def fake_urlopen(url, timeout=0):
        class Resp:
            def read(self):
                return json.dumps({"models": [{"name": n} for n in names]}).encode()
        yield Resp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)


def test_available_matches_exact_tag_not_family(monkeypatch):
    _fake_tags(monkeypatch, ["qwen3:4b"])  # only the judge model pulled
    assert llm.LocalLLM("http://x", "qwen3:4b").available() is True
    assert llm.LocalLLM("http://x", "qwen3:1.7b").available() is False  # not pulled


def test_available_tagless_config_matches_any_tag(monkeypatch):
    _fake_tags(monkeypatch, ["qwen3:1.7b"])
    assert llm.LocalLLM("http://x", "qwen3").available() is True


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

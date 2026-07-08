"""Local LLM access via Ollama — strictly an enhancer.

engram must work with no model installed: every caller treats a None/failed
response as "fall back to verbatim". No cloud path exists here by design;
the only endpoint is localhost.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

_THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL)


def clamp01(value: Any, default: float) -> float:
    """Parse a model-supplied number into [0, 1], falling back to `default`
    on null/non-numeric input. Shared by extract (importance) and resolve
    (confidence) — a loose envelope must degrade, never crash the write."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class LocalLLM:
    PROBE_TTL = 60.0  # long-lived processes (the M1 daemon) re-probe

    def __init__(self, url: str, model: str, timeout: float = 60.0):
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._available: bool | None = None
        self._probed_at = 0.0

    def available(self) -> bool:
        now = time.monotonic()
        if self._available is None or now - self._probed_at > self.PROBE_TTL:
            try:
                with urllib.request.urlopen(self._url + "/api/tags", timeout=2.0) as resp:
                    tags = json.loads(resp.read())
                names = [m.get("name", "") for m in tags.get("models", [])]
                self._available = any(n.startswith(self._model.split(":")[0]) for n in names)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                self._available = False
            self._probed_at = now
        return self._available

    def _request(self, payload: dict) -> urllib.request.Request:
        return urllib.request.Request(
            self._url + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def chat(self, messages: list[dict], *, temperature: float = 0.4):
        """Stream a chat completion from Ollama, yielding text chunks as they
        arrive. Silent empty generator on any failure — the caller shows a
        'model offline' state. `think` is off (qwen3) since a streamed
        response can't be regex-stripped of <think> after the fact."""
        req = self._request({
            "model": self._model,
            "messages": messages,
            "stream": True,
            "think": False,
            "options": {"temperature": temperature},
        })
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
        except (urllib.error.URLError, TimeoutError, OSError):
            return
        try:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                chunk = obj.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    break
        except (urllib.error.URLError, TimeoutError, OSError):
            # Mid-stream drop (Ollama died / timed out): end the generator
            # cleanly so the caller can still emit its terminal frame.
            return
        finally:
            resp.close()

    def generate_json(self, system: str, prompt: str) -> Any | None:
        """One chat turn constrained to JSON. Returns the parsed value, or
        None on any failure — callers must have a verbatim fallback."""
        req = self._request({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "think": False,  # qwen3: skip thinking tokens
            "options": {"temperature": 0.1},
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                content = json.loads(resp.read())["message"]["content"]
            content = _THINK_TAGS.sub("", content).strip()
            return json.loads(content)
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError):
            return None

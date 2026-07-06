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

    def generate_json(self, system: str, prompt: str) -> Any | None:
        """One chat turn constrained to JSON. Returns the parsed value, or
        None on any failure — callers must have a verbatim fallback."""
        body = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": "json",
                "think": False,  # qwen3: skip thinking tokens
                "options": {"temperature": 0.1},
            }
        ).encode()
        req = urllib.request.Request(
            self._url + "/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                content = json.loads(resp.read())["message"]["content"]
            content = _THINK_TAGS.sub("", content).strip()
            return json.loads(content)
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError):
            return None

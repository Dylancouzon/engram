"""Stage-0 redaction: a deterministic local scrubber that runs before
extraction, embedding, persistence, and any sync. Secrets never land,
even transiently.

Two tiers:
- REDACT: known secret shapes and high-entropy tokens are replaced with a
  placeholder; the write continues.
- REFUSE: high-confidence catastrophic secrets (private key blocks) refuse
  the whole write — a redacted key is still a signal something leaked.

Deliberately regex + entropy, no ML: it must be fast, offline, and
predictable. Pure-hex strings (git SHAs, digests) are exempt from the
entropy check — they're identifiers more often than secrets.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

PLACEHOLDER = "[REDACTED:{kind}]"

REFUSE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----"),
    ),
]

# Order matters: specific providers before generic shapes.
REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),  # OpenAI/Anthropic style
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("url-credential", re.compile(r"(?<=://)[^/\s:@]+:([^@/\s]+)(?=@)")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b"
            r"\s*[:=]\s*[\"']?([A-Za-z0-9!$%^&*_+/=.-]{6,})[\"']?"
        ),
    ),
]

_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
_ENTROPY_THRESHOLD = 4.2  # bits/char; base64-ish secrets sit ~5.2, English ~3.0


def _shannon_entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    if _HEX_ONLY.match(token):
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if not (has_upper and has_lower and has_digit):
        return False
    return _shannon_entropy(token) >= _ENTROPY_THRESHOLD


@dataclass
class RedactionResult:
    text: str
    hits: list[str] = field(default_factory=list)  # kinds redacted, in order found
    refused: bool = False
    refusal_reason: str | None = None

    @property
    def clean(self) -> bool:
        return not self.hits and not self.refused


def redact(text: str, enabled: bool = True) -> RedactionResult:
    """Scrub `text`. On a refuse-tier hit, returns refused=True and the
    original text must not be persisted anywhere."""
    if not enabled:
        return RedactionResult(text=text)

    for kind, pattern in REFUSE_PATTERNS:
        if pattern.search(text):
            return RedactionResult(
                text="",
                refused=True,
                refusal_reason=f"contains a {kind}; refusing to store",
                hits=[kind],
            )

    hits: list[str] = []

    def _sub(kind: str):
        def replace(m: re.Match[str]) -> str:
            hits.append(kind)
            # Patterns with a capture group redact just the secret part
            # (e.g. the password inside a URL), keeping surrounding context.
            partial = kind in ("url-credential", "assigned-secret")
            if partial and m.groups() and m.group(1) is not None:
                start, end = m.span(1)
                s = m.span(0)[0]
                whole = m.group(0)
                return whole[: start - s] + PLACEHOLDER.format(kind=kind) + whole[end - s :]
            return PLACEHOLDER.format(kind=kind)

        return replace

    for kind, pattern in REDACT_PATTERNS:
        text = pattern.sub(_sub(kind), text)

    def _entropy_sub(m: re.Match[str]) -> str:
        token = m.group(0)
        if "REDACTED" in token or not _looks_like_secret(token):
            return token
        hits.append("high-entropy")
        return PLACEHOLDER.format(kind="high-entropy")

    text = _ENTROPY_CANDIDATE.sub(_entropy_sub, text)

    return RedactionResult(text=text, hits=hits)

"""Secret redaction for logs and error text.

Two detectors, because neither alone is enough:

* managed-value matching — the exact secret values this hub holds (vault
  entries as they are read, session tokens). Precise, but blind to anything
  it was never told about;
* vendor-shape matching — credential formats the hub does not manage
  (cloud keys, bearer tokens, private-key blocks, `password=...`).

Both are proven at start-up by `self_test()`: canaries must be redacted and
ordinary identifiers (a git SHA, a UUID) must survive. If the self-test fails
the log formatter withholds every line rather than risk writing a secret.
Not entropy-based: that would corrupt ids while still missing short keys.
"""
from __future__ import annotations

import logging
import re
import threading

MASK = "[REDACTED]"

# One source of truth for credential shapes; the repository isolation guard
# (tests/test_isolation_guard.py) imports this same pattern.
VENDOR_SHAPES = re.compile(
    r"AKIA[0-9A-Z]{16}"                                  # AWS access key id
    r"|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}"  # GitHub tokens
    r"|sk-[A-Za-z0-9_-]{32,}"                            # OpenAI/Anthropic-style keys
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"                     # Slack tokens
    r"|tskey-[A-Za-z0-9-]{20,}"                          # Tailscale auth keys
    r"|AIza[0-9A-Za-z_-]{35}"                            # Google API keys
    r"|-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
)
BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}")
KEY_VALUE = re.compile(
    r"(?i)\b(pass(?:word|phrase)?|secret|token|api[_-]?key|access[_-]?key|authorization|setup[_-]?code)"
    r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',;]{4,})")

_CANARIES = ("ghp_" + "A1b2C3d4" * 4 + "Z9y8", "password=hunter2hunter2", "Bearer abcdefghijklmnopqrstuvwxyz0123")
_SURVIVORS = ("3da350c1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5", "2070ec75-73e4-4946-9ca7-ceb2fbb7b92b")


class Redactor:
    MIN_MANAGED = 8

    def __init__(self) -> None:
        self._known: set[str] = set()
        self._lock = threading.Lock()
        self.healthy = self.self_test()

    def register(self, value: str | None) -> None:
        """Remember a live secret value so it is masked wherever it appears."""
        if value and len(value) >= self.MIN_MANAGED:
            with self._lock:
                self._known.add(value)

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            known = sorted(self._known, key=len, reverse=True)
        for v in known:
            if v in text:
                text = text.replace(v, MASK)
        text = VENDOR_SHAPES.sub(MASK, text)
        text = BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
        text = KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
        return text

    def self_test(self) -> bool:
        probe = Redactor.__new__(Redactor)
        probe._known, probe._lock = {"managed-canary-value-123"}, threading.Lock()
        try:
            if MASK not in probe.redact("x managed-canary-value-123 y"):
                return False
            if any(c in probe.redact(f"log {c} end") for c in _CANARIES):
                return False
            return all(probe.redact(f"id {s}") == f"id {s}" for s in _SURVIVORS)
        except Exception:
            return False


REDACTOR = Redactor()


class RedactingFormatter(logging.Formatter):
    """Formats, then redacts the whole line — message, args and traceback."""

    def __init__(self, fmt: str | None = None, redactor: Redactor = REDACTOR) -> None:
        super().__init__(fmt or "%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        if not self.redactor.healthy:
            return f"{self.formatTime(record)} {record.levelname} {record.name}: [line withheld — redaction self-test failed]"
        return self.redactor.redact(super().format(record))

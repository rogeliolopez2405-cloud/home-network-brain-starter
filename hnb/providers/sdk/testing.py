"""Test helpers for provider modules."""
from __future__ import annotations


class FakeSecrets:
    """An in-memory SecretHandle. In the real hub, secrets come from the
    encrypted vault; in tests, hand the adapter one of these."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def __repr__(self) -> str:
        return f"FakeSecrets({sorted(self.values)})"

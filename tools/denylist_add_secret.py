"""Add a secret to the private denylist as a SHA-256 hash (never the value).

    python tools/denylist_add_secret.py

Paste the secret at the hidden prompt. Only its hash is written to
~/.hnb-starter/private-denylist.txt (or $HNB_PRIVATE_DENYLIST). The isolation
guard then fails if that exact value ever appears in the repository.
"""
from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path


def main() -> int:
    path = Path(os.environ.get("HNB_PRIVATE_DENYLIST") or Path.home() / ".hnb-starter" / "private-denylist.txt")
    value = getpass.getpass("Secret to guard against (input hidden): ").strip()
    if len(value) < 12:
        print("Too short to match safely (need 12+ characters). Nothing written.")
        return 1
    digest = hashlib.sha256(value.encode()).hexdigest()
    del value
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"sha256:{digest}" in existing:
        print("Already guarded.")
        return 0
    with path.open("a", encoding="utf-8") as f:
        f.write(f"sha256:{digest}\n")
    if os.name == "posix":
        path.chmod(0o600)
    print(f"Added one secret hash to {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

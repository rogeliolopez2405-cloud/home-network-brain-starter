"""Encrypted provider-secret store.

* Cipher: XChaCha20-Poly1305 (libsodium AEAD via PyNaCl), 256-bit key,
  192-bit random nonce per write — large enough that random nonces never
  collide in practice.
* Associated data binds every ciphertext to its (provider, name) slot with an
  unambiguous length-prefixed encoding, so a ciphertext copied into another
  row fails to decrypt instead of being accepted.
* The key is 32 random bytes in a file outside the database (installed at
  /var/lib/hnb/secrets/vault.key, mounted read-only into the container). A DB
  file or DB backup on its own cannot be decrypted.
* Each row stores the key's fingerprint (`key_id`), so a wrong key is reported
  as "sealed with a different key" rather than as corruption.
* Values never leave through normal APIs: `names()` lists what exists, and only
  a provider's own ScopedSecrets handle can read its values.

Design reviewed against the production secret box (AES-GCM) — kept its
versioned framing, key-id and fail-closed behaviour; fixed its ambiguous AAD
separator, missing key-file permissions and silent key overwrite.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import struct
from datetime import datetime, timezone
from pathlib import Path

import nacl.exceptions
import nacl.secret
import nacl.utils

from hnb.security.redact import REDACTOR, Redactor

KEY_BYTES = nacl.secret.Aead.KEY_SIZE      # 32
FORMAT = b"hnb-vault/1"
_O_BINARY = getattr(os, "O_BINARY", 0)     # Windows: no newline translation


class VaultError(Exception):
    pass


class VaultKeyError(VaultError):
    """The key file is missing, malformed or too widely readable."""


class VaultLockedError(VaultError):
    """A stored secret can't be decrypted with this hub's key."""


def key_id(key: bytes) -> str:
    """Public fingerprint of a key. Reveals nothing usable about the key."""
    return hashlib.blake2b(key, digest_size=8, person=b"hnb-vault-kid").hexdigest()


def generate_key_file(path: Path) -> bool:
    """Create a new key file (0600). Never overwrites: returns False if one exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, nacl.utils.random(KEY_BYTES))
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def load_key(path: Path, *, enforce_permissions: bool = True) -> bytes:
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise VaultKeyError(f"Vault key not found at {path}. Run the installer, or restore the key.") from None
    if not stat.S_ISREG(st.st_mode):
        raise VaultKeyError("Vault key path is not a regular file.")
    if enforce_permissions and os.name == "posix" and st.st_mode & 0o077:
        raise VaultKeyError("Vault key file is readable by other users. Fix with: chmod 600 <key file>")
    data = path.read_bytes()
    if len(data) != KEY_BYTES:
        raise VaultKeyError("Vault key file has the wrong length; it must hold exactly 32 random bytes.")
    return data


def _aad(provider_id: str, name: str) -> bytes:
    parts = (FORMAT, provider_id.encode(), name.encode())
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretVault:
    def __init__(self, conn: sqlite3.Connection, key: bytes, redactor: Redactor = REDACTOR):
        if len(key) != KEY_BYTES:
            raise VaultKeyError("Vault key must be 32 bytes.")
        self._conn = conn
        self._box = nacl.secret.Aead(key)
        self.key_id = key_id(key)
        self._redactor = redactor

    def put(self, provider_id: str, name: str, value: str) -> None:
        """Encrypt and store. Call inside the caller's transaction."""
        if not value:
            raise VaultError("Refusing to store an empty secret.")
        nonce = nacl.utils.random(nacl.secret.Aead.NONCE_SIZE)
        sealed = self._box.encrypt(value.encode("utf-8"), aad=_aad(provider_id, name), nonce=nonce)
        now = _now()
        self._conn.execute(
            "INSERT INTO provider_secrets (provider_id, name, key_id, nonce, ciphertext, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (provider_id, name) DO UPDATE SET "
            "key_id = excluded.key_id, nonce = excluded.nonce, ciphertext = excluded.ciphertext, "
            "updated_at = excluded.updated_at",
            (provider_id, name, self.key_id, nonce, sealed.ciphertext, now, now))
        self._redactor.register(value)

    def get(self, provider_id: str, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT key_id, nonce, ciphertext FROM provider_secrets WHERE provider_id = ? AND name = ?",
            (provider_id, name)).fetchone()
        if row is None:
            return None
        kid, nonce, ct = row[0], bytes(row[1]), bytes(row[2])
        if kid != self.key_id:
            raise VaultLockedError("This secret was sealed with a different vault key.")
        try:
            value = self._box.decrypt(ct, aad=_aad(provider_id, name), nonce=nonce).decode("utf-8")
        except nacl.exceptions.CryptoError:
            raise VaultLockedError("This secret failed its integrity check.") from None
        self._redactor.register(value)
        return value

    def delete(self, provider_id: str, name: str) -> None:
        self._conn.execute("DELETE FROM provider_secrets WHERE provider_id = ? AND name = ?", (provider_id, name))

    def names(self, provider_id: str) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT name FROM provider_secrets WHERE provider_id = ? ORDER BY name", (provider_id,))]

    def status(self) -> dict[str, int | str]:
        total, sealed_elsewhere = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(key_id != ?), 0) FROM provider_secrets", (self.key_id,)).fetchone()
        return {"key_id": self.key_id, "entries": total, "sealed_with_other_key": sealed_elsewhere}


class ScopedSecrets:
    """One provider's read-only view of its own secrets (the SecretHandle)."""

    def __init__(self, vault: SecretVault, provider_id: str):
        self._vault, self._pid = vault, provider_id

    def get(self, name: str) -> str | None:
        return self._vault.get(self._pid, name)

    def __repr__(self) -> str:        # never show values, even by accident
        return f"ScopedSecrets(provider={self._pid!r})"

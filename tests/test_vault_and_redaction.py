"""Secret store and log redaction."""
import logging
import os

import pytest

from hnb.security import vault as V
from hnb.security.redact import MASK, RedactingFormatter, Redactor
from hnb.store import db

SECRET = "tok_" + "Zq9" * 12


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "hnb.db")
    c.execute("INSERT INTO providers (provider_id, display_name, created_at) VALUES ('p1', 'P1', 'now'), "
              "('p2', 'P2', 'now')")
    yield c
    c.close()


def key(tmp_path, name="vault.key"):
    path = tmp_path / name
    assert V.generate_key_file(path)
    return V.load_key(path)


def test_round_trip_and_no_plaintext_on_disk(tmp_path, conn):
    v = V.SecretVault(conn, key(tmp_path), redactor=Redactor())
    with db.tx(conn):
        v.put("p1", "token", SECRET)
    assert v.get("p1", "token") == SECRET
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = (tmp_path / "hnb.db").read_bytes()
    assert SECRET.encode() not in raw
    assert v.names("p1") == ["token"]


def test_database_alone_cannot_decrypt(tmp_path, conn):
    with db.tx(conn):
        V.SecretVault(conn, key(tmp_path, "a.key")).put("p1", "token", SECRET)
    other = V.SecretVault(conn, key(tmp_path, "b.key"))
    with pytest.raises(V.VaultLockedError, match="different vault key"):
        other.get("p1", "token")
    assert other.status()["sealed_with_other_key"] == 1


def test_ciphertext_is_bound_to_its_slot(tmp_path, conn):
    v = V.SecretVault(conn, key(tmp_path))
    with db.tx(conn):
        v.put("p1", "token", SECRET)
        row = conn.execute("SELECT key_id, nonce, ciphertext FROM provider_secrets").fetchone()
        conn.execute("INSERT INTO provider_secrets VALUES ('p2', 'token', ?, ?, ?, 'now', 'now')", tuple(row))
    with pytest.raises(V.VaultLockedError, match="integrity"):
        v.get("p2", "token")


def test_tampered_ciphertext_is_rejected(tmp_path, conn):
    v = V.SecretVault(conn, key(tmp_path))
    with db.tx(conn):
        v.put("p1", "token", SECRET)
    conn.execute("UPDATE provider_secrets SET ciphertext = zeroblob(length(ciphertext))")
    with pytest.raises(V.VaultLockedError):
        v.get("p1", "token")


def test_key_file_is_never_overwritten(tmp_path):
    path = tmp_path / "vault.key"
    assert V.generate_key_file(path)
    first = path.read_bytes()
    assert V.generate_key_file(path) is False
    assert path.read_bytes() == first and len(first) == 32


@pytest.mark.parametrize("content", [b"", b"short", b"x" * 33])
def test_malformed_key_is_refused(tmp_path, content):
    path = tmp_path / "bad.key"
    path.write_bytes(content)
    with pytest.raises(V.VaultKeyError):
        V.load_key(path, enforce_permissions=False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_world_readable_key_is_refused(tmp_path):
    path = tmp_path / "vault.key"
    V.generate_key_file(path)
    path.chmod(0o644)
    with pytest.raises(V.VaultKeyError, match="readable by other users"):
        V.load_key(path)
    assert (path.stat().st_mode & 0o777) == 0o644


def test_key_id_is_a_fingerprint_not_the_key(tmp_path):
    k = key(tmp_path)
    kid = V.key_id(k)
    assert len(kid) == 16 and k.hex() not in kid


def test_scoped_handle_never_shows_values(tmp_path, conn):
    v = V.SecretVault(conn, key(tmp_path))
    with db.tx(conn):
        v.put("p1", "token", SECRET)
    h = V.ScopedSecrets(v, "p1")
    assert h.get("token") == SECRET and SECRET not in repr(h)
    assert V.ScopedSecrets(v, "p2").get("token") is None


# ── redaction ────────────────────────────────────────────────────────────
def test_redactor_self_test_passes():
    assert Redactor().healthy


def test_redacts_managed_values_and_vendor_shapes():
    r = Redactor()
    r.register(SECRET)
    line = (f"token {SECRET} and ghp_{'a' * 36} Authorization: Bearer abcdefghijklmnopqrstuvwx "
            "password=hunter2hunter2 id 2070ec75-73e4-4946-9ca7-ceb2fbb7b92b")
    out = r.redact(line)
    assert SECRET not in out and "ghp_" not in out and "hunter2" not in out and "abcdefghijklmnop" not in out
    assert "2070ec75-73e4-4946-9ca7-ceb2fbb7b92b" in out and out.count(MASK) >= 4


def test_formatter_redacts_tracebacks_and_withholds_when_unhealthy():
    r = Redactor()
    r.register(SECRET)
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed with %s", (SECRET,), None)
    assert SECRET not in RedactingFormatter(redactor=r).format(rec)
    r.healthy = False
    assert "withheld" in RedactingFormatter(redactor=r).format(rec)

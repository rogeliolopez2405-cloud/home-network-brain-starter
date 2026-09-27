"""Backup and restore."""
import json
import tarfile

import pytest

from conftest import Hub, make_settings
from hnb.ops import backup as B


def _prepare(settings, clock, brightness=21):
    with Hub(settings, clock) as h:
        h.bootstrap()
        h.claim_all()
        dev = h.device("Lamp")
        h.write("POST", f"/api/devices/{dev['id']}/actions", json={"capability": "BRIGHTNESS", "value": brightness})
        return dev["id"]


def test_backup_is_verified_and_excludes_the_key(settings, clock):
    _prepare(settings, clock)
    r = B.create_backup(settings)
    archive = settings.backup_dir / r["file"]
    assert r["verified"] and archive.exists()
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "hnb.db" in names and "manifest.json" in names and "providers/demo/world.json" in names
    assert not any("key" in n or "vault" in n for n in names)
    key_bytes = settings.vault_key_file.read_bytes()
    assert key_bytes not in archive.read_bytes()
    m = B.verify_backup(archive)
    assert m["checks"] == {"integrity_check": "ok", "quick_check": "ok", "foreign_key_check": "ok"}
    with Hub(settings, clock) as h:
        h.login()
        assert h.client.get("/api/health").json()["components"]["backup"]["status"] == "ok"
        assert "BACKUP_CREATED" in [e["action"] for e in h.client.get("/api/audit").json()["entries"]]


def test_tampered_backup_fails_verification(settings, clock, tmp_path):
    _prepare(settings, clock)
    archive = settings.backup_dir / B.create_backup(settings)["file"]
    out = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == "hnb.db":
                data = data[:-10] + b"\x00" * 10
            m.size = len(data)
            import io
            dst.addfile(m, io.BytesIO(data))
    with pytest.raises(B.BackupError):
        B.verify_backup(out)


def test_restore_brings_back_state_and_records_it(settings, clock):
    dev_id = _prepare(settings, clock, brightness=21)
    archive = B.create_backup(settings)["file"]
    with Hub(settings, clock) as h:                     # change things after the backup
        h.login()
        h.write("POST", f"/api/devices/{dev_id}/actions", json={"capability": "BRIGHTNESS", "value": 99})
    result = B.restore_backup(settings, settings.backup_dir / archive)
    assert result["secrets_readable"] is True and result["pre_restore_snapshot"]
    with Hub(settings, clock) as h:
        h.login()
        home = h.client.get("/api/home").json()
        lamp = next(d for d in home["devices"] if d["id"] == dev_id)
        assert next(c for c in lamp["controls"] if c["capability"] == "BRIGHTNESS")["value"] == 21
        acts = [e["action"] for e in h.client.get("/api/audit?limit=500").json()["entries"]]
        assert "BACKUP_RESTORED" in acts


def test_restore_onto_a_hub_with_a_different_key_keeps_everything_but_secrets(settings, clock, tmp_path):
    dev_id = _prepare(settings, clock)
    archive = settings.backup_dir / B.create_backup(settings)["file"]
    fresh = make_settings(tmp_path / "new-hub")          # new hardware, new key
    result = B.restore_backup(fresh, archive)
    assert result["secrets_readable"] is False
    with Hub(fresh, clock) as h:
        h.login()                                        # owner and passphrase came back
        home = h.client.get("/api/home").json()
        assert dev_id in [d["id"] for d in home["devices"]]
        health = h.client.get("/api/health").json()["components"]
        assert health["vault"]["status"] == "locked"
        assert health["providers"][0]["status"] == "AUTH_REQUIRED"      # reconnect needed, not "off"


def test_unsafe_archive_entries_are_refused(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    import io
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(B.BackupError, match="unsafe"):
        B.verify_backup(bad)


def test_prune_keeps_the_newest(settings, clock):
    settings = type(settings)(**{**settings.__dict__, "backup_keep": 2})
    _prepare(settings, clock)
    names = []
    for i in range(3):
        f = settings.backup_dir / f"{B.PREFIX}2026010{i}T000000Z.tar.gz"
        f.write_bytes(b"")
        names.append(f.name)
    removed = B.prune(settings)
    assert removed == [names[0]]
    assert json.dumps(removed)

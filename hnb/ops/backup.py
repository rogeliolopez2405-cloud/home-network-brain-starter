"""Backup and restore.

A backup archive (`hnb-backup-<UTC>.tar.gz`, mode 0600) contains:

    manifest.json         format, app version, schema, vault key fingerprint,
                          sha256 + size of every file, integrity results
    hnb.db                WAL-safe online copy (SQLite backup API), then
                          integrity_check + quick_check + foreign_key_check
    providers/…           adapters' non-secret local state
    config/hnb.env        non-secret configuration, when present

It never contains the vault key. Provider secrets inside hnb.db stay sealed;
restoring on a hub without the original key keeps every room, device and
audit entry, and the owner simply reconnects providers. See BACKUP_RESTORE.md.

Restore refuses while the hub is running (it holds hnb.lock), verifies the
archive first, snapshots the current database, then swaps files atomically.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from hnb import __version__
from hnb.config import Settings
from hnb.core.model import AuditAction, HistoryEvent
from hnb.security.vault import key_id, load_key
from hnb.store import db
from hnb.store.repo import Repo

FORMAT = "hnb-backup/1"
PREFIX = "hnb-backup-"


class BackupError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _checks(db_file: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    try:
        integ = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        quick = [r[0] for r in conn.execute("PRAGMA quick_check")]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    out = {"integrity_check": "ok" if integ == ["ok"] else "; ".join(integ[:3]),
           "quick_check": "ok" if quick == ["ok"] else "; ".join(quick[:3]),
           "foreign_key_check": "ok" if not fk else f"{len(fk)} violation(s)",
           "schema": str(version)}
    if any(out[k] != "ok" for k in ("integrity_check", "quick_check", "foreign_key_check")):
        raise BackupError(f"Database checks failed: {out}")
    return out


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_backup(settings: Settings, *, actor: str = "system", source: str = "cli") -> dict[str, Any]:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    if not settings.db_path.exists():
        raise BackupError("There is no database to back up yet.")
    stamp = _stamp()
    with tempfile.TemporaryDirectory(dir=settings.backup_dir, prefix=".work-") as work_s:
        work = Path(work_s)
        live = db.connect(settings.db_path)
        try:
            db.backup_to(live, work / "hnb.db")
        finally:
            live.close()
        checks = _checks(work / "hnb.db")

        files: list[tuple[Path, str]] = [(work / "hnb.db", "hnb.db")]
        if settings.providers_dir.is_dir():
            for p in sorted(settings.providers_dir.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    files.append((p, "providers/" + p.relative_to(settings.providers_dir).as_posix()))
        if settings.config_file and settings.config_file.is_file():
            files.append((settings.config_file, "config/hnb.env"))

        try:
            kid = key_id(load_key(settings.vault_key_file, enforce_permissions=False))
        except Exception:
            kid = None
        manifest = {
            "format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
            "app_version": __version__, "schema": int(checks["schema"]), "vault_key_id": kid,
            "checks": {k: v for k, v in checks.items() if k != "schema"},
            "files": [{"path": arc, "sha256": _sha256(src), "bytes": src.stat().st_size} for src, arc in files],
            "excluded": ["vault key — kept separately; see BACKUP_RESTORE.md"],
        }
        name = f"{PREFIX}{stamp}.tar.gz"
        partial = settings.backup_dir / f".{name}.partial"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode, info.mtime = len(data), 0o600, int(datetime.now().timestamp())
            tar.addfile(info, io.BytesIO(data))
            for src, arc in files:
                tar.add(src, arcname=arc, recursive=False)
            raw.flush()
            os.fsync(raw.fileno())
        final = settings.backup_dir / name
        os.replace(partial, final)

    verified = verify_backup(final)
    sha = _sha256(final)
    info = {"at": manifest["created_at"], "file": name, "sha256": sha, "verified": True,
            "bytes": final.stat().st_size}
    conn = db.connect(settings.db_path)
    try:
        repo = Repo(conn)
        with db.tx(conn):
            repo.kv_set("last_backup", json.dumps(info))
            repo.append_events([HistoryEvent(AuditAction.BACKUP_CREATED, actor, source, target=name,
                                             result="VERIFIED", data={"sha256": sha, "bytes": info["bytes"]})])
    finally:
        conn.close()
    pruned = prune(settings)
    return {**info, "manifest": verified, "pruned": pruned}


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for m in members:
        p = PurePosixPath(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise BackupError(f"Refusing unsafe archive entry: {m.name!r}")
    return members


def verify_backup(archive: Path) -> dict[str, Any]:
    archive = Path(archive)
    if not archive.is_file():
        raise BackupError(f"No backup at {archive}.")
    with tempfile.TemporaryDirectory(prefix="hnb-verify-") as tmp_s:
        tmp = Path(tmp_s)
        with tarfile.open(archive, "r:gz") as tar:
            _safe_members(tar)
            tar.extractall(tmp, filter="data")
        mpath = tmp / "manifest.json"
        if not mpath.is_file():
            raise BackupError("Backup has no manifest.")
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise BackupError(f"Unknown backup format {manifest.get('format')!r}.")
        listed = {f["path"] for f in manifest["files"]}
        present = {p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file()} - {"manifest.json"}
        if listed != present:
            raise BackupError(f"Backup contents don't match its manifest (extra/missing: {sorted(listed ^ present)}).")
        if any("vault" in p or p.endswith(".key") for p in present):
            raise BackupError("Backup unexpectedly contains key material.")
        for f in manifest["files"]:
            if _sha256(tmp / f["path"]) != f["sha256"]:
                raise BackupError(f"Checksum mismatch for {f['path']}.")
        checks = _checks(tmp / "hnb.db")
        if int(checks["schema"]) > db.latest_version():
            raise BackupError("This backup is from a newer version of the hub.")
    return {"format": manifest["format"], "created_at": manifest["created_at"],
            "app_version": manifest["app_version"], "schema": manifest["schema"],
            "vault_key_id": manifest.get("vault_key_id"), "files": len(manifest["files"]),
            "checks": {k: v for k, v in checks.items() if k != "schema"}}


def list_backups(settings: Settings) -> list[Path]:
    return sorted(settings.backup_dir.glob(f"{PREFIX}*.tar.gz"), reverse=True)


def prune(settings: Settings) -> list[str]:
    removed = []
    for old in list_backups(settings)[settings.backup_keep:]:
        old.unlink()
        removed.append(old.name)
    return removed


def _hub_running(settings: Settings) -> bool:
    try:
        import fcntl
    except ImportError:          # non-POSIX dev machine: rely on the operator
        return False
    if not settings.lock_file.exists():
        return False
    with settings.lock_file.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def restore_backup(settings: Settings, archive: Path) -> dict[str, Any]:
    archive = Path(archive)
    if not archive.is_absolute() and not archive.exists():
        archive = settings.backup_dir / archive
    if _hub_running(settings):
        raise BackupError("The hub is running. Stop it first (bin/restore does this for you).")
    manifest = verify_backup(archive)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    snapshot = None
    if settings.db_path.exists():
        live = db.connect(settings.db_path)
        try:
            snapshot = db.backup_to(live, settings.backup_dir / f"pre-restore-{_stamp()}.db")
        finally:
            live.close()

    with tempfile.TemporaryDirectory(dir=settings.data_dir, prefix=".restore-") as tmp_s:
        tmp = Path(tmp_s)
        with tarfile.open(archive, "r:gz") as tar:
            _safe_members(tar)
            tar.extractall(tmp, filter="data")
        staged = tmp / "hnb.db"
        conn = sqlite3.connect(str(staged))
        try:
            conn.execute("INSERT INTO kv (key, value, updated_at) VALUES ('restored_from', ?, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (json.dumps({"file": archive.name, "created_at": manifest["created_at"]}),
                          datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
        os.replace(staged, settings.db_path)
        if (tmp / "providers").is_dir():
            if settings.providers_dir.exists():
                shutil.rmtree(settings.providers_dir)
            shutil.copytree(tmp / "providers", settings.providers_dir)

    try:
        current_kid = key_id(load_key(settings.vault_key_file, enforce_permissions=False))
    except Exception:
        current_kid = None
    return {"restored": archive.name, "created_at": manifest["created_at"],
            "pre_restore_snapshot": snapshot.name if snapshot else None,
            "secrets_readable": manifest.get("vault_key_id") is not None and manifest.get("vault_key_id") == current_kid}

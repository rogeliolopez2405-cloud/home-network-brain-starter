"""SQLite: connection policy, migrations, integrity checks, transactions.

* WAL journal, foreign keys ON, synchronous=NORMAL (durable across app
  crashes; a power cut can lose at most the last transaction, never corrupt).
* Migrations are numbered SQL files, each applied in its own transaction with
  PRAGMA user_version bumped inside it. Before migrating an existing database
  a copy is taken with SQLite's online backup API.
* `tx()` is BEGIN IMMEDIATE ... COMMIT, rolled back on any exception.
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

log = logging.getLogger("hnb.store")

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class StoreError(Exception):
    pass


class IntegrityError(StoreError):
    pass


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    # check_same_thread stays ON: the hub's connection belongs to the event-loop
    # thread. Anything that touches it from a worker thread (a sync FastAPI
    # dependency, asyncio.to_thread) raises at once instead of interleaving two
    # threads on one connection — which corrupted reads under parallel requests.
    if readonly:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True,
                               isolation_level=None, timeout=10)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StoreError(f"Could not enable WAL (got {mode}).")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrations() -> list[tuple[int, str, str]]:
    out = []
    for entry in resources.files("hnb.store.migrations").iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort()
    versions = [v for v, _, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise StoreError(f"Migration numbering has gaps: {versions}")
    return out


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version() -> int:
    return len(migrations())


def backup_to(conn: sqlite3.Connection, dest: Path) -> Path:
    """WAL-safe online copy of a live database into a standalone file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
        out.execute("PRAGMA journal_mode = DELETE")   # self-contained single file
    finally:
        out.close()
    return dest


def migrate(conn: sqlite3.Connection, *, snapshot_dir: Path | None = None) -> list[int]:
    current = schema_version(conn)
    pending = [(v, name, sql) for v, name, sql in migrations() if v > current]
    if current > latest_version():
        raise StoreError(f"Database schema {current} is newer than this app ({latest_version()}). "
                         "Install the newer version or restore a matching backup.")
    if not pending:
        return []
    if current > 0 and snapshot_dir is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snap = backup_to(conn, snapshot_dir / f"pre-migrate-v{current}-{stamp}.db")
        log.info("Pre-migration snapshot written: %s", snap.name)
    applied = []
    for version, name, sql in pending:
        # executescript() commits any open transaction first, then runs the
        # script; BEGIN/COMMIT inside make the migration atomic.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise StoreError(f"Migration {name} failed and was rolled back.")
        log.info("Applied migration %s", name)
        applied.append(version)
    return applied


def check_integrity(conn: sqlite3.Connection, *, full: bool = False) -> dict[str, str]:
    pragma = "integrity_check" if full else "quick_check"
    rows = [r[0] for r in conn.execute(f"PRAGMA {pragma}")]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    result = {pragma: "ok" if rows == ["ok"] else "; ".join(rows[:5]),
              "foreign_key_check": "ok" if not fk else f"{len(fk)} violation(s)"}
    if result[pragma] != "ok" or result["foreign_key_check"] != "ok":
        raise IntegrityError(f"Database integrity check failed: {result}")
    return result


def open_database(path: Path, *, snapshot_dir: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    try:
        migrate(conn, snapshot_dir=snapshot_dir)
        check_integrity(conn)
    except BaseException:
        conn.close()
        raise
    return conn

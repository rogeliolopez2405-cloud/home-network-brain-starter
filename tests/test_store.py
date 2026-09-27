"""SQLite store: pragmas, migrations, round trips, append-only audit, transactions."""
import asyncio
import sqlite3

import pytest

from hnb.core.capabilities import CapabilityType
from hnb.core.home import Home
from hnb.core.model import AuditAction, Ctx, Freshness, House, ReviewStatus
from hnb.providers.demo.adapter import DemoAdapter
from hnb.store import db
from hnb.store.repo import Repo

OWNER = Ctx("owner:test", "test")


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "hnb.db", snapshot_dir=tmp_path / "snap")
    yield c
    c.close()


def test_pragmas_wal_and_foreign_keys(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.schema_version(conn) == db.latest_version() >= 1


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == []


def test_newer_schema_is_refused(tmp_path):
    c = db.connect(tmp_path / "x.db")
    c.execute(f"PRAGMA user_version = {db.latest_version() + 1}")
    with pytest.raises(db.StoreError):
        db.migrate(c)
    c.close()


def test_failed_transaction_writes_nothing(conn):
    repo = Repo(conn)
    with pytest.raises(RuntimeError):
        with db.tx(conn):
            repo.kv_set("a", "1")
            raise RuntimeError("boom")
    assert repo.kv_get("a") is None


def test_audit_log_is_append_only(conn):
    repo = Repo(conn)
    from hnb.core.model import HistoryEvent
    with db.tx(conn):
        repo.append_events([HistoryEvent(AuditAction.SYSTEM_STARTED, "system", "system", result="OK")])
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET result = 'forged'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    assert repo.audit_count() == 1


def test_second_owner_is_refused_by_the_database(conn):
    conn.execute("INSERT INTO owners (id, username, pass_hash, created_at) VALUES ('a', 'one', 'h', 'now')")
    with pytest.raises(sqlite3.DatabaseError, match="owner already exists"):
        conn.execute("INSERT INTO owners (id, username, pass_hash, created_at) VALUES ('b', 'two', 'h', 'now')")


def test_home_round_trips_through_sqlite(conn):
    async def build():
        home = Home(House("Round Trip"), [DemoAdapter()])
        await home.discover("demo")
        lamp = next(d for d in home.devices.values() if d.name == "Lamp")
        home.review(lamp.id, ReviewStatus.MINE, OWNER)
        home.set_favorite(lamp.id, True, OWNER)
        await home.execute(lamp.id, CapabilityType.BRIGHTNESS, 55, OWNER)
        return home, lamp

    home, lamp = asyncio.run(build())
    repo = Repo(conn)
    with db.tx(conn):
        repo.add_provider("demo", "Demo home")
        repo.save_house(home.house)
        for r in home.rooms.values():
            repo.save_room(r)
        for d in home.devices.values():
            repo.save_device(d)
        repo.append_events(home.drain())

    loaded = {d.id: d for d in repo.load_devices()}
    assert set(loaded) == set(home.devices)
    again = loaded[lamp.id]
    assert (again.name, again.review, again.favorite, again.room_id) == ("Lamp", ReviewStatus.MINE, True, lamp.room_id)
    assert again.capabilities == lamp.capabilities
    assert again.capability_route == lamp.capability_route
    assert again.state[CapabilityType.BRIGHTNESS].value == 55
    assert again.state[CapabilityType.BRIGHTNESS].freshness is Freshness.CONFIRMED
    assert [c.key for c in again.connections] == [c.key for c in lamp.connections]
    assert {r.name for r in repo.load_rooms()} == {r.name for r in home.rooms.values()}
    actions = [e["action"] for e in repo.audit(500)]
    assert "COMMAND_CONFIRMED" in actions and "DEVICE_DISCOVERED" in actions


def test_one_provider_handle_is_one_device(conn):
    repo = Repo(conn)
    conn.execute("INSERT INTO providers (provider_id, display_name, created_at) VALUES ('demo', 'Demo', 'now')")
    for dev in ("d1", "d2"):
        conn.execute("INSERT INTO devices (id, name, kind, review, created_at, updated_at) "
                     "VALUES (?, 'x', 'LIGHT', 'UNREVIEWED', 'now', 'now')", (dev,))
    conn.execute("INSERT INTO connections (id, device_id, provider_id, external_id) VALUES ('c1', 'd1', 'demo', 'lamp')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO connections (id, device_id, provider_id, external_id) VALUES ('c2', 'd2', 'demo', 'lamp')")
    assert repo is not None


def test_integrity_check_reports_ok(conn):
    assert db.check_integrity(conn, full=True) == {"integrity_check": "ok", "foreign_key_check": "ok"}

"""Repository: maps the domain model to SQLite rows and back.

The domain (hnb.core) never imports this module. Callers wrap writes in
`hnb.store.db.tx()` so a device, its action and its audit events commit
together or not at all.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from hnb.core.capabilities import CapabilitySpec, CapabilityType
from hnb.core.model import (
    Action,
    Connection,
    Device,
    DeviceKind,
    Freshness,
    HistoryEvent,
    House,
    ReviewStatus,
    Room,
    StateValue,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str, separators=(",", ":"))


def _unj(s: str | None) -> Any:
    return None if s is None else json.loads(s)


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── kv ───────────────────────────────────────────────────────────────
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) "
                          "DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                          (key, value, _now()))

    def kv_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ── house ────────────────────────────────────────────────────────────
    def load_house(self) -> House | None:
        row = self.conn.execute("SELECT id, name, timezone FROM house").fetchone()
        return House(id=row["id"], name=row["name"], timezone=row["timezone"]) if row else None

    def save_house(self, house: House) -> None:
        self.conn.execute("INSERT INTO house (id, name, timezone, created_at) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT (id) DO UPDATE SET name = excluded.name, timezone = excluded.timezone",
                          (house.id, house.name, house.timezone, _now()))

    # ── providers ────────────────────────────────────────────────────────
    def providers(self) -> list[dict[str, Any]]:
        return [{"provider_id": r["provider_id"], "display_name": r["display_name"],
                 "config": json.loads(r["config_json"]), "enabled": bool(r["enabled"]),
                 "created_at": r["created_at"]}
                for r in self.conn.execute("SELECT * FROM providers ORDER BY provider_id")]

    def add_provider(self, provider_id: str, display_name: str, config: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT INTO providers (provider_id, display_name, config_json, created_at) "
                          "VALUES (?, ?, ?, ?)", (provider_id, display_name, _j(config or {}), _now()))

    def set_provider_config(self, provider_id: str, config: dict[str, Any]) -> None:
        cur = self.conn.execute("UPDATE providers SET config_json = ? WHERE provider_id = ?",
                                (_j(config), provider_id))
        if cur.rowcount != 1:
            raise KeyError(provider_id)

    # ── rooms ────────────────────────────────────────────────────────────
    def load_rooms(self) -> list[Room]:
        return [Room(id=r["id"], name=r["name"], icon=r["icon"], sort=r["sort"])
                for r in self.conn.execute("SELECT * FROM rooms ORDER BY sort, name")]

    def save_room(self, room: Room) -> None:
        self.conn.execute("INSERT INTO rooms (id, name, icon, sort, created_at) VALUES (?, ?, ?, ?, ?) "
                          "ON CONFLICT (id) DO UPDATE SET name = excluded.name, icon = excluded.icon, "
                          "sort = excluded.sort", (room.id, room.name, room.icon, room.sort, _now()))

    def delete_room(self, room_id: str) -> None:
        self.conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))

    # ── devices ──────────────────────────────────────────────────────────
    def load_devices(self) -> list[Device]:
        devices: dict[str, Device] = {}
        for r in self.conn.execute("SELECT * FROM devices ORDER BY created_at, id"):
            devices[r["id"]] = Device(
                id=r["id"], name=r["name"], kind=DeviceKind(r["kind"]), room_id=r["room_id"],
                review=ReviewStatus(r["review"]), favorite=bool(r["favorite"]),
                manufacturer=r["manufacturer"], model=r["model"])
        for r in self.conn.execute("SELECT * FROM connections ORDER BY priority, id"):
            devices[r["device_id"]].connections.append(
                Connection(id=r["id"], provider=r["provider_id"], external_id=r["external_id"], priority=r["priority"]))
        for r in self.conn.execute("SELECT * FROM capabilities"):
            cap = CapabilityType(r["type"])
            dev = devices[r["device_id"]]
            dev.capabilities[cap] = CapabilitySpec(cap, readable=bool(r["readable"]), writable=bool(r["writable"]),
                                                   options=json.loads(r["options_json"]))
            dev.capability_route[cap] = r["connection_id"]
        for r in self.conn.execute("SELECT * FROM device_state"):
            cap = CapabilityType(r["capability"])
            devices[r["device_id"]].state[cap] = StateValue(
                cap, _unj(r["value_json"]), Freshness(r["freshness"]), _dt(r["observed_at"]), r["via_connection_id"])
        return list(devices.values())

    def save_device(self, dev: Device) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO devices (id, name, kind, room_id, review, favorite, manufacturer, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name, "
            "kind = excluded.kind, room_id = excluded.room_id, review = excluded.review, favorite = excluded.favorite, "
            "manufacturer = excluded.manufacturer, model = excluded.model, updated_at = excluded.updated_at",
            (dev.id, dev.name, dev.kind.value, dev.room_id, dev.review.value, int(dev.favorite),
             dev.manufacturer, dev.model, now, now))
        for c in dev.connections:
            self.conn.execute(
                "INSERT INTO connections (id, device_id, provider_id, external_id, priority) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET priority = excluded.priority",
                (c.id, dev.id, c.provider, c.external_id, c.priority))
        self.conn.execute("DELETE FROM capabilities WHERE device_id = ?", (dev.id,))
        self.conn.executemany(
            "INSERT INTO capabilities (device_id, type, connection_id, readable, writable, options_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(dev.id, cap.value, dev.capability_route[cap], int(spec.readable), int(spec.writable), _j(spec.options))
             for cap, spec in dev.capabilities.items()])
        self.save_state(dev)

    def save_state(self, dev: Device) -> None:
        self.conn.execute("DELETE FROM device_state WHERE device_id = ?", (dev.id,))
        self.conn.executemany(
            "INSERT INTO device_state (device_id, capability, value_json, freshness, observed_at, via_connection_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(dev.id, cap.value, _j(sv.value), sv.freshness.value, _iso(sv.observed_at), sv.via)
             for cap, sv in dev.state.items()])

    # ── actions ──────────────────────────────────────────────────────────
    def save_action(self, a: Action) -> None:
        self.conn.execute(
            "INSERT INTO actions (id, device_id, capability, value_json, actor, source, outcome, detail, "
            "connection_id, requested_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET value_json = excluded.value_json, outcome = excluded.outcome, "
            "detail = excluded.detail, connection_id = excluded.connection_id, completed_at = excluded.completed_at",
            (a.id, a.device_id, a.capability.value, _j(a.value), a.actor, a.source, a.outcome.value, a.detail,
             a.connection_id, _iso(a.requested_at), _iso(a.completed_at)))

    def pending_actions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM actions WHERE outcome = 'PENDING'").fetchall()

    def fail_pending_action(self, action_id: str, detail: str) -> None:
        self.conn.execute("UPDATE actions SET outcome = 'FAILED', detail = ?, completed_at = ? WHERE id = ?",
                          (detail, _now(), action_id))

    # ── audit ────────────────────────────────────────────────────────────
    def append_events(self, events: Iterable[HistoryEvent]) -> int:
        rows = [(e.id, _iso(e.at), e.actor, e.action.value, e.target,
                 None if e.requested is None else _j(e.requested), e.result, e.source, _j(e.data))
                for e in events]
        self.conn.executemany(
            "INSERT INTO audit_log (id, at, actor, action, target, requested_json, result, source, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def audit(self, limit: int = 100, before_seq: int | None = None, target: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM audit_log WHERE 1 = 1", []
        if before_seq is not None:
            sql += " AND seq < ?"
            args.append(before_seq)
        if target:
            sql += " AND target = ?"
            args.append(target)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [{"seq": r["seq"], "id": r["id"], "at": r["at"], "actor": r["actor"], "action": r["action"],
                 "target": r["target"], "requested": _unj(r["requested_json"]), "result": r["result"],
                 "source": r["source"], "data": json.loads(r["data_json"])}
                for r in self.conn.execute(sql, args)]

    def audit_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

"""HubService — the one place that orchestrates core, store, vault and adapters.

    HTTP handler → HubService → Home (core) → ProviderAdapter
                        ↘ Repo (SQLite, one transaction per change)

Handlers never touch the core or the database directly. Every change follows
the same shape: mutate the in-memory Home, then persist the changed objects
and the events the Home emitted in ONE transaction. If that transaction fails
the in-memory model is reloaded from the database, so memory never claims
something the disk does not.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from hnb import __version__
from hnb.auth import AuthService
from hnb.config import Settings
from hnb.core.capabilities import CapabilityType
from hnb.core.home import Home, HomeError, NotFound
from hnb.core.model import (
    Action,
    ActionOutcome,
    AuditAction,
    Ctx,
    Device,
    HistoryEvent,
    House,
    ReviewStatus,
)
from hnb.core.view import HEALTH_WORDS, device_view, home_view, review_view
from hnb.providers.base import HealthStatus, PairingError, ProviderAdapter
from hnb.providers.catalog import KNOWN_PROVIDERS
from hnb.providers.registry import load_adapters
from hnb.security.vault import ScopedSecrets, SecretVault, VaultKeyError, generate_key_file, load_key
from hnb.store import db
from hnb.store.repo import Repo

log = logging.getLogger("hnb.service")

TRANSITIONS = {
    HealthStatus.DOWN: AuditAction.PROVIDER_UNREACHABLE,
    HealthStatus.UNKNOWN: AuditAction.PROVIDER_UNREACHABLE,
    HealthStatus.AUTH_REQUIRED: AuditAction.PROVIDER_AUTH_REQUIRED,
}
BACKUP_STALE_H = 48
MAX_BACKOFF_S = 300.0


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _snapshot(dev: Device) -> dict[str, tuple[Any, str]]:
    return {c.value: (json.dumps(sv.value, sort_keys=True, default=str), sv.freshness.value)
            for c, sv in dev.state.items()}


class HubService:
    def __init__(self, settings: Settings, *, adapter_classes: dict[str, type[ProviderAdapter]] | None = None,
                 auth_now=None):
        self.settings = settings
        if adapter_classes is None:
            adapter_classes, self.load_errors = load_adapters()
        else:
            self.load_errors = []
        self.adapter_classes = adapter_classes
        self._auth_now = auth_now
        self.conn: sqlite3.Connection | None = None
        self.repo: Repo | None = None
        self.vault: SecretVault | None = None
        self.auth: AuthService | None = None
        self.home: Home | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._discovered: set[str] = set()
        self._pairing = asyncio.Lock()
        self._poll = True
        self._pushed: dict[str, asyncio.Task] = {}          # coalesced refreshes triggered by adapter events
        self._started = time.monotonic()
        self.integrity: dict[str, str] = {}

    # ── lifecycle ────────────────────────────────────────────────────────
    def open(self) -> "HubService":
        """Synchronous part of start-up: database, vault, auth, model."""
        s = self.settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.backup_dir.mkdir(parents=True, exist_ok=True)
        self.conn = db.open_database(s.db_path, snapshot_dir=s.backup_dir)
        self.integrity = db.check_integrity(self.conn)
        self.repo = Repo(self.conn)

        if s.dev and not s.vault_key_file.exists():
            generate_key_file(s.vault_key_file)
            log.warning("Development mode: generated a local vault key.")
        key = load_key(s.vault_key_file, enforce_permissions=not s.dev)
        self.vault = SecretVault(self.conn, key)
        del key

        self.auth = AuthService(self.conn, s.setup_code_file, idle_s=s.session_idle_s,
                                absolute_s=s.session_absolute_s, now=self._auth_now)

        with db.tx(self.conn):
            house = self.repo.load_house()
            if house is None:
                house = House(name="My Home")
                self.repo.save_house(house)
            events = self._recover_pending_actions()
            restored = self.repo.kv_get("restored_from")
            if restored:
                events.append(HistoryEvent(AuditAction.BACKUP_RESTORED, "system", "system",
                                           result="RESTORED", data=json.loads(restored)))
                self.repo.kv_delete("restored_from")
            if s.enable_demo and "demo" in self.adapter_classes and \
                    not any(p["provider_id"] == "demo" for p in self.repo.providers()):
                events.append(self._connect_provider("demo"))
            events.append(HistoryEvent(AuditAction.SYSTEM_STARTED, "system", "system", result="OK",
                                       data={"version": __version__, "schema": db.schema_version(self.conn)}))
            self.repo.append_events(events)

        self.home = self._build_home(house)
        self.auth.ensure_setup_code()
        return self

    def _connect_provider(self, provider_id: str, config: dict[str, Any] | None = None,
                          ctx: Ctx | None = None) -> HistoryEvent:
        cls = self.adapter_classes[provider_id]
        self.repo.add_provider(provider_id, cls.display_name, config or {})
        for name, value in cls.initial_secrets().items():
            self.vault.put(provider_id, name, value)
        ctx = ctx or Ctx("system", "system")
        return HistoryEvent(AuditAction.PROVIDER_CONNECTED, ctx.actor, ctx.source, target=provider_id,
                            result="CONNECTED", data={"name": cls.display_name, "config": config or {},
                                                      "credentials": self.vault.names(provider_id)})

    def _make_adapter(self, provider_id: str, config: dict[str, Any]) -> ProviderAdapter:
        cls = self.adapter_classes[provider_id]
        storage = self.settings.providers_dir / provider_id
        storage.mkdir(parents=True, exist_ok=True)
        adapter = cls(secrets=ScopedSecrets(self.vault, provider_id), config=config, storage=storage)
        adapter.set_update_listener(lambda pid=provider_id: self._schedule_refresh(pid))
        return adapter

    def _schedule_refresh(self, provider_id: str) -> None:
        """An adapter pushed a change. Refresh that provider shortly, coalescing
        bursts (one refresh per 300 ms at most per provider)."""
        if self.conn is None or self.home is None:
            return
        task = self._pushed.get(provider_id)
        if task is not None and not task.done():
            return

        async def run() -> None:
            await asyncio.sleep(0.3)
            try:
                if provider_id in self.home.adapters:
                    await self.poll_provider(provider_id)
            except Exception:
                log.exception("Pushed refresh of %s failed", provider_id)

        try:
            self._pushed[provider_id] = asyncio.get_running_loop().create_task(run(), name=f"push:{provider_id}")
        except RuntimeError:
            pass                                               # no running loop (e.g. during shutdown)

    async def _start_adapter(self, adapter: ProviderAdapter) -> None:
        try:
            await asyncio.wait_for(adapter.start(), 15)
        except Exception:
            log.warning("Starting %s failed; it will report as unreachable until it recovers", adapter.provider_id)

    def _recover_pending_actions(self) -> list[HistoryEvent]:
        events = []
        detail = "The hub restarted before the device answered. It may or may not have changed."
        for row in self.repo.pending_actions():
            self.repo.fail_pending_action(row["id"], detail)
            events.append(HistoryEvent(AuditAction.COMMAND_FAILED, "system", "system", target=row["device_id"],
                                       requested={"capability": row["capability"], "value": json.loads(row["value_json"])},
                                       result="FAILED", data={"action_id": row["id"], "detail": detail}))
        return events

    def _build_home(self, house: House) -> Home:
        adapters = []
        for p in self.repo.providers():
            cls = self.adapter_classes.get(p["provider_id"])
            if not p["enabled"] or cls is None:
                continue
            adapters.append(self._make_adapter(p["provider_id"], p["config"]))
        home = Home(house, adapters, timeout_s=self.settings.provider_timeout_s)
        for room in self.repo.load_rooms():
            home.load_room(room)
        for dev in self.repo.load_devices():
            home.load_device(dev)
        return home

    def _reload(self) -> None:
        """Memory and disk diverged (a commit failed): trust the disk."""
        old = self.home
        house = self.repo.load_house() or old.house
        fresh = Home(house, old.adapters.values(), timeout_s=old.timeout_s)
        fresh.provider_health = old.provider_health
        for room in self.repo.load_rooms():
            fresh.load_room(room)
        for dev in self.repo.load_devices():
            fresh.load_device(dev)
        self.home = fresh

    async def start(self, *, poll: bool = True) -> None:
        if self.conn is None:
            self.open()
        self._poll = poll
        for adapter in list(self.home.adapters.values()):
            await self._start_adapter(adapter)
        for pid in list(self.home.adapters):
            try:
                await self.poll_provider(pid)
            except Exception:
                log.exception("Initial poll of %s failed", pid)
        if poll:
            for pid in list(self.home.adapters):
                self._tasks[pid] = asyncio.create_task(self._provider_loop(pid), name=f"poll:{pid}")

    async def stop(self) -> None:
        for t in self._pushed.values():
            t.cancel()
        self._pushed.clear()
        for t in self._tasks.values():
            t.cancel()
        for t in self._tasks.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self.home:
            for a in self.home.adapters.values():
                try:
                    await asyncio.wait_for(a.close(), 5)
                except Exception:
                    log.exception("Closing adapter %s failed", a.provider_id)
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    # ── persistence ──────────────────────────────────────────────────────
    def _commit(self, *, devices: list[Device] = (), actions: list[Action] = (),
                extra: list[HistoryEvent] = ()) -> None:
        events = self.home.drain() + list(extra)
        created_rooms = {e.target for e in events if e.action is AuditAction.ROOM_CREATED}
        try:
            with db.tx(self.conn):
                for rid in created_rooms:
                    self.repo.save_room(self.home.rooms[rid])
                for d in {d.id: d for d in devices}.values():
                    self.repo.save_device(d)
                for a in actions:
                    self.repo.save_action(a)
                if events:
                    self.repo.append_events(events)
        except Exception:
            log.exception("Commit failed; reloading the model from the database")
            self._reload()
            raise

    # ── polling ──────────────────────────────────────────────────────────
    async def _provider_loop(self, pid: str) -> None:
        interval = self.settings.poll_interval_s
        # Back off while a provider is failing, but not so far that a recovered
        # service stays "Can't reach" for minutes: at most 4 poll intervals.
        max_delay = min(MAX_BACKOFF_S, interval * 4)
        delay = interval
        while True:
            await asyncio.sleep(delay * random.uniform(0.9, 1.1))
            try:
                await self.poll_provider(pid)
                h = self.home.provider_health.get(pid)
                delay = interval if h and h.status is HealthStatus.OK else min(delay * 2, max_delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling %s failed", pid)
                delay = min(delay * 2, max_delay)

    async def poll_provider(self, pid: str) -> None:
        adapter = self.home.adapters[pid]
        row = next((p for p in self.repo.providers() if p["provider_id"] == pid), None)
        if row is not None and row["config"] != adapter._config:
            adapter._config = dict(row["config"])       # config edits take effect on the next poll

        before = self.home.provider_health.get(pid)
        await self.home.check_health([pid])
        after = self.home.provider_health[pid]
        extra = []
        if before is None or before.status is not after.status:
            if after.status is not HealthStatus.OK:
                log.warning("Provider %s is %s: %s", pid, after.status.value, after.detail or "no detail")
            elif before is not None:
                log.info("Provider %s recovered", pid)
            if after.status in TRANSITIONS:
                extra.append(HistoryEvent(TRANSITIONS[after.status], f"provider:{pid}", "poller", target=pid,
                                          result=after.status.value, data={"detail": after.detail}))
            elif before is not None and after.status is HealthStatus.OK:
                extra.append(HistoryEvent(AuditAction.PROVIDER_RECOVERED, f"provider:{pid}", "poller",
                                          target=pid, result="OK"))

        touched: list[Device] = []
        if after.status is HealthStatus.OK and pid not in self._discovered:
            try:
                touched += await self.home.discover(pid, Ctx(f"provider:{pid}", "poller"))
                self._discovered.add(pid)
            except Exception:
                log.warning("Discovery on %s failed; will retry", pid)

        for dev in self.home.devices_of(pid):
            snap = _snapshot(dev)
            await self.home.refresh(dev.id)
            if _snapshot(dev) != snap:
                touched.append(dev)
        self._commit(devices=touched, extra=extra)

    # ── owner operations ─────────────────────────────────────────────────
    async def bootstrap(self, **kw: Any):
        new = await self.auth.bootstrap(**kw)
        self.home.house = self.repo.load_house()      # the owner just named the home
        return new

    def _dev(self, device_id: str) -> Device:
        try:
            return self.home.device(device_id)
        except NotFound as e:
            raise ServiceError(404, str(e)) from None

    async def review(self, device_id: str, status: str, ctx: Ctx) -> dict[str, Any]:
        dev = self._dev(device_id)
        try:
            self.home.review(dev.id, ReviewStatus(status), ctx)
        except (ValueError, HomeError):
            raise ServiceError(400, "Choose MINE, NOT_MINE or IGNORED.") from None
        self._commit(devices=[dev])
        if dev.review is ReviewStatus.MINE:
            await self.home.refresh(dev.id)
            self._commit(devices=[dev])
        return device_view(self.home, dev, technical=True) | {"review": dev.review.value}

    async def update_device(self, device_id: str, ctx: Ctx, *, name: str | None = None,
                            room_id: str | None = None, room_name: str | None = None,
                            clear_room: bool = False, favorite: bool | None = None) -> dict[str, Any]:
        dev = self._dev(device_id)
        try:
            if name is not None and name != dev.name:
                self.home.rename(dev.id, name, ctx)
            if room_name:
                room_id = self.home.add_room(room_name, ctx).id
            if clear_room:
                self.home.assign_room(dev.id, None, ctx)
            elif room_id is not None and room_id != dev.room_id:
                self.home.assign_room(dev.id, room_id, ctx)
            if favorite is not None and favorite != dev.favorite:
                self.home.set_favorite(dev.id, favorite, ctx)
        except NotFound as e:
            self.home.drain()
            self._reload()
            raise ServiceError(404, str(e)) from None
        except HomeError as e:
            self.home.drain()
            self._reload()
            raise ServiceError(400, str(e)) from None
        self._commit(devices=[dev])
        return device_view(self.home, dev, technical=True) | {"review": dev.review.value}

    async def create_room(self, name: str, ctx: Ctx) -> dict[str, Any]:
        try:
            room = self.home.add_room(name, ctx)
        except HomeError as e:
            raise ServiceError(400, str(e)) from None
        self._commit()
        return {"id": room.id, "name": room.name, "icon": room.icon}

    # ── services (providers) and adding devices ──────────────────────────
    @staticmethod
    def _field_view(f) -> dict[str, Any]:
        return {"name": f.name, "label": f.label, "default": f.default, "help": f.help, "choices": list(f.choices)}

    def available_providers(self) -> list[dict[str, Any]]:
        """Installed provider modules (connectable) plus well-known providers whose
        module isn't installed — shown as such, never as connectable."""
        connected = {p["provider_id"] for p in self.repo.providers()}
        out = [{"id": pid, "name": cls.display_name, "description": cls.description, "installed": True,
                "connected": pid in connected, "supports_pairing": cls.supports_pairing,
                "settings": [self._field_view(f) for f in cls.config_fields],
                "secrets": [self._field_view(f) for f in cls.secret_fields]}
               for pid, cls in sorted(self.adapter_classes.items())]
        for k in KNOWN_PROVIDERS:
            if k["id"] not in self.adapter_classes:
                out.append({"id": k["id"], "name": k["name"], "description": k["note"], "installed": False,
                            "connected": False, "supports_pairing": False, "settings": [], "secrets": [],
                            "status_text": "Provider module not installed"})
        return out

    def _clean_config(self, provider_id: str, config: dict[str, Any] | None) -> dict[str, Any]:
        cls = self.adapter_classes[provider_id]
        fields = {f.name: f for f in cls.config_fields}
        out: dict[str, Any] = {}
        for k, v in (config or {}).items():
            if k not in fields:
                raise ServiceError(400, f"{cls.display_name} has no setting {k!r}.")
            v = str(v).strip()
            if not v or len(v) > 200:
                raise ServiceError(400, f"Check the value for {fields[k].label}.")
            if fields[k].choices and v not in fields[k].choices:
                raise ServiceError(400, f"{fields[k].label} must be one of: {', '.join(fields[k].choices)}.")
            if k.endswith("_url") and not v.startswith(("ws://", "wss://", "http://", "https://")):
                raise ServiceError(400, f"{fields[k].label} must start with ws:// or wss://.")
            out[k] = v
        return out

    def _clean_secrets(self, provider_id: str, secrets: dict[str, Any] | None) -> dict[str, str]:
        cls = self.adapter_classes[provider_id]
        fields = {f.name: f for f in cls.secret_fields}
        out: dict[str, str] = {}
        for k, v in (secrets or {}).items():
            if k not in fields:
                raise ServiceError(400, f"{cls.display_name} has no credential {k!r}.")
            v = str(v)
            if not v.strip():
                continue                                   # blank = keep the stored value
            if len(v) > 4096:
                raise ServiceError(400, f"{fields[k].label} is too long.")
            out[k] = v
        return out

    async def _restart_adapter(self, provider_id: str, config: dict[str, Any]) -> None:
        task = self._tasks.pop(provider_id, None)
        if task:
            task.cancel()
        old = self.home.adapters.get(provider_id)
        if old is not None:
            await old.close()
        adapter = self._make_adapter(provider_id, config)
        self.home.adapters[provider_id] = adapter
        await self._start_adapter(adapter)
        await self.poll_provider(provider_id)
        if self._poll:
            self._tasks[provider_id] = asyncio.create_task(self._provider_loop(provider_id), name=f"poll:{provider_id}")

    async def connect_provider(self, provider_id: str, config: dict[str, Any] | None, ctx: Ctx,
                               secrets: dict[str, Any] | None = None) -> dict[str, Any]:
        if provider_id not in self.adapter_classes:
            known = any(k["id"] == provider_id for k in KNOWN_PROVIDERS)
            raise ServiceError(404 if not known else 409,
                               "Provider module not installed. See ADDING_A_PROVIDER.md." if known else "No such service.")
        if provider_id in self.home.adapters:
            raise ServiceError(409, "That service is already connected.")
        clean, creds = self._clean_config(provider_id, config), self._clean_secrets(provider_id, secrets)
        missing = [f.label for f in self.adapter_classes[provider_id].secret_fields if f.name not in creds]
        if missing:
            raise ServiceError(400, f"Enter {', '.join(missing)} to connect.")
        with db.tx(self.conn):
            event = self._connect_provider(provider_id, clean, ctx)
            for name, value in creds.items():
                self.vault.put(provider_id, name, value)
            event.data["credentials"] = self.vault.names(provider_id)       # names only, never values
            self.repo.append_events([event])
        await self._restart_adapter(provider_id, clean)
        return next(p for p in self.providers() if p["id"] == provider_id)

    async def update_provider_settings(self, provider_id: str, config: dict[str, Any] | None, ctx: Ctx,
                                       secrets: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge changed settings into the stored ones, replace any credentials
        given (blank keeps the old one), then restart the adapter."""
        if provider_id not in self.home.adapters:
            raise ServiceError(404, "That service isn't connected.")
        current = next(p["config"] for p in self.repo.providers() if p["provider_id"] == provider_id)
        merged = {**current, **self._clean_config(provider_id, config)}
        creds = self._clean_secrets(provider_id, secrets)
        with db.tx(self.conn):
            self.repo.set_provider_config(provider_id, merged)
            for name, value in creds.items():
                self.vault.put(provider_id, name, value)
            self.repo.append_events([HistoryEvent(AuditAction.PROVIDER_SETTINGS_CHANGED, ctx.actor, ctx.source,
                                                  target=provider_id, requested=merged, result="CHANGED",
                                                  data={"credentials_replaced": sorted(creds)})])
        await self._restart_adapter(provider_id, merged)
        return next(p for p in self.providers() if p["id"] == provider_id)

    async def discover_now(self, provider_id: str, ctx: Ctx) -> dict[str, Any]:
        if provider_id not in self.home.adapters:
            raise ServiceError(404, "That service isn't connected.")
        before = set(self.home.devices)
        try:
            found = await self.home.discover(provider_id, Ctx(f"provider:{provider_id}", ctx.source))
        except Exception:
            self.home.drain()
            raise ServiceError(502, "The service didn't answer. Check Settings → System health.") from None
        self._discovered.add(provider_id)
        for d in found:
            await self.home.refresh(d.id)
        self._commit(devices=found)
        new = [d for d in found if d.id not in before]
        return {"found": len(found), "new": [device_view(self.home, d, technical=True) | {"review": d.review.value}
                                             for d in new]}

    # ── rooms ────────────────────────────────────────────────────────────
    async def rename_room(self, room_id: str, name: str, ctx: Ctx) -> dict[str, Any]:
        room = self.home.rooms.get(room_id)
        if room is None:
            raise ServiceError(404, "No such room.")
        name = " ".join(str(name or "").split())
        if not name or len(name) > 60:
            raise ServiceError(400, "Use a room name of 1–60 characters.")
        other = self.home.find_room(name)
        if other and other.id != room_id:
            raise ServiceError(409, "Another room already has that name.")
        before, room.name = room.name, name
        with db.tx(self.conn):
            self.repo.save_room(room)
            self.repo.append_events([HistoryEvent(AuditAction.ROOM_RENAMED, ctx.actor, ctx.source, target=room.id,
                                                  requested=name, result="CHANGED", data={"before": before})])
        return {"id": room.id, "name": room.name, "icon": room.icon}

    async def delete_room(self, room_id: str, ctx: Ctx) -> None:
        room = self.home.rooms.get(room_id)
        if room is None:
            raise ServiceError(404, "No such room.")
        moved = [d for d in self.home.devices.values() if d.room_id == room_id]
        with db.tx(self.conn):
            for d in moved:
                d.room_id = None
                self.repo.save_device(d)
            self.repo.delete_room(room_id)
            self.repo.append_events([HistoryEvent(AuditAction.ROOM_DELETED, ctx.actor, ctx.source, target=room_id,
                                                  result="DELETED", data={"name": room.name,
                                                                          "devices_unassigned": [d.id for d in moved]})])
        del self.home.rooms[room_id]

    # ── backups, system, setup ───────────────────────────────────────────
    def list_backups(self) -> list[dict[str, Any]]:
        from hnb.ops import backup as ops
        return [{"file": p.name, "bytes": p.stat().st_size,
                 "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
                for p in ops.list_backups(self.settings)]

    async def create_backup(self, ctx: Ctx) -> dict[str, Any]:
        from hnb.ops import backup as ops
        try:
            r = await asyncio.to_thread(ops.create_backup, self.settings, actor=ctx.actor, source=ctx.source)
        except ops.BackupError as e:
            raise ServiceError(500, f"Backup failed: {e}") from None
        return {k: r[k] for k in ("file", "sha256", "bytes", "verified", "at")}

    async def verify_backup(self, name: str) -> dict[str, Any]:
        from hnb.ops import backup as ops
        if "/" in name or "\\" in name or not name.startswith(ops.PREFIX):
            raise ServiceError(400, "Not a backup name.")
        try:
            return await asyncio.to_thread(ops.verify_backup, self.settings.backup_dir / name)
        except ops.BackupError as e:
            raise ServiceError(422, str(e)) from None

    def system_info(self) -> dict[str, Any]:
        from hnb.ops.system import network_facts
        counts = {r.value: 0 for r in ReviewStatus}
        for d in self.home.devices.values():
            counts[d.review.value] += 1
        return {
            "version": __version__, "schema": db.schema_version(self.conn),
            "uptime_s": int(time.monotonic() - self._started),
            "network": network_facts(self.settings.port),
            "devices": counts, "rooms": len(self.home.rooms),
            "providers": {"installed": sorted(self.adapter_classes), "connected": sorted(self.home.adapters)},
            "provider_load_errors": [{"source": e.source, "reason": e.reason} for e in self.load_errors],
            "setup_complete": self.repo.kv_get("setup_completed_at") is not None,
        }

    def complete_setup(self, ctx: Ctx) -> dict[str, Any]:
        with db.tx(self.conn):
            if self.repo.kv_get("setup_completed_at") is None:
                self.repo.kv_set("setup_completed_at", datetime.now(timezone.utc).isoformat())
                self.repo.append_events([HistoryEvent(AuditAction.SETUP_COMPLETED, ctx.actor, ctx.source,
                                                      result="DONE")])
        return {"setup_complete": True}

    async def pair_device(self, provider_id: str, code: str, ctx: Ctx) -> dict[str, Any]:
        """Add a device with a setup code. The code is a credential: it is never
        stored, audited or logged — only the outcome is."""
        adapter = self.home.adapters.get(provider_id)
        if adapter is None:
            raise ServiceError(404, "Connect that service first.")
        if not adapter.supports_pairing:
            raise ServiceError(400, f"{adapter.display_name} doesn't add devices through the hub.")
        if self._pairing.locked():
            raise ServiceError(409, "Already adding a device. Wait for it to finish.")
        async with self._pairing:
            self._commit(extra=[HistoryEvent(AuditAction.PAIRING_REQUESTED, ctx.actor, ctx.source,
                                             target=provider_id, result="PENDING")])
            try:
                refs = await asyncio.wait_for(adapter.pair(code), adapter.pairing_timeout_s + 10)
            except (PairingError, asyncio.TimeoutError) as e:
                why = str(e) if isinstance(e, PairingError) else "Adding the device took too long."
                self._commit(extra=[HistoryEvent(AuditAction.PAIRING_FAILED, ctx.actor, ctx.source,
                                                 target=provider_id, result="FAILED", data={"detail": why})])
                raise ServiceError(422, why) from None
            before = set(self.home.devices)
            await self.home.discover(provider_id, Ctx(f"provider:{provider_id}", "api"))
            keys = {(r.provider, r.external_id) for r in refs}
            added = [d for d in self.home.devices.values()
                     if any(c.key in keys for c in d.connections)]
            for d in added:
                await self.home.refresh(d.id)
            await self._recheck_if_stale(provider_id)         # it just worked: don't keep showing it as down
            self._commit(devices=added, extra=[HistoryEvent(
                AuditAction.PAIRING_SUCCEEDED, ctx.actor, ctx.source, target=provider_id, result="ADDED",
                data={"devices": [d.id for d in added], "names": [d.name for d in added],
                      "new": [d.id for d in added if d.id not in before]})])
        return {"added": [device_view(self.home, d, technical=True) | {"review": d.review.value} for d in added]}

    async def _recheck_if_stale(self, provider_id: str) -> None:
        """A provider just answered, but its recorded health says it's in
        trouble (e.g. its server restarted and the adapter reconnected between
        polls). Re-check now instead of showing its devices as unreachable
        until the next — possibly backed-off — poll."""
        h = self.home.provider_health.get(provider_id)
        if h is None or h.status is not HealthStatus.OK:
            await self.poll_provider(provider_id)

    async def act(self, device_id: str, capability: str, value: Any, ctx: Ctx,
                  confirmed: bool = False) -> dict[str, Any]:
        dev = self._dev(device_id)
        try:
            cap = CapabilityType(capability)
        except ValueError:
            raise ServiceError(400, f"Unknown control: {capability!r}.") from None
        action = self.home.prepare_action(dev.id, cap, value, ctx, confirmed)
        self._commit(actions=[action])                    # the request is on disk before any provider call
        if action.outcome is ActionOutcome.PENDING:
            await self.home.perform_action(action, ctx)
            self._commit(devices=[dev], actions=[action])
            if action.outcome in (ActionOutcome.CONFIRMED, ActionOutcome.ACCEPTED) and action.connection_id:
                await self._recheck_if_stale(dev.connection(action.connection_id).provider)
        return {
            "id": action.id, "device_id": dev.id, "capability": cap.value, "value": action.value,
            "outcome": action.outcome.value, "detail": action.detail,
            "device": device_view(self.home, dev),
        }

    # ── reads ────────────────────────────────────────────────────────────
    def home_view(self) -> dict[str, Any]:
        return home_view(self.home, system={"backup": self.backup_status(), "version": __version__})

    def devices(self) -> list[dict[str, Any]]:
        return review_view(self.home)

    def device_detail(self, device_id: str) -> dict[str, Any]:
        dev = self._dev(device_id)
        view = device_view(self.home, dev, technical=True)
        view["review"] = dev.review.value
        view["room"] = self.home.rooms[dev.room_id].name if dev.room_id in self.home.rooms else None
        view["capabilities"] = [{"type": c.value, "readable": spec.readable, "writable": spec.writable,
                                 "sensitivity": spec.sensitivity.value, "options": spec.options}
                                for c, spec in dev.capabilities.items()]
        return view

    def rooms(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for d in self.home.devices.values():
            if d.room_id and d.review is ReviewStatus.MINE:
                counts[d.room_id] = counts.get(d.room_id, 0) + 1
        return [{"id": r.id, "name": r.name, "icon": r.icon, "device_count": counts.get(r.id, 0)}
                for r in sorted(self.home.rooms.values(), key=lambda r: (r.sort, r.name))]

    def audit(self, limit: int = 100, before: int | None = None, target: str | None = None) -> list[dict[str, Any]]:
        return self.repo.audit(limit, before, target)

    def providers(self) -> list[dict[str, Any]]:
        out = []
        for p in self.repo.providers():
            h = self.home.provider_health.get(p["provider_id"])
            status = h.status if h else HealthStatus.UNKNOWN
            out.append({
                "id": p["provider_id"], "name": p["display_name"], "enabled": p["enabled"],
                "status": status.value, "label": HEALTH_WORDS[status], "detail": h.detail if h else "",
                "devices": len(self.home.devices_of(p["provider_id"])),
                "credentials": self.vault.names(p["provider_id"]),        # names only, never values
                "supports_pairing": bool(getattr(self.adapter_classes.get(p["provider_id"]), "supports_pairing", False)),
                "settings": p["config"],
            })
        return out

    def backup_status(self) -> dict[str, Any]:
        raw = self.repo.kv_get("last_backup")
        if not raw:
            return {"status": "never", "label": "Not backed up yet"}
        info = json.loads(raw)
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(info["at"])).total_seconds() / 3600
        status = "ok" if info.get("verified") and age_h <= BACKUP_STALE_H else "stale"
        return {"status": status, "at": info["at"], "age_hours": round(age_h, 1), "file": info.get("file"),
                "verified": bool(info.get("verified")),
                "label": "Backed up" if status == "ok" else "Backup is out of date"}

    def health(self, detailed: bool) -> dict[str, Any]:
        try:
            self.conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            db_ok = False
        out: dict[str, Any] = {"status": "ok" if db_ok else "error", "version": __version__,
                               "setup_required": self.auth.setup_required() if db_ok else None}
        if not detailed:
            return out
        s = self.settings
        usage = shutil.disk_usage(s.data_dir)
        db_size = sum(p.stat().st_size for p in s.data_dir.glob("hnb.db*") if p.is_file())
        storage_status = "ok" if usage.free > 1_000_000_000 and usage.free / usage.total > 0.05 else "warn"
        providers = self.providers()
        vault = self.vault.status()
        out["components"] = {
            "api": {"status": "ok", "uptime_s": int(time.monotonic() - self._started)},
            "database": {"status": "ok" if db_ok else "error", "schema": db.schema_version(self.conn),
                         "journal_mode": self.conn.execute("PRAGMA journal_mode").fetchone()[0],
                         "foreign_keys": bool(self.conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                         "checks": self.integrity, "size_bytes": db_size,
                         "audit_entries": self.repo.audit_count()},
            "vault": {"status": "ok" if vault["sealed_with_other_key"] == 0 else "locked",
                      "entries": vault["entries"], "sealed_with_other_key": vault["sealed_with_other_key"]},
            "providers": [{k: p[k] for k in ("name", "status", "label", "detail", "devices")} for p in providers],
            "backup": self.backup_status(),
            "storage": {"status": storage_status, "free_bytes": usage.free, "total_bytes": usage.total,
                        "database_bytes": db_size},
            "poller": {"running": sorted(pid for pid, t in self._tasks.items() if not t.done())},
        }
        if any(p["status"] != "OK" for p in providers) or storage_status != "ok" or \
                out["components"]["backup"]["status"] != "ok" or out["components"]["vault"]["status"] != "ok":
            out["status"] = "degraded" if db_ok else "error"
        return out

    # ── operations used by the CLI ───────────────────────────────────────
    def set_provider_config(self, provider_id: str, config: dict[str, Any]) -> None:
        with db.tx(self.conn):
            self.repo.set_provider_config(provider_id, config)


__all__ = ["HubService", "ServiceError", "VaultKeyError"]

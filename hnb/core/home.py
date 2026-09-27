"""The Home aggregate: rooms and devices, plus what the core does with them —
merge discoveries, record the owner's review, and execute actions.

The core knows nothing about storage or HTTP. Every change it makes is
announced as a HistoryEvent in `outbox`; the service layer drains the outbox
and persists events and changed objects in one transaction.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Iterable

from hnb.providers.base import (
    AuthExpired,
    DeviceRef,
    DeviceUnavailable,
    DiscoveredDevice,
    ExecuteStatus,
    HealthStatus,
    ProviderAdapter,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
)

from .capabilities import CapabilityError, CapabilitySpec, CapabilityType, Sensitivity, validate
from .model import (
    OUTCOME_AUDIT,
    REVIEW_AUDIT,
    SYSTEM,
    Action,
    ActionOutcome,
    AuditAction,
    Connection,
    Ctx,
    Device,
    Freshness,
    HistoryEvent,
    House,
    ReviewStatus,
    Room,
    StateValue,
    utcnow,
)

DEFAULT_TIMEOUT_S = 8.0
ROOM_NAME_MAX = 60
DEVICE_NAME_MAX = 80


class HomeError(Exception):
    pass


class NotFound(HomeError):
    pass


def _clean_name(name: str, limit: int, what: str) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise HomeError(f"A {what} needs a name.")
    if len(name) > limit:
        raise HomeError(f"Keep the {what} name under {limit} characters.")
    return name


class Home:
    def __init__(self, house: House, adapters: Iterable[ProviderAdapter] = (), timeout_s: float = DEFAULT_TIMEOUT_S):
        self.house = house
        self.rooms: dict[str, Room] = {}
        self.devices: dict[str, Device] = {}
        self.outbox: list[HistoryEvent] = []
        self.adapters: dict[str, ProviderAdapter] = {a.provider_id: a for a in adapters}
        self.provider_health: dict[str, ProviderHealth] = {}
        self.timeout_s = timeout_s
        self._by_connection: dict[tuple[str, str], str] = {}   # (provider, external_id) -> device id
        self._locks: dict[str, asyncio.Lock] = {}

    # ── events ───────────────────────────────────────────────────────────
    def _record(self, action: AuditAction, ctx: Ctx, target: str | None = None, *,
                requested: Any = None, result: str | None = None, **data: Any) -> HistoryEvent:
        ev = HistoryEvent(action=action, actor=ctx.actor, source=ctx.source, target=target,
                          requested=requested, result=result, data=data)
        self.outbox.append(ev)
        return ev

    def drain(self) -> list[HistoryEvent]:
        out, self.outbox = self.outbox, []
        return out

    # ── loading (from a store) ───────────────────────────────────────────
    def load_room(self, room: Room) -> None:
        self.rooms[room.id] = room

    def load_device(self, dev: Device) -> None:
        self.devices[dev.id] = dev
        for c in dev.connections:
            self._by_connection[c.key] = dev.id

    # ── rooms ────────────────────────────────────────────────────────────
    def find_room(self, name: str) -> Room | None:
        key = " ".join(name.split()).casefold()
        return next((r for r in self.rooms.values() if r.name.casefold() == key), None)

    def add_room(self, name: str, ctx: Ctx = SYSTEM, icon: str = "room") -> Room:
        name = _clean_name(name, ROOM_NAME_MAX, "room")
        existing = self.find_room(name)
        if existing:
            return existing
        room = Room(name=name, icon=icon, sort=len(self.rooms))
        self.rooms[room.id] = room
        self._record(AuditAction.ROOM_CREATED, ctx, room.id, requested=name, result="CREATED")
        return room

    # ── discovery ────────────────────────────────────────────────────────
    async def discover(self, provider_id: str, ctx: Ctx = SYSTEM) -> list[Device]:
        """Ask one provider what it sees and merge it in.

        New devices arrive UNREVIEWED. Devices seen before keep everything the
        owner decided (name, room, review, favourite); only what the provider is
        authoritative for — capabilities, manufacturer, model — is refreshed.
        """
        adapter = self._adapter(provider_id)
        found = await asyncio.wait_for(adapter.discover_devices(), self.timeout_s)
        touched: list[Device] = []
        for d in found:
            if d.ref.provider != provider_id:
                raise HomeError(f"{provider_id} returned a device claiming to be from {d.ref.provider}")
            touched.append(self._ingest(d, ctx))
        return touched

    def _ingest(self, d: DiscoveredDevice, ctx: Ctx) -> Device:
        key = (d.ref.provider, d.ref.external_id)
        existing_id = self._by_connection.get(key)
        if existing_id:
            dev = self.devices[existing_id]
            conn = next(c for c in dev.connections if c.key == key)
            self._set_capabilities(dev, conn, d.capabilities)
            dev.manufacturer = d.manufacturer or dev.manufacturer
            dev.model = d.model or dev.model
            return dev

        conn = Connection(provider=d.ref.provider, external_id=d.ref.external_id)
        dev = Device(name=_clean_name(d.name, DEVICE_NAME_MAX, "device"), kind=d.kind, connections=[conn],
                     manufacturer=d.manufacturer, model=d.model)
        self._set_capabilities(dev, conn, d.capabilities)
        if d.suggested_room:
            dev.room_id = self.add_room(d.suggested_room, ctx).id
        self.devices[dev.id] = dev
        self._by_connection[key] = dev.id
        self._record(AuditAction.DEVICE_DISCOVERED, ctx, dev.id, result="UNREVIEWED",
                     provider=d.ref.provider, name=dev.name, kind=d.kind.value)
        return dev

    def _set_capabilities(self, dev: Device, conn: Connection, specs: Iterable[CapabilitySpec]) -> None:
        # Drop what this connection previously provided, then re-add.
        for cap in [c for c, cid in dev.capability_route.items() if cid == conn.id]:
            del dev.capability_route[cap]
            del dev.capabilities[cap]
        for spec in specs:
            current = dev.capability_route.get(spec.type)
            if current is None or dev.connection(current).priority > conn.priority:
                dev.capabilities[spec.type] = spec
                dev.capability_route[spec.type] = conn.id
        for cap in list(dev.state):
            if cap not in dev.capabilities:
                del dev.state[cap]

    # ── owner decisions ──────────────────────────────────────────────────
    def review(self, device_id: str, status: ReviewStatus, ctx: Ctx) -> Device:
        dev = self.device(device_id)
        if status is ReviewStatus.UNREVIEWED:
            raise HomeError("A device can be marked mine, not mine, or ignored.")
        before = dev.review
        dev.review = status
        self._record(REVIEW_AUDIT[status], ctx, dev.id, requested=status.value,
                     result=status.value, before=before.value, name=dev.name)
        return dev

    def assign_room(self, device_id: str, room_id: str | None, ctx: Ctx) -> Device:
        dev = self.device(device_id)
        if room_id is not None and room_id not in self.rooms:
            raise NotFound("That room doesn't exist.")
        before = dev.room_id
        dev.room_id = room_id
        self._record(AuditAction.ROOM_CHANGED, ctx, dev.id, requested=room_id, result="CHANGED",
                     before=before, name=dev.name)
        return dev

    def rename(self, device_id: str, name: str, ctx: Ctx) -> Device:
        dev = self.device(device_id)
        name = _clean_name(name, DEVICE_NAME_MAX, "device")
        before, dev.name = dev.name, name
        self._record(AuditAction.DEVICE_RENAMED, ctx, dev.id, requested=name, result="CHANGED", before=before)
        return dev

    def set_favorite(self, device_id: str, favorite: bool, ctx: Ctx) -> Device:
        dev = self.device(device_id)
        dev.favorite = bool(favorite)
        self._record(AuditAction.FAVORITE_CHANGED, ctx, dev.id, requested=dev.favorite, result="CHANGED",
                     name=dev.name)
        return dev

    # ── state ────────────────────────────────────────────────────────────
    async def refresh(self, device_id: str) -> Device:
        # Same per-device lock as perform_action: a read that started before a
        # command must not land after it and restore the old value. The wait is
        # bounded by the provider timeout.
        dev = self.device(device_id)
        async with self._locks.setdefault(dev.id, asyncio.Lock()):
            return await self._refresh_locked(dev)

    async def _refresh_locked(self, dev: Device) -> Device:
        for conn in dev.connections:
            adapter = self.adapters.get(conn.provider)
            caps = [c for c, cid in dev.capability_route.items() if cid == conn.id]
            if adapter is None:
                self._mark_unknown(dev, caps)
                continue
            try:
                readings = await asyncio.wait_for(
                    adapter.get_state(DeviceRef(conn.provider, conn.external_id)), self.timeout_s)
            except (ProviderError, asyncio.TimeoutError):
                # Provider trouble says nothing about the physical device.
                self._mark_unknown(dev, caps)
                continue
            now = utcnow()
            reported = set()
            for r in readings:
                if dev.capability_route.get(r.capability) == conn.id:
                    dev.state[r.capability] = StateValue(r.capability, r.value, Freshness.CONFIRMED, now, conn.id)
                    reported.add(r.capability)
            # The provider answered. Capabilities it has no reading for (write-only
            # ones like APP_LAUNCH, CAMERA_LIVE) must not stay stuck at UNKNOWN from
            # an earlier outage — that would show the device as unreachable forever.
            for cap in caps:
                sv = dev.state.get(cap)
                if cap not in reported and sv is not None and sv.freshness is Freshness.UNKNOWN:
                    del dev.state[cap]
        return dev

    def _mark_unknown(self, dev: Device, caps: Iterable[CapabilityType]) -> None:
        for cap in caps:
            prev = dev.state.get(cap)
            dev.state[cap] = StateValue(cap, prev.value if prev else None, Freshness.UNKNOWN,
                                        prev.observed_at if prev else None, prev.via if prev else None)

    def effective_freshness(self, dev: Device, sv: StateValue) -> Freshness:
        if sv.freshness is not Freshness.CONFIRMED or sv.observed_at is None:
            return sv.freshness
        try:
            provider = dev.connection(sv.via).provider if sv.via else None
        except KeyError:
            provider = None
        adapter = self.adapters.get(provider) if provider else None
        window = adapter.freshness_window if adapter else 60.0
        return Freshness.STALE if utcnow() - sv.observed_at > timedelta(seconds=window) else Freshness.CONFIRMED

    async def check_health(self, provider_ids: Iterable[str] | None = None) -> dict[str, ProviderHealth]:
        for pid in list(provider_ids or self.adapters):
            adapter = self.adapters[pid]
            try:
                h = await asyncio.wait_for(adapter.health(), self.timeout_s)
            except asyncio.TimeoutError:
                h = ProviderHealth(HealthStatus.DOWN, "No answer from the service.")
            except Exception as e:  # health() must not raise; if it does, that is itself the answer
                h = ProviderHealth(HealthStatus.UNKNOWN, f"health check failed: {type(e).__name__}")
            self.provider_health[pid] = h
        return dict(self.provider_health)

    # ── actions ──────────────────────────────────────────────────────────
    def prepare_action(self, device_id: str, capability: CapabilityType, value: Any, ctx: Ctx,
                       confirmed: bool = False) -> Action:
        """Validate a command. Returns PENDING (ready to send) or REJECTED.

        Split from `perform_action` so the request can be persisted before any
        provider is contacted — a crash mid-call still leaves evidence.
        """
        dev = self.device(device_id)
        action = Action(device_id=dev.id, capability=capability, value=value, actor=ctx.actor, source=ctx.source)
        self._record(AuditAction.COMMAND_REQUESTED, ctx, dev.id,
                     requested={"capability": capability.value, "value": value},
                     result="PENDING", action_id=action.id, name=dev.name)

        why = None
        spec = dev.capabilities.get(capability)
        if dev.review is not ReviewStatus.MINE:
            why = "This device hasn't been claimed as yours yet."
        elif spec is None:
            why = f"{dev.name} can't do that."
        else:
            try:
                action.value = validate(spec, value)
            except CapabilityError as e:
                why = str(e)
            else:
                if spec.sensitivity is Sensitivity.HIGH and not confirmed:
                    why = "This needs a confirmation step first."
                else:
                    conn = dev.connection(dev.capability_route[capability])
                    action.connection_id = conn.id
                    if conn.provider not in self.adapters:
                        why = "The service that controls this device isn't connected."
        if why:
            self._finish(action, ActionOutcome.REJECTED, why, ctx)
        return action

    async def perform_action(self, action: Action, ctx: Ctx) -> Action:
        if action.outcome is not ActionOutcome.PENDING:
            return action
        dev = self.device(action.device_id)
        conn = dev.connection(action.connection_id)
        adapter = self.adapters[conn.provider]
        capability = action.capability

        lock = self._locks.setdefault(dev.id, asyncio.Lock())
        async with lock:
            try:
                result = await asyncio.wait_for(
                    adapter.execute(DeviceRef(conn.provider, conn.external_id), capability, action.value),
                    self.timeout_s)
            except asyncio.TimeoutError:
                self._mark_unknown(dev, [capability])
                return self._finish(action, ActionOutcome.FAILED,
                                    f"No answer within {self.timeout_s:g}s. The device may or may not have changed.", ctx)
            except AuthExpired:
                self.provider_health[conn.provider] = ProviderHealth(HealthStatus.AUTH_REQUIRED)
                return self._finish(action, ActionOutcome.FAILED, "The connection needs you to sign in again.", ctx)
            except ProviderUnavailable as e:
                self._mark_unknown(dev, [capability])
                return self._finish(action, ActionOutcome.FAILED, str(e) or "Couldn't reach the device's service.", ctx)
            except DeviceUnavailable as e:
                # The device didn't carry out the command: whatever we last knew is no longer trustworthy.
                self._mark_unknown(dev, [capability])
                return self._finish(action, ActionOutcome.FAILED, str(e) or "The device didn't answer.", ctx)
            except ProviderError as e:
                return self._finish(action, ActionOutcome.FAILED, str(e) or "The device's service reported an error.", ctx)

            now = utcnow()
            if result.status is ExecuteStatus.CONFIRMED:
                observed = result.observed.value if result.observed else action.value
                dev.state[capability] = StateValue(capability, observed, Freshness.CONFIRMED, now, conn.id)
                if observed == action.value:
                    return self._finish(action, ActionOutcome.CONFIRMED, "", ctx)
                return self._finish(action, ActionOutcome.ACCEPTED,
                                    "The device reports a different value than requested.", ctx)
            dev.state[capability] = StateValue(capability, action.value, Freshness.ASSUMED, now, conn.id)
            return self._finish(action, ActionOutcome.ACCEPTED, "", ctx)

    def _finish(self, action: Action, outcome: ActionOutcome, detail: str, ctx: Ctx) -> Action:
        action.outcome, action.detail, action.completed_at = outcome, detail, utcnow()
        provider = None
        if action.connection_id:
            try:
                provider = self.devices[action.device_id].connection(action.connection_id).provider
            except KeyError:
                pass
        self._record(OUTCOME_AUDIT[outcome], ctx, action.device_id,
                     requested={"capability": action.capability.value, "value": action.value},
                     result=outcome.value, action_id=action.id, detail=detail, provider=provider)
        return action

    async def execute(self, device_id: str, capability: CapabilityType, value: Any, ctx: Ctx,
                      confirmed: bool = False) -> Action:
        action = self.prepare_action(device_id, capability, value, ctx, confirmed)
        return await self.perform_action(action, ctx)

    # ── helpers ──────────────────────────────────────────────────────────
    def device(self, device_id: str) -> Device:
        try:
            return self.devices[device_id]
        except KeyError:
            raise NotFound("No such device.") from None

    def _adapter(self, provider_id: str) -> ProviderAdapter:
        try:
            return self.adapters[provider_id]
        except KeyError:
            raise HomeError(f"Provider {provider_id!r} isn't connected.") from None

    def claimed(self) -> list[Device]:
        return [d for d in self.devices.values() if d.review is ReviewStatus.MINE]

    def unreviewed(self) -> list[Device]:
        return [d for d in self.devices.values() if d.review is ReviewStatus.UNREVIEWED]

    def devices_of(self, provider_id: str) -> list[Device]:
        return [d for d in self.devices.values() if any(c.provider == provider_id for c in d.connections)]


__all__ = ["Home", "HomeError", "NotFound"]

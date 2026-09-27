"""Canonical home model.

    HOUSE → ROOM → DEVICE → CONNECTION → CAPABILITY → STATE → ACTION → HISTORY

Identity rules (the ones that hurt when they are wrong):

* A Device's identity is its own UUID, minted by the hub. Never an IP address,
  never a MAC, never a provider's id.
* A Connection is one provider's handle on a device: (provider, external_id).
  That pair is unique across the house. One physical device may have several
  connections (e.g. a switch reachable both through Matter and through Alexa);
  it is still one Device.
* Truth is explicit. A provider being unreachable makes a device's state
  UNKNOWN — not OFF, not OFFLINE. A command accepted by a provider is ACCEPTED,
  and only becomes CONFIRMED when state read back agrees.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .capabilities import CapabilitySpec, CapabilityType


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeviceKind(str, Enum):
    LIGHT = "LIGHT"
    SWITCH = "SWITCH"
    PLUG = "PLUG"
    TV = "TV"
    SPEAKER = "SPEAKER"
    CAMERA = "CAMERA"
    DOORBELL = "DOORBELL"
    LOCK = "LOCK"
    THERMOSTAT = "THERMOSTAT"
    SENSOR = "SENSOR"
    NETWORK = "NETWORK"   # router, access point, the hub itself
    OTHER = "OTHER"


class ReviewStatus(str, Enum):
    """Every discovered device starts UNREVIEWED. Only MINE devices are
    controllable or shown on the Home Hub."""
    UNREVIEWED = "UNREVIEWED"
    MINE = "MINE"
    NOT_MINE = "NOT_MINE"
    IGNORED = "IGNORED"


class Freshness(str, Enum):
    CONFIRMED = "CONFIRMED"   # read from the device/provider recently
    ASSUMED = "ASSUMED"       # what we last commanded; not yet read back
    STALE = "STALE"           # was confirmed, but older than the provider's freshness window
    UNKNOWN = "UNKNOWN"       # we cannot say (provider down, never read)


class ActionOutcome(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"         # provider took the command; physical result not yet seen
    CONFIRMED = "CONFIRMED"       # state read back matches the request
    FAILED = "FAILED"             # provider error or timeout — the device may or may not have acted
    REJECTED = "REJECTED"         # refused by the hub before reaching any provider


@dataclass
class House:
    name: str
    timezone: str = "UTC"
    id: str = field(default_factory=new_id)


@dataclass
class Room:
    name: str
    icon: str = "room"
    sort: int = 0
    id: str = field(default_factory=new_id)


@dataclass
class Connection:
    provider: str          # adapter id, e.g. "demo", "matter", "home_assistant"
    external_id: str       # the provider's own id for the device — opaque to the core
    priority: int = 0      # lower wins when several connections offer a capability
    id: str = field(default_factory=new_id)

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.external_id)


@dataclass
class StateValue:
    capability: CapabilityType
    value: Any
    freshness: Freshness
    observed_at: datetime | None = None
    via: str | None = None   # connection id that reported it


@dataclass
class Device:
    name: str
    kind: DeviceKind
    connections: list[Connection] = field(default_factory=list)
    capabilities: dict[CapabilityType, CapabilitySpec] = field(default_factory=dict)
    # Which connection serves each capability (resolved from connection priority).
    capability_route: dict[CapabilityType, str] = field(default_factory=dict)
    state: dict[CapabilityType, StateValue] = field(default_factory=dict)
    room_id: str | None = None
    review: ReviewStatus = ReviewStatus.UNREVIEWED
    favorite: bool = False
    manufacturer: str | None = None
    model: str | None = None
    id: str = field(default_factory=new_id)

    def connection(self, connection_id: str) -> Connection:
        for c in self.connections:
            if c.id == connection_id:
                return c
        raise KeyError(connection_id)


@dataclass(frozen=True)
class Ctx:
    """Who is asking, and through which channel. Travels with every change."""
    actor: str    # "owner:<username>" | "system" | "provider:<id>"
    source: str   # "api" | "system" | "poller" | "cli"


SYSTEM = Ctx("system", "system")


@dataclass
class Action:
    device_id: str
    capability: CapabilityType
    value: Any
    actor: str
    source: str = "system"
    outcome: ActionOutcome = ActionOutcome.PENDING
    detail: str = ""
    connection_id: str | None = None
    requested_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None
    id: str = field(default_factory=new_id)


class AuditAction(str, Enum):
    SYSTEM_STARTED = "SYSTEM_STARTED"
    OWNER_CREATED = "OWNER_CREATED"
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    HOUSE_RENAMED = "HOUSE_RENAMED"
    PROVIDER_CONNECTED = "PROVIDER_CONNECTED"
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    PROVIDER_AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"
    PROVIDER_RECOVERED = "PROVIDER_RECOVERED"
    ROOM_CREATED = "ROOM_CREATED"
    DEVICE_DISCOVERED = "DEVICE_DISCOVERED"
    DEVICE_CLAIMED = "DEVICE_CLAIMED"
    DEVICE_NOT_MINE = "DEVICE_NOT_MINE"
    DEVICE_IGNORED = "DEVICE_IGNORED"
    DEVICE_RENAMED = "DEVICE_RENAMED"
    FAVORITE_CHANGED = "FAVORITE_CHANGED"
    ROOM_CHANGED = "ROOM_CHANGED"
    COMMAND_REQUESTED = "COMMAND_REQUESTED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
    COMMAND_CONFIRMED = "COMMAND_CONFIRMED"
    COMMAND_FAILED = "COMMAND_FAILED"
    BACKUP_CREATED = "BACKUP_CREATED"
    BACKUP_RESTORED = "BACKUP_RESTORED"
    PROVIDER_SETTINGS_CHANGED = "PROVIDER_SETTINGS_CHANGED"
    PAIRING_REQUESTED = "PAIRING_REQUESTED"
    PAIRING_SUCCEEDED = "PAIRING_SUCCEEDED"
    PAIRING_FAILED = "PAIRING_FAILED"
    ROOM_RENAMED = "ROOM_RENAMED"
    ROOM_DELETED = "ROOM_DELETED"
    SETUP_COMPLETED = "SETUP_COMPLETED"


REVIEW_AUDIT = {
    ReviewStatus.MINE: AuditAction.DEVICE_CLAIMED,
    ReviewStatus.NOT_MINE: AuditAction.DEVICE_NOT_MINE,
    ReviewStatus.IGNORED: AuditAction.DEVICE_IGNORED,
}
OUTCOME_AUDIT = {
    ActionOutcome.REJECTED: AuditAction.COMMAND_REJECTED,
    ActionOutcome.ACCEPTED: AuditAction.COMMAND_ACCEPTED,
    ActionOutcome.CONFIRMED: AuditAction.COMMAND_CONFIRMED,
    ActionOutcome.FAILED: AuditAction.COMMAND_FAILED,
}


@dataclass(frozen=True)
class HistoryEvent:
    """Append-only audit record: who did what, to what, asking for what, with
    what result, when, and through which channel. Never updated or deleted."""
    action: AuditAction
    actor: str
    source: str
    target: str | None = None
    requested: Any = None
    result: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=utcnow)
    id: str = field(default_factory=new_id)

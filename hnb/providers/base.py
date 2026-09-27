"""The ProviderAdapter contract.

An adapter is the only code that knows a provider exists. It speaks the
provider's protocol on one side and the canonical vocabulary on the other.

What an adapter is given, and what it is not:

* It receives a `DeviceRef` (its own external id) — never the hub's device
  UUID, room, owner or any other provider's data.
* It receives its own credentials through a `SecretHandle` scoped to it. It
  cannot enumerate or read any other provider's secrets.
* It does not enforce timeouts, retries or review status — the core does, so
  a misbehaving adapter cannot hang the hub or act on a device the owner
  has not claimed.

What an adapter must do:

* Declare only capabilities the device really has. The Hub shows a control
  for every declared writable capability and nothing else.
* Report honestly. If the provider is unreachable, `get_state` raises
  `ProviderUnavailable`; it does not return OFF.
* Distinguish "the provider accepted my command" (ACCEPTED) from "I read the
  state back and it matches" (CONFIRMED).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Protocol

from hnb.core.capabilities import CapabilitySpec, CapabilityType
from hnb.core.model import DeviceKind


@dataclass(frozen=True)
class DeviceRef:
    provider: str
    external_id: str


@dataclass(frozen=True)
class DiscoveredDevice:
    ref: DeviceRef
    name: str                      # the provider's name for it, e.g. "Kitchen Ceiling"
    kind: DeviceKind
    capabilities: tuple[CapabilitySpec, ...]
    suggested_room: str | None = None   # the provider's room/group name, if it has one
    manufacturer: str | None = None
    model: str | None = None
    # Hints for merging duplicates across providers (e.g. a Matter unique id).
    # Never used as identity on their own.
    identity_hints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StateReading:
    capability: CapabilityType
    value: Any


class ExecuteStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    CONFIRMED = "CONFIRMED"


@dataclass(frozen=True)
class ExecuteResult:
    status: ExecuteStatus
    # Optional read-back the adapter already has, to avoid a second round trip.
    observed: StateReading | None = None


class HealthStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"            # working, but slow or partially failing
    AUTH_REQUIRED = "AUTH_REQUIRED"  # the owner must sign in again
    DOWN = "DOWN"                    # provider unreachable
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProviderHealth:
    status: HealthStatus
    detail: str = ""   # human-readable; must never contain a secret


class AuthStatus(str, Enum):
    OK = "OK"
    NEEDS_USER = "NEEDS_USER"   # show the owner `url` / instructions
    FAILED = "FAILED"


@dataclass(frozen=True)
class AuthResult:
    status: AuthStatus
    url: str | None = None
    detail: str = ""


class ProviderError(Exception):
    """Base for adapter failures. Messages are shown to the owner — no secrets."""


class ProviderUnavailable(ProviderError):
    """The provider could not be reached. Says nothing about the device."""


class DeviceUnavailable(ProviderError):
    """The provider answered and reports this device unreachable."""


class AuthExpired(ProviderError):
    """Credentials were rejected; the owner must re-authenticate."""


class PairingError(ProviderError):
    """Adding a device failed. The message is shown to the owner as-is."""


@dataclass(frozen=True)
class ConfigField:
    """A non-secret setting the owner may see and change for a provider.
    With `choices`, the value must be one of them (shown as a picker)."""
    name: str
    label: str
    default: str
    help: str = ""
    choices: tuple[str, ...] = ()


class SecretHandle(Protocol):
    """Read-only access to one provider's own secrets."""

    def get(self, name: str) -> str | None: ...


class ProviderAdapter(abc.ABC):
    """Implement one of these per provider. See PROVIDERS.md."""

    provider_id: ClassVar[str]          # stable, lowercase: "matter", "home_assistant"
    display_name: ClassVar[str]         # shown to the owner: "Matter", "Home Assistant"
    # Seconds a CONFIRMED reading stays fresh before it is shown as STALE.
    freshness_window: ClassVar[float] = 60.0
    # Names of secrets this adapter reads through its SecretHandle.
    required_secrets: ClassVar[tuple[str, ...]] = ()
    # One line the owner sees when choosing a service to add.
    description: ClassVar[str] = ""
    # Non-secret settings (e.g. where the provider's server is). Never secrets.
    config_fields: ClassVar[tuple[ConfigField, ...]] = ()
    # Credentials the OWNER types in (API keys, tokens). Stored only in the
    # encrypted vault, never returned by any API; read them with
    # self._secrets.get(name). List each name in required_secrets too.
    secret_fields: ClassVar[tuple[ConfigField, ...]] = ()
    # True if the owner can add devices through the hub (see `pair`).
    supports_pairing: ClassVar[bool] = False
    # How long adding one device may take (Matter commissioning can take minutes).
    pairing_timeout_s: ClassVar[float] = 180.0

    def __init__(self, secrets: SecretHandle | None = None, config: dict[str, Any] | None = None,
                 storage: Path | None = None):
        """`storage` is a private directory for non-secret provider state
        (caches, pairing metadata). Secrets never go there — only the vault."""
        self._secrets = secrets
        self._config = dict(config or {})
        self._storage = storage
        self._on_update: Callable[[], None] | None = None

    def set_update_listener(self, callback: Callable[[], None] | None) -> None:
        """The core registers a callback; adapters that learn about changes by
        push (subscriptions, events) call `_notify_update()` so the Hub refreshes
        now instead of at the next poll. Polling stays the safety net."""
        self._on_update = callback

    def _notify_update(self) -> None:
        if self._on_update is not None:
            try:
                self._on_update()
            except Exception:
                pass                       # a listener bug must never break the adapter

    @classmethod
    def initial_secrets(cls) -> dict[str, str]:
        """Secrets the hub can mint itself when the provider is connected.
        Real providers return {} — their secrets come from the owner's sign-in."""
        return {}

    @abc.abstractmethod
    async def discover_devices(self) -> list[DiscoveredDevice]:
        """Everything this provider can see. The core decides what is new."""

    @abc.abstractmethod
    async def get_capabilities(self, ref: DeviceRef) -> list[CapabilitySpec]:
        """Current capabilities (firmware updates can change them)."""

    @abc.abstractmethod
    async def get_state(self, ref: DeviceRef) -> list[StateReading]:
        """Current state of every readable capability."""

    @abc.abstractmethod
    async def execute(self, ref: DeviceRef, capability: CapabilityType, value: Any) -> ExecuteResult:
        """Perform a write. `value` has already been validated by the core."""

    @abc.abstractmethod
    async def health(self) -> ProviderHealth:
        """Cheap liveness + auth check. Must not raise."""

    async def reauthenticate(self) -> AuthResult:
        """Refresh or restart authentication. Default: nothing to do."""
        return AuthResult(AuthStatus.OK)

    async def pair(self, code: str) -> list[DeviceRef]:
        """Add a device using an owner-supplied setup code (only when
        `supports_pairing`). Returns the refs of the device(s) added; the core
        then discovers them, and they start UNREVIEWED like everything else.
        The code is a credential: never log it, never put it in an error."""
        raise PairingError(f"{self.display_name} doesn't add devices through the hub.")

    async def start(self) -> None:
        """Open long-lived connections. Called once before the first poll."""

    async def close(self) -> None:
        """Release connections. Called on shutdown."""

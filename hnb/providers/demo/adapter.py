"""Demo provider — a fictional Alexa-style house.

The reference implementation of the ProviderAdapter contract, and the adapter
M1 ships enabled so a fresh install has something to control. It runs through
exactly the same service, database, vault and audit path as a real provider:

* Its "physical world" (what each fake device is doing) lives in the
  adapter's private storage directory, so it survives hub restarts the way a
  real lamp keeps its state when the hub reboots.
* It needs an access token from the vault, minted when the provider is
  connected. If the vault can't produce it (wrong key after a restore, entry
  deleted) the provider reports AUTH_REQUIRED — just as a real cloud would.

Failure injection via provider config — the owner can flip "Simulate a
problem" in Settings to see how the Hub reports trouble (real backend path):
    {"simulate": "outage"}           provider unreachable   (legacy: {"fail": "down"})
    {"simulate": "expired sign-in"}  credentials rejected   (legacy: {"fail": "auth"})
    {"delay_s": 10}       slow provider (timeouts)
    {"confirm": false}    accepts writes without read-back
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets as _secrets
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from hnb.core.capabilities import CapabilitySpec, CapabilityType
from hnb.core.model import DeviceKind
from hnb.providers.base import (
    AuthExpired,
    AuthResult,
    AuthStatus,
    ConfigField,
    DeviceRef,
    DeviceUnavailable,
    DiscoveredDevice,
    ExecuteResult,
    ExecuteStatus,
    HealthStatus,
    ProviderAdapter,
    ProviderHealth,
    ProviderUnavailable,
    StateReading,
)

DEFAULT_FIXTURE = "alexa_style_house.json"
WORLD_FILE = "world.json"
TOKEN = "access_token"


def load_fixture(name: str = DEFAULT_FIXTURE) -> dict[str, Any]:
    text = resources.files("hnb.providers.demo").joinpath("fixtures", name).read_text(encoding="utf-8")
    return json.loads(text)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".world-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class DemoAdapter(ProviderAdapter):
    provider_id = "demo"
    display_name = "Demo home"
    description = "A made-up Alexa-style house to try the hub with. No account needed."
    freshness_window = 3600.0
    required_secrets = (TOKEN,)
    config_fields = (ConfigField("simulate", "Simulate a problem", "none",
                                 "See how the Home Hub reports a service that stops answering or needs signing in again.",
                                 choices=("none", "outage", "expired sign-in")),)

    def __init__(self, secrets=None, config: dict[str, Any] | None = None, storage: Path | None = None):
        super().__init__(secrets, config, storage)
        data = load_fixture(self._config.get("fixture", DEFAULT_FIXTURE))
        self.house = data["house"]
        self._devices = {d["id"]: d for d in data["devices"]}
        self._state = {d["id"]: copy.deepcopy(d.get("state", {})) for d in data["devices"]}
        self._world_lock = asyncio.Lock()
        world = self._world_path()
        if world and world.exists():
            saved = json.loads(world.read_text(encoding="utf-8"))
            for dev_id, st in saved.get("state", {}).items():
                if dev_id in self._state:
                    self._state[dev_id].update(st)

    @classmethod
    def initial_secrets(cls) -> dict[str, str]:
        return {TOKEN: "demo_" + _secrets.token_urlsafe(24)}

    def _fail(self) -> str | None:
        return self._config.get("fail") or {"outage": "down", "expired sign-in": "auth"}.get(
            str(self._config.get("simulate", "none")))

    def _world_path(self) -> Path | None:
        return self._storage / WORLD_FILE if self._storage else None

    def _token_ok(self) -> bool:
        if self._secrets is None:        # used without a vault (unit tests)
            return True
        try:
            tok = self._secrets.get(TOKEN)
        except Exception:                 # vault can't decrypt: treat as signed out
            return False
        return bool(tok) and tok.startswith("demo_")

    async def _gate(self) -> None:
        delay = self._config.get("delay_s")
        if delay:
            await asyncio.sleep(float(delay))
        fail = self._fail()
        if fail == "down":
            raise ProviderUnavailable("The demo service is switched off.")
        if fail == "auth" or not self._token_ok():
            raise AuthExpired("Demo sign-in expired.")

    def _raw(self, ref: DeviceRef) -> dict[str, Any]:
        try:
            return self._devices[ref.external_id]
        except KeyError:
            raise DeviceUnavailable("The demo home has no such device.") from None

    @staticmethod
    def _specs(raw: dict[str, Any]) -> tuple[CapabilitySpec, ...]:
        return tuple(CapabilitySpec(CapabilityType(c["type"]), options=c.get("options", {}))
                     for c in raw.get("capabilities", []))

    async def discover_devices(self) -> list[DiscoveredDevice]:
        await self._gate()
        return [
            DiscoveredDevice(
                ref=DeviceRef(self.provider_id, raw["id"]),
                name=raw["name"],
                kind=DeviceKind(raw["kind"]),
                capabilities=self._specs(raw),
                suggested_room=raw.get("room"),
                manufacturer=raw.get("manufacturer"),
                model=raw.get("model"),
            )
            for raw in self._devices.values()
        ]

    async def get_capabilities(self, ref: DeviceRef) -> list[CapabilitySpec]:
        await self._gate()
        return list(self._specs(self._raw(ref)))

    async def get_state(self, ref: DeviceRef) -> list[StateReading]:
        await self._gate()
        self._raw(ref)
        return [StateReading(CapabilityType(k), copy.deepcopy(v)) for k, v in self._state[ref.external_id].items()]

    async def execute(self, ref: DeviceRef, capability: CapabilityType, value: Any) -> ExecuteResult:
        await self._gate()
        raw = self._raw(ref)
        if capability.value not in {c["type"] for c in raw.get("capabilities", [])}:
            # The core should never let this through; if it does, refuse loudly.
            raise DeviceUnavailable(f"{raw['name']} has no {capability.value}.")
        async with self._world_lock:
            self._state[ref.external_id][capability.value] = copy.deepcopy(value)
            world = self._world_path()
            if world:
                await asyncio.to_thread(_atomic_write_json, world, {"version": 1, "state": self._state})
        if self._config.get("confirm", True):
            return ExecuteResult(ExecuteStatus.CONFIRMED, StateReading(capability, copy.deepcopy(value)))
        return ExecuteResult(ExecuteStatus.ACCEPTED)

    async def health(self) -> ProviderHealth:
        fail = self._fail()
        if fail == "down":
            return ProviderHealth(HealthStatus.DOWN, "Demo service is switched off.")
        if fail == "auth" or not self._token_ok():
            return ProviderHealth(HealthStatus.AUTH_REQUIRED, "Demo sign-in expired. Reconnect the service.")
        return ProviderHealth(HealthStatus.OK)

    async def reauthenticate(self) -> AuthResult:
        if self._fail() == "auth":
            self._config.pop("fail", None)
            self._config["simulate"] = "none"
        if not self._token_ok():
            return AuthResult(AuthStatus.NEEDS_USER, detail="Reconnect the demo service in Settings.")
        return AuthResult(AuthStatus.OK)

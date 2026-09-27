"""Example provider: "Acme" smart plugs, reached through a small hub on the LAN.

This file is the template `python -m hnb.cli new-provider <id>` copies. Read it
top to bottom — every rule a provider must follow is shown once, in context.

The Acme hub speaks newline-delimited JSON over TCP (see fake_device.py):

    → {"key": "<api key>", "op": "list"}
    ← {"ok": true, "plugs": [{"id": "p1", "name": "Desk Plug", "on": false, "level": null}, ...]}
    → {"key": "...", "op": "set", "id": "p2", "level": 40}
    ← {"ok": true}
    ← {"ok": false, "error": "unauthorized" | "no such plug" | "..."}

Your provider will speak something else (HTTP, WebSocket, a vendor SDK); the
shape of the adapter stays the same.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from hnb.providers.sdk import (
    AuthExpired,
    CapabilitySpec,
    CapabilityType,
    ConfigField,
    DeviceKind,
    DeviceRef,
    DeviceUnavailable,
    DiscoveredDevice,
    ExecuteResult,
    ExecuteStatus,
    HealthStatus,
    ProviderAdapter,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
    StateReading,
)

REQUEST_TIMEOUT_S = 5.0     # the core also times every call out (8 s); fail a little sooner with a clear reason


class AcmeAdapter(ProviderAdapter):
    # ── identity: what the owner sees in Settings → Add a service ─────────
    provider_id = "acme_plug"                       # stable, lowercase, unique; never change it once shipped
    display_name = "Acme plugs"
    description = "Acme smart plugs through the Acme hub on your network."

    # ── settings (non-secret) and credentials (secret) ─────────────────────
    config_fields = (
        ConfigField("host", "Acme hub address", "127.0.0.1", "The hub's IP address or name on your network."),
        ConfigField("port", "Acme hub port", "7788"),
    )
    secret_fields = (ConfigField("api_key", "Acme API key", "", "From the Acme app → Settings → Developer."),)
    required_secrets = ("api_key",)                 # read with self._secrets.get("api_key")

    # State we read is trustworthy for this long before the Hub shows "Last known".
    freshness_window = 120.0

    # ── transport: one request, one answer, always with a timeout ─────────
    async def _call(self, op: str, **args: Any) -> dict[str, Any]:
        key = self._secrets.get("api_key") if self._secrets else None
        if not key:
            raise AuthExpired("No Acme API key saved. Add it in Settings.")
        host = self._config.get("host", "127.0.0.1")
        port = int(self._config.get("port", "7788"))
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), REQUEST_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            # Can't reach the PROVIDER. This says nothing about the devices: the core
            # will show them as "Can't reach" (UNKNOWN), never as off.
            raise ProviderUnavailable("Can't reach the Acme hub.") from None
        try:
            writer.write((json.dumps({"key": key, "op": op, **args}) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            raise ProviderUnavailable("The Acme hub stopped answering.") from None
        finally:
            writer.close()
        if not line:
            raise ProviderUnavailable("The Acme hub closed the connection.")
        reply = json.loads(line)
        if reply.get("ok"):
            return reply
        error = str(reply.get("error", "error"))
        # Map provider errors onto the SDK's error model. Messages are shown to the
        # owner: plain words, and never a secret.
        if error == "unauthorized":
            raise AuthExpired("The Acme hub rejected the API key.")
        if error == "no such plug":
            raise DeviceUnavailable("That plug is no longer on the Acme hub.")
        raise ProviderError("The Acme hub reported an error.")

    # ── mapping: provider data → canonical devices and capabilities ───────
    @staticmethod
    def _specs(plug: dict[str, Any]) -> tuple[CapabilitySpec, ...]:
        specs = [CapabilitySpec(CapabilityType.POWER)]
        if plug.get("level") is not None:           # declare only what the device really has
            specs.append(CapabilitySpec(CapabilityType.BRIGHTNESS))
        return tuple(specs)

    @staticmethod
    def _readings(plug: dict[str, Any]) -> list[StateReading]:
        out = [StateReading(CapabilityType.POWER, bool(plug["on"]))]
        if plug.get("level") is not None:
            out.append(StateReading(CapabilityType.BRIGHTNESS, int(plug["level"])))
        return out

    async def _plug(self, ref: DeviceRef) -> dict[str, Any]:
        return (await self._call("get", id=ref.external_id))["plug"]

    # ── the contract ──────────────────────────────────────────────────────
    async def discover_devices(self) -> list[DiscoveredDevice]:
        plugs = (await self._call("list"))["plugs"]
        return [DiscoveredDevice(
            ref=DeviceRef(self.provider_id, p["id"]),        # the provider's own id: opaque to the core
            name=p["name"],
            kind=DeviceKind.PLUG,
            capabilities=self._specs(p),
            suggested_room=p.get("room"),                  # the owner can change it; it's a suggestion
            manufacturer="Acme",
            model=p.get("model"),
        ) for p in plugs]

    async def get_capabilities(self, ref: DeviceRef) -> list[CapabilitySpec]:
        return list(self._specs(await self._plug(ref)))

    async def get_state(self, ref: DeviceRef) -> list[StateReading]:
        return self._readings(await self._plug(ref))

    async def execute(self, ref: DeviceRef, capability: CapabilityType, value: Any) -> ExecuteResult:
        # `value` is already validated and normalised by the core (e.g. brightness 0–100 int).
        if capability is CapabilityType.POWER:
            await self._call("set", id=ref.external_id, on=bool(value))
        elif capability is CapabilityType.BRIGHTNESS:
            await self._call("set", id=ref.external_id, level=int(value))
        else:
            raise DeviceUnavailable("Acme plugs can't do that.")
        # State verification: "sent" is not "done". Read the device back and report
        # what it actually says. The core marks CONFIRMED only if it matches.
        try:
            state = {r.capability: r.value for r in await self.get_state(ref)}
        except ProviderError:
            return ExecuteResult(ExecuteStatus.ACCEPTED)   # sent, but we couldn't confirm
        return ExecuteResult(ExecuteStatus.CONFIRMED, StateReading(capability, state.get(capability)))

    async def health(self) -> ProviderHealth:
        # Cheap, and must never raise: turn every failure into a status.
        try:
            await self._call("list")
        except AuthExpired:
            return ProviderHealth(HealthStatus.AUTH_REQUIRED, "The Acme API key was rejected. Update it in Settings.")
        except ProviderUnavailable:
            return ProviderHealth(HealthStatus.DOWN, "Can't reach the Acme hub.")
        except Exception:
            return ProviderHealth(HealthStatus.UNKNOWN, "The Acme hub gave an unexpected answer.")
        return ProviderHealth(HealthStatus.OK)


# The hub loads plugins/<name>/adapter.py and uses ADAPTER.
ADAPTER = AcmeAdapter

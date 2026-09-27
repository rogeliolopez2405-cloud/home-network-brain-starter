"""Matter provider adapter.

    Matter server (own container) ⇄ MatterServerClient ⇄ MatterAdapter
        → normalized ProviderAdapter → Starter core → Home Hub

The core never sees node ids, clusters or attribute paths: the adapter speaks
canonical capabilities and opaque refs ("<node>-<endpoint>").

M2 scope: lights, in-wall switches and plugs — POWER / SWITCH / BRIGHTNESS /
COLOR / COLOR_TEMPERATURE, only when the device serves them. Devices are added
by **network commissioning** with a setup code: the path for a device another
ecosystem (e.g. Alexa) already controls and shares via Matter multi-admin.
Out-of-box Bluetooth commissioning is not in M2.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from hnb.core.capabilities import CapabilitySpec, CapabilityType
from hnb.providers.base import (
    ConfigField,
    DeviceRef,
    DeviceUnavailable,
    DiscoveredDevice,
    ExecuteResult,
    ExecuteStatus,
    HealthStatus,
    PairingError,
    ProviderAdapter,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
    StateReading,
)
from hnb.security.redact import REDACTOR

from . import mapping as M
from .client import MatterServerClient, MatterServerError, ServerUnavailable

log = logging.getLogger("hnb.matter")

DEFAULT_URL = "ws://127.0.0.1:5580/ws"
MANUAL_CODE = re.compile(r"^\d{11}$|^\d{21}$")
QR_CODE = re.compile(r"^MT:[0-9A-Z.\-]{19,}$")


def pairing_failure_message(details: str) -> str:
    """Turn the Matter server's reason into an accurate owner message. The
    server's text never contains the setup code; it still isn't shown raw."""
    d = (details or "").lower()
    if "checksum" in d:
        return "That code has a typo — check the digits and try again."
    if "attestation" in d or "certificate" in d or "dcl" in d:
        return ("The hub refused this device because its security certificate isn't from a certified "
                "Matter manufacturer. Certified devices (like ones in Alexa) don't have this problem.")
    if "pase" in d or "passcode" in d or "setup code" in d or "not found" in d or "discover" in d:
        return ("The device didn't accept that code or couldn't be found. Get a fresh code from the other app "
                "(codes expire after 15 minutes and work once) and try again.")
    return ("The device couldn't be added. Get a fresh code from the other app (codes expire after "
            "15 minutes) and try again.")


def normalize_code(raw: str) -> str:
    """Accept an 11- or 21-digit manual pairing code (spaces/dashes allowed)
    or an `MT:` QR payload. Raise PairingError with a plain-English reason."""
    s = (raw or "").strip().upper()
    if s.startswith("MT:"):
        if QR_CODE.match(s):
            return s
        raise PairingError("That QR text doesn't look like a Matter code. It starts with MT: followed by letters and digits.")
    digits = re.sub(r"[\s\-]", "", s)
    if MANUAL_CODE.match(digits):
        return digits
    raise PairingError("Enter the 11-digit setup code (for example 1234-567-8901) or the MT: text from the QR code.")


class MatterAdapter(ProviderAdapter):
    provider_id = "matter"
    display_name = "Matter"
    description = "Matter lights, switches and plugs — including ones you share from the Alexa app."
    config_fields = (ConfigField("server_url", "Matter server address", DEFAULT_URL,
                                 "Where the hub's Matter server listens. Leave as is unless told otherwise."),)
    supports_pairing = True
    pairing_timeout_s = 180.0
    freshness_window = 300.0          # state arrives by subscription, not only by polling

    def __init__(self, secrets=None, config: dict[str, Any] | None = None, storage=None,
                 client: MatterServerClient | None = None):
        super().__init__(secrets, config, storage)
        self.url = str(self._config.get("server_url") or DEFAULT_URL)
        self.client = client or MatterServerClient(self.url)
        self.client.on_update = self._notify_update

    async def start(self) -> None:
        await self.client.start()
        await self.client.wait_ready(5)          # don't block start-up on a slow server

    async def close(self) -> None:
        await self.client.stop()

    # ── helpers ──────────────────────────────────────────────────────────
    def _require(self) -> None:
        if not self.client.connected:
            raise ProviderUnavailable("Can't reach the Matter server.")

    def _node(self, ref: DeviceRef) -> tuple[dict, int, int]:
        self._require()
        try:
            node_id, ep = M.parse_external_id(ref.external_id)
        except M.MappingError:
            raise DeviceUnavailable("Not a Matter device id.") from None
        node = self.client.nodes.get(node_id)
        if node is None:
            raise DeviceUnavailable("This device is no longer on the Matter server.")
        if node.get("available") is False:
            raise DeviceUnavailable("The device isn't answering on the network.")
        if ep not in M.controllable_endpoints(node):
            raise DeviceUnavailable("This part of the device is no longer offered.")
        return node, node_id, ep

    def _discovered(self, node: dict) -> list[DiscoveredDevice]:
        eps = M.controllable_endpoints(node)
        out = []
        for ep in eps:
            uid = M.unique_id(node, ep)
            out.append(DiscoveredDevice(
                ref=DeviceRef(self.provider_id, M.external_id(node["node_id"], ep)),
                name=M.name_for(node, ep, multi=len(eps) > 1),
                kind=M.device_kind(M.device_types(node, ep)),
                capabilities=M.capabilities(node, ep),
                suggested_room=None,                 # Matter has no rooms; the owner assigns them
                manufacturer=M.vendor(node, ep),
                model=M.product(node, ep),
                identity_hints={"matter_unique_id": uid} if uid else {},
            ))
        return out

    # ── contract ─────────────────────────────────────────────────────────
    async def discover_devices(self) -> list[DiscoveredDevice]:
        self._require()
        out: list[DiscoveredDevice] = []
        for node in list(self.client.nodes.values()):
            out.extend(self._discovered(node))
        return out

    async def get_capabilities(self, ref: DeviceRef) -> list[CapabilitySpec]:
        node, _, ep = self._node(ref)
        return list(M.capabilities(node, ep))

    async def get_state(self, ref: DeviceRef) -> list[StateReading]:
        node, _, ep = self._node(ref)
        caps = [s.type for s in M.capabilities(node, ep)]
        return [StateReading(c, v) for c, v in M.readings(node, ep, caps).items()]

    async def execute(self, ref: DeviceRef, capability: CapabilityType, value: Any) -> ExecuteResult:
        node, node_id, ep = self._node(ref)
        if capability not in {s.type for s in M.capabilities(node, ep)}:
            raise DeviceUnavailable("The device doesn't offer that control.")
        try:
            cmd = M.command_for(ep, capability, value)
        except M.MappingError as e:
            raise ProviderError(str(e)) from None
        try:
            await self.client.command("device_command", {
                "node_id": node_id, "endpoint_id": ep, "cluster_id": cmd.cluster_id,
                "command_name": cmd.command_name, "payload": cmd.payload})
        except ServerUnavailable:
            raise ProviderUnavailable("Lost the connection to the Matter server.") from None
        except asyncio.TimeoutError:
            raise                                                    # the core reports "may or may not have changed"
        except MatterServerError:
            raise DeviceUnavailable("The device didn't accept the command.") from None

        # Read back from the device itself — "sent" is not "done".
        observed: dict[str, Any] = {}
        try:
            for p in cmd.readback:
                res = await self.client.command("read_attribute", {"node_id": node_id, "attribute_path": p})
                observed[p] = res.get(p, next(iter(res.values()), None)) if isinstance(res, dict) else res
        except (ServerUnavailable, MatterServerError, asyncio.TimeoutError):
            return ExecuteResult(ExecuteStatus.ACCEPTED)             # sent; the device's answer is unknown
        for p, v in observed.items():                                # keep the cache truthful immediately
            node.setdefault("attributes", {})[p] = v
        return ExecuteResult(ExecuteStatus.CONFIRMED,
                             StateReading(capability, M.observed_value(capability, cmd, observed, value)))

    async def health(self) -> ProviderHealth:
        if not self.client.connected:
            # The technical reason (client.last_error) goes to the logs, not the owner.
            return ProviderHealth(HealthStatus.DOWN, "Can't reach the hub's Matter server.")
        total = len(self.client.nodes)
        down = sum(1 for n in self.client.nodes.values() if n.get("available") is False)
        detail = f"{total} Matter device{'s' if total != 1 else ''}" + (f", {down} not answering" if down else "")
        return ProviderHealth(HealthStatus.OK, detail)

    async def pair(self, code: str) -> list[DeviceRef]:
        code = normalize_code(code)
        REDACTOR.register(code)                                      # never readable in any log line
        if not self.client.connected:
            raise PairingError("The hub's Matter server isn't running. Check Settings → System health.")
        try:
            node = await self.client.command("commission_with_code", {"code": code, "network_only": True},
                                             timeout=self.pairing_timeout_s)
        except asyncio.TimeoutError:
            raise PairingError("The device didn't answer in time. Make sure it's powered, the code is less "
                               "than 15 minutes old, and try again.") from None
        except ServerUnavailable:
            raise PairingError("Lost the connection to the hub's Matter server. Try again.") from None
        except MatterServerError as e:
            log.warning("Matter commissioning failed (code %s): %s", e.code, e.details)
            raise PairingError(pairing_failure_message(e.details)) from None
        if not isinstance(node, dict) or "node_id" not in node:
            raise PairingError("The Matter server didn't return the new device.")
        self.client.nodes[int(node["node_id"])] = node
        refs = [d.ref for d in self._discovered(node)]
        if not refs:
            raise PairingError("Added, but this device has nothing the hub can control yet "
                               "(this release supports lights, switches and plugs).")
        return refs

"""A small in-process stand-in for a Matter server's WebSocket API, for
hermetic tests. It holds fictional nodes (tests/fixtures/matter_nodes.json),
applies On/Off, Level and Color commands to their attributes, answers
read_attribute from them, and "commissions" one fixture node for a known code.

Knobs, to prove the adapter tells the truth:
    ignore_commands  — device accepts commands but never changes (read-back disagrees)
    fail_commands    — every device_command returns an error
    unavailable      — nodes marked not available
    drop()           — close every client connection
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from websockets.asyncio.server import serve

NODES = json.loads((Path(__file__).parent / "fixtures" / "matter_nodes.json").read_text(encoding="utf-8"))
VALID_CODE = "34970112332"          # the well-known Matter test code, not a real device's


class FakeMatterServer:
    def __init__(self, nodes: list[str] = ("color_light", "plug")):
        self.nodes = {NODES[k]["node_id"]: copy.deepcopy(NODES[k]) for k in nodes}
        self.ignore_commands = False
        self.fail_commands = False
        self.commission_error: str | None = None       # e.g. an attestation refusal
        self.commands: list[dict] = []
        self._clients: set = set()
        self._server = None
        self.port = 0

    async def __aenter__(self) -> "FakeMatterServer":
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def drop(self) -> None:
        for ws in list(self._clients):
            await ws.close()

    async def _emit(self, event: str, data) -> None:
        for ws in list(self._clients):
            try:
                await ws.send(json.dumps({"event": event, "data": data}))
            except Exception:
                pass

    async def _handle(self, ws) -> None:
        await ws.send(json.dumps({"fabric_id": 1, "compressed_fabric_id": 1, "schema_version": 11,
                                  "min_supported_schema_version": 9, "sdk_version": "fake"}))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                assert "args" in msg, "matterjs-server needs args on every request"
                mid, cmd, args = msg["message_id"], msg["command"], msg["args"]
                try:
                    result = await self._dispatch(ws, cmd, args)
                    await ws.send(json.dumps({"message_id": mid, "result": result}))
                except ValueError as e:
                    await ws.send(json.dumps({"message_id": mid, "error_code": 1, "details": str(e)}))
        finally:
            self._clients.discard(ws)

    async def _dispatch(self, ws, cmd: str, a: dict):
        if cmd == "start_listening":
            self._clients.add(ws)
            return list(self.nodes.values())
        if cmd == "get_nodes":
            return list(self.nodes.values())
        if cmd == "read_attribute":
            node = self.nodes[a["node_id"]]
            if node.get("available") is False:
                raise ValueError("node unavailable")
            return {a["attribute_path"]: node["attributes"].get(a["attribute_path"])}
        if cmd == "device_command":
            self.commands.append(a)
            node = self.nodes.get(a["node_id"])
            if self.fail_commands or node is None or node.get("available") is False:
                raise ValueError("command failed")
            if not self.ignore_commands:
                for path, value in self._apply(a).items():
                    node["attributes"][path] = value
                    await self._emit("attribute_updated", [a["node_id"], path, value])
            return None
        if cmd == "commission_with_code":
            if self.commission_error:
                raise ValueError(self.commission_error)
            if a.get("code") != VALID_CODE or a.get("network_only") is not True:
                raise ValueError("commissioning failed")
            node = copy.deepcopy(NODES["two_gang_switch"])
            node["node_id"] = max(self.nodes) + 1
            self.nodes[node["node_id"]] = node
            await self._emit("node_added", node)
            return node
        raise ValueError(f"unknown command {cmd}")

    @staticmethod
    def _apply(a: dict) -> dict:
        ep, c, name, p = a["endpoint_id"], a["cluster_id"], a["command_name"], a.get("payload") or {}
        if c == 6:
            return {f"{ep}/6/0": {"On": True, "Off": False}[name]}
        if c == 8 and name == "MoveToLevelWithOnOff":
            return {f"{ep}/8/0": p["level"], f"{ep}/6/0": True}
        if c == 768 and name == "MoveToHueAndSaturation":
            return {f"{ep}/768/0": p["hue"], f"{ep}/768/1": p["saturation"]}
        if c == 768 and name == "MoveToColorTemperature":
            return {f"{ep}/768/7": p["colorTemperatureMireds"]}
        raise ValueError("unsupported command")


async def wait_until(pred, timeout: float = 5.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()

"""A stand-in Acme hub for tests and local development.

    python fake_device.py            # serve on 127.0.0.1:7788 with API key "acme-dev-key-0001"

Then connect "Acme plugs" in the hub's Settings with that key. In tests it is
started in-process (see test_conformance.py).
"""
from __future__ import annotations

import asyncio
import copy
import json

DEV_KEY = "acme-dev-key-0001"
PLUGS = [
    {"id": "p1", "name": "Desk Plug", "room": "Office", "model": "AP-1", "on": False, "level": None},
    {"id": "p2", "name": "Floor Lamp", "room": "Living Room", "model": "AP-1D", "on": True, "level": 60},
]


class FakeAcmeHub:
    def __init__(self, key: str = DEV_KEY, port: int = 0):
        self.key, self.port = key, port
        self.plugs = {p["id"]: copy.deepcopy(p) for p in PLUGS}
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> "FakeAcmeHub":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(await reader.readline())
            writer.write((json.dumps(self._answer(req)) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    def _answer(self, req: dict) -> dict:
        if req.get("key") != self.key:
            return {"ok": False, "error": "unauthorized"}
        op = req.get("op")
        if op == "list":
            return {"ok": True, "plugs": list(self.plugs.values())}
        plug = self.plugs.get(req.get("id"))
        if plug is None:
            return {"ok": False, "error": "no such plug"}
        if op == "get":
            return {"ok": True, "plug": plug}
        if op == "set":
            if "on" in req:
                plug["on"] = bool(req["on"])
            if "level" in req and plug["level"] is not None:
                plug["level"], plug["on"] = int(req["level"]), True
            return {"ok": True}
        return {"ok": False, "error": "unknown op"}


async def _main() -> None:
    hub = await FakeAcmeHub(port=7788).start()
    print(f"Fake Acme hub on 127.0.0.1:{hub.port} — API key {DEV_KEY}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())

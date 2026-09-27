"""Async client for a Matter server's WebSocket API (matterjs-server, which is
API-compatible with python-matter-server).

    → {"message_id": "7", "command": "device_command", "args": {...}}
    ← {"message_id": "7", "result": ...}                        success
    ← {"message_id": "7", "error_code": 5, "details": "..."}   failure
    ← {"event": "attribute_updated", "data": [node_id, "1/6/0", true]}

On connect the server first sends its server_info; the client then calls
`start_listening`, which returns every node and subscribes to events. The node
cache is kept current from `node_added / node_updated / node_removed /
attribute_updated` events, so state reads are cheap and truthful between polls.

Engineering rules: every request has a timeout; at most MAX_IN_FLIGHT
requests; a dropped connection fails every pending request immediately
(never leaves a caller hanging) and reconnects with capped backoff; stop()
cancels cleanly. The client never logs command arguments (pairing codes).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

log = logging.getLogger("hnb.matter")

MAX_IN_FLIGHT = 64
MAX_MESSAGE = 16 * 1024 * 1024
BACKOFF_MAX_S = 30.0


class ServerUnavailable(Exception):
    """No connection to the Matter server."""


class MatterServerError(Exception):
    def __init__(self, code: int | None, details: str):
        super().__init__(details)
        self.code, self.details = code, details


class MatterServerClient:
    def __init__(self, url: str, *, request_timeout: float = 15.0, connect_timeout: float = 5.0):
        self.url = url
        self.request_timeout = request_timeout
        self.connect_timeout = connect_timeout
        self.server_info: dict[str, Any] | None = None
        self.nodes: dict[int, dict[str, Any]] = {}
        self.last_error: str = ""
        self._ws = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closing = False
        self.on_update: Callable[[], None] | None = None     # node added/changed/removed/availability

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._run(), name="matter-client")

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_pending("Matter client stopped.")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._closing:
            try:
                async with connect(self.url, open_timeout=self.connect_timeout, max_size=MAX_MESSAGE,
                                   ping_interval=20, ping_timeout=20) as ws:
                    first = json.loads(await asyncio.wait_for(ws.recv(), self.connect_timeout))
                    self.server_info = first
                    self._ws = ws
                    reader = asyncio.create_task(self._reader(ws), name="matter-reader")
                    try:
                        nodes = await self.command("start_listening", timeout=120)
                        self.nodes = {int(n["node_id"]): n for n in (nodes or [])}
                        self._ready.set()
                        self.last_error = ""
                        backoff = 1.0
                        log.info("Connected to Matter server (%d nodes)", len(self.nodes))
                        await reader
                    finally:
                        reader.cancel()
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, asyncio.TimeoutError, ValueError, KeyError,
                    MatterServerError, ServerUnavailable) as e:
                self.last_error = type(e).__name__
                log.warning("Matter server connection failed (%s); retrying in %.0fs", type(e).__name__, backoff)
            finally:
                self._ws = None
                self._ready.clear()
                self._fail_pending("Lost the connection to the Matter server.")
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "message_id" in msg:
                fut = self._pending.pop(str(msg["message_id"]), None)
                if fut is None or fut.done():
                    continue
                if "error_code" in msg:
                    fut.set_exception(MatterServerError(msg.get("error_code"), str(msg.get("details") or "error")))
                else:
                    fut.set_result(msg.get("result"))
            elif "event" in msg:
                self._on_event(msg["event"], msg.get("data"))

    def _on_event(self, event: str, data: Any) -> None:
        changed = self._apply_event(event, data)
        if changed and self.on_update is not None:
            self.on_update()

    def _apply_event(self, event: str, data: Any) -> bool:
        if event in ("node_added", "node_updated") and isinstance(data, dict) and "node_id" in data:
            self.nodes[int(data["node_id"])] = data
            return True
        if event == "node_removed":
            return self.nodes.pop(int(data), None) is not None
        if event == "attribute_updated" and isinstance(data, list) and len(data) == 3:
            node = self.nodes.get(int(data[0]))
            if node is not None:
                node.setdefault("attributes", {})[str(data[1])] = data[2]
                return True
            return False
        if event in ("node_available", "node_unavailable"):            # tolerate either vocabulary
            node = self.nodes.get(int(data)) if not isinstance(data, dict) else None
            if node is not None:
                node["available"] = event == "node_available"
                return True
        return False

    def _fail_pending(self, why: str) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ServerUnavailable(why))

    # ── requests ─────────────────────────────────────────────────────────
    async def command(self, name: str, args: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        ws = self._ws
        if ws is None:
            raise ServerUnavailable("Not connected to the Matter server.")
        async with self._slots:
            mid = str(next(self._ids))
            fut = asyncio.get_running_loop().create_future()
            self._pending[mid] = fut
            # Always send "args": some matterjs-server handlers read it without a null check.
            msg: dict[str, Any] = {"message_id": mid, "command": name, "args": args or {}}
            try:
                await ws.send(json.dumps(msg))
            except WebSocketException:
                self._pending.pop(mid, None)
                raise ServerUnavailable("Lost the connection to the Matter server.") from None
            try:
                return await asyncio.wait_for(fut, timeout or self.request_timeout)
            except asyncio.TimeoutError:
                self._pending.pop(mid, None)
                raise

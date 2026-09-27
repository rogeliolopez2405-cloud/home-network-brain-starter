"""Shared fixtures. Everything is built in tmp_path: no network, no real
accounts, no state outside the test."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hnb.api.app import create_app
from hnb.config import Settings
from hnb.providers.registry import BUILTIN
from hnb.security.vault import generate_key_file
from hnb.service import HubService

LAN_CLIENT = ("127.0.0.1", 50000)        # loopback: always inside the network policy
OUTSIDE_CLIENT = ("203.0.113.9", 50000)  # TEST-NET-3 documentation address: outside it
PASSPHRASE = "correct horse battery"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def make_settings(root: Path, **over) -> Settings:
    key = root / "secrets" / "vault.key"
    generate_key_file(key)
    base = dict(data_dir=root / "data", backup_dir=root / "backups", log_dir=None, vault_key_file=key,
                poll_interval_s=3600, provider_timeout_s=0.5)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def clock() -> Clock:
    return Clock()


class Hub:
    """A running app + client, restartable against the same data directory."""

    def __init__(self, settings: Settings, clock: Clock | None = None, client=LAN_CLIENT):
        self.settings, self.clock, self.client_addr = settings, clock, client
        self.client: TestClient | None = None
        self.csrf: str | None = None

    def __enter__(self) -> "Hub":
        # Built-in modules only: a developer's plugins/ must never change core test results.
        self.service = HubService(self.settings, adapter_classes=dict(BUILTIN), auth_now=self.clock)
        self.app = create_app(self.settings, self.service, poll=False)
        self.client = TestClient(self.app, client=self.client_addr)
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    def run(self, fn, *args):
        return self.client.portal.call(fn, *args)

    def setup_code(self) -> str:
        return self.settings.setup_code_file.read_text().strip()

    def bootstrap(self, username="alex", passphrase=PASSPHRASE, house="Test House"):
        r = self.client.post("/api/auth/bootstrap", json={"setup_code": self.setup_code(), "username": username,
                                                          "passphrase": passphrase, "house_name": house})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
        return r

    def login(self, username="alex", passphrase=PASSPHRASE):
        r = self.client.post("/api/auth/login", json={"username": username, "passphrase": passphrase})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
        return r

    def write(self, method: str, url: str, **kw):
        headers = kw.pop("headers", {})
        if self.csrf:
            headers.setdefault("X-CSRF-Token", self.csrf)
        return self.client.request(method, url, headers=headers, **kw)

    def device(self, name: str) -> dict:
        return next(d for d in self.client.get("/api/devices").json()["devices"] if d["name"] == name)

    def claim_all(self) -> None:
        for d in self.client.get("/api/devices").json()["devices"]:
            assert self.write("POST", f"/api/devices/{d['id']}/review", json={"status": "MINE"}).status_code == 200


@pytest.fixture
def hub(settings, clock):
    with Hub(settings, clock) as h:
        yield h

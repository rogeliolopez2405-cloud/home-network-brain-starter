"""Conformance for the example provider — the pattern every plugin follows.

    python -m pytest plugins/<your_provider>        (or examples/providers/acme_plug)
"""
import contextlib
import importlib.util
import sys
from pathlib import Path

import pytest

from hnb.providers.sdk import CapabilityType, ExecuteStatus
from hnb.providers.sdk.conformance import CHECKS, run_check
from hnb.providers.sdk.testing import FakeSecrets

HERE = Path(__file__).resolve().parent


def _load(name: str):
    """Plugins aren't a package on sys.path; load siblings by file path."""
    spec = importlib.util.spec_from_file_location(f"{HERE.name}_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter_mod, fake = _load("adapter"), _load("fake_device")


class Harness:
    """Builds the adapter against the fake hub; knows how to break it."""

    @contextlib.asynccontextmanager
    async def adapter(self):
        self.hub = await fake.FakeAcmeHub().start()
        self.a = adapter_mod.ADAPTER(secrets=FakeSecrets({"api_key": fake.DEV_KEY}),
                                     config={"host": "127.0.0.1", "port": str(self.hub.port)})
        try:
            yield self.a
        finally:
            await self.hub.stop()

    async def break_provider(self):
        await self.hub.stop()                   # the hub goes away

    async def expire_auth(self):
        self.hub.key = "rotated-key"            # the owner changed the key in the Acme app


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_conformance(check):
    run_check(Harness, check)


def test_capabilities_follow_the_device():
    async def body(h, a):
        found = {d.name: {s.type.value for s in d.capabilities} for d in await a.discover_devices()}
        assert found == {"Desk Plug": {"POWER"}, "Floor Lamp": {"POWER", "BRIGHTNESS"}}
    run_check(Harness, body)


def test_writes_are_confirmed_by_reading_the_device_back():
    """The kit accepts ACCEPTED, which is honest for services that can't report
    state. This one can, so every write must come back CONFIRMED, in both
    directions, and the (simulated) device must really have changed."""
    async def body(h, a):
        ref = next(d.ref for d in await a.discover_devices() if d.name == "Floor Lamp")
        for on in (False, True):
            result = await a.execute(ref, CapabilityType.POWER, on)
            assert result.status is ExecuteStatus.CONFIRMED and result.observed.value is on
            assert h.hub.plugs["p2"]["on"] is on
    run_check(Harness, body)

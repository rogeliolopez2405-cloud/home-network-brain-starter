"""The built-in adapters pass the same conformance kit third-party provider
modules use (hnb.providers.sdk.conformance). Each harness builds its adapter
against a local simulator — never a live account."""
import contextlib

import pytest

from fake_matter_server import FakeMatterServer
from hnb.providers.demo.adapter import DemoAdapter
from hnb.providers.matter.adapter import MatterAdapter
from hnb.providers.sdk import ExecuteResult, ExecuteStatus, StateReading
from hnb.providers.sdk.conformance import CHECKS, run_check
from hnb.providers.sdk.testing import FakeSecrets


class DemoHarness:
    @contextlib.asynccontextmanager
    async def adapter(self):
        self.a = DemoAdapter(secrets=FakeSecrets(DemoAdapter.initial_secrets()))
        yield self.a

    async def break_provider(self):
        self.a._config["simulate"] = "outage"

    async def expire_auth(self):
        self.a._config["simulate"] = "expired sign-in"


class MatterHarness:
    @contextlib.asynccontextmanager
    async def adapter(self):
        async with FakeMatterServer(["color_light", "plug", "two_gang_switch", "bridge"]) as srv:
            a = MatterAdapter(config={"server_url": srv.url})
            await a.start()
            assert await a.client.wait_ready(5)
            self.a = a
            try:
                yield a
            finally:
                await a.close()

    async def break_provider(self):
        await self.a.client.stop()                 # the hub loses its Matter server

    expire_auth = None                             # Matter adapters hold no credentials


HARNESSES = {"demo": DemoHarness, "matter": MatterHarness}


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("harness", HARNESSES.values(), ids=HARNESSES.keys())
def test_conformance(harness, check):
    run_check(harness, check)


def test_the_kit_can_fail():
    """A kit that can't fail proves nothing: a deliberately broken adapter must fail it."""
    class LyingDemo(DemoAdapter):
        async def get_state(self, ref):                 # invents state while "unreachable"
            self._config.pop("simulate", None)
            return await super().get_state(ref)

    class LyingHarness(DemoHarness):
        @contextlib.asynccontextmanager
        async def adapter(self):
            self.a = LyingDemo(secrets=FakeSecrets(DemoAdapter.initial_secrets()))
            yield self.a

    from hnb.providers.sdk import conformance as K
    with pytest.raises(AssertionError):
        run_check(LyingHarness, K.unreachable_raises_instead_of_inventing_state)


def test_the_kit_catches_a_write_that_does_nothing():
    """An adapter that claims CONFIRMED without changing the device must fail the
    read-back check, whatever state the device happened to start in."""
    class NoOpWriter(DemoAdapter):
        async def execute(self, ref, capability, value):
            return ExecuteResult(ExecuteStatus.CONFIRMED, StateReading(capability, value))

    class NoOpHarness(DemoHarness):
        @contextlib.asynccontextmanager
        async def adapter(self):
            self.a = NoOpWriter(secrets=FakeSecrets(DemoAdapter.initial_secrets()))
            yield self.a

    from hnb.providers.sdk import conformance as K
    with pytest.raises(AssertionError, match="CONFIRMED as"):
        run_check(NoOpHarness, K.write_then_read_back)

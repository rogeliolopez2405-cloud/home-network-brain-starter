"""Matter adapter against the fake Matter server: discovery, truthful
execution with read-back, pairing, outages, and the full core path."""
import asyncio

import pytest

from fake_matter_server import VALID_CODE, FakeMatterServer, wait_until
from hnb.core.capabilities import CapabilityType as C
from hnb.core.home import Home
from hnb.core.model import ActionOutcome, Ctx, Freshness, House, ReviewStatus
from hnb.providers.base import (
    DeviceRef,
    DeviceUnavailable,
    ExecuteStatus,
    HealthStatus,
    PairingError,
    ProviderUnavailable,
)
from hnb.providers.matter.adapter import MatterAdapter, normalize_code

OWNER = Ctx("owner:test", "test")


def run(coro):
    return asyncio.run(coro)


async def started(server: FakeMatterServer) -> MatterAdapter:
    a = MatterAdapter(config={"server_url": server.url})
    await a.start()
    assert await a.client.wait_ready(5)
    return a


def test_discovers_only_controllable_endpoints_with_real_capabilities():
    async def go():
        async with FakeMatterServer(["color_light", "plug", "sensor_only"]) as srv:
            a = await started(srv)
            try:
                return {d.ref.external_id: d for d in await a.discover_devices()}
            finally:
                await a.close()
    found = run(go())
    assert set(found) == {"5-1", "7-1"}                                   # the sensor offers nothing controllable
    assert {s.type for s in found["5-1"].capabilities} == {C.POWER, C.BRIGHTNESS, C.COLOR, C.COLOR_TEMPERATURE}
    assert {s.type for s in found["7-1"].capabilities} == {C.POWER}
    assert found["5-1"].suggested_room is None and found["5-1"].manufacturer == "Example Lighting"


@pytest.mark.parametrize("cap,value", [(C.POWER, False), (C.BRIGHTNESS, 30), (C.COLOR, {"h": 200, "s": 70}),
                                       (C.COLOR_TEMPERATURE, 3000)])
def test_execute_reads_back_from_the_device(cap, value):
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                result = await a.execute(DeviceRef("matter", "5-1"), cap, value)
                state = {r.capability: r.value for r in await a.get_state(DeviceRef("matter", "5-1"))}
                return result, state, srv.commands[-1]
            finally:
                await a.close()
    result, state, sent = run(go())
    assert result.status is ExecuteStatus.CONFIRMED and result.observed.value == value
    assert state[cap] == value
    assert sent["node_id"] == 5 and sent["endpoint_id"] == 1


def test_device_that_ignores_the_command_is_not_reported_as_done():
    async def go():
        async with FakeMatterServer() as srv:
            srv.ignore_commands = True
            a = await started(srv)
            try:
                return await a.execute(DeviceRef("matter", "5-1"), C.POWER, False)   # light is on
            finally:
                await a.close()
    result = run(go())
    assert result.status is ExecuteStatus.CONFIRMED and result.observed.value is True   # read-back shows the truth


def test_command_error_is_a_device_failure_not_a_success():
    async def go():
        async with FakeMatterServer() as srv:
            srv.fail_commands = True
            a = await started(srv)
            try:
                with pytest.raises(DeviceUnavailable):
                    await a.execute(DeviceRef("matter", "7-1"), C.POWER, True)
            finally:
                await a.close()
    run(go())


def test_undeclared_capability_is_refused():
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                with pytest.raises(DeviceUnavailable):
                    await a.execute(DeviceRef("matter", "7-1"), C.BRIGHTNESS, 50)    # a plug has no dimmer
                assert srv.commands == []
            finally:
                await a.close()
    run(go())


def test_server_down_is_provider_unavailable_and_health_down():
    async def go():
        a = MatterAdapter(config={"server_url": "ws://127.0.0.1:9/ws"})         # nothing listens there
        await a.start()
        try:
            h = await a.health()
            with pytest.raises(ProviderUnavailable):
                await a.get_state(DeviceRef("matter", "5-1"))
            return h
        finally:
            await a.close()
    h = run(go())
    assert h.status is HealthStatus.DOWN


def test_reconnects_and_recovers_after_the_server_drops():
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                await srv.drop()
                assert await wait_until(lambda: not a.client.connected, 3)
                assert await wait_until(lambda: a.client.connected, 5)            # backoff starts at 1 s
                return (await a.health()).status
            finally:
                await a.close()
    assert run(go()) is HealthStatus.OK


def test_unavailable_node_is_device_unavailable():
    async def go():
        async with FakeMatterServer() as srv:
            srv.nodes[5]["available"] = False
            a = await started(srv)
            try:
                with pytest.raises(DeviceUnavailable):
                    await a.get_state(DeviceRef("matter", "5-1"))
            finally:
                await a.close()
    run(go())


def test_subscription_keeps_state_current_without_polling():
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                srv.nodes[7]["attributes"]["1/6/0"] = True                          # someone used Alexa
                await srv._emit("attribute_updated", [7, "1/6/0", True])
                return await wait_until(lambda: a.client.nodes[7]["attributes"]["1/6/0"] is True, 2)
            finally:
                await a.close()
    assert run(go())


# ── pairing ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,ok", [("3497-011-2332", "34970112332"), ("3497 0112 332", "34970112332"),
                                    ("MT:Y.K9042C00KA0648G00", "MT:Y.K9042C00KA0648G00")])
def test_codes_are_normalised(raw, ok):
    assert normalize_code(raw) == ok


@pytest.mark.parametrize("raw", ["", "1234", "abcdefghijk", "MT:!!"])
def test_bad_codes_are_refused_with_a_reason(raw):
    with pytest.raises(PairingError):
        normalize_code(raw)


def test_pairing_adds_devices_and_wrong_code_fails_cleanly():
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                with pytest.raises(PairingError, match="fresh code"):
                    await a.pair("1111-111-1111")
                refs = await a.pair("3497-011-2332")
                ids = {d.ref.external_id for d in await a.discover_devices()}
                return refs, ids, srv.commands
            finally:
                await a.close()
    refs, ids, commands = run(go())
    assert [r.external_id for r in refs] == ["8-1", "8-2"]                     # new node 8: two-gang switch → two devices
    assert {"8-1", "8-2"} <= ids
    assert commands == []                                                        # pairing sent no device command


def test_certificate_refusal_is_explained_accurately():
    """Regression (VM run of 2349778): a device with a test certificate was
    refused by attestation, and the owner was wrongly told to get a fresh code."""
    async def go():
        async with FakeMatterServer() as srv:
            srv.commission_error = ("Commission failed: This device uses a test/development certificate. "
                                    "Device attestation produced 3 finding(s) and was rejected by policy")
            a = await started(srv)
            try:
                with pytest.raises(PairingError) as e:
                    await a.pair(VALID_CODE)
                return str(e.value)
            finally:
                await a.close()
    msg = run(go())
    assert "certificate" in msg and "fresh code" not in msg


def test_pairing_code_never_appears_in_an_error():
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                with pytest.raises(PairingError) as e:
                    await a.pair("2222-222-2222")
                return str(e.value)
            finally:
                await a.close()
    assert "2222" not in run(go())


# ── through the core: discover → UNREVIEWED → claim → control → read-back ──
def test_matter_device_through_the_core():
    async def go():
        async with FakeMatterServer() as srv:
            a = await started(srv)
            try:
                home = Home(House("Matter Home"), [a], timeout_s=5)
                await home.discover("matter")
                lamp = next(d for d in home.devices.values() if d.name == "Living Room Lamp")
                assert lamp.review is ReviewStatus.UNREVIEWED
                rejected = await home.execute(lamp.id, C.POWER, False, OWNER)
                home.review(lamp.id, ReviewStatus.MINE, OWNER)
                done = await home.execute(lamp.id, C.BRIGHTNESS, 40, OWNER)
                srv.nodes[5]["available"] = False
                a.client.nodes[5]["available"] = False
                await home.refresh(lamp.id)
                return rejected, done, lamp
            finally:
                await a.close()
    rejected, done, lamp = run(go())
    assert rejected.outcome is ActionOutcome.REJECTED                            # not claimed yet
    assert done.outcome is ActionOutcome.CONFIRMED
    assert all(sv.freshness is Freshness.UNKNOWN for sv in lamp.state.values())  # device gone → unknown, not off


@pytest.mark.parametrize("details,expect", [
    ("Commission failed: Invalid checksum", "typo"),                    # real matterjs-server text (VM run)
    ("Device attestation produced 3 finding(s) and was rejected by policy", "certificate"),
    ("PASE session failed", "fresh code"),
    ("something else", "fresh code"),
])
def test_pairing_failures_are_worded_by_cause(details, expect):
    from hnb.providers.matter.adapter import pairing_failure_message
    assert expect in pairing_failure_message(details)

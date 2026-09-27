"""Core rules: review gate, capability gate, honest failures, owner choices survive rediscovery."""
import asyncio

import pytest

from hnb.core.capabilities import CapabilityError, CapabilitySpec, CapabilityType, validate
from hnb.core.home import Home
from hnb.core.model import ActionOutcome, AuditAction, Ctx, Freshness, House, ReviewStatus
from hnb.core.view import home_view
from hnb.providers.demo.adapter import DemoAdapter

OWNER = Ctx("owner:test", "test")
C = CapabilityType


def run(coro):
    return asyncio.run(coro)


async def _home(**cfg) -> Home:
    adapter = DemoAdapter(config=cfg)
    home = Home(House("Test Home"), [adapter], timeout_s=0.5)
    # discovery happens with a healthy provider; failures are injected afterwards
    fail = adapter._config.pop("fail", None)
    delay = adapter._config.pop("delay_s", None)
    await home.discover("demo")
    if fail:
        adapter._config["fail"] = fail
    if delay:
        adapter._config["delay_s"] = delay
    return home


def _dev(home: Home, name: str):
    return next(d for d in home.devices.values() if d.name == name)


# ── capability vocabulary ────────────────────────────────────────────────
@pytest.mark.parametrize("cap,value,expected", [
    (C.POWER, True, True),
    (C.BRIGHTNESS, 55.4, 55),
    (C.COLOR, {"h": 360, "s": 50}, {"h": 0, "s": 50}),
    (C.LOCK, "LOCKED", "LOCKED"),
    (C.THERMOSTAT, {"mode": "HEAT", "setpoint_c": 20.04}, {"mode": "HEAT", "setpoint_c": 20.0}),
])
def test_validate_accepts_and_normalises(cap, value, expected):
    assert validate(CapabilitySpec(cap), value) == expected


@pytest.mark.parametrize("cap,value", [
    (C.POWER, "on"), (C.POWER, 1), (C.BRIGHTNESS, 101), (C.BRIGHTNESS, True),
    (C.COLOR, {"h": 10}), (C.LOCK, "OPEN"), (C.APP_LAUNCH, "Netflix"),
    (C.THERMOSTAT, {"mode": "HEAT", "setpoint_c": 90}),
])
def test_validate_rejects(cap, value):
    with pytest.raises(CapabilityError):
        validate(CapabilitySpec(cap), value)


def test_camera_capabilities_are_never_writable_even_if_declared():
    spec = CapabilitySpec(C.CAMERA_LIVE, writable=True)
    assert spec.writable is False


def test_color_temperature_respects_device_range():
    spec = CapabilitySpec(C.COLOR_TEMPERATURE, options={"min": 2700, "max": 5000})
    assert validate(spec, 3000) == 3000
    with pytest.raises(CapabilityError):
        validate(spec, 6500)


# ── discovery and review ─────────────────────────────────────────────────
def test_everything_discovered_starts_unreviewed_and_off_the_hub():
    home = run(_home())
    assert home.devices and all(d.review is ReviewStatus.UNREVIEWED for d in home.devices.values())
    view = home_view(home)
    assert view["devices"] == [] and view["status"]["needs_review"] == len(home.devices)


def test_rediscovery_never_overwrites_owner_choices_or_duplicates():
    async def go():
        home = await _home()
        dev = _dev(home, "Lamp")
        kitchen = home.add_room("Kitchen")
        home.review(dev.id, ReviewStatus.MINE, OWNER)
        home.rename(dev.id, "Reading Lamp", OWNER)
        home.assign_room(dev.id, kitchen.id, OWNER)
        count = len(home.devices)
        await home.discover("demo")
        return home, dev.id, kitchen.id, count
    home, dev_id, kitchen_id, count = run(go())
    dev = home.devices[dev_id]
    assert len(home.devices) == count
    assert (dev.name, dev.room_id, dev.review) == ("Reading Lamp", kitchen_id, ReviewStatus.MINE)


def test_review_emits_the_owner_decision():
    home = run(_home())
    dev = next(iter(home.devices.values()))
    home.drain()
    home.review(dev.id, ReviewStatus.IGNORED, OWNER)
    (ev,) = home.drain()
    assert (ev.action, ev.target, ev.result, ev.data["before"]) == (AuditAction.DEVICE_IGNORED, dev.id, "IGNORED", "UNREVIEWED")


def test_review_cannot_be_reset_to_unreviewed():
    home = run(_home())
    from hnb.core.home import HomeError
    with pytest.raises(HomeError):
        home.review(next(iter(home.devices)), ReviewStatus.UNREVIEWED, OWNER)


# ── actions ──────────────────────────────────────────────────────────────
def test_unclaimed_device_cannot_be_controlled():
    async def go():
        home = await _home()
        return await home.execute(_dev(home, "Lamp").id, C.POWER, True, OWNER)
    a = run(go())
    assert a.outcome is ActionOutcome.REJECTED


def test_unsupported_capability_is_rejected_before_any_provider_call():
    async def go():
        home = await _home()
        sw = _dev(home, "Kitchen Ceiling")          # on/off switch, no dimming
        home.review(sw.id, ReviewStatus.MINE, OWNER)
        return await home.execute(sw.id, C.BRIGHTNESS, 50, OWNER)
    a = run(go())
    assert a.outcome is ActionOutcome.REJECTED and a.connection_id is None


def test_claimed_device_action_is_confirmed_and_audited():
    async def go():
        home = await _home()
        lamp = _dev(home, "Lamp")
        home.review(lamp.id, ReviewStatus.MINE, OWNER)
        a = await home.execute(lamp.id, C.BRIGHTNESS, 80, OWNER)
        return home, lamp, a
    home, lamp, a = run(go())
    assert a.outcome is ActionOutcome.CONFIRMED
    assert lamp.state[C.BRIGHTNESS].value == 80 and lamp.state[C.BRIGHTNESS].freshness is Freshness.CONFIRMED
    events = home.drain()
    requested, done = events[-2], events[-1]
    assert requested.action is AuditAction.COMMAND_REQUESTED and requested.requested == {"capability": "BRIGHTNESS", "value": 80}
    assert (done.action, done.actor, done.source, done.target, done.result) == (
        AuditAction.COMMAND_CONFIRMED, "owner:test", "test", lamp.id, "CONFIRMED")


def test_accepted_without_readback_is_not_reported_as_confirmed():
    async def go():
        home = await _home(confirm=False)
        lamp = _dev(home, "Lamp")
        home.review(lamp.id, ReviewStatus.MINE, OWNER)
        return lamp, await home.execute(lamp.id, C.POWER, True, OWNER)
    lamp, a = run(go())
    assert a.outcome is ActionOutcome.ACCEPTED
    assert lamp.state[C.POWER].freshness is Freshness.ASSUMED


def test_provider_down_makes_state_unknown_not_off():
    async def go():
        home = await _home(fail="down")
        lamp = _dev(home, "Living Room Ceiling Lights")
        home.review(lamp.id, ReviewStatus.MINE, OWNER)
        await home.refresh(lamp.id)
        a = await home.execute(lamp.id, C.POWER, False, OWNER)
        await home.check_health()
        return home, lamp, a
    home, lamp, a = run(go())
    assert a.outcome is ActionOutcome.FAILED
    assert all(sv.freshness is Freshness.UNKNOWN for sv in lamp.state.values())
    view = home_view(home)
    assert view["status"]["level"] == "warn"
    shown = next(d for d in view["devices"] if d["id"] == lamp.id)
    assert shown["on"] is None and shown["reachable"] is False          # can't say — never OFF


def test_recovery_clears_unknown_for_capabilities_without_readings():
    """Regression: TV APP_LAUNCH has no readable state; after an outage it
    stayed UNKNOWN forever and the TV showed as unreachable after recovery."""
    async def go():
        home = await _home(fail="down")
        tv = _dev(home, "Living Room TV")
        home.review(tv.id, ReviewStatus.MINE, OWNER)
        await home.refresh(tv.id)
        assert home.devices[tv.id].state[C.APP_LAUNCH].freshness is Freshness.UNKNOWN
        home.adapters["demo"]._config.pop("fail")
        await home.check_health()
        await home.refresh(tv.id)
        return home, tv
    home, tv = run(go())
    assert C.APP_LAUNCH not in tv.state
    assert next(d for d in home_view(home)["devices"] if d["id"] == tv.id)["reachable"] is True


def test_slow_poll_cannot_overwrite_a_newer_command_result():
    """Regression (review of c0669a9): a status read that started before a
    command must not land after it and restore the old value."""
    from hnb.providers.base import StateReading

    class SlowRead(DemoAdapter):
        async def get_state(self, ref):
            before = [StateReading(r.capability, r.value) for r in await super().get_state(ref)]
            await asyncio.sleep(0.2)            # the read was taken, the answer is slow
            return before

    async def go():
        home = Home(House("Race"), [SlowRead()], timeout_s=2)
        await home.discover("demo")
        lamp = _dev(home, "Lamp")                # fixture: POWER False
        home.review(lamp.id, ReviewStatus.MINE, OWNER)
        poll = asyncio.create_task(home.refresh(lamp.id))
        await asyncio.sleep(0.05)                # poll is mid-read
        action = await home.execute(lamp.id, C.POWER, True, OWNER)
        await poll
        return lamp, action
    lamp, action = run(go())
    assert action.outcome is ActionOutcome.CONFIRMED
    assert lamp.state[C.POWER].value is True


def test_timeout_is_a_truthful_failure():
    async def go():
        home = await _home(delay_s=2)
        lamp = _dev(home, "Lamp")
        home.review(lamp.id, ReviewStatus.MINE, OWNER)
        return await home.execute(lamp.id, C.POWER, True, OWNER)
    a = run(go())
    assert a.outcome is ActionOutcome.FAILED and "may or may not" in a.detail


def test_high_sensitivity_capability_needs_confirmation():
    from hnb.core.model import Device, DeviceKind
    home = run(_home())
    lock = Device(name="Door Lock", kind=DeviceKind.LOCK, review=ReviewStatus.MINE)
    lock.capabilities[C.LOCK] = CapabilitySpec(C.LOCK)
    home.devices[lock.id] = lock
    a = run(home.execute(lock.id, C.LOCK, "UNLOCKED", OWNER))
    assert a.outcome is ActionOutcome.REJECTED and "confirmation" in a.detail


def test_hub_view_exposes_only_declared_controls_and_no_provider_names():
    async def go():
        home = await _home()
        for d in list(home.devices.values()):
            home.review(d.id, ReviewStatus.MINE, OWNER)
        return home_view(home)
    view = run(go())
    switch = next(d for d in view["devices"] if d["name"] == "Kitchen Ceiling")
    assert [c["capability"] for c in switch["controls"]] == ["SWITCH"]
    assert "demo" not in str({k: v for k, v in view.items() if k != "house"}).lower().replace("demo home", "")
    assert all("technical" not in d for d in view["devices"])

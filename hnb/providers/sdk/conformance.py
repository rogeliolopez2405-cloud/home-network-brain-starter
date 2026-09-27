"""Conformance kit: the checks every provider adapter must pass.

Write a *harness* for your adapter — it builds the adapter against a local
simulator (never a live account) and knows how to break it:

    class AcmeHarness:
        @contextlib.asynccontextmanager
        async def adapter(self):            # yield a started adapter; clean up after
            ...
        async def break_provider(self):     # make the provider unreachable
            ...
        expire_auth = None                  # or an async method that makes credentials fail

Then, in your plugin's test file:

    import pytest
    from hnb.providers.sdk.conformance import CHECKS, run_check
    from .adapter import AcmeHarness   # or wherever it lives

    @pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
    def test_conformance(check):
        run_check(AcmeHarness, check)

A check that doesn't apply (e.g. credential checks for an adapter without
credentials) raises `NotApplicable`, which pytest reports as skipped.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from hnb.core.capabilities import CapabilityError, CapabilityType, validate
from hnb.providers.base import (
    AuthExpired,
    DeviceRef,
    ExecuteStatus,
    HealthStatus,
    ProviderAdapter,
    ProviderHealth,
    ProviderUnavailable,
)

Check = Callable[[Any, ProviderAdapter], Awaitable[None]]

# Two different values per writable capability, written in turn: a write that does
# nothing can't pass by coincidence with the device's starting state.
SAMPLE_VALUES: dict[CapabilityType, tuple[Any, Any]] = {
    CapabilityType.POWER: (True, False), CapabilityType.SWITCH: (True, False), CapabilityType.MUTE: (True, False),
    CapabilityType.BRIGHTNESS: (42, 77), CapabilityType.VOLUME: (10, 30),
    CapabilityType.COLOR: ({"h": 120, "s": 80}, {"h": 30, "s": 50}),
    CapabilityType.COLOR_TEMPERATURE: (3000, 4000),
    CapabilityType.LOCK: ("LOCKED", "UNLOCKED"),
    CapabilityType.THERMOSTAT: ({"mode": "HEAT", "setpoint_c": 20.0}, {"mode": "COOL", "setpoint_c": 22.5}),
}


class NotApplicable(Exception):
    """This check doesn't apply to this adapter."""


def run_check(harness_cls: type, check: Check) -> None:
    """Run one check against a fresh adapter from the harness (pytest-friendly)."""
    async def go() -> None:
        h = harness_cls()
        async with h.adapter() as a:
            await check(h, a)
    try:
        asyncio.run(go())
    except NotApplicable as e:
        import pytest
        pytest.skip(str(e))


# ── the checks ─────────────────────────────────────────────────────────
async def identity(h, a: ProviderAdapter) -> None:
    """Stable lowercase id, a name and a one-line description for the owner."""
    assert isinstance(a, ProviderAdapter)
    assert a.provider_id and a.provider_id == a.provider_id.lower(), "provider_id must be lowercase"
    assert a.display_name, "display_name is required"
    assert a.description, "description is required (shown when adding the service)"


async def discovery_refs_unique_and_own(h, a: ProviderAdapter) -> None:
    """Discovery returns at least one device; refs are unique and carry this provider id."""
    refs = [d.ref for d in await a.discover_devices()]
    assert refs, "discovery returned nothing (the harness should expose at least one device)"
    assert len(set(refs)) == len(refs), "duplicate DeviceRefs"
    assert all(r.provider == a.provider_id for r in refs), "a ref names another provider"


async def capabilities_match_discovery(h, a: ProviderAdapter) -> None:
    """get_capabilities agrees with what discovery declared."""
    for d in await a.discover_devices():
        got = {s.type for s in await a.get_capabilities(d.ref)}
        assert got == {s.type for s in d.capabilities}, f"{d.name}: capabilities differ"


async def state_is_declared_and_valid(h, a: ProviderAdapter) -> None:
    """State only for declared capabilities, and every writable value passes the core validator."""
    for d in await a.discover_devices():
        specs = {s.type: s for s in d.capabilities}
        for r in await a.get_state(d.ref):
            assert r.capability in specs, f"{d.name}: state for undeclared {r.capability.value}"
            if specs[r.capability].writable:
                validate(specs[r.capability], r.value)


async def write_then_read_back(h, a: ProviderAdapter) -> None:
    """Writes return ACCEPTED or CONFIRMED; CONFIRMED means get_state now reports the value.
    Each writable capability is set to two different values in turn."""
    for d in await a.discover_devices():
        for spec in d.capabilities:
            if not spec.writable or spec.type not in SAMPLE_VALUES:
                continue
            for sample in SAMPLE_VALUES[spec.type]:
                try:
                    value = validate(spec, sample)
                except CapabilityError:
                    continue                                  # outside this device's range
                result = await a.execute(d.ref, spec.type, value)
                assert result.status in (ExecuteStatus.ACCEPTED, ExecuteStatus.CONFIRMED)
                if result.status is ExecuteStatus.CONFIRMED:
                    state = {r.capability: r.value for r in await a.get_state(d.ref)}
                    assert state.get(spec.type) == value,                         f"{d.name}: {spec.type.value} CONFIRMED as {value!r} but the device reports {state.get(spec.type)!r}"


async def health_reports_trouble_without_raising(h, a: ProviderAdapter) -> None:
    """health() is OK when working and DOWN when the provider is unreachable — never raises."""
    assert (await a.health()).status is HealthStatus.OK
    await h.break_provider()
    health = await a.health()
    assert isinstance(health, ProviderHealth) and health.status is HealthStatus.DOWN


async def unreachable_raises_instead_of_inventing_state(h, a: ProviderAdapter) -> None:
    """An unreachable provider raises ProviderUnavailable — it never reports OFF."""
    ref = (await a.discover_devices())[0].ref
    await h.break_provider()
    try:
        await a.get_state(ref)
    except ProviderUnavailable:
        return
    raise AssertionError("get_state returned a value while the provider was unreachable")


async def expired_auth_is_distinguishable(h, a: ProviderAdapter) -> None:
    """Rejected credentials raise AuthExpired and health says AUTH_REQUIRED."""
    if getattr(h, "expire_auth", None) is None:
        raise NotApplicable("adapter holds no credentials")
    ref = (await a.discover_devices())[0].ref
    await h.expire_auth()
    try:
        await a.get_state(ref)
    except AuthExpired:
        pass
    else:
        raise AssertionError("get_state did not raise AuthExpired with bad credentials")
    assert (await a.health()).status is HealthStatus.AUTH_REQUIRED


async def missing_credentials_are_auth_required(h, a: ProviderAdapter) -> None:
    """With required secrets missing, health is AUTH_REQUIRED — not DOWN, not OK."""
    if not a.required_secrets:
        raise NotApplicable("adapter needs no credentials")
    values = getattr(a._secrets, "values", None)
    if not isinstance(values, dict):
        raise NotApplicable("harness does not use FakeSecrets")
    values.clear()
    assert (await a.health()).status is HealthStatus.AUTH_REQUIRED


async def unknown_device_is_an_error(h, a: ProviderAdapter) -> None:
    """Asking about a device that doesn't exist raises; it doesn't return empty state."""
    try:
        await a.get_state(DeviceRef(a.provider_id, "does-not-exist"))
    except Exception:
        return
    raise AssertionError("get_state accepted an unknown device")


CHECKS: list[Check] = [
    identity, discovery_refs_unique_and_own, capabilities_match_discovery, state_is_declared_and_valid,
    write_then_read_back, health_reports_trouble_without_raising, unreachable_raises_instead_of_inventing_state,
    expired_auth_is_distinguishable, missing_credentials_are_auth_required, unknown_device_is_an_error,
]

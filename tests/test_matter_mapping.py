"""Matter ⇄ canonical mapping, against fictional node dumps."""
import json
from pathlib import Path

import pytest

from hnb.core.capabilities import CapabilityType as C, validate
from hnb.core.model import DeviceKind
from hnb.providers.matter import mapping as M

NODES = json.loads((Path(__file__).parent / "fixtures" / "matter_nodes.json").read_text(encoding="utf-8"))


def caps(node, ep):
    return {s.type: s for s in M.capabilities(node, ep)}


def test_color_light_exposes_exactly_what_it_serves():
    n = NODES["color_light"]
    assert M.controllable_endpoints(n) == [1]                   # endpoint 0 is never a device
    c = caps(n, 1)
    assert set(c) == {C.POWER, C.BRIGHTNESS, C.COLOR, C.COLOR_TEMPERATURE}
    assert c[C.COLOR_TEMPERATURE].options == {"min": 2700, "max": 6550}
    assert M.device_kind(M.device_types(n, 1)) is DeviceKind.LIGHT
    assert M.name_for(n, 1, multi=False) == "Living Room Lamp"
    r = M.readings(n, 1, c)
    assert r == {C.POWER: True, C.BRIGHTNESS: 50, C.COLOR: {"h": 120, "s": 80}, C.COLOR_TEMPERATURE: 4000}


def test_named_struct_fields_and_ct_only_light():
    n = NODES["ct_light_named_structs"]
    c = caps(n, 1)
    assert set(c) == {C.POWER, C.BRIGHTNESS, C.COLOR_TEMPERATURE}      # feature map says CT only
    assert M.readings(n, 1, c)[C.COLOR_TEMPERATURE] == 2700
    assert M.name_for(n, 1, multi=False) == "Tunable White Downlight"


def test_plug_has_power_only():
    n = NODES["plug"]
    assert set(caps(n, 1)) == {C.POWER}
    assert M.device_kind(M.device_types(n, 1)) is DeviceKind.PLUG


def test_two_gang_switch_is_two_devices():
    n = NODES["two_gang_switch"]
    assert M.controllable_endpoints(n) == [1, 2]
    assert [M.name_for(n, ep, multi=True) for ep in (1, 2)] == ["Kitchen Switch 1", "Kitchen Switch 2"]
    assert M.readings(n, 2, caps(n, 2)) == {C.POWER: False}


def test_dimmer_switch_serving_on_off_is_a_switch():
    n = NODES["dimmer_switch_with_server"]
    assert set(caps(n, 1)) == {C.SWITCH, C.BRIGHTNESS}
    assert M.device_kind(M.device_types(n, 1)) is DeviceKind.SWITCH


def test_bridge_children_are_devices_named_by_their_bridged_info():
    n = NODES["bridge"]
    assert M.controllable_endpoints(n) == [2, 3]              # aggregator endpoint 1 serves no On/Off
    assert M.name_for(n, 2, multi=True) == "Porch Light"
    assert M.name_for(n, 3, multi=True) == "Legacy Plug"
    assert M.vendor(n, 2) == "Example Legacy" and M.unique_id(n, 2) == "UID-BRIDGED-0002#2"
    assert set(caps(n, 2)) == {C.POWER, C.BRIGHTNESS}


def test_sensor_only_node_offers_no_controllable_device():
    assert M.controllable_endpoints(NODES["sensor_only"]) == []


def test_missing_attribute_means_capability_not_offered():
    n = json.loads(json.dumps(NODES["color_light"]))
    del n["attributes"]["1/768/0"]                           # no CurrentHue reported
    assert C.COLOR not in caps(n, 1)


def test_brightness_round_trips_for_every_percent():
    assert [M.level_to_percent(M.percent_to_level(p)) for p in range(1, 101)] == list(range(1, 101))


@pytest.mark.parametrize("kelvin", [2200, 2500, 2700, 3000, 3500, 4000, 4500, 5000, 5500, 6000, 6500])
def test_common_colour_temperatures_round_trip(kelvin):
    assert M.mireds_to_kelvin(M.kelvin_to_mireds(kelvin)) == kelvin


def test_brightness_zero_never_sends_level_zero():
    assert M.percent_to_level(0) == 1


@pytest.mark.parametrize("deg", [0, 1, 45, 120, 200, 359])
def test_hue_round_trip_within_one_degree(deg):
    assert abs(M.hue_to_degrees(M.degrees_to_hue(deg)) - deg) <= 1 or {deg, M.hue_to_degrees(M.degrees_to_hue(deg))} == {359, 0}


def test_commands_and_readback_paths():
    on = M.command_for(1, C.POWER, True)
    assert (on.cluster_id, on.command_name, on.payload, on.readback) == (6, "On", {}, ("1/6/0",))
    b = M.command_for(3, C.BRIGHTNESS, 50)
    assert b.command_name == "MoveToLevelWithOnOff" and b.payload["level"] == 127 and b.readback == ("3/8/0",)
    ct = M.command_for(1, C.COLOR_TEMPERATURE, 4000)
    assert ct.payload["colorTemperatureMireds"] == 250
    col = M.command_for(1, C.COLOR, {"h": 120, "s": 80})
    assert (col.payload["hue"], col.payload["saturation"]) == (85, 203)


def test_confirmation_tolerates_device_rounding_but_not_disagreement():
    b = M.command_for(1, C.BRIGHTNESS, 50)                  # level 127
    assert M.confirms(b, {"1/8/0": 128})
    assert M.observed_value(C.BRIGHTNESS, b, {"1/8/0": 128}, 50) == 50
    assert not M.confirms(b, {"1/8/0": 200})
    assert M.observed_value(C.BRIGHTNESS, b, {"1/8/0": 200}, 50) == 79
    off = M.command_for(1, C.POWER, False)
    assert not M.confirms(off, {"1/6/0": True}) and not M.confirms(off, {})


def test_every_exposed_capability_accepts_its_own_reading():
    """What the mapping reads back must pass the core's validator."""
    for key in ("color_light", "ct_light_named_structs", "plug", "two_gang_switch", "bridge"):
        n = NODES[key]
        for ep in M.controllable_endpoints(n):
            specs = M.capabilities(n, ep)
            for cap, value in M.readings(n, ep, [s.type for s in specs]).items():
                spec = next(s for s in specs if s.type is cap)
                validate(spec, value)


def test_external_ids():
    assert M.external_id(5, 1) == "5-1" and M.parse_external_id("5-1") == (5, 1)
    with pytest.raises(M.MappingError):
        M.parse_external_id("nope")

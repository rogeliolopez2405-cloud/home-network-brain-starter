"""Matter ⇄ canonical mapping. Pure functions, no I/O.

Input is a node as the Matter server reports it (python-matter-server /
matterjs-server shape): `{"node_id": 5, "available": true, "attributes":
{"<endpoint>/<cluster>/<attribute>": value, ...}}`.

Rules that keep the Hub honest:

* A Starter device is one Matter **endpoint** that has an On/Off *server*
  cluster. A two-gang switch is two devices; a bridge is one device per
  bridged endpoint. Endpoint 0 (the node's root) is never a device.
* Capabilities come only from clusters the endpoint actually serves (Descriptor
  ServerList) and, for colour, from the features it declares. Nothing is
  assumed from the device type alone.
* Values are converted at the edge: Matter levels 1–254 ⇄ percent, hue and
  saturation 0–254 ⇄ degrees/percent, mireds ⇄ kelvin. A read-back is
  "confirmed" when the device reports what we sent *in device units*, so
  rounding between unit systems never turns a success into a mismatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from hnb.core.capabilities import CapabilitySpec, CapabilityType
from hnb.core.model import DeviceKind

C = CapabilityType

# Cluster ids
DESCRIPTOR = 0x001D
BASIC_INFO = 0x0028
BRIDGED_BASIC_INFO = 0x0039
ON_OFF = 0x0006
LEVEL = 0x0008
COLOR = 0x0300

# Attribute ids
FEATURE_MAP = 0xFFFC
DESC_DEVICE_TYPES, DESC_SERVERS, DESC_PARTS = 0, 1, 3
BASIC_VENDOR, BASIC_PRODUCT, BASIC_LABEL, BASIC_UNIQUE_ID = 1, 3, 5, 18
ONOFF_ON = 0
LEVEL_CURRENT, LEVEL_MIN, LEVEL_MAX = 0, 2, 3
COLOR_HUE, COLOR_SAT, COLOR_MIREDS = 0, 1, 7
COLOR_CAPABILITIES, COLOR_MIN_MIREDS, COLOR_MAX_MIREDS = 0x400A, 0x400B, 0x400C

# Colour Control feature bits (FeatureMap and ColorCapabilities share the layout)
FEAT_HS, FEAT_CT = 1 << 0, 1 << 4

# Device types
DT_ONOFF_LIGHT, DT_DIMMABLE_LIGHT = 0x0100, 0x0101
DT_ONOFF_SWITCH, DT_DIMMER_SWITCH, DT_COLOR_DIMMER_SWITCH = 0x0103, 0x0104, 0x0105
DT_ONOFF_PLUG, DT_DIMMABLE_PLUG = 0x010A, 0x010B
DT_CT_LIGHT, DT_EXT_COLOR_LIGHT = 0x010C, 0x010D
DT_AGGREGATOR, DT_BRIDGED_NODE = 0x000E, 0x0013

LIGHT_TYPES = {DT_ONOFF_LIGHT, DT_DIMMABLE_LIGHT, DT_CT_LIGHT, DT_EXT_COLOR_LIGHT}
PLUG_TYPES = {DT_ONOFF_PLUG, DT_DIMMABLE_PLUG}
SWITCH_TYPES = {DT_ONOFF_SWITCH, DT_DIMMER_SWITCH, DT_COLOR_DIMMER_SWITCH}

LEVEL_MAX_MATTER = 254
DEFAULT_MIREDS = (153, 500)          # 6500 K … 2000 K, used only when the device omits its range
TRANSITION = 0                        # tenths of a second; act now so read-back can confirm


class MappingError(ValueError):
    pass


# ── ids ─────────────────────────────────────────────────────────────────
def external_id(node_id: int, endpoint: int) -> str:
    return f"{int(node_id)}-{int(endpoint)}"


def parse_external_id(ext: str) -> tuple[int, int]:
    try:
        node, ep = ext.split("-", 1)
        return int(node), int(ep)
    except (ValueError, AttributeError):
        raise MappingError(f"not a Matter device id: {ext!r}") from None


def path(endpoint: int, cluster: int, attribute: int) -> str:
    return f"{endpoint}/{cluster}/{attribute}"


# ── reading a node ──────────────────────────────────────────────────────
def attr(node: dict, endpoint: int, cluster: int, attribute: int, default: Any = None) -> Any:
    return (node.get("attributes") or {}).get(path(endpoint, cluster, attribute), default)


def endpoints(node: dict) -> list[int]:
    eps = set()
    for key in node.get("attributes") or {}:
        try:
            eps.add(int(str(key).split("/", 1)[0]))
        except ValueError:
            continue
    return sorted(eps)


def _struct_field(item: Any, *names: str) -> Any:
    """Matter structs arrive keyed by field tag ("0") or by name; accept both."""
    if isinstance(item, dict):
        for n in names:
            if n in item:
                return item[n]
        return None
    return item


def device_types(node: dict, endpoint: int) -> set[int]:
    out = set()
    for item in attr(node, endpoint, DESCRIPTOR, DESC_DEVICE_TYPES, []) or []:
        dt = _struct_field(item, "0", "deviceType", "device_type")
        if isinstance(dt, int):
            out.add(dt)
    return out


def servers(node: dict, endpoint: int) -> set[int]:
    return {c for c in attr(node, endpoint, DESCRIPTOR, DESC_SERVERS, []) or [] if isinstance(c, int)}


def controllable_endpoints(node: dict) -> list[int]:
    return [ep for ep in endpoints(node) if ep != 0 and ON_OFF in servers(node, ep)]


def device_kind(types: Iterable[int]) -> DeviceKind:
    types = set(types)
    if types & PLUG_TYPES:
        return DeviceKind.PLUG
    if types & SWITCH_TYPES:
        return DeviceKind.SWITCH
    return DeviceKind.LIGHT


def _color_features(node: dict, ep: int) -> int:
    fm = attr(node, ep, COLOR, FEATURE_MAP)
    if isinstance(fm, int):
        return fm
    caps = attr(node, ep, COLOR, COLOR_CAPABILITIES)
    return caps if isinstance(caps, int) else 0


def kelvin_range(node: dict, ep: int) -> tuple[int, int]:
    lo_m = attr(node, ep, COLOR, COLOR_MIN_MIREDS) or DEFAULT_MIREDS[0]
    hi_m = attr(node, ep, COLOR, COLOR_MAX_MIREDS) or DEFAULT_MIREDS[1]
    lo_m, hi_m = max(1, int(lo_m)), max(1, int(hi_m))
    return mireds_to_kelvin(hi_m), mireds_to_kelvin(lo_m)


def capabilities(node: dict, ep: int) -> tuple[CapabilitySpec, ...]:
    srv, types = servers(node, ep), device_types(node, ep)
    specs: list[CapabilitySpec] = []
    if ON_OFF in srv and attr(node, ep, ON_OFF, ONOFF_ON) is not None:
        specs.append(CapabilitySpec(C.SWITCH if types & SWITCH_TYPES else C.POWER))
    if LEVEL in srv and attr(node, ep, LEVEL, LEVEL_CURRENT, "missing") != "missing":
        specs.append(CapabilitySpec(C.BRIGHTNESS))
    if COLOR in srv:
        feats = _color_features(node, ep)
        if feats & FEAT_HS and attr(node, ep, COLOR, COLOR_HUE) is not None:
            specs.append(CapabilitySpec(C.COLOR))
        if feats & FEAT_CT and attr(node, ep, COLOR, COLOR_MIREDS) is not None:
            k_min, k_max = kelvin_range(node, ep)
            specs.append(CapabilitySpec(C.COLOR_TEMPERATURE, options={"min": k_min, "max": k_max}))
    return tuple(specs)


def name_for(node: dict, ep: int, multi: bool) -> str:
    bridged = attr(node, ep, BRIDGED_BASIC_INFO, BASIC_LABEL) or attr(node, ep, BRIDGED_BASIC_INFO, BASIC_PRODUCT)
    if bridged:
        return str(bridged).strip()[:80]
    base = (attr(node, 0, BASIC_INFO, BASIC_LABEL) or attr(node, 0, BASIC_INFO, BASIC_PRODUCT)
            or f"Matter device {node.get('node_id')}")
    base = str(base).strip()
    return (f"{base} {ep}" if multi else base)[:80]


def vendor(node: dict, ep: int) -> str | None:
    v = attr(node, ep, BRIDGED_BASIC_INFO, BASIC_VENDOR) or attr(node, 0, BASIC_INFO, BASIC_VENDOR)
    return str(v)[:80] if v else None


def product(node: dict, ep: int) -> str | None:
    p = attr(node, ep, BRIDGED_BASIC_INFO, BASIC_PRODUCT) or attr(node, 0, BASIC_INFO, BASIC_PRODUCT)
    return str(p)[:80] if p else None


def unique_id(node: dict, ep: int) -> str | None:
    u = attr(node, ep, BRIDGED_BASIC_INFO, BASIC_UNIQUE_ID) or attr(node, 0, BASIC_INFO, BASIC_UNIQUE_ID)
    return f"{u}#{ep}" if u else None


# ── unit conversion ─────────────────────────────────────────────────────
def level_to_percent(level: int | None) -> int | None:
    if level is None:
        return None
    return max(0, min(100, round(int(level) * 100 / LEVEL_MAX_MATTER)))


def percent_to_level(pct: int) -> int:
    return max(1, min(LEVEL_MAX_MATTER, round(int(pct) * LEVEL_MAX_MATTER / 100)))


def hue_to_degrees(h: int) -> int:
    return round(int(h) * 360 / 254) % 360


def degrees_to_hue(deg: int) -> int:
    return max(0, min(254, round((int(deg) % 360) * 254 / 360)))


def sat_to_percent(s: int) -> int:
    return max(0, min(100, round(int(s) * 100 / 254)))


def percent_to_sat(p: int) -> int:
    return max(0, min(254, round(int(p) * 254 / 100)))


def mireds_to_kelvin(m: int) -> int:
    # Snapped to 50 K. Mireds are coarse in kelvin terms (333 mireds is 3003 K;
    # at the cool end one mired is ~42 K), so without snapping a light set to
    # 3000 K or 6500 K would read back as 3003 K / 6490 K on the next poll.
    return int(round(1_000_000 / max(1, int(m)) / 50) * 50)


def kelvin_to_mireds(k: int) -> int:
    return max(1, round(1_000_000 / max(1, int(k))))


# ── state ───────────────────────────────────────────────────────────────
def readings(node: dict, ep: int, caps: Iterable[CapabilityType]) -> dict[CapabilityType, Any]:
    out: dict[CapabilityType, Any] = {}
    for cap in caps:
        if cap in (C.POWER, C.SWITCH):
            v = attr(node, ep, ON_OFF, ONOFF_ON)
            if v is not None:
                out[cap] = bool(v)
        elif cap is C.BRIGHTNESS:
            v = level_to_percent(attr(node, ep, LEVEL, LEVEL_CURRENT))
            if v is not None:
                out[cap] = v
        elif cap is C.COLOR:
            h, s = attr(node, ep, COLOR, COLOR_HUE), attr(node, ep, COLOR, COLOR_SAT)
            if h is not None and s is not None:
                out[cap] = {"h": hue_to_degrees(h), "s": sat_to_percent(s)}
        elif cap is C.COLOR_TEMPERATURE:
            m = attr(node, ep, COLOR, COLOR_MIREDS)
            if m:
                out[cap] = mireds_to_kelvin(m)
    return out


# ── commands ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Command:
    cluster_id: int
    command_name: str
    payload: dict[str, Any]
    readback: tuple[str, ...]          # attribute paths to read afterwards
    expected: dict[str, Any]           # path → value the device should report, in device units


def command_for(ep: int, cap: CapabilityType, value: Any) -> Command:
    """`value` has already been validated/normalised by the core."""
    opts = {"optionsMask": 0, "optionsOverride": 0}
    if cap in (C.POWER, C.SWITCH):
        p = path(ep, ON_OFF, ONOFF_ON)
        return Command(ON_OFF, "On" if value else "Off", {}, (p,), {p: bool(value)})
    if cap is C.BRIGHTNESS:
        level = percent_to_level(value)
        p = path(ep, LEVEL, LEVEL_CURRENT)
        return Command(LEVEL, "MoveToLevelWithOnOff", {"level": level, "transitionTime": TRANSITION, **opts},
                       (p,), {p: level})
    if cap is C.COLOR:
        hue, sat = degrees_to_hue(value["h"]), percent_to_sat(value["s"])
        ph, ps = path(ep, COLOR, COLOR_HUE), path(ep, COLOR, COLOR_SAT)
        return Command(COLOR, "MoveToHueAndSaturation",
                       {"hue": hue, "saturation": sat, "transitionTime": TRANSITION, **opts},
                       (ph, ps), {ph: hue, ps: sat})
    if cap is C.COLOR_TEMPERATURE:
        m = kelvin_to_mireds(value)
        p = path(ep, COLOR, COLOR_MIREDS)
        return Command(COLOR, "MoveToColorTemperature",
                       {"colorTemperatureMireds": m, "transitionTime": TRANSITION, **opts}, (p,), {p: m})
    raise MappingError(f"Matter adapter can't do {cap.value} yet")


# Devices round and clamp; a read-back within this many device units counts as confirmed.
TOLERANCE = {ON_OFF: 0, LEVEL: 1, COLOR: 2}


def confirms(cmd: Command, observed: dict[str, Any]) -> bool:
    for p, want in cmd.expected.items():
        got = observed.get(p)
        if got is None:
            return False
        if isinstance(want, bool):
            if bool(got) is not want:
                return False
        elif abs(int(got) - int(want)) > TOLERANCE.get(cmd.cluster_id, 0):
            return False
    return True


def observed_value(cap: CapabilityType, cmd: Command, observed: dict[str, Any], requested: Any) -> Any:
    """The canonical value to record: what we asked for when the device agrees
    (within tolerance), otherwise what the device actually reports."""
    if confirms(cmd, observed):
        return requested
    node = {"attributes": observed}
    ep = int(next(iter(cmd.readback)).split("/", 1)[0])
    return readings(node, ep, [cap]).get(cap)

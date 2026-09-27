"""Canonical capability vocabulary.

Every provider adapter translates its own device features into these types.
The Home Hub renders controls from capabilities alone, so a device only ever
shows a button for something its adapter declared it can actually do.

Values are validated and normalised here, once, before any adapter sees them.
An adapter never has to defend itself against an out-of-range brightness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CapabilityType(str, Enum):
    POWER = "POWER"                          # bool — the device itself on/off (lamp, TV)
    SWITCH = "SWITCH"                        # bool — a wall switch driving a load
    BRIGHTNESS = "BRIGHTNESS"                # int 0..100 (percent)
    COLOR = "COLOR"                          # {"h": 0..360, "s": 0..100}
    COLOR_TEMPERATURE = "COLOR_TEMPERATURE"  # int kelvin, within the spec's min/max
    VOLUME = "VOLUME"                        # int 0..100
    MUTE = "MUTE"                            # bool
    APP_LAUNCH = "APP_LAUNCH"                # str, one of spec.options["apps"]
    LOCK = "LOCK"                            # "LOCKED" | "UNLOCKED"
    THERMOSTAT = "THERMOSTAT"                # {"mode": ..., "setpoint_c": float}
    CAMERA_LIVE = "CAMERA_LIVE"              # read-only: a stream is available
    CAMERA_EVENT = "CAMERA_EVENT"            # read-only: motion / doorbell events


class Sensitivity(str, Enum):
    """How much proof of intent a write needs.

    NORMAL  — an authenticated owner session is enough.
    HIGH    — additionally requires an explicit confirmation step in the UI
              and is always written to the audit trail with the actor.
    """
    NORMAL = "NORMAL"
    HIGH = "HIGH"


THERMOSTAT_MODES = ("OFF", "HEAT", "COOL", "AUTO")
LOCK_VALUES = ("LOCKED", "UNLOCKED")

# Capabilities that can never be written, whatever an adapter claims.
READ_ONLY = frozenset({CapabilityType.CAMERA_LIVE, CapabilityType.CAMERA_EVENT})

SENSITIVITY = {CapabilityType.LOCK: Sensitivity.HIGH}


class CapabilityError(ValueError):
    """A value, or a request, that the capability cannot accept."""


@dataclass(frozen=True)
class CapabilitySpec:
    """What a device can do for one capability, as declared by its adapter."""

    type: CapabilityType
    readable: bool = True
    writable: bool = True
    # Type-specific bounds: {"min": 2200, "max": 6500} for COLOR_TEMPERATURE,
    # {"apps": ["Netflix", ...]} for APP_LAUNCH, {"modes": [...]} for THERMOSTAT.
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type in READ_ONLY and self.writable:
            object.__setattr__(self, "writable", False)

    @property
    def sensitivity(self) -> Sensitivity:
        return SENSITIVITY.get(self.type, Sensitivity.NORMAL)


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise CapabilityError(f"expected true/false, got {v!r}")


def _percent(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CapabilityError(f"expected a number 0-100, got {v!r}")
    if not 0 <= v <= 100:
        raise CapabilityError(f"{v} is outside 0-100")
    return int(round(v))


def validate(spec: CapabilitySpec, value: Any) -> Any:
    """Return the normalised value for a write, or raise CapabilityError."""
    if not spec.writable:
        raise CapabilityError(f"{spec.type.value} is read-only on this device")

    t = spec.type
    if t in (CapabilityType.POWER, CapabilityType.SWITCH, CapabilityType.MUTE):
        return _bool(value)
    if t in (CapabilityType.BRIGHTNESS, CapabilityType.VOLUME):
        return _percent(value)
    if t is CapabilityType.COLOR:
        if not isinstance(value, dict) or set(value) != {"h", "s"}:
            raise CapabilityError('expected {"h": 0-360, "s": 0-100}')
        h, s = value["h"], value["s"]
        if not isinstance(h, (int, float)) or isinstance(h, bool) or not 0 <= h <= 360:
            raise CapabilityError(f"hue {h!r} is outside 0-360")
        return {"h": int(round(h)) % 360, "s": _percent(s)}
    if t is CapabilityType.COLOR_TEMPERATURE:
        lo, hi = spec.options.get("min", 2000), spec.options.get("max", 6500)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise CapabilityError(f"expected kelvin {lo}-{hi}, got {value!r}")
        return int(round(value))
    if t is CapabilityType.APP_LAUNCH:
        apps = spec.options.get("apps", [])
        if value not in apps:
            raise CapabilityError(f"{value!r} is not an app this device offers")
        return value
    if t is CapabilityType.LOCK:
        if value not in LOCK_VALUES:
            raise CapabilityError(f"expected one of {LOCK_VALUES}, got {value!r}")
        return value
    if t is CapabilityType.THERMOSTAT:
        modes = spec.options.get("modes", list(THERMOSTAT_MODES))
        lo, hi = spec.options.get("min_c", 7.0), spec.options.get("max_c", 32.0)
        if not isinstance(value, dict) or value.get("mode") not in modes:
            raise CapabilityError(f"expected mode in {modes}")
        sp = value.get("setpoint_c")
        if isinstance(sp, bool) or not isinstance(sp, (int, float)) or not lo <= sp <= hi:
            raise CapabilityError(f"setpoint must be {lo}-{hi} °C")
        return {"mode": value["mode"], "setpoint_c": round(float(sp), 1)}
    raise CapabilityError(f"no validator for {t.value}")  # pragma: no cover


# Human- and developer-facing description of each capability's value.
DESCRIPTIONS: dict[CapabilityType, tuple[str, str]] = {
    CapabilityType.POWER: ("The device itself on or off (lamp, TV, plug).", "true | false"),
    CapabilityType.SWITCH: ("A wall switch driving a load.", "true | false"),
    CapabilityType.BRIGHTNESS: ("Light level.", "integer 0–100 (percent)"),
    CapabilityType.COLOR: ("Colour as hue and saturation.", '{"h": 0–360, "s": 0–100}'),
    CapabilityType.COLOR_TEMPERATURE: ("White warmth.", "integer kelvin within options.min … options.max"),
    CapabilityType.VOLUME: ("Audio volume.", "integer 0–100"),
    CapabilityType.MUTE: ("Audio muted.", "true | false"),
    CapabilityType.APP_LAUNCH: ("Open an app on a TV.", "one of options.apps"),
    CapabilityType.LOCK: ("Door lock. HIGH sensitivity: the action needs confirm=true.", '"LOCKED" | "UNLOCKED"'),
    CapabilityType.THERMOSTAT: ("Heating/cooling.", '{"mode": one of options.modes, "setpoint_c": number}'),
    CapabilityType.CAMERA_LIVE: ("A live stream is available. Read-only.", "(provider-specific; read-only)"),
    CapabilityType.CAMERA_EVENT: ("Motion / doorbell events. Read-only.", '{"last": str, "minutes_ago": int} (read-only)'),
}


def catalog() -> list[dict[str, Any]]:
    """The vocabulary, for GET /api/capabilities and the docs."""
    return [{"type": t.value, "description": DESCRIPTIONS[t][0], "value": DESCRIPTIONS[t][1],
             "writable": t not in READ_ONLY, "sensitivity": SENSITIVITY.get(t, Sensitivity.NORMAL).value}
            for t in CapabilityType]

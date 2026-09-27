"""HomeView — the one shape the Home Hub renders.

Pure projection of the in-memory Home: it never calls a provider, so a slow or
failing provider cannot slow the Hub down. A provider in trouble degrades only
the devices it serves (and the sections they sit in); everything else renders
normally.

The Hub never sees provider names, external ids or connection details. Those
live under `technical`, used only by Settings and the review screen.
"""
from __future__ import annotations

from typing import Any

from hnb.providers.base import HealthStatus

from .capabilities import CapabilityType
from .home import Home
from .model import Device, DeviceKind, Freshness, ReviewStatus, utcnow

SECTION_OF = {
    DeviceKind.LIGHT: "lights", DeviceKind.SWITCH: "lights", DeviceKind.PLUG: "lights",
    DeviceKind.TV: "entertainment", DeviceKind.SPEAKER: "entertainment",
    DeviceKind.CAMERA: "cameras", DeviceKind.DOORBELL: "cameras",
    DeviceKind.NETWORK: "network",
    DeviceKind.LOCK: "more", DeviceKind.THERMOSTAT: "more",
    DeviceKind.SENSOR: "more", DeviceKind.OTHER: "more",
}
SECTIONS = ("lights", "entertainment", "cameras", "network", "more")
ON_CAPS = (CapabilityType.POWER, CapabilityType.SWITCH)
HEALTH_WORDS = {
    HealthStatus.OK: "Working", HealthStatus.DEGRADED: "Slow",
    HealthStatus.AUTH_REQUIRED: "Needs sign-in", HealthStatus.DOWN: "Can't reach",
    HealthStatus.UNKNOWN: "Unknown",
}


def _provider_ok(home: Home, dev: Device) -> bool:
    for c in dev.connections:
        h = home.provider_health.get(c.provider)
        if h is not None and h.status not in (HealthStatus.OK, HealthStatus.DEGRADED):
            return False
    return True


def _reachable(home: Home, dev: Device) -> bool:
    if not _provider_ok(home, dev):
        return False
    return not any(sv.freshness is Freshness.UNKNOWN for sv in dev.state.values())


def _is_on(home: Home, dev: Device) -> bool | None:
    """True/False only when we actually know. None means 'can't say' — never OFF."""
    if not _reachable(home, dev):
        return None
    for cap in ON_CAPS:
        sv = dev.state.get(cap)
        if sv is not None and sv.freshness is not Freshness.UNKNOWN and sv.value is not None:
            return bool(sv.value)
    return None


def device_view(home: Home, dev: Device, technical: bool = False) -> dict[str, Any]:
    reachable = _reachable(home, dev)
    controls = []
    for cap, spec in dev.capabilities.items():
        sv = dev.state.get(cap)
        if sv is None:
            freshness = Freshness.UNKNOWN
        elif not reachable:
            freshness = Freshness.UNKNOWN
        else:
            freshness = home.effective_freshness(dev, sv)
        controls.append({
            "capability": cap.value,
            "writable": spec.writable,
            "sensitivity": spec.sensitivity.value,
            "options": spec.options,
            # Last known value is kept for context, but freshness says whether to trust it.
            "value": sv.value if sv else None,
            "freshness": freshness.value,
        })
    out: dict[str, Any] = {
        "id": dev.id, "name": dev.name, "kind": dev.kind.value,
        "room_id": dev.room_id, "favorite": dev.favorite,
        "section": SECTION_OF[dev.kind], "reachable": reachable,
        "on": _is_on(home, dev), "controls": controls,
    }
    if technical:
        out["technical"] = {
            "review": dev.review.value, "manufacturer": dev.manufacturer, "model": dev.model,
            "connections": [{"provider": c.provider, "priority": c.priority} for c in dev.connections],
        }
    return out


def home_view(home: Home, system: dict[str, Any] | None = None) -> dict[str, Any]:
    mine = home.claimed()
    devices = [device_view(home, d) for d in mine]
    by_id = {d["id"]: d for d in devices}
    unreachable = [d for d in devices if not d["reachable"]]

    sections: dict[str, Any] = {}
    for name in SECTIONS:
        ids = [d["id"] for d in devices if d["section"] == name]
        down = [i for i in ids if not by_id[i]["reachable"]]
        sections[name] = {"device_ids": ids, "degraded": bool(down), "unreachable": len(down)}
    sections["favorites"] = {"device_ids": [d["id"] for d in devices if d["favorite"]],
                             "degraded": any(not d["reachable"] for d in devices if d["favorite"])}

    if unreachable:
        level, summary = "warn", ("1 device can't be reached right now" if len(unreachable) == 1
                                  else f"{len(unreachable)} devices can't be reached right now")
    else:
        level, summary = "ok", "Everything is working"

    rooms = []
    for room in sorted(home.rooms.values(), key=lambda r: (r.sort, r.name)):
        in_room = [by_id[d.id] for d in mine if d.room_id == room.id]
        if not in_room:
            continue
        rooms.append({
            "id": room.id, "name": room.name, "icon": room.icon,
            "device_count": len(in_room),
            "lights_on": sum(1 for d in in_room if d["section"] == "lights" and d["on"] is True),
            "degraded": any(not d["reachable"] for d in in_room),
        })

    providers = []
    for pid, adapter in sorted(home.adapters.items()):
        h = home.provider_health.get(pid)
        status = h.status if h else HealthStatus.UNKNOWN
        providers.append({"name": adapter.display_name, "status": status.value,
                          "label": HEALTH_WORDS[status],
                          "devices": sum(1 for d in mine if any(c.provider == pid for c in d.connections))})

    return {
        "generated_at": utcnow().isoformat(),
        "house": {"name": home.house.name},
        "status": {
            "level": level, "summary": summary,
            "lights_on": sum(1 for d in devices if d["section"] == "lights" and d["on"] is True),
            # Lights we can't read are counted separately: "0 on" would be a claim.
            "lights_unknown": sum(1 for d in devices if d["section"] == "lights" and d["on"] is None),
            "devices": len(devices),
            "unreachable": len(unreachable),
            "needs_review": len(home.unreviewed()),
        },
        "rooms": rooms,
        "sections": sections,
        "devices": devices,
        "system": {
            "devices_responding": {"ok": len(devices) - len(unreachable), "total": len(devices)},
            "needs_review": len(home.unreviewed()),
            "services": providers,
            **(system or {}),
        },
    }


def review_view(home: Home) -> list[dict[str, Any]]:
    """Every device the hub knows, with its review status — what the
    'Is this yours?' step and the Devices page show."""
    return [
        {**device_view(home, d, technical=True),
         "review": d.review.value,
         "room": home.rooms[d.room_id].name if d.room_id in home.rooms else None}
        for d in sorted(home.devices.values(), key=lambda d: (d.review is not ReviewStatus.UNREVIEWED, d.name))
    ]

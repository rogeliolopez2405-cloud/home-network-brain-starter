"""Classify a pilot home's device inventory and pick the easiest first real device.

    python tools/classify_inventory.py /path/outside/the/repo/my-inventory.csv

Reads the CSV filled in from pilot/inventory-template.csv (keep the filled-in
file OUTSIDE this repository — it is the homeowner's data). Prints a
Markdown report; writes nothing.

Categories:
    DIRECT MATTER              the hub can control it itself (Matter; shared from Alexa by multi-admin)
    VENDOR ADAPTER             a documented local/official API exists; needs that brand's adapter (M3+)
    HOME ASSISTANT BRIDGE      only reachable through a Home Assistant integration
    ALEXA-ONLY                 not directly controllable (e.g. Zigbee paired to an Echo, skill-only cloud)
    UNKNOWN                    not enough information — check the device/app
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

DIRECT, VENDOR, HA, ALEXA, UNKNOWN = ("DIRECT MATTER", "VENDOR ADAPTER", "HOME ASSISTANT BRIDGE",
                                      "ALEXA-ONLY", "UNKNOWN")
YES = {"yes", "y", "true", "1", "matter", "logo", "code", "qr"}
FIRST_DEVICE_TYPES = ("switch", "dimmer", "plug", "bulb", "light", "lamp", "outlet")

# Brand → (category, why). Lower-case substring match on the brand column.
BRANDS: list[tuple[str, str, str]] = [
    ("philips hue", DIRECT, "Hue Bridge v2 shares its lights over Matter"),
    ("hue", DIRECT, "Hue Bridge v2 shares its lights over Matter"),
    ("lutron", VENDOR, "Caséta/RA3 LEAP local API"),
    ("caseta", VENDOR, "Caséta LEAP local API"),
    ("kasa", VENDOR, "TP-Link local API (Matter models share directly)"),
    ("tapo", VENDOR, "TP-Link local API (Matter models share directly)"),
    ("tp-link", VENDOR, "TP-Link local API (Matter models share directly)"),
    ("govee", VENDOR, "Govee LAN API (enable LAN control in the Govee app)"),
    ("ring", VENDOR, "Ring Partner API (private app)"),
    ("roku", VENDOR, "Roku ECP local API"),
    ("fire tv", VENDOR, "ADB over the network"),
    ("google tv", VENDOR, "Android TV Remote protocol"),
    ("android tv", VENDOR, "Android TV Remote protocol"),
    ("lg", VENDOR, "webOS local API"),
    ("samsung", VENDOR, "Tizen local API"),
    ("cync", HA, "GE Cync — Home Assistant integration"),
    ("meross", HA, "meross_lan (Home Assistant custom integration)"),
    ("tuya", HA, "Tuya — Home Assistant (cloud or local)"),
    ("smart life", HA, "Tuya/Smart Life — Home Assistant"),
    ("wyze", HA, "no official local API — Home Assistant community integration"),
    ("blink", HA, "blinkpy via Home Assistant (unofficial)"),
    ("echo", ALEXA, "Echo speakers are Alexa itself; some act as Thread/Zigbee hubs"),
    ("amazon", ALEXA, "Amazon device — controlled through Alexa"),
]


@dataclass
class Row:
    raw: dict[str, str]
    category: str
    why: str
    score: int


def _val(r: dict[str, str], key: str) -> str:
    return (r.get(key) or "").strip().lower()


def classify(r: dict[str, str]) -> tuple[str, str]:
    brand, radio, matter = _val(r, "brand"), _val(r, "radio"), _val(r, "matter_logo_or_code")
    by, dtype = _val(r, "controlled_by"), _val(r, "device_type")
    if "echo" in dtype or "echo" in brand:
        return ALEXA, "Echo device — part of Alexa; note the model (Thread/Zigbee hub?)"
    if matter in YES or "matter" in by:
        return DIRECT, "Matter: share from Alexa (Other Assistants and Apps → Add Another)"
    if radio == "zigbee" and ("alexa" in by or not brand):
        return ALEXA, "Zigbee paired to an Echo — not reachable without re-pairing to a hub-owned coordinator"
    for key, cat, why in BRANDS:
        if key in brand:
            return cat, why
    if "alexa" in by and not brand:
        return UNKNOWN, "Only known through Alexa — check the brand on the device or box"
    return UNKNOWN, "Brand/connection unclear — look for a Matter logo and the brand's app"


def score(r: dict[str, str], category: str) -> int:
    """Higher = easier first real device. Only DIRECT MATTER can be first in M2."""
    if category != DIRECT:
        return 0
    s = 50
    dtype, radio = _val(r, "device_type"), _val(r, "radio")
    if any(t in dtype for t in FIRST_DEVICE_TYPES):
        s += 30
    if "plug" in dtype or "outlet" in dtype:
        s += 5                       # easy to see it switch (everything is already installed)
    if radio in ("wi-fi", "wifi"):
        s += 15                      # reachable directly on the LAN
    # Thread earns nothing: shared from Alexa, the hub must reach it through an
    # Echo's Thread border router — the riskiest link for a first attempt.
    if _val(r, "in_alexa") in YES:
        s += 5                       # shareable from the Alexa app today
    return s


def classify_file(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]
    out = []
    for r in rows:
        cat, why = classify(r)
        out.append(Row(r, cat, why, score(r, cat)))
    return out


def report(rows: list[Row]) -> str:
    lines = ["# Pilot inventory — classification", "",
             "| Room | Device | Brand / model | Category | Why |", "|---|---|---|---|---|"]
    for x in rows:
        r = x.raw
        lines.append(f"| {r.get('room', '')} | {r.get('device_type', '')} | "
                     f"{r.get('brand', '')} {r.get('model', '')} | **{x.category}** | {x.why} |")
    counts = {c: sum(1 for x in rows if x.category == c) for c in (DIRECT, VENDOR, HA, ALEXA, UNKNOWN)}
    lines += ["", "**Totals:** " + " · ".join(f"{c}: {n}" for c, n in counts.items()), ""]
    ranked = sorted((x for x in rows if x.score), key=lambda x: -x.score)
    if ranked:
        best = ranked[0].raw
        lines += ["## Recommended first real device", "",
                  f"**{best.get('room', '')} — {best.get('brand', '')} {best.get('model', '')} "
                  f"({best.get('device_type', '')})**. Share it from the Alexa app with a setup code; "
                  "Alexa keeps controlling it.", ""]
        if len(ranked) > 1:
            lines.append("Runners-up: " + "; ".join(
                f"{x.raw.get('room', '')} {x.raw.get('brand', '')} {x.raw.get('device_type', '')}" for x in ranked[1:4]))
    else:
        top = max(counts, key=lambda c: counts[c] if c != UNKNOWN else -1)
        lines += ["## No Matter device found", "",
                  f"Nothing is directly controllable yet. Largest group: **{top}** — that decides M2's fallback "
                  "(brand adapter, or the Home Assistant bridge)."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1]).resolve()
    repo = Path(__file__).resolve().parents[1]
    if repo in path.parents and path.name != "inventory-template.csv":
        print("Refusing: keep the filled-in inventory outside the repository (it is the homeowner's data).")
        return 1
    print(report(classify_file(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

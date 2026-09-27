"""Providers people commonly ask for, shown in the wizard and Settings even when
their module isn't installed — clearly marked "Provider module not installed",
never faked as connectable. Metadata only: no code, no connectivity claims.

`note` states the real integration route (see PROVIDERS.md for sources).
"""
from __future__ import annotations

KNOWN_PROVIDERS: list[dict[str, str]] = [
    {"id": "alexa", "name": "Amazon Alexa",
     "note": "Amazon has no public API to control the devices in an Alexa account. Share Matter devices from "
             "the Alexa app instead, or build a skill-based bridge as a provider module."},
    {"id": "home_assistant", "name": "Home Assistant",
     "note": "Connect to an existing Home Assistant over its WebSocket API (design: docs/HOME_ASSISTANT_FALLBACK.md)."},
    {"id": "google_home", "name": "Google Home",
     "note": "Google offers no general third-party control API for Google Home devices; share them over Matter."},
    {"id": "kasa", "name": "TP-Link Kasa / Tapo", "note": "Local network control of TP-Link plugs, switches and bulbs."},
    {"id": "govee", "name": "Govee", "note": "Govee's documented LAN API for lights (enable LAN control in the Govee app)."},
    {"id": "tuya", "name": "Tuya / Smart Life", "note": "Tuya cloud API or local protocol."},
    {"id": "lutron", "name": "Lutron Caséta / RA3", "note": "Lutron LEAP local API for switches and dimmers."},
    {"id": "android_tv", "name": "Android TV / Google TV", "note": "Android TV Remote protocol (power, keys, apps)."},
    {"id": "roku", "name": "Roku", "note": "Roku External Control Protocol on the local network."},
    {"id": "onvif", "name": "IP cameras (ONVIF / RTSP)", "note": "Standard camera discovery, snapshots and streams."},
    {"id": "ring", "name": "Ring", "note": "Ring Partner API (private app) for doorbells and cameras."},
]

"""The M2 owner flow through the real HTTP stack, against the fake Matter server:

Settings → connect Matter → Add device (setup code) → commission → discover →
UNREVIEWED → owner claims → assigns room → Home Hub → control → read-back →
audit. No mock of the adapter, the service or the core — only the Matter
server on the other end of the WebSocket is simulated."""
import json

from conftest import Hub
from fake_matter_server import VALID_CODE, FakeMatterServer


def open_server(hub: Hub) -> FakeMatterServer:
    srv = FakeMatterServer(["color_light"])
    hub.run(srv.__aenter__)                       # lives in the app's event loop
    return srv


def test_add_a_matter_device_and_control_it(hub):
    hub.bootstrap()
    srv = open_server(hub)
    try:
        body = hub.client.get("/api/providers").json()
        matter = next(p for p in body["available"] if p["id"] == "matter")
        assert matter["connected"] is False and matter["supports_pairing"] is True
        assert matter["settings"][0]["name"] == "server_url"

        r = hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"server_url": srv.url}})
        assert r.status_code == 200 and r.json()["status"] == "OK", r.text
        assert hub.write("POST", "/api/providers", json={"provider_id": "matter"}).status_code == 409

        lamp = hub.device("Living Room Lamp")                     # node already on the Matter server
        assert lamp["review"] == "UNREVIEWED" and lamp["technical"]["connections"][0]["provider"] == "matter"

        bad = hub.write("POST", "/api/providers/matter/pair", json={"code": "1111-111-1111"})
        assert bad.status_code == 422 and "fresh code" in bad.json()["error"]   # the fake has no checksum check
        short = hub.write("POST", "/api/providers/matter/pair", json={"code": "12"})
        assert short.status_code == 422 and "11-digit" in short.json()["error"]

        r = hub.write("POST", "/api/providers/matter/pair", json={"code": "3497-011-2332"})
        assert r.status_code == 200, r.text
        added = r.json()["added"]
        assert [d["name"] for d in added] == ["Kitchen Switch 1", "Kitchen Switch 2"]
        assert {d["review"] for d in added} == {"UNREVIEWED"}                  # paired ≠ claimed
        sw = added[0]

        home = hub.client.get("/api/home").json()
        assert sw["id"] not in [d["id"] for d in home["devices"]]            # not on the Hub until claimed
        refused = hub.write("POST", f"/api/devices/{sw['id']}/actions", json={"capability": "POWER", "value": False})
        assert refused.status_code == 422

        assert hub.write("POST", f"/api/devices/{sw['id']}/review", json={"status": "MINE"}).status_code == 200
        assert hub.write("PATCH", f"/api/devices/{sw['id']}", json={"room_name": "Kitchen"}).status_code == 200
        home = hub.client.get("/api/home").json()
        shown = next(d for d in home["devices"] if d["id"] == sw["id"])
        assert shown["on"] is True and "Kitchen" in [r["name"] for r in home["rooms"]]
        assert [c["capability"] for c in shown["controls"]] == ["POWER"]     # only what the device serves

        r = hub.write("POST", f"/api/devices/{sw['id']}/actions", json={"capability": "POWER", "value": False})
        assert r.status_code == 200 and r.json()["outcome"] == "CONFIRMED"
        sent = srv.commands[-1]
        assert (sent["cluster_id"], sent["command_name"], sent["endpoint_id"]) == (6, "Off", 1)
        assert srv.nodes[max(srv.nodes)]["attributes"]["1/6/0"] is False        # the "physical" device changed

        audit = hub.client.get("/api/audit?limit=500").json()["entries"]
        acts = [e["action"] for e in audit]
        for want in ("PROVIDER_CONNECTED", "PAIRING_REQUESTED", "PAIRING_FAILED", "PAIRING_SUCCEEDED",
                     "DEVICE_DISCOVERED", "DEVICE_CLAIMED", "ROOM_CHANGED", "COMMAND_CONFIRMED"):
            assert want in acts, want
        done = next(e for e in audit if e["action"] == "COMMAND_CONFIRMED")
        assert (done["actor"], done["source"], done["target"], done["data"]["provider"]) == (
            "owner:alex", "api", sw["id"], "matter")
        dump = json.dumps(audit)
        assert VALID_CODE not in dump and "3497-011-2332" not in dump and "1111-111-1111" not in dump and "11111111111" not in dump   # the refused code, either form
    finally:
        hub.run(srv.__aexit__, None, None, None)


def test_matter_server_outage_shows_cant_reach(hub):
    hub.bootstrap()
    srv = open_server(hub)
    try:
        hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"server_url": srv.url}})
        lamp = hub.device("Living Room Lamp")
        hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})
    finally:
        hub.run(srv.__aexit__, None, None, None)
    hub.run(srv.drop)
    hub.run(hub.service.poll_provider, "matter")
    home = hub.client.get("/api/home").json()
    shown = next(d for d in home["devices"] if d["id"] == lamp["id"])
    assert shown["reachable"] is False and shown["on"] is None
    matter = next(s for s in home["system"]["services"] if s["name"] == "Matter")
    assert matter["label"] == "Can't reach"
    demo_ok = [d for d in home["devices"] if d["id"] != lamp["id"]]
    assert all(d["reachable"] for d in demo_ok)                              # only Matter's devices degrade


def test_health_recovers_as_soon_as_the_matter_server_is_back(hub):
    """Regression (VM run of 28a5a12): after the Matter server restarted, the
    client reconnected and commands were CONFIRMED, but the service kept its
    stale DOWN health until the next poll, so every Matter device showed as
    unreachable for up to two minutes."""
    from fake_matter_server import wait_until
    hub.bootstrap()
    srv = open_server(hub)
    try:
        hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"server_url": srv.url}})
        lamp = hub.device("Living Room Lamp")
        hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})
        adapter = hub.service.home.adapters["matter"]
        hub.run(srv.drop)
        hub.run(hub.service.poll_provider, "matter")                 # the poller sees the outage
        assert hub.client.get("/api/home").json()["status"]["level"] == "warn"
        assert hub.run(wait_until, lambda: adapter.client.connected, 5)  # …and the client reconnects

        r = hub.write("POST", f"/api/devices/{lamp['id']}/actions", json={"capability": "POWER", "value": False})
        assert r.json()["outcome"] == "CONFIRMED"
        home = hub.client.get("/api/home").json()
        shown = next(d for d in home["devices"] if d["id"] == lamp["id"])
        assert shown["reachable"] is True and shown["on"] is False       # not "Can't reach" after a confirmed command
        assert next(s for s in home["system"]["services"] if s["name"] == "Matter")["label"] == "Working"
        r = hub.write("POST", "/api/providers/matter/pair", json={"code": "3497-011-2332"})
        assert r.status_code == 200 and all(d["reachable"] for d in r.json()["added"])
    finally:
        hub.run(srv.__aexit__, None, None, None)


def test_matter_events_update_the_hub_without_waiting_for_a_poll(hub):
    """Regression (VM run of e977676): a device that went offline was only
    noticed at the next poll. Availability and attribute events from the Matter
    server now refresh the Hub straight away — including changes made with Alexa."""
    hub.bootstrap()
    srv = open_server(hub)
    try:
        hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"server_url": srv.url}})
        lamp = hub.device("Living Room Lamp")
        hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})

        def shown():
            return next(d for d in hub.client.get("/api/home").json()["devices"] if d["id"] == lamp["id"])

        def soon(pred, timeout=3.0):                                   # poll from the test thread
            import time
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if pred():
                    return True
                time.sleep(0.05)
            return pred()

        srv.nodes[5]["attributes"]["1/6/0"] = False                      # someone says "Alexa, lamp off"
        hub.run(srv._emit, "attribute_updated", [5, "1/6/0", False])
        assert soon(lambda: shown()["on"] is False)

        srv.nodes[5]["available"] = False                                # unplugged; server marks it unavailable
        hub.run(srv._emit, "node_updated", srv.nodes[5])
        assert soon(lambda: shown()["reachable"] is False)
        assert shown()["on"] is None                                     # can't say — never "off"
    finally:
        hub.run(srv.__aexit__, None, None, None)


def test_failed_command_to_an_unreachable_device_makes_state_unknown(hub):
    hub.bootstrap()
    srv = open_server(hub)
    try:
        hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"server_url": srv.url}})
        lamp = hub.device("Living Room Lamp")
        hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})
        srv.fail_commands = True                                         # the device doesn't answer
        r = hub.write("POST", f"/api/devices/{lamp['id']}/actions", json={"capability": "POWER", "value": False})
        assert r.json()["outcome"] == "FAILED"
        power = next(c for c in r.json()["device"]["controls"] if c["capability"] == "POWER")
        assert power["freshness"] == "UNKNOWN"                           # not the old "On"
    finally:
        hub.run(srv.__aexit__, None, None, None)


def test_provider_settings_are_validated(hub):
    hub.bootstrap()
    r = hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"server_url": "file:///etc"}})
    assert r.status_code == 400
    r = hub.write("POST", "/api/providers", json={"provider_id": "matter", "settings": {"password": "x"}})
    assert r.status_code == 400
    assert hub.write("POST", "/api/providers", json={"provider_id": "nope"}).status_code == 404
    assert hub.write("POST", "/api/providers/demo/pair", json={"code": "34970112332"}).status_code == 400

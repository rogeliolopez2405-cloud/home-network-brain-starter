"""End-to-end through the real stack:
HTTP → service → core → demo adapter → SQLite / vault / audit, and back."""
import json

import pytest

from conftest import OUTSIDE_CLIENT, PASSPHRASE, Hub


def light(h: Hub, name="Living Room Ceiling Lights"):
    return h.device(name)


def audit_actions(h: Hub, limit=500):
    return [e["action"] for e in h.client.get(f"/api/audit?limit={limit}").json()["entries"]]


# ── network policy ───────────────────────────────────────────────────────
def test_callers_outside_the_home_network_are_refused(settings):
    with Hub(settings, client=OUTSIDE_CLIENT) as h:
        assert h.client.get("/api/health").status_code == 403
        r = h.client.post("/api/auth/bootstrap", json={"setup_code": "x", "username": "a", "passphrase": "b"})
        assert r.status_code == 403


def test_forwarded_headers_are_not_trusted(settings):
    with Hub(settings, client=OUTSIDE_CLIENT) as h:
        assert h.client.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


# ── bootstrap ────────────────────────────────────────────────────────────
def test_setup_is_open_until_an_owner_exists(hub):
    s = hub.client.get("/api/session").json()
    assert s == {"setup_required": True, "house": "My Home", "authenticated": False}
    assert hub.settings.setup_code_file.exists()


def test_bootstrap_needs_the_setup_code(hub):
    r = hub.client.post("/api/auth/bootstrap", json={"setup_code": "AAAA-BBBB-CCCC", "username": "alex",
                                                     "passphrase": PASSPHRASE})
    assert r.status_code == 403
    assert "sudo /opt/hnb/bin/setup-code" in r.json()["error"]          # the same command the setup screen shows
    assert hub.client.get("/api/session").json()["setup_required"] is True


def test_bootstrap_creates_owner_closes_setup_and_signs_in(hub):
    r = hub.bootstrap()
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alex" and "token" not in json.dumps(body).replace("csrf_token", "")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert not hub.settings.setup_code_file.exists()
    s = hub.client.get("/api/session").json()
    assert s["authenticated"] and not s["setup_required"] and s["house"] == "Test House"
    again = hub.client.post("/api/auth/bootstrap", json={"setup_code": "anything", "username": "mallory",
                                                         "passphrase": PASSPHRASE})
    assert again.status_code == 409
    assert "OWNER_CREATED" in audit_actions(hub)


def test_weak_passphrase_is_refused(hub):
    assert hub.bootstrap(passphrase="short").status_code == 400


# ── sessions ─────────────────────────────────────────────────────────────
def test_reads_need_a_session(hub):
    for url in ("/api/home", "/api/devices", "/api/rooms", "/api/audit", "/api/providers"):
        assert hub.client.get(url).status_code == 401


def test_writes_need_the_csrf_token(hub):
    hub.bootstrap()
    lamp = hub.device("Lamp")
    r = hub.client.post(f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})
    assert r.status_code == 403
    r = hub.client.post(f"/api/devices/{lamp['id']}/review", json={"status": "MINE"},
                        headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    assert hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"}).status_code == 200


def test_cross_site_writes_are_refused(hub):
    hub.bootstrap()
    lamp = hub.device("Lamp")
    r = hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"},
                  headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_login_logout_and_uniform_failures(hub):
    hub.bootstrap()
    assert hub.write("POST", "/api/auth/logout").status_code == 200
    assert hub.client.get("/api/home").status_code == 401
    wrong_pass = hub.client.post("/api/auth/login", json={"username": "alex", "passphrase": "nope nope nope"})
    no_user = hub.client.post("/api/auth/login", json={"username": "nobody", "passphrase": "nope nope nope"})
    assert wrong_pass.status_code == no_user.status_code == 401
    assert wrong_pass.json() == no_user.json()
    assert hub.login().status_code == 200
    assert hub.client.get("/api/home").status_code == 200
    acts = audit_actions(hub)
    assert acts.count("LOGIN_FAILED") == 2 and "LOGIN_SUCCEEDED" in acts and "LOGOUT" in acts


def test_repeated_failures_are_rate_limited(hub):
    hub.bootstrap()
    hub.write("POST", "/api/auth/logout")
    for _ in range(5):
        hub.client.post("/api/auth/login", json={"username": "alex", "passphrase": "wrong wrong wrong"})
    r = hub.login()
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_sessions_expire_when_idle(hub, clock):
    hub.bootstrap()
    assert hub.client.get("/api/home").status_code == 200
    clock.advance(days=8)
    assert hub.client.get("/api/home").status_code == 401


# ── the M1 path: claim, control, persist, audit ─────────────────────────
def test_devices_arrive_unreviewed_and_hub_is_empty_until_claimed(hub):
    hub.bootstrap()
    devices = hub.client.get("/api/devices").json()["devices"]
    assert len(devices) == 14 and {d["review"] for d in devices} == {"UNREVIEWED"}
    home = hub.client.get("/api/home").json()
    assert home["devices"] == [] and home["status"]["needs_review"] == 14


def test_control_a_light_end_to_end(hub):
    hub.bootstrap()
    hub.claim_all()
    dev = light(hub)
    r = hub.write("POST", f"/api/devices/{dev['id']}/actions", json={"capability": "BRIGHTNESS", "value": 42})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "CONFIRMED" and body["value"] == 42
    home = hub.client.get("/api/home").json()
    shown = next(d for d in home["devices"] if d["id"] == dev["id"])
    b = next(c for c in shown["controls"] if c["capability"] == "BRIGHTNESS")
    assert (b["value"], b["freshness"]) == (42, "CONFIRMED")

    entries = hub.client.get(f"/api/audit?target={dev['id']}").json()["entries"]
    done, requested = entries[0], entries[1]
    assert requested["action"] == "COMMAND_REQUESTED"
    assert requested["requested"] == {"capability": "BRIGHTNESS", "value": 42}
    assert (done["action"], done["actor"], done["source"], done["result"]) == (
        "COMMAND_CONFIRMED", "owner:alex", "api", "CONFIRMED")
    assert done["at"] and done["target"] == dev["id"]


def test_unsupported_and_unclaimed_commands_are_rejected(hub):
    hub.bootstrap()
    sw = hub.device("Kitchen Ceiling")
    r = hub.write("POST", f"/api/devices/{sw['id']}/actions", json={"capability": "SWITCH", "value": True})
    assert r.status_code == 422 and r.json()["outcome"] == "REJECTED"
    hub.write("POST", f"/api/devices/{sw['id']}/review", json={"status": "MINE"})
    r = hub.write("POST", f"/api/devices/{sw['id']}/actions", json={"capability": "BRIGHTNESS", "value": 10})
    assert r.status_code == 422 and "can't do that" in r.json()["detail"]
    assert "COMMAND_REJECTED" in audit_actions(hub)


def test_review_rooms_and_favorites_are_audited(hub):
    hub.bootstrap()
    lamp = hub.device("Lamp")
    hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})
    printer = hub.device("Printer")
    hub.write("POST", f"/api/devices/{printer['id']}/review", json={"status": "IGNORED"})
    r = hub.write("PATCH", f"/api/devices/{lamp['id']}", json={"room_name": "Den", "favorite": True, "name": "Reading Lamp"})
    assert r.status_code == 200 and r.json()["name"] == "Reading Lamp" and r.json()["favorite"] is True
    acts = audit_actions(hub)
    for expected in ("DEVICE_CLAIMED", "DEVICE_IGNORED", "ROOM_CREATED", "ROOM_CHANGED", "FAVORITE_CHANGED",
                     "DEVICE_RENAMED", "DEVICE_DISCOVERED", "PROVIDER_CONNECTED", "SYSTEM_STARTED"):
        assert expected in acts, expected
    home = hub.client.get("/api/home").json()
    assert lamp["id"] in home["sections"]["favorites"]["device_ids"]
    assert "Den" in [r["name"] for r in home["rooms"]]


def test_state_and_audit_survive_a_restart(settings, clock):
    with Hub(settings, clock) as h:
        h.bootstrap()
        h.claim_all()
        dev = light(h)
        h.write("POST", f"/api/devices/{dev['id']}/actions", json={"capability": "BRIGHTNESS", "value": 37})
        cookies = dict(h.client.cookies)
        before = h.client.get("/api/audit?limit=500").json()["entries"]

    with Hub(settings, clock) as h2:
        h2.client.cookies.update(cookies)                       # the same browser, after the reboot
        assert h2.client.get("/api/session").json()["authenticated"] is True
        home = h2.client.get("/api/home").json()
        shown = next(d for d in home["devices"] if d["id"] == dev["id"])
        b = next(c for c in shown["controls"] if c["capability"] == "BRIGHTNESS")
        assert b["value"] == 37 and b["freshness"] == "CONFIRMED"   # read back from the (persistent) device
        after = h2.client.get("/api/audit?limit=500").json()["entries"]
        assert [e["id"] for e in after[-len(before):]] == [e["id"] for e in before]
        assert [e["action"] for e in after].count("SYSTEM_STARTED") == 2
        assert h2.login().status_code == 200


def test_provider_outage_shows_cant_reach_not_off_and_only_degrades_its_devices(hub):
    hub.bootstrap()
    hub.claim_all()
    dev = light(hub)
    hub.run(hub.service.set_provider_config, "demo", {"fail": "down"})
    hub.run(hub.service.poll_provider, "demo")
    home = hub.client.get("/api/home").json()
    shown = next(d for d in home["devices"] if d["id"] == dev["id"])
    assert shown["reachable"] is False and shown["on"] is None
    assert {c["freshness"] for c in shown["controls"]} == {"UNKNOWN"}
    assert home["status"]["level"] == "warn" and home["sections"]["lights"]["degraded"] is True
    assert home["system"]["services"][0]["label"] == "Can't reach"
    r = hub.write("POST", f"/api/devices/{dev['id']}/actions", json={"capability": "POWER", "value": False})
    assert r.json()["outcome"] == "FAILED"
    assert "PROVIDER_UNREACHABLE" in audit_actions(hub)

    hub.run(hub.service.set_provider_config, "demo", {})
    hub.run(hub.service.poll_provider, "demo")
    home = hub.client.get("/api/home").json()
    assert home["status"]["level"] == "ok"
    assert "PROVIDER_RECOVERED" in audit_actions(hub)


def test_outage_in_one_provider_leaves_others_untouched(settings, clock):
    from hnb.providers.demo.adapter import DemoAdapter
    from hnb.providers.registry import ADAPTERS

    class OtherDemo(DemoAdapter):
        provider_id = "other"
        display_name = "Other service"

    from fastapi.testclient import TestClient

    from hnb.api.app import create_app
    from hnb.service import HubService
    from hnb.store import db

    classes = {**ADAPTERS, "other": OtherDemo}
    setup = HubService(settings, adapter_classes=classes, auth_now=clock).open()
    with db.tx(setup.conn):                                  # a second provider, connected the normal way
        setup.repo.append_events([setup._connect_provider("other")])
    setup.conn.close()
    svc = HubService(settings, adapter_classes=classes, auth_now=clock)   # opened by the app, on its loop
    with TestClient(create_app(settings, svc, poll=False), client=("127.0.0.1", 1)) as c:
        code = settings.setup_code_file.read_text().strip()
        csrf = c.post("/api/auth/bootstrap", json={"setup_code": code, "username": "alex",
                                                   "passphrase": PASSPHRASE}).json()["csrf_token"]
        for d in c.get("/api/devices").json()["devices"]:
            c.post(f"/api/devices/{d['id']}/review", json={"status": "MINE"}, headers={"X-CSRF-Token": csrf})
        c.portal.call(svc.set_provider_config, "other", {"fail": "down"})
        c.portal.call(svc.poll_provider, "other")
        home = c.get("/api/home").json()
        by_provider = {}
        for d in c.get("/api/devices").json()["devices"]:
            by_provider.setdefault(d["technical"]["connections"][0]["provider"], []).append(d["id"])
        reach = {d["id"]: d["reachable"] for d in home["devices"]}
        assert all(reach[i] for i in by_provider["demo"])
        assert not any(reach[i] for i in by_provider["other"])


def test_database_connection_refuses_other_threads(hub):
    """Regression: the auth dependency was a plain `def`, so FastAPI ran it in a
    worker thread and parallel browser requests shared one sqlite3 connection
    across threads (corrupted reads, HTTP 500). The connection must stay
    strict so any such path fails loudly in tests, not quietly in a home."""
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        hub.service.conn.execute("SELECT 1")          # this test thread is not the app's loop thread
    assert hub.run(lambda: hub.service.conn.execute("SELECT 1").fetchone()[0]) == 1


# ── health ───────────────────────────────────────────────────────────────
def test_health_is_minimal_anonymously_and_detailed_for_the_owner(hub):
    anon = hub.client.get("/api/health").json()
    assert set(anon) == {"status", "version", "setup_required"} and anon["status"] == "ok"
    hub.bootstrap()
    full = hub.client.get("/api/health").json()
    comp = full["components"]
    assert comp["database"]["journal_mode"] == "wal" and comp["database"]["foreign_keys"] is True
    assert comp["vault"]["status"] == "ok" and comp["vault"]["entries"] == 1
    assert comp["backup"]["status"] == "never" and full["status"] == "degraded"
    assert {"api", "database", "vault", "providers", "backup", "storage", "poller"} <= set(comp)
    token = hub.run(hub.service.vault.get, "demo", "access_token")
    text = json.dumps(full) + json.dumps(hub.client.get("/api/providers").json())
    assert token not in text and "access_token" in hub.client.get("/api/providers").text


def test_security_headers(hub):
    r = hub.client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert hub.client.get("/api/health").headers["cache-control"] == "no-store"
    for asset in ("/", "/app.js", "/app.css"):                 # upgrades must reach the browser
        assert hub.client.get(asset).headers["cache-control"] == "no-cache"


def test_unknown_routes_and_path_tricks_404(hub):
    assert hub.client.get("/api/nope").status_code == 404
    assert hub.client.get("/fonts/..%2Fapp.py").status_code == 404
    assert hub.client.get("/secret.txt").status_code == 404


@pytest.mark.parametrize("body", [{"capability": "POWER"}, {"capability": "BRIGHTNESS", "value": 101}])
def test_bad_values_never_reach_the_provider(hub, body):
    hub.bootstrap()
    hub.claim_all()
    dev = light(hub)
    r = hub.write("POST", f"/api/devices/{dev['id']}/actions", json=body)
    assert r.status_code == 422 and r.json()["outcome"] == "REJECTED"

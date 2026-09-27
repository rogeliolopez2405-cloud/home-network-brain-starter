"""The developer-facing API surfaces: capabilities, system, OpenAPI, device
detail/history, rooms, discovery, backups, setup completion, provider catalog,
owner-entered credentials, the demo's simulated outage, and plugin loading."""
import json
import textwrap

from conftest import Hub, PASSPHRASE


def test_capability_vocabulary_is_published(hub):
    hub.bootstrap()
    caps = {c["type"]: c for c in hub.client.get("/api/capabilities").json()["capabilities"]}
    assert len(caps) == 12 and caps["LOCK"]["sensitivity"] == "HIGH" and caps["CAMERA_LIVE"]["writable"] is False
    assert "0–100" in caps["BRIGHTNESS"]["value"]


def test_openapi_is_served_to_owners_only_and_covers_the_api(hub):
    assert hub.client.get("/api/openapi.json").status_code == 401
    hub.bootstrap()
    spec = hub.client.get("/api/openapi.json").json()
    paths = spec["paths"]
    for p in ("/api/auth/login", "/api/home", "/api/rooms/{room_id}", "/api/devices/{device_id}/actions",
              "/api/providers/{provider_id}/discover", "/api/providers/{provider_id}/pair", "/api/audit",
              "/api/backups", "/api/system", "/api/capabilities", "/api/setup/complete"):
        assert p in paths, p
    tags = {t["name"] for t in spec["tags"]}
    assert {"auth", "home", "rooms", "devices", "actions", "providers", "discovery", "audit", "backups", "system"} <= tags
    assert all(op.get("summary") for ops in paths.values() for op in ops.values())


def test_system_info_is_truthful(hub):
    hub.bootstrap()
    info = hub.client.get("/api/system").json()
    assert info["providers"]["installed"] == ["demo", "matter"] and info["providers"]["connected"] == ["demo"]
    assert info["devices"]["UNREVIEWED"] == 14 and info["setup_complete"] is False
    assert "hostname" in info["network"] and "ipv6" in info["network"]
    assert info["provider_load_errors"] == []


def test_device_detail_and_history(hub):
    hub.bootstrap()
    lamp = hub.device("Lamp")
    assert hub.client.get("/api/devices/nope").status_code == 404
    hub.write("POST", f"/api/devices/{lamp['id']}/review", json={"status": "MINE"})
    hub.write("POST", f"/api/devices/{lamp['id']}/actions", json={"capability": "POWER", "value": True})
    d = hub.client.get(f"/api/devices/{lamp['id']}").json()
    assert d["review"] == "MINE" and d["room"] == "Living Room"
    assert {c["type"] for c in d["capabilities"]} == {"POWER", "BRIGHTNESS", "COLOR"}
    hist = [e["action"] for e in hub.client.get(f"/api/devices/{lamp['id']}/history").json()["entries"]]
    assert hist[:2] == ["COMMAND_CONFIRMED", "COMMAND_REQUESTED"] and "DEVICE_CLAIMED" in hist


def test_rooms_rename_and_delete(hub):
    hub.bootstrap()
    hub.claim_all()
    rooms = {r["name"]: r for r in hub.client.get("/api/rooms").json()["rooms"]}
    r = hub.write("PATCH", f"/api/rooms/{rooms['Kitchen']['id']}", json={"name": "Cocina"})
    assert r.status_code == 200 and r.json()["name"] == "Cocina"
    assert hub.write("PATCH", f"/api/rooms/{rooms['Bedroom']['id']}", json={"name": "cocina"}).status_code == 409
    assert hub.write("DELETE", f"/api/rooms/{rooms['Bedroom']['id']}").status_code == 200
    names = [r["name"] for r in hub.client.get("/api/rooms").json()["rooms"]]
    assert "Bedroom" not in names and "Cocina" in names
    bedroom_lights = hub.device("Bedroom Lights")
    assert bedroom_lights["room"] is None and bedroom_lights["review"] == "MINE"    # still a working device
    acts = [e["action"] for e in hub.client.get("/api/audit?limit=200").json()["entries"]]
    assert "ROOM_RENAMED" in acts and "ROOM_DELETED" in acts


def test_discover_now(hub):
    hub.bootstrap()
    r = hub.write("POST", "/api/providers/demo/discover")
    assert r.status_code == 200 and r.json()["found"] == 14 and r.json()["new"] == []   # nothing duplicated
    assert hub.write("POST", "/api/providers/matter/discover").status_code == 404        # not connected


def test_backups_create_list_verify(hub):
    hub.bootstrap()
    r = hub.write("POST", "/api/backups")
    assert r.status_code == 200 and r.json()["verified"] is True
    listing = hub.client.get("/api/backups").json()
    assert [b["file"] for b in listing["backups"]] == [r.json()["file"]] and listing["status"]["status"] == "ok"
    v = hub.write("POST", f"/api/backups/{r.json()['file']}/verify")
    assert v.status_code == 200 and v.json()["checks"]["integrity_check"] == "ok"
    assert hub.write("POST", "/api/backups/..%2Fhnb.db/verify").status_code in (400, 404)
    assert "BACKUP_CREATED" in [e["action"] for e in hub.client.get("/api/audit").json()["entries"]]


def test_setup_completion_is_remembered(hub):
    hub.bootstrap()
    assert hub.client.get("/api/session").json()["setup_complete"] is False
    assert hub.write("POST", "/api/setup/complete").status_code == 200
    assert hub.client.get("/api/session").json()["setup_complete"] is True
    assert "SETUP_COMPLETED" in [e["action"] for e in hub.client.get("/api/audit").json()["entries"]]


def test_catalog_shows_uninstalled_providers_without_faking_them(hub):
    hub.bootstrap()
    avail = {p["id"]: p for p in hub.client.get("/api/providers").json()["available"]}
    assert avail["demo"]["installed"] and avail["matter"]["installed"]
    for pid in ("alexa", "home_assistant", "kasa", "govee", "tuya", "google_home", "android_tv", "onvif"):
        assert avail[pid]["installed"] is False and avail[pid]["status_text"] == "Provider module not installed"
    r = hub.write("POST", "/api/providers", json={"provider_id": "kasa"})
    assert r.status_code == 409 and "not installed" in r.json()["error"]


def test_demo_outage_through_real_settings(hub):
    hub.bootstrap()
    hub.claim_all()
    bad = hub.write("PATCH", "/api/providers/demo", json={"settings": {"simulate": "meteor"}})
    assert bad.status_code == 400
    r = hub.write("PATCH", "/api/providers/demo", json={"settings": {"simulate": "outage"}})
    assert r.status_code == 200 and r.json()["label"] == "Can't reach"
    home = hub.client.get("/api/home").json()
    assert home["status"]["level"] == "warn" and all(d["on"] is None for d in home["devices"] if d["section"] == "lights")
    r = hub.write("PATCH", "/api/providers/demo", json={"settings": {"simulate": "none"}})
    assert r.json()["label"] == "Working" and hub.client.get("/api/home").json()["status"]["level"] == "ok"


def test_owner_entered_credentials_go_to_the_vault_and_never_come_back(settings, clock):
    from contextlib import contextmanager  # noqa: F401
    from hnb.providers.demo.adapter import DemoAdapter
    from hnb.providers.registry import BUILTIN
    from hnb.providers.sdk import ConfigField, HealthStatus, ProviderHealth
    from hnb.service import HubService
    from hnb.api.app import create_app
    from fastapi.testclient import TestClient

    class KeyedDemo(DemoAdapter):
        provider_id = "keyed"
        display_name = "Keyed demo"
        description = "Demo that needs an API key."
        required_secrets = ("api_key",)
        secret_fields = (ConfigField("api_key", "API key", ""),)

        @classmethod
        def initial_secrets(cls):
            return {}

        async def health(self):
            return ProviderHealth(HealthStatus.OK if self._secrets.get("api_key") == "k-" + "7" * 20
                                  else HealthStatus.AUTH_REQUIRED)

    key = "k-" + "7" * 20
    svc = HubService(settings, adapter_classes={**BUILTIN, "keyed": KeyedDemo}, auth_now=clock)
    with TestClient(create_app(settings, svc, poll=False), client=("127.0.0.1", 1)) as c:
        code = settings.setup_code_file.read_text().strip()
        csrf = c.post("/api/auth/bootstrap", json={"setup_code": code, "username": "alex",
                                                   "passphrase": PASSPHRASE}).json()["csrf_token"]
        h = {"X-CSRF-Token": csrf}
        assert c.post("/api/providers", json={"provider_id": "keyed"}, headers=h).status_code == 400   # key required
        r = c.post("/api/providers", json={"provider_id": "keyed", "secrets": {"api_key": key}}, headers=h)
        assert r.status_code == 200 and r.json()["status"] == "OK" and r.json()["credentials"] == ["api_key"]
        everything = json.dumps([c.get(u).json() for u in ("/api/providers", "/api/audit?limit=500", "/api/health",
                                                            "/api/system")])
        assert key not in everything
        r = c.patch("/api/providers/keyed", json={"secrets": {"api_key": "wrong-key-000000"}}, headers=h)
        assert r.json()["status"] == "AUTH_REQUIRED"
        r = c.patch("/api/providers/keyed", json={"secrets": {"api_key": ""}}, headers=h)   # blank keeps the stored one
        assert r.json()["status"] == "AUTH_REQUIRED"
        audit = c.get("/api/audit?limit=500").json()["entries"]
        changed = next(e for e in audit if e["action"] == "PROVIDER_SETTINGS_CHANGED")
        assert changed["data"]["credentials_replaced"] in (["api_key"], [])


def test_plugins_load_without_touching_the_core_and_bad_ones_are_reported(tmp_path, monkeypatch):
    from hnb.providers.registry import load_adapters
    good = tmp_path / "acme"
    good.mkdir()
    (good / "adapter.py").write_text(textwrap.dedent('''
        from hnb.providers.demo.adapter import DemoAdapter

        class AcmeAdapter(DemoAdapter):
            provider_id = "acme"
            display_name = "Acme"
            description = "Acme test plugin."

        ADAPTER = AcmeAdapter
    '''), encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "adapter.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    nameless = tmp_path / "nameless"
    nameless.mkdir()
    (nameless / "adapter.py").write_text("ADAPTER = object\n", encoding="utf-8")
    (tmp_path / "_skipped").mkdir()

    adapters, errors = load_adapters([tmp_path], entry_points=False)
    assert "acme" in adapters and {"demo", "matter"} <= set(adapters)
    reasons = {e.source: e.reason for e in errors}
    assert "boom" in reasons["plugins/broken"] and "ProviderAdapter" in reasons["plugins/nameless"]
    assert not any("_skipped" in s for s in reasons)


def test_demo_lock_needs_confirmation_and_thermostat_is_bounded(hub):
    hub.bootstrap()
    hub.claim_all()
    lock, thermo = hub.device("Front Door Lock"), hub.device("Thermostat")
    r = hub.write("POST", f"/api/devices/{lock['id']}/actions", json={"capability": "LOCK", "value": "UNLOCKED"})
    assert r.status_code == 422 and r.json()["outcome"] == "REJECTED"               # no confirmation step
    r = hub.write("POST", f"/api/devices/{lock['id']}/actions",
                  json={"capability": "LOCK", "value": "UNLOCKED", "confirm": True})
    assert r.status_code == 200 and r.json()["outcome"] == "CONFIRMED"
    bad = hub.write("POST", f"/api/devices/{thermo['id']}/actions",
                    json={"capability": "THERMOSTAT", "value": {"mode": "HEAT", "setpoint_c": 35}})
    assert bad.status_code == 422                                                    # outside this device's 10–30 °C
    ok = hub.write("POST", f"/api/devices/{thermo['id']}/actions",
                   json={"capability": "THERMOSTAT", "value": {"mode": "COOL", "setpoint_c": 22.5}})
    assert ok.json()["outcome"] == "CONFIRMED"
    home = hub.client.get("/api/home").json()
    assert {lock["id"], thermo["id"]} <= set(home["sections"]["more"]["device_ids"])     # with the claimed Printer
    audit = hub.client.get("/api/audit?limit=50").json()["entries"]
    assert any(e["action"] == "COMMAND_CONFIRMED" and e["requested"]["capability"] == "LOCK" and e["actor"] == "owner:alex"
               for e in audit)


def test_core_test_hubs_ignore_local_plugins_but_the_real_hub_loads_them(tmp_path, monkeypatch, settings, clock):
    """A developer's module in a plugin folder must not change core test results
    (the fresh-developer test found it did), while the real hub still loads it."""
    from hnb.service import HubService
    plugin = tmp_path / "extra" / "acme_local"
    plugin.mkdir(parents=True)
    (plugin / "adapter.py").write_text(textwrap.dedent('''
        from hnb.providers.demo.adapter import DemoAdapter

        class LocalAdapter(DemoAdapter):
            provider_id = "acme_local"
            display_name = "Local"
            description = "A developer's module."

        ADAPTER = LocalAdapter
    '''), encoding="utf-8")
    monkeypatch.setenv("HNB_PLUGIN_DIRS", str(tmp_path / "extra"))
    assert "acme_local" in HubService(settings).adapter_classes          # the real hub finds it
    with Hub(settings, clock) as h:                                      # the test hub doesn't
        h.bootstrap()
        assert h.client.get("/api/system").json()["providers"]["installed"] == ["demo", "matter"]


def test_provider_trouble_reaches_the_log_not_only_the_audit(hub, caplog):
    """The third fresh-developer run found an outage left the log silent."""
    import logging
    hub.bootstrap()
    with caplog.at_level(logging.INFO, logger="hnb.service"):
        hub.write("PATCH", "/api/providers/demo", json={"settings": {"simulate": "outage"}})
        hub.write("PATCH", "/api/providers/demo", json={"settings": {"simulate": "expired sign-in"}})
        hub.write("PATCH", "/api/providers/demo", json={"settings": {"simulate": "none"}})
    lines = [r.getMessage() for r in caplog.records if r.name == "hnb.service"]
    assert any(m.startswith("Provider demo is DOWN") for m in lines), lines
    assert any(m.startswith("Provider demo is AUTH_REQUIRED") for m in lines), lines
    assert "Provider demo recovered" in lines, lines

"""M1 acceptance checks against a real installed hub (Debian 13 VM).

Runs on the CI runner, talks to the VM over HTTP (QEMU port forward) and SSH.
Standard library only. Each phase appends results to state.json so the
post-reboot phase can compare against what was true before the reboot.

    python3 acceptance.py install  --base URL --ssh "ssh ... debian@127.0.0.1"
    python3 acceptance.py reboot   --base URL --ssh "..."     (after the host reboot)
    python3 acceptance.py browser  --base URL --ssh "..."     (needs Playwright + Chrome on the runner)
    python3 acceptance.py outage   --base URL --ssh "..."
    python3 acceptance.py restore  --base URL --ssh "..."
    python3 acceptance.py upgrade  --base URL --ssh "..."     (after re-running install.sh)
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE = Path("state.json")
USER, PASS = "owner", "acceptance passphrase one"
LIGHT = "Living Room Ceiling Lights"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' — ' + detail if detail else ''}", flush=True)
    return ok


class Client:
    def __init__(self, base: str, jar_file: Path | None = None):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.LWPCookieJar(str(jar_file) if jar_file else None)
        if jar_file and jar_file.exists():
            self.jar.load(ignore_discard=True, ignore_expires=True)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None

    def req(self, method: str, path: str, body=None, csrf: bool = True):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Accept", "application/json")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if csrf and self.csrf and method != "GET":
            r.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors="replace")

    def save(self, path: Path) -> None:
        self.jar.filename = str(path)
        self.jar.save(ignore_discard=True, ignore_expires=True)


def ssh(cmd: str, base_ssh: str, check_rc: bool = True) -> str:
    p = subprocess.run(shlex.split(base_ssh) + [cmd], capture_output=True, text=True, timeout=900)
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"ssh `{cmd}` failed ({p.returncode}): {p.stderr.strip()[-400:]}")
    return p.stdout.strip()


def wait_health(c: Client, seconds: int = 300) -> dict | None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            status, body = c.req("GET", "/api/health")
            if status == 200 and isinstance(body, dict) and body.get("status") in ("ok", "degraded"):
                return body
        except Exception:
            pass
        time.sleep(5)
    return None


def device(c: Client, name: str) -> dict:
    _, body = c.req("GET", "/api/devices")
    return next(d for d in body["devices"] if d["name"] == name)


def home_device(c: Client, dev_id: str) -> dict | None:
    _, body = c.req("GET", "/api/home")
    return next((d for d in body["devices"] if d["id"] == dev_id), None)


def control(d: dict, cap: str) -> dict | None:
    return next((x for x in d["controls"] if x["capability"] == cap), None)


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2))


# ── phases ────────────────────────────────────────────────────────────────
def phase_install(a) -> None:
    c = Client(a.base)
    h = wait_health(c)
    check("1-2 installed hub answers /api/health", h is not None, json.dumps(h))
    check("anonymous health exposes no internals", h and set(h) == {"status", "version", "setup_required"})
    check("setup is open on a fresh install", h and h.get("setup_required") is True)

    code = ssh("sudo /opt/hnb/bin/setup-code", a.ssh)
    check("setup code readable only via sudo on the hub", len(code.replace("-", "")) == 12)
    install_out = Path("install.log").read_text(errors="replace") if Path("install.log").exists() else ""
    check("installer output never contains the setup code", bool(install_out) and code not in install_out)
    urls = [l.strip() for l in install_out.splitlines() if l.strip().startswith("http://")]
    bridge = [u for u in urls if u.split("//")[1].startswith(tuple(f"172.{n}." for n in range(17, 32)))]
    check("installer prints the LAN address, not Docker bridges", bool(urls) and not bridge, " ".join(urls))
    s, _ = c.req("POST", "/api/auth/bootstrap", {"setup_code": "WRONG-CODE-0000", "username": USER, "passphrase": PASS})
    check("wrong setup code refused", s == 403, str(s))
    s, body = c.req("POST", "/api/auth/bootstrap", {"setup_code": code, "username": USER, "passphrase": PASS,
                                                    "house_name": "Acceptance House"})
    check("3 first owner created", s == 200, str(s))
    c.csrf = body["csrf_token"]
    s, _ = c.req("POST", "/api/auth/bootstrap", {"setup_code": code, "username": "second", "passphrase": PASS})
    check("bootstrap closed permanently", s == 409, str(s))
    check("setup code deleted after use", "Setup is closed" in ssh("sudo /opt/hnb/bin/setup-code 2>&1 || true", a.ssh))

    c2 = Client(a.base)
    s, body = c2.req("POST", "/api/auth/login", {"username": USER, "passphrase": PASS})
    check("4 sign in", s == 200, str(s))
    s, _ = Client(a.base).req("GET", "/api/home")
    check("home requires a session", s == 401, str(s))

    s, body = c.req("GET", "/api/devices")
    devs = body["devices"]
    check("14 demo devices discovered, all UNREVIEWED", len(devs) == 14 and {d["review"] for d in devs} == {"UNREVIEWED"},
          f"{len(devs)} devices")
    for d in devs:
        c.req("POST", f"/api/devices/{d['id']}/review", {"status": "MINE"})
    s, home = c.req("GET", "/api/home")
    check("5 Home Hub shows claimed devices", s == 200 and home["status"]["devices"] == 14, str(home["status"]))

    from concurrent.futures import ThreadPoolExecutor
    paths = ["/api/home", "/api/devices", "/api/rooms", "/api/health", "/api/providers", "/api/audit?limit=20"] * 7
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda p: c.req("GET", p)[0], paths[:40]))
    check("40 parallel requests all succeed (no shared-connection corruption)", set(codes) == {200},
          str(sorted(set(codes))))

    light = device(c, LIGHT)
    s, _ = c.req("POST", f"/api/devices/{light['id']}/actions", {"capability": "BRIGHTNESS", "value": 42}, csrf=False)
    check("write without CSRF token refused", s == 403, str(s))
    s1, r1 = c.req("POST", f"/api/devices/{light['id']}/actions", {"capability": "POWER", "value": True})
    s2, r2 = c.req("POST", f"/api/devices/{light['id']}/actions", {"capability": "BRIGHTNESS", "value": 42})
    check("7 control a demo light (CONFIRMED)", s1 == s2 == 200 and r1["outcome"] == r2["outcome"] == "CONFIRMED",
          f"{r1.get('outcome')}/{r2.get('outcome')}")
    hd = home_device(c, light["id"])
    b = control(hd, "BRIGHTNESS")
    check("Hub refresh shows the new state", b["value"] == 42 and b["freshness"] == "CONFIRMED", json.dumps(b))

    _, audit = c.req("GET", f"/api/audit?target={light['id']}")
    acts = [(e["action"], e["actor"], e["source"]) for e in audit["entries"]]
    check("9 audit: COMMAND_REQUESTED + COMMAND_CONFIRMED by the owner via api",
          ("COMMAND_CONFIRMED", f"owner:{USER}", "api") in acts and ("COMMAND_REQUESTED", f"owner:{USER}", "api") in acts)

    db = ssh("sudo ls -l /var/lib/hnb/data/", a.ssh)
    check("8 SQLite database persisted on the host (WAL)", "hnb.db" in db and "hnb.db-wal" in db)

    out = ssh("sudo /opt/hnb/bin/backup", a.ssh)
    bk = json.loads(out[out.index("{"):])
    check("12 verified backup created", bk.get("verified") is True, bk.get("file", ""))
    members = ssh(f"sudo tar -tzf /var/lib/hnb/backups/{bk['file']}", a.ssh).split()
    check("backup excludes the vault key", "hnb.db" in members and not any("key" in m for m in members), " ".join(members))

    insp = json.loads(ssh("sudo docker inspect $(sudo docker compose --env-file /etc/hnb/hnb.env "
                          "-f /opt/hnb/deploy/compose.yaml ps -q hub)", a.ssh))[0]
    hc = insp["HostConfig"]
    user = insp["Config"]["User"]
    check("container runs as non-root", user and not user.startswith("0:") and user != "root", user)
    check("read-only root filesystem", hc["ReadonlyRootfs"] is True)
    check("all capabilities dropped", hc.get("CapDrop") == ["ALL"], str(hc.get("CapDrop")))
    check("no-new-privileges", any("no-new-privileges" in o for o in hc.get("SecurityOpt") or []))
    check("memory and PID limits", hc["Memory"] == 512 * 1024 * 1024 and hc["PidsLimit"] == 128,
          f"{hc['Memory']} / {hc['PidsLimit']}")
    check("restart policy unless-stopped", hc["RestartPolicy"]["Name"] == "unless-stopped")
    check("hub on host networking (to reach the loopback-only Matter API)", hc["NetworkMode"] == "host")
    perms = ssh("sudo stat -c '%a %U %s' /var/lib/hnb/secrets/vault.key /etc/hnb/hnb.env", a.ssh).splitlines()
    check("vault key 0400 hnb, 32 bytes", perms[0] == "400 hnb 32", perms[0])
    check("config 0640", perms[1].startswith("640 root"), perms[1])
    probe = ("import glob; k=open('/run/secrets/vault_key','rb').read(); "
             "print(any(k in open(p,'rb').read() for p in glob.glob('/var/lib/hnb/data/hnb.db*')))")
    leaked = ssh("sudo docker compose --env-file /etc/hnb/hnb.env -f /opt/hnb/deploy/compose.yaml "
                 f"exec -T hub python -c {shlex.quote(probe)}", a.ssh)
    check("vault key bytes appear nowhere in the database files", leaked == "False", leaked)
    s, full = c.req("GET", "/api/health")
    comp = full["components"]
    check("health: api, db, providers, backup, storage, version", all(k in comp for k in
          ("api", "database", "providers", "backup", "storage")) and full["version"], full["status"])
    check("health shows backup ok and vault ok", comp["backup"]["status"] == "ok" and comp["vault"]["status"] == "ok")

    c.save(Path("cookies.txt"))
    st = {"light_id": light["id"], "csrf": c.csrf, "audit_ids": [e["id"] for e in c.req("GET", "/api/audit?limit=500")[1]["entries"]],
          "backup": bk["file"], "boot_id": ssh("cat /proc/sys/kernel/random/boot_id", a.ssh)}
    save_state(st)


def phase_reboot(a) -> None:
    st = load_state()
    boot = ssh("cat /proc/sys/kernel/random/boot_id", a.ssh)
    check("10 host really rebooted (new boot id)", boot != st["boot_id"])
    c = Client(a.base, Path("cookies.txt"))
    c.csrf = st["csrf"]
    h = wait_health(c)
    check("11 hub came back healthy by itself", h is not None)
    check("systemd service active and enabled",
          ssh("systemctl is-active hnb.service; systemctl is-enabled hnb.service", a.ssh).split() == ["active", "enabled"])
    s, sess = c.req("GET", "/api/session")
    check("existing session survived the reboot", s == 200 and sess.get("authenticated") is True)
    s, _ = Client(a.base).req("POST", "/api/auth/login", {"username": USER, "passphrase": PASS})
    check("auth works after reboot", s == 200, str(s))
    time.sleep(3)
    hd = home_device(c, st["light_id"])
    b, p = control(hd, "BRIGHTNESS"), control(hd, "POWER")
    check("state preserved (brightness 42, power on)", b["value"] == 42 and p["value"] is True, f"{b} {p}")
    _, audit = c.req("GET", "/api/audit?limit=500")
    ids = [e["id"] for e in audit["entries"]]
    check("audit preserved (every pre-reboot entry still there)", set(st["audit_ids"]) <= set(ids),
          f"{len(st['audit_ids'])} before, {len(ids)} now")
    check("audit records the restart", [e["action"] for e in audit["entries"]].count("SYSTEM_STARTED") >= 2)
    _, full = c.req("GET", "/api/health")
    check("health after reboot: database ok, provider ok", full["components"]["database"]["status"] == "ok"
          and full["components"]["providers"][0]["status"] == "OK")


def phase_browser(a) -> None:
    """The Home Hub in Chrome on the runner, against the hub in the VM — as a phone
    on the home network would use it. Needs Playwright on the runner (the workflow
    installs it); everything else in this file is standard library."""
    from playwright.sync_api import sync_playwright

    api = Client(a.base)
    s, body = api.req("POST", "/api/auth/login", {"username": USER, "passphrase": PASS})
    api.csrf = body.get("csrf_token") if s == 200 else None
    lamp = device(api, "Lamp")
    errors: list[str] = []
    failed: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for label, (w, h) in (("phone", (390, 844)), ("desktop", (1440, 900))):
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            page.on("console", lambda m: m.type == "error" and "status of 403" not in m.text and errors.append(m.text))
            page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and r.status != 403
                    and failed.append(f"{r.status} {r.request.method} {r.url}"))
            page.goto(a.base + "/")
            page.fill("#li-user", USER)
            page.fill("#li-pass", PASS)
            page.click("#login-form button[type=submit]")
            page.wait_for_selector(".hero", timeout=30000)
            check(f"browser {label}: sign-in through the page reaches the Home Hub", page.locator(".hero").count() == 1)
            names = page.locator(".tile-name").all_inner_texts()
            sections = page.locator("section.section h2").all_inner_texts()
            check(f"browser {label}: demo devices render in their sections",
                  "Lamp" in names and "Front Door Lock" in names and any("Lights" in t for t in sections)
                  and any("Locks" in t for t in sections), f"{len(names)} cards; {sections}")
            check(f"browser {label}: no sideways scrolling",
                  page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            if label == "phone":
                page.screenshot(path="hub-phone.png", full_page=True)
                card = page.locator(".tile").filter(has=page.locator(".tile-name", has_text="Lamp")).first
                for _ in range(2):                                   # off → on (or on → off → on): leaves it as found
                    before = card.locator("[data-act=power]").get_attribute("aria-checked")
                    card.locator("[data-act=power]").click()
                    page.wait_for_function(
                        "([sel, b]) => { const t = [...document.querySelectorAll('.tile')].find(x => x.querySelector('.tile-name')"
                        " && x.querySelector('.tile-name').textContent === 'Lamp'); return t && t.querySelector(sel)"
                        ".getAttribute('aria-checked') !== b; }", arg=["[data-act=power]", before], timeout=20000)
                    want = before != "true"
                    power = control(home_device(api, lamp["id"]), "POWER")
                    _, audit = api.req("GET", "/api/audit?limit=5")
                    done = next((e for e in audit["entries"] if e["action"].startswith("COMMAND_")), {})
                    check(f"browser tap → API → device read-back → audit (Lamp {'on' if want else 'off'})",
                          power["value"] is want and power["freshness"] == "CONFIRMED" and done.get("action") == "COMMAND_CONFIRMED"
                          and done.get("actor") == f"owner:{USER}", f"{power} {done.get('action')}")
                forged = page.evaluate("""id => fetch(`/api/devices/${id}/actions`, {method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({capability: 'POWER', value: true})})
                    .then(r => r.status)""", lamp["id"])
                check("browser: a write from the page without the CSRF token is refused", forged == 403, str(forged))
            ctx.close()
        browser.close()
    check("browser: no console errors", not errors, "; ".join(errors[:3]))
    check("browser: no failed API calls", not failed, "; ".join(failed[:3]))


def phase_outage(a) -> None:
    st = load_state()
    c = Client(a.base, Path("cookies.txt"))
    c.csrf = st["csrf"]
    ssh("sudo /opt/hnb/bin/hnb provider-config demo '{\"fail\": \"down\"}'", a.ssh)
    seen = None
    for _ in range(40):
        hd = home_device(c, st["light_id"])
        if hd and hd["reachable"] is False:
            seen = hd
            break
        time.sleep(3)
    check("provider outage detected by the poller", seen is not None)
    if seen:
        check("outage shows CAN'T REACH / UNKNOWN, not OFF",
              seen["on"] is None and {x["freshness"] for x in seen["controls"]} == {"UNKNOWN"})
        _, home = c.req("GET", "/api/home")
        check("status says devices can't be reached", home["status"]["level"] == "warn" and
              home["system"]["services"][0]["label"] == "Can't reach")
    ssh("sudo /opt/hnb/bin/hnb provider-config demo '{}'", a.ssh)
    ok = False
    for _ in range(60):
        hd = home_device(c, st["light_id"])
        if hd and hd["reachable"]:
            ok = True
            break
        time.sleep(3)
    check("provider recovers and state returns", ok)
    _, audit = c.req("GET", "/api/audit?limit=200")
    acts = [e["action"] for e in audit["entries"]]
    check("outage and recovery audited", "PROVIDER_UNREACHABLE" in acts and "PROVIDER_RECOVERED" in acts)


def phase_restore(a) -> None:
    st = load_state()
    c = Client(a.base, Path("cookies.txt"))
    c.csrf = st["csrf"]
    c.req("POST", f"/api/devices/{st['light_id']}/actions", {"capability": "BRIGHTNESS", "value": 77})
    out = ssh(f"sudo /opt/hnb/bin/restore {st['backup']}", a.ssh)
    check("restore completes and hub is healthy", "hub is healthy" in out, out.splitlines()[-1] if out else "")
    c2 = Client(a.base)
    s, body = c2.req("POST", "/api/auth/login", {"username": USER, "passphrase": PASS})
    c2.csrf = body.get("csrf_token") if isinstance(body, dict) else None
    time.sleep(3)
    b = control(home_device(c2, st["light_id"]), "BRIGHTNESS")
    check("restored state is the backed-up state (42, not 77)", b["value"] == 42, json.dumps(b))
    _, audit = c2.req("GET", "/api/audit?limit=100")
    check("restore recorded in audit", "BACKUP_RESTORED" in [e["action"] for e in audit["entries"]])
    c2.save(Path("cookies.txt"))
    st["csrf"] = c2.csrf
    save_state(st)


def phase_upgrade(a) -> None:
    st = load_state()
    c = Client(a.base, Path("cookies.txt"))
    h = wait_health(c)
    check("re-running install.sh leaves a healthy hub", h is not None)
    s, sess = c.req("GET", "/api/session")
    check("owner and session kept across re-install", sess.get("authenticated") is True)
    b = control(home_device(c, st["light_id"]), "BRIGHTNESS")
    check("state kept across re-install", b["value"] == 42)
    backups = ssh("sudo ls /var/lib/hnb/backups", a.ssh)
    check("re-install took a pre-upgrade backup", backups.count("hnb-backup-") >= 2)


MATTER_TEST_CODE = "34970112332"      # well-known Matter test code of the virtual light
COMPOSE = "sudo docker compose --env-file /etc/hnb/hnb.env -f /opt/hnb/deploy/compose.yaml"


def phase_matter(a) -> None:
    """Real Matter protocol, virtual device: the whole M2 path on a clean host."""
    st = load_state()
    c = Client(a.base)
    s, body = c.req("POST", "/api/auth/login", {"username": USER, "passphrase": PASS})
    c.csrf = body.get("csrf_token") if isinstance(body, dict) else None

    insp = json.loads(ssh(f"sudo docker inspect $({COMPOSE} ps -q matter)", a.ssh))[0]
    hc = insp["HostConfig"]
    check("Matter server runs in its own container", insp["State"]["Running"] is True)
    check("Matter container: non-root, read-only, no capabilities",
          not str(insp["Config"]["User"]).startswith("0") and hc["ReadonlyRootfs"] and hc.get("CapDrop") == ["ALL"],
          str(insp["Config"]["User"]))
    listen = ssh("sudo ss -Hltn 'sport = :5580'", a.ssh)
    check("Matter API listens on 127.0.0.1 only (it has no authentication)",
          "127.0.0.1:5580" in listen and "0.0.0.0:5580" not in listen and "*:5580" not in listen, listen.strip())

    s, p = c.req("POST", "/api/providers", {"provider_id": "matter"})
    check("owner connects the Matter service", s == 200 and p.get("status") == "OK", f"{s} {p}")

    ssh("sudo docker rm -f virtual-light >/dev/null 2>&1; sudo docker run -d --name virtual-light --network host "
        "-v $HOME/starter/tests/vm/virtual_light.mjs:/app/light.mjs:ro -w /app node:24-trixie-slim "
        "sh -c 'npm i --no-save --no-fund --no-audit --silent @matter/main@0.17.9 && node light.mjs'", a.ssh)
    ready = False
    for _ in range(90):
        if "ready for commissioning" in ssh("sudo docker logs virtual-light 2>&1 | tail -50", a.ssh, check_rc=False):
            ready = True
            break
        time.sleep(4)
    check("virtual Matter light is up and commissionable", ready)
    time.sleep(5)

    s, bad = c.req("POST", "/api/providers/matter/pair", {"code": "1111-111-1111"})
    check("wrong setup code refused with a reason", s == 422, str(bad)[:120])

    # Negative control: the production trust policy must refuse the virtual
    # light's TEST certificate (real certified devices pass attestation).
    s, prod = c.req("POST", "/api/providers/matter/pair", {"code": "3497-011-2332"})
    check("production policy refuses a test-certificate device, and says why",
          s == 422 and "certificate" in str(prod), str(prod)[:160])

    # Test-only override (tests/vm/compose.test-dcl.yaml): accept test certificates.
    ssh(f"{COMPOSE} -f $HOME/starter/tests/vm/compose.test-dcl.yaml up -d matter", a.ssh)
    time.sleep(8)
    s, r = 0, {}
    for _ in range(12):                     # the hub reconnects to the recreated Matter server with backoff
        s, r = c.req("POST", "/api/providers/matter/pair", {"code": "3497-011-2332"})
        if s == 200 or "isn't running" not in str(r) and "Lost the connection" not in str(r):
            break
        time.sleep(10)
    added = r.get("added", []) if isinstance(r, dict) else []
    check("light commissioned over Matter and discovered", s == 200 and len(added) == 1, f"{s} {str(r)[:160]}")
    if not added:
        return
    dev = added[0]
    check("paired device starts UNREVIEWED", dev["review"] == "UNREVIEWED")
    check("only capabilities the device reports", [x["capability"] for x in dev["controls"]] == ["POWER"],
          str([x["capability"] for x in dev["controls"]]))
    c.req("POST", f"/api/devices/{dev['id']}/review", {"status": "MINE"})
    c.req("PATCH", f"/api/devices/{dev['id']}", {"room_name": "Test Room"})
    shown = home_device(c, dev["id"])
    check("claimed light appears in the Home Hub in its room", shown is not None)

    outcomes = []
    for value in (True, False, True):
        s, res = c.req("POST", f"/api/devices/{dev['id']}/actions", {"capability": "POWER", "value": value})
        outcomes.append(res.get("outcome") if isinstance(res, dict) else s)
        time.sleep(1)
    check("owner taps control → device confirms by read-back (on, off, on)", outcomes == ["CONFIRMED"] * 3,
          str(outcomes))
    logs = ssh("sudo docker logs virtual-light 2>&1 | grep VIRTUAL-LIGHT | tail -5", a.ssh, check_rc=False)
    check("the device itself changed state", "onOff=true" in logs and "onOff=false" in logs, logs.replace("\n", " | "))
    p = control(home_device(c, dev["id"]), "POWER")
    check("Hub shows the confirmed state", p["value"] is True and p["freshness"] == "CONFIRMED", json.dumps(p))

    _, audit = c.req("GET", "/api/audit?limit=200")
    entries = audit["entries"]
    acts = [e["action"] for e in entries]
    done = [e for e in entries if e["action"] == "COMMAND_CONFIRMED" and e["target"] == dev["id"]]
    check("audit: pairing and confirmed commands recorded",
          "PAIRING_SUCCEEDED" in acts and len(done) == 3 and done[0]["data"].get("provider") == "matter")
    check("setup code never stored in the audit log", MATTER_TEST_CODE not in json.dumps(entries))

    ssh("sudo docker stop virtual-light >/dev/null", a.ssh)
    started, gone = time.time(), None
    # Matter's own offline detection: subscription timeout (~2 min) + the server's
    # offline grace period (~3 min). The hub reacts to the server's event at once.
    for _ in range(96):
        shown = home_device(c, dev["id"])
        if shown and shown["reachable"] is False:
            gone = shown
            break
        time.sleep(5)
    took = int(time.time() - started)
    check("device switched off at the wall → Can't reach, never Off", gone is not None and gone["on"] is None,
          f"detected after {took}s" if gone else f"still reachable after {took}s")
    ssh("sudo docker rm -f virtual-light >/dev/null 2>&1 || true", a.ssh, check_rc=False)
    st["matter_device"] = dev["id"]
    save_state(st)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["install", "reboot", "browser", "outage", "restore", "upgrade", "matter"])
    ap.add_argument("--base", required=True)
    ap.add_argument("--ssh", required=True)
    a = ap.parse_args()
    print(f"== phase: {a.phase}", flush=True)
    try:
        globals()[f"phase_{a.phase}"](a)
    except Exception as e:  # a crash is a failed phase, reported as such
        check(f"{a.phase} phase ran to completion", False, f"{type(e).__name__}: {e}")
    with open("results.jsonl", "a") as f:
        for name, ok, detail in RESULTS:
            f.write(json.dumps({"phase": a.phase, "check": name, "pass": ok, "detail": detail}) + "\n")
    failed = [r for r in RESULTS if not r[1]]
    print(f"== {a.phase}: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

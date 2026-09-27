"""The Home Hub in a real browser against a real server (HTTP, not TestClient).

Guards what API tests can't see: what the owner is shown and can tap.
Runs when Playwright is installed (`pip install -e ".[browser]"`) and a
Chromium is available — the system Chrome, or `python -m playwright install
chromium`. Otherwise the whole module is skipped.
"""
from __future__ import annotations

import socket
import threading
import time

import os

import httpx
import pytest

# CI sets HNB_REQUIRE_BROWSER=1 so a missing browser fails the run instead of skipping it.
REQUIRED = os.environ.get("HNB_REQUIRE_BROWSER") == "1"
try:
    from playwright import sync_api
except ImportError:
    if REQUIRED:
        raise
    pytest.skip("Playwright not installed (pip install -e \".[browser]\")", allow_module_level=True)

import uvicorn  # noqa: E402

from conftest import PASSPHRASE, make_settings  # noqa: E402
from hnb.api.app import create_app  # noqa: E402
from hnb.providers.registry import BUILTIN  # noqa: E402
from hnb.service import HubService  # noqa: E402

PHONE, TABLET, DESKTOP = (390, 844), (820, 1180), (1440, 900)
EXPECTED_REFUSALS = {409}        # tests below deliberately trigger a duplicate-name conflict


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A hub on a real port with the demo home: owner created, everything reviewed,
    the unnamed TV marked Not mine and the printer ignored — as the wizard would."""
    settings = make_settings(tmp_path_factory.mktemp("browser"), provider_timeout_s=2)
    app = create_app(settings, HubService(settings, adapter_classes=dict(BUILTIN)), poll=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base) as c:
        code = settings.setup_code_file.read_text().strip()
        csrf = c.post("/api/auth/bootstrap", json={"setup_code": code, "username": "alex", "passphrase": PASSPHRASE,
                                                    "house_name": "Maple House"}).json()["csrf_token"]
        h = {"X-CSRF-Token": csrf}
        for d in c.get("/api/devices").json()["devices"]:
            status = {"TV (unnamed)": "NOT_MINE", "Printer": "IGNORED"}.get(d["name"], "MINE")
            c.post(f"/api/devices/{d['id']}/review", json={"status": status}, headers=h)
            if d["name"] in ("Front Door Lock", "Thermostat", "Lamp"):
                c.patch(f"/api/devices/{d['id']}", json={"favorite": True}, headers=h)
        c.post("/api/setup/complete", headers=h)
    yield base
    server.should_exit = True
    thread.join(10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except Exception:
            try:
                b = p.chromium.launch()
            except Exception as e:
                if REQUIRED:
                    raise
                pytest.skip(f"no Chromium available: {e}")
        yield b
        b.close()


class Page:
    """A signed-in page that records console errors and failed API calls."""

    def __init__(self, browser, base: str, size=PHONE, scheme="light"):
        self.base = base
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, color_scheme=scheme)
        self.p = self.ctx.new_page()
        self.console_errors: list[str] = []
        self.failed: list[str] = []
        self.home_fetches = 0
        self.p.on("console", self._console)
        self.p.on("response", self._response)
        self.p.goto(base + "/")
        self.p.fill("#li-user", "alex")
        self.p.fill("#li-pass", PASSPHRASE)
        self.p.click("#login-form button[type=submit]")
        self.p.wait_for_selector(".hero")

    def _console(self, m) -> None:
        # The browser also logs each refused request; expected refusals aren't errors.
        if m.type == "error" and not any(f"status of {s}" in m.text for s in EXPECTED_REFUSALS):
            self.console_errors.append(m.text)

    def _response(self, r) -> None:
        if "/api/" in r.url:
            if r.url.endswith("/api/home"):
                self.home_fetches += 1
            if r.status >= 400 and r.status not in EXPECTED_REFUSALS:
                self.failed.append(f"{r.status} {r.request.method} {r.url}")

    def go(self, route: str) -> None:
        self.p.evaluate(f"location.hash = '#{route}'")
        self.p.wait_for_timeout(600)

    def card(self, name: str):
        return self.p.locator(".tile").filter(has=self.p.locator(".tile-name", has_text=name)).first

    def api(self, path: str):
        return self.p.evaluate(f"fetch('{path}').then(r => r.json())")

    def clean(self) -> None:
        assert self.console_errors == [], self.console_errors
        assert self.failed == [], self.failed
        self.ctx.close()


def test_locks_climate_and_other_devices_render_with_only_their_controls(live, browser):
    pg = Page(browser, live)
    section = pg.p.locator("section.section", has=pg.p.locator("h2", has_text="Locks, climate"))
    assert section.count() == 1
    lock, thermo = pg.card("Front Door Lock"), pg.card("Thermostat")
    assert "Locked" in lock.inner_text() and lock.locator("[data-lockask]").count() == 1
    assert "20.5 °C" in thermo.inner_text() and thermo.locator("[data-act=thermomode]").count() == 4
    thermo.locator("[data-act=thermomode][data-mode=COOL]").click()      # goes to the activity log (checked later)
    sync_api.expect(pg.card("Thermostat")).to_contain_text("Cool · set to")
    # A favourite lock is a lock card — never the camera card it used to fall through to.
    favs = pg.p.locator("section.section", has=pg.p.locator("h2", has_text="Favorites"))
    assert "camera" not in favs.inner_text().lower()
    pg.clean()


def test_unlock_needs_the_confirmation_sheet_and_reaches_the_device(live, browser):
    pg = Page(browser, live)
    pg.card("Front Door Lock").locator("[data-lockask]").click()
    sheet = pg.p.locator("#sheet")
    assert "Unlock Front Door Lock?" in sheet.inner_text()
    sheet.get_by_role("button", name="Cancel").click()                   # cancelling changes nothing
    pg.p.wait_for_timeout(400)
    assert "Locked" in pg.card("Front Door Lock").inner_text()
    pg.card("Front Door Lock").locator("[data-lockask]").click()
    sheet.get_by_role("button", name="Unlock", exact=True).click()
    pg.p.wait_for_function("() => document.body.innerText.includes('Unlocked')")
    lock_id = next(d["id"] for d in pg.api("/api/devices")["devices"] if d["name"] == "Front Door Lock")
    hist = pg.api(f"/api/devices/{lock_id}/history")["entries"]
    assert hist[0]["action"] == "COMMAND_CONFIRMED" and hist[0]["requested"] == {"capability": "LOCK", "value": "UNLOCKED"}
    pg.clean()


def test_rooms_can_be_added_renamed_and_deleted_from_the_ui(live, browser):
    pg = Page(browser, live)
    pg.go("rooms")
    pg.p.click("[data-editrooms]")
    pg.p.fill("#new-room", "Garage")
    pg.p.press("#new-room", "Enter")
    pg.p.wait_for_selector("input[data-rename][value='Garage']")
    kitchen = pg.p.locator("input[data-rename][value='Kitchen']")
    kitchen.fill("Cocina")
    kitchen.press("Tab")                                                 # saved on change
    pg.p.wait_for_selector("input[data-rename][value='Cocina']")
    bedroom = pg.p.locator("input[data-rename][value='Bedroom']")
    bedroom.fill("garage")                                               # duplicate → refused, field restored
    bedroom.press("Tab")
    pg.p.wait_for_selector("input[data-rename][value='Bedroom']")
    garage_id = pg.p.locator("input[data-rename][value='Garage']").get_attribute("data-rename")
    delete = pg.p.locator(f"[data-delroom='{garage_id}']")
    delete.click()
    sync_api.expect(delete).to_have_text("Tap again to delete")
    delete.click()
    pg.p.wait_for_timeout(600)
    names = [r["name"] for r in pg.api("/api/rooms")["rooms"]]
    assert "Garage" not in names and "Cocina" in names and "Bedroom" in names
    pg.go("settings")                                                    # the activity log says what happened, in words
    activity = pg.p.locator("table").inner_text()
    assert "Kitchen → Cocina" in activity and '{"' not in activity
    pg.clean()


def test_outage_says_cant_reach_never_off_and_recovers(live, browser):
    pg = Page(browser, live)
    pg.go("settings")
    form = pg.p.locator(".prov-form")
    form.locator("select").select_option("outage")
    form.locator("button[type=submit]").click()
    pg.p.wait_for_function("() => document.body.innerText.includes(\"Can't reach\")")
    pg.go("home")
    facts = pg.p.locator(".hero-facts").inner_text()
    assert "0 lights on" not in facts and "unknown" in facts
    tiles = pg.p.locator(".tile").all_inner_texts()
    assert not [t for t in tiles if "\nOff\n" in f"\n{t}\n"], "a device claims Off during an outage"
    assert pg.p.locator("[data-lockask], [data-act^=thermo]").count() == 0     # no controls for unknown state
    pg.go("settings")
    form.locator("select").select_option("none")
    form.locator("button[type=submit]").click()
    pg.p.wait_for_timeout(800)
    pg.go("home")
    assert "Everything is working" in pg.p.locator(".status-line").inner_text()
    pg.clean()


@pytest.mark.parametrize("size,scheme", [(PHONE, "light"), (TABLET, "dark"), (DESKTOP, "light")],
                         ids=["phone", "tablet-dark", "desktop"])
def test_layout_touch_targets_and_no_dead_buttons(live, browser, size, scheme):
    pg = Page(browser, live, size, scheme)
    report = pg.p.evaluate("""() => {
      const KNOWN = ['act','open','close','room','rev','all','fav','step','finish','discover','backupNow','verify',
                     'openform','pair','closeGo','connect','logout','retry','themeSet','lockask','lockgo','editrooms','delroom','go'];
      const hit = el => { const r = el.getBoundingClientRect(), x = r.left + r.width / 2;
        return [r.top - 4, r.bottom + 4].every(y => y < 0 || y > innerHeight || document.elementFromPoint(x, y) === el) ? 44 : r.height; };
      const visible = [...document.querySelectorAll('main button, main input[type=range]')].filter(e => e.getBoundingClientRect().height > 0);
      return {
        hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        dead: visible.filter(b => b.tagName === 'BUTTON' && !b.disabled && !Object.keys(b.dataset).some(k => KNOWN.includes(k))
                                && !(b.type === 'submit' && b.form)).map(b => b.outerHTML.slice(0, 80)),
        small: visible.filter(e => Math.max(e.getBoundingClientRect().height, e.classList.contains('toggle') ? hit(e) : 0) < 40)
                      .map(e => e.className + ' ' + Math.round(e.getBoundingClientRect().height)),
        bg: getComputedStyle(document.body).backgroundColor,
      };
    }""")
    assert not report["hscroll"] and report["dead"] == [] and report["small"] == [], report
    assert (report["bg"] == "rgb(7, 10, 18)") == (scheme == "dark"), report["bg"]
    pg.clean()


def test_one_home_fetch_per_refresh(live, browser):
    pg = Page(browser, live)
    pg.p.wait_for_timeout(500)
    before = pg.home_fetches
    pg.card("Lamp").locator("[data-act=power]").click()                 # act → refresh
    pg.p.wait_for_timeout(1000)
    assert pg.home_fetches - before == 1
    pg.clean()


def test_writes_from_the_page_carry_csrf_and_forged_ones_fail(live, browser):
    pg = Page(browser, live)
    lamp = next(d["id"] for d in pg.api("/api/devices")["devices"] if d["name"] == "Lamp")
    status = pg.p.evaluate("""id => fetch(`/api/devices/${id}/actions`, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({capability: 'POWER', value: true})})
        .then(r => r.status)""", lamp)
    assert status == 403                                                  # same session, no CSRF token
    pg.failed.clear()                                                     # that refusal was the point
    pg.console_errors = [e for e in pg.console_errors if "status of 403" not in e]
    pg.clean()

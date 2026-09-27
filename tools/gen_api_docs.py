"""Regenerate docs/openapi.json and API.md from the running code.

    python tools/gen_api_docs.py

Starts a throwaway hub in a temp directory (demo provider, fresh key), signs
in, and calls the API for real: the examples in API.md are recorded responses,
not hand-written ones. tests/test_api_docs.py fails if docs/openapi.json no
longer matches the code.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from hnb.api.app import create_app  # noqa: E402
from hnb.config import Settings  # noqa: E402
from hnb.security.vault import generate_key_file  # noqa: E402
from hnb.providers.registry import BUILTIN  # noqa: E402
from hnb.service import HubService  # noqa: E402

PASSPHRASE = "example passphrase one"


def build_app(tmp: Path, *, enable_demo: bool = True):
    key = tmp / "secrets" / "vault.key"
    generate_key_file(key)
    settings = Settings(data_dir=tmp / "data", backup_dir=tmp / "backups", log_dir=None, vault_key_file=key,
                        poll_interval_s=3600, provider_timeout_s=2, enable_demo=enable_demo)
    # Built-in modules only, so a developer's plugins/ never leak into the published docs.
    return settings, create_app(settings, HubService(settings, adapter_classes=dict(BUILTIN)), poll=False)


def openapi_spec() -> dict:
    with tempfile.TemporaryDirectory() as t:
        _, app = build_app(Path(t))
        spec = app.openapi()
    return json.loads(json.dumps(spec, sort_keys=True))


def shorten(value, depth=0):
    """Keep examples readable: trim long lists, keep structure."""
    if isinstance(value, list):
        out = [shorten(v, depth + 1) for v in value[:2]]
        if len(value) > 2:
            out.append(f"… {len(value) - 2} more")
        return out
    if isinstance(value, dict):
        return {k: shorten(v, depth + 1) for k, v in value.items()}
    return value


def example_network(port: int) -> dict:
    """The machine generating the docs must not appear in them."""
    return {"hostname": "homehub", "lan_address": "192.0.2.20", "urls": [f"http://192.0.2.20:{port}"],
            "ipv6": {"enabled": True, "link_local": True}}


def record_examples() -> list[tuple[str, str, dict | None, int, object]]:
    import hnb.ops.system
    hnb.ops.system.network_facts = example_network
    rows = []
    with tempfile.TemporaryDirectory() as t:
        settings, app = build_app(Path(t), enable_demo=False)     # the demo is connected below, on the record
        with TestClient(app, client=("127.0.0.1", 50000)) as c:
            def call(method, path, body=None, csrf=None, label=None):
                headers = {"X-CSRF-Token": csrf} if csrf else {}
                r = c.request(method, path, json=body, headers=headers)
                shown = r.json()
                if isinstance(shown, dict) and "csrf_token" in shown:      # never print a token, even a throwaway one
                    shown = {**shown, "csrf_token": "<csrf token>"}
                rows.append((method, label or path, body, r.status_code, shown))
                return r.json()

            call("GET", "/api/health")
            call("GET", "/api/devices", label="/api/devices  (signed out → 401)")
            code = settings.setup_code_file.read_text().strip()
            s = call("POST", "/api/auth/bootstrap", {"setup_code": "ABCD-EFGH-JKLM", "username": "alex",
                                                     "passphrase": PASSPHRASE, "house_name": "Maple House"},
                     label="/api/auth/bootstrap  (wrong code)")
            s = c.post("/api/auth/bootstrap", json={"setup_code": code, "username": "alex",
                                                     "passphrase": PASSPHRASE, "house_name": "Maple House"})
            rows.append(("POST", "/api/auth/bootstrap", {"setup_code": "<from sudo /opt/hnb/bin/setup-code>", "username": "alex",
                                                          "passphrase": "<passphrase>", "house_name": "Maple House"},
                         s.status_code, {**s.json(), "csrf_token": "<csrf token>"}))
            csrf = s.json()["csrf_token"]
            call("GET", "/api/session")
            call("POST", "/api/providers", {"provider_id": "demo", "settings": {}, "secrets": {}}, csrf,
                 label="/api/providers  (connect an installed module)")
            devices = call("GET", "/api/devices")["devices"]
            lamp = next(d for d in devices if d["name"] == "Lamp")
            for d in devices:
                c.post(f"/api/devices/{d['id']}/review", json={"status": "MINE"}, headers={"X-CSRF-Token": csrf})
            call("POST", "/api/devices/{id}/review".replace("{id}", lamp["id"]), {"status": "MINE"}, csrf,
                 label="/api/devices/{id}/review")
            call("POST", f"/api/devices/{lamp['id']}/actions", {"capability": "BRIGHTNESS", "value": 40}, csrf,
                 label="/api/devices/{id}/actions")
            call("POST", f"/api/devices/{lamp['id']}/actions", {"capability": "VOLUME", "value": 5}, csrf,
                 label="/api/devices/{id}/actions  (unsupported → REJECTED)")
            call("POST", f"/api/devices/{lamp['id']}/actions", {"capability": "POWER", "value": True}, None,
                 label="/api/devices/{id}/actions  (no X-CSRF-Token → 403)")
            lock = next(d for d in devices if d["name"] == "Front Door Lock")
            call("POST", f"/api/devices/{lock['id']}/actions", {"capability": "LOCK", "value": "UNLOCKED"}, csrf,
                 label="/api/devices/{id}/actions  (LOCK without confirm → REJECTED)")
            call("POST", f"/api/devices/{lock['id']}/actions", {"capability": "LOCK", "value": "UNLOCKED", "confirm": True},
                 csrf, label="/api/devices/{id}/actions  (LOCK with confirm)")
            call("GET", f"/api/devices/{lamp['id']}", label="/api/devices/{id}")
            call("PATCH", f"/api/devices/{lamp['id']}", {"favorite": True}, csrf, label="/api/devices/{id}")
            call("GET", "/api/home")
            call("GET", "/api/rooms")
            call("GET", "/api/capabilities")
            call("GET", "/api/providers")
            call("POST", "/api/providers", {"provider_id": "kasa"}, csrf,
                 label="/api/providers  (catalog entry, module not installed → 409)")
            call("POST", "/api/providers/demo/discover", None, csrf, label="/api/providers/{id}/discover")
            call("PATCH", "/api/providers/demo", {"settings": {"simulate": "outage"}}, csrf, label="/api/providers/{id}")
            call("PATCH", "/api/providers/demo", {"settings": {"simulate": "none"}}, csrf, label="/api/providers/{id}")
            call("POST", "/api/backups", None, csrf)
            call("GET", "/api/audit?limit=3", label="/api/audit?limit=3")
            call("GET", "/api/system")
    return rows


REFERENCE = """
## Reference

### Signing in and sessions

| | |
|---|---|
| First owner | `POST /api/auth/bootstrap` works only while no owner exists, only from the home network, and only with the one-time setup code the hub writes on first start (`sudo /opt/hnb/bin/setup-code` on an installed hub, `python -m hnb.cli setup-code` in development). The code is deleted on use; afterwards bootstrap answers 409. |
| Sign in | `POST /api/auth/login` with `{"username", "passphrase"}`. Sets an `HttpOnly; SameSite=Strict` session cookie (`Secure` over HTTPS) and returns `csrf_token`. |
| Session state | `GET /api/session` → `setup_required`, `authenticated`, `username`, `house`, `setup_complete`, `csrf_token`. Safe to call signed out. |
| Lifetime | 7 days idle, 30 days absolute. `POST /api/auth/logout` ends it. |
| Brute force | 5 failed sign-ins or bootstraps per client per 15 minutes, then 429 with `Retry-After`. |
| Reads | Every `GET` except `/api/health` and `/api/session` needs a session (401 otherwise). |

### Writes: CSRF and origin

Every `POST`, `PATCH` and `DELETE` needs **both** the session cookie **and** its token in the
`X-CSRF-Token` header. A missing or wrong token is 403. A request whose `Origin` header names
another site is 403 even with a valid token. Login and bootstrap accept JSON bodies only (415
otherwise). Chunked bodies without a `Content-Length` are refused (411) and bodies
must be small (413). Writes that take no body (discover, backups, logout, setup
completion) accept an empty `POST` (`curl -X POST`).

### Status codes

| Status | Meaning |
|---|---|
| 200 | Done. For device actions, read `outcome` — a 200 can still be `FAILED` (see below). |
| 400 | A value the hub can't accept (unknown setting, bad choice, missing credential). |
| 401 | Not signed in, or the session expired. |
| 403 | Missing/wrong CSRF token, cross-site `Origin`, wrong setup code, or a caller outside the home network. |
| 404 | No such device, room, provider, backup or route. |
| 409 | Conflict: a duplicate room name, an owner already exists, a provider already connected, or a provider module that isn't installed. |
| 411 / 413 / 415 | Body without `Content-Length` / body too large / not JSON. |
| 422 | Request shape invalid (`"Check <field>: …"`), a device action `REJECTED` before any provider call, a pairing refused, or a backup that fails verification. |
| 429 | Too many sign-in attempts; wait for `Retry-After` seconds. |
| 500 / 502 | The hub failed (backup could not be written) / a provider didn't answer during pairing or discovery. |

Every error body is `{"error": "<plain-English reason>"}` — written to be shown to the owner as-is,
and never containing a secret.

### Controlling a device

```
POST /api/devices/{id}/actions  {"capability": "BRIGHTNESS", "value": 40}
  0. capability name not in the vocabulary                                           → 400, nothing recorded
  1. recorded: audit COMMAND_REQUESTED (PENDING), for every request
  2. validate — device is MINE, capability declared, value valid for this device,
     HIGH sensitivity (LOCK) has "confirm": true, its provider is connected
       any of these fails → REJECTED (422), audit COMMAND_REJECTED, nothing sent
  3. committed before the provider call, so a crash mid-call still leaves evidence
  4. adapter.execute(), bounded by the provider timeout (default 8 s)
       timeout / provider down / device down / auth expired                          → FAILED (200)
  5. adapter reads the device back
       matches → CONFIRMED · differs → ACCEPTED with detail · not read back → ACCEPTED
  6. state + outcome + audit COMMAND_<OUTCOME> committed together; the response carries the outcome
```

| Outcome | Meaning | State afterwards |
|---|---|---|
| `CONFIRMED` | The device reported the requested value. | `CONFIRMED` |
| `ACCEPTED` | Sent, but not read back (or the device reports something else — see `detail`). | `ASSUMED` |
| `FAILED` | Provider error or no answer. The device may or may not have changed. | `UNKNOWN` for that capability |
| `REJECTED` | Refused before any provider call. Nothing was sent. | unchanged |

State freshness on every control: `CONFIRMED` (read recently) · `ASSUMED` (we sent it, not read
back) · `STALE` (older than the provider's freshness window) · `UNKNOWN` (can't reach). The Hub shows
these as nothing, *Not confirmed yet*, *Last known* and *Can't reach*. A provider outage makes state
`UNKNOWN`; it never turns a device off. Always read a control's `value` together with its
`freshness`: with `UNKNOWN`, `value` is only the last thing the hub knew (the device's summary `on`
is `null` then).

Devices start `UNREVIEWED` and are controllable only once the owner marks them `MINE`
(`POST /api/devices/{id}/review` with `MINE`, `NOT_MINE` or `IGNORED`).

### Providers: how services are registered

- A **provider module** is Python code implementing `ProviderAdapter` (see `ADDING_A_PROVIDER.md`).
  The hub finds modules at start-up: built-ins, the `hnb.providers` entry-point group, and every
  `plugins/<name>/adapter.py` that defines `ADAPTER`. There is no API for uploading code.
- `GET /api/providers` → `providers` (connected, with status, device count, settings, and the
  *names* of stored credentials) and `available` (everything installed plus the catalog of
  well-known services; uninstalled ones have `installed: false` and
  `status_text: "Provider module not installed"`).
- `POST /api/providers` `{"provider_id", "settings": {…}, "secrets": {…}}` connects an installed
  module. `settings` must match the module's `settings` fields (and `choices`); `secrets` must
  cover its credential fields. Secrets go straight to the encrypted vault and are never returned.
  Connecting a catalog entry that isn't installed is 409.
- `PATCH /api/providers/{id}` changes settings or replaces credentials (a blank credential keeps
  the stored one). `POST …/discover` asks the provider for devices now; `POST …/pair` adds a device
  with a setup code (only for modules with `supports_pairing`). New devices start `UNREVIEWED`.

### Health

- `GET /api/health` signed out → only `status`, `version`, `setup_required` (for Docker and the
  installer). Signed in, it adds `components`: API uptime, database (schema, journal mode,
  integrity checks, size, audit rows), vault (entries, any sealed with another key), each provider's
  status in plain words, backup (age, verified), storage and poller. `status` is `ok`, `degraded`
  (a provider not OK, low disk, backup overdue or unverified, or credentials sealed with another
  key) or `error` (the database failed its checks).
- `GET /api/system` → version, schema, network facts (hostname, LAN address, IPv6), device counts
  by review status, installed and connected providers, and provider modules that failed to load.
"""


def reference_sections() -> list[str]:
    from hnb.core.capabilities import catalog
    from hnb.core.view import SECTION_OF
    lines = REFERENCE.strip("\n").splitlines() + ["", "### Capability values", "",
             "Every write is validated against this vocabulary before a provider sees it "
             "(also served at `GET /api/capabilities`).", "",
             "| Capability | Value | Writable | Sensitivity | What it is |", "|---|---|---|---|---|"]
    for c in catalog():
        cell = lambda s: str(s).replace("|", "\\|")                     # a `|` inside a cell splits the row
        lines.append(f"| `{c['type']}` | {cell(c['value'])} | {'yes' if c['writable'] else 'read-only'} | "
                     f"{c['sensitivity']} | {cell(c['description'])} |")
    lines += ["", "Device-specific bounds travel in each control's `options` (colour-temperature `min`/`max`, "
              "a TV's `apps`, thermostat `modes`, `min_c`, `max_c`).", "",
              "### Device kinds and where the Hub shows them", "", "| Kind | Hub section |", "|---|---|"]
    for kind, sec in SECTION_OF.items():
        lines.append(f"| `{kind.value}` | {sec} |")
    return lines + [""]


def write_api_md(spec: dict, examples) -> str:
    lines = ["# API", "",
             "The hub's HTTP API — the same one the Home Hub uses. Generated by `python tools/gen_api_docs.py`;",
             "the machine-readable schema is [`docs/openapi.json`](docs/openapi.json), also served to signed-in",
             "owners at `GET /api/openapi.json`.", "",
             "## Basics", "",
             spec["info"]["description"].strip(), "",
             "### From the command line", "",
             "```bash",
             "HUB=http://<hub-address>:8780",
             "# first time only: create the owner with the one-time setup code (sudo /opt/hnb/bin/setup-code)",
             "curl -s -c jar -H 'Content-Type: application/json' \\",
             "  -d '{\"setup_code\":\"<code>\",\"username\":\"alex\",\"passphrase\":\"<passphrase>\",\"house_name\":\"Maple House\"}' \\",
             "  $HUB/api/auth/bootstrap",
             "# afterwards: sign in (keeps the session cookie) and capture the CSRF token for writes",
             "CSRF=$(curl -s -c jar -H 'Content-Type: application/json' \\",
             "  -d '{\"username\":\"alex\",\"passphrase\":\"<passphrase>\"}' $HUB/api/auth/login | jq -r .csrf_token)",
             "curl -s -b jar $HUB/api/home | jq .status",
             "curl -s -b jar -H \"X-CSRF-Token: $CSRF\" -H 'Content-Type: application/json' \\",
             "  -d '{\"capability\":\"POWER\",\"value\":true}' $HUB/api/devices/<device-id>/actions",
             "```", ""]
    lines += reference_sections()
    lines += ["## Endpoints", ""]
    by_tag: dict[str, list] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            by_tag.setdefault((op.get("tags") or ["other"])[0], []).append((path, method.upper(), op))
    tag_desc = {t["name"]: t["description"] for t in spec.get("tags", [])}
    for tag in [t["name"] for t in spec.get("tags", [])]:
        if tag not in by_tag:
            continue
        lines += [f"### {tag}", "", tag_desc.get(tag, ""), "", "| Method | Path | What it does |", "|---|---|---|"]
        for path, method, op in sorted(by_tag[tag], key=lambda x: (x[0], x[1])):
            lines.append(f"| `{method}` | `{path}` | {op.get('summary', '')} |")
        for path, method, op in sorted(by_tag[tag], key=lambda x: (x[0], x[1])):
            desc = (op.get("description") or "").strip()
            if desc:
                lines += ["", f"**`{method} {path}`** — " + " ".join(desc.split())]
        lines.append("")
    lines += ["## Recorded examples", "",
              "Real requests against a fresh hub running the demo home (long lists trimmed).", ""]
    for method, label, body, status, resp in examples:
        lines += [f"### `{method} {label}` → {status}", ""]
        if body is not None:
            lines += ["Request:", "```json", json.dumps(body, indent=2, ensure_ascii=False), "```"]
        lines += ["Response:", "```json", json.dumps(shorten(resp), indent=2, default=str, ensure_ascii=False), "```", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    spec = openapi_spec()
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "openapi.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                                                    newline="\n")
    (ROOT / "API.md").write_text(write_api_md(spec, record_examples()), encoding="utf-8", newline="\n")
    n = sum(len(v) for v in spec["paths"].values())
    print(f"docs/openapi.json: {len(spec['paths'])} paths, {n} operations · API.md written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

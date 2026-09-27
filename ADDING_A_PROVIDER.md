# Adding a provider

A **provider module** teaches the hub to talk to one kind of service: a brand's
local API, a cloud API, a bridge such as Home Assistant. Everything
house-specific lives in provider modules. The core, the API and the Home Hub
never change when you add one.

This guide takes you from nothing to a working device on the Home Hub. Budget an
hour for the walk-through and longer for a real API. It assumes a development
setup (see `DEVELOPMENT.md`, "Set up"):

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                   # everything green before you start
```

On Windows the folder is `Scripts` instead of `bin`: `.venv\Scripts\pip` in
PowerShell or cmd, `.venv/Scripts/pip` in Git Bash. Commands below write
`python`; use your virtual environment's interpreter. The project already runs
pytest quietly (`addopts = "-q"`), so don't add another `-q` or the pass count
is hidden.

## What you will build

```
plugins/<your_id>/
  adapter.py            the adapter: your service's protocol on one side, the hub's vocabulary on the other
  fake_device.py        a simulator of your service, so tests never need a real account or device
  test_<your_id>.py     the SDK conformance checks, run against adapter + simulator, plus your own tests
  README.md             what the module talks to and how to set it up
```

The hub loads every `plugins/<name>/adapter.py` that defines `ADAPTER` (your
adapter class) when it starts. That is the whole registration step. No list to
edit, no core import.

## 1. Scaffold from the working example

```bash
python -m hnb.cli new-provider my_switches --name "My switches"
python -m pytest plugins/my_switches            # 12 passed: 10 conformance checks + 2 example tests
```

`new-provider` copies `examples/providers/acme_plug/`, an adapter for a
made-up "Acme" plug hub that speaks newline-delimited JSON over TCP, into
`plugins/my_switches/`. It renames the id, the display name and the class, names
the test file `test_my_switches.py`, gives the simulator its own port (derived
from the id), and leaves a placeholder `description` for you to rewrite. The id must be 2–32 characters of lowercase letters, digits or `_`, starting with a
letter, and must not be a built-in (`demo`, `matter`).

Read `plugins/my_switches/adapter.py` top to bottom once. Every rule in this
guide is shown there in context. You will replace the Acme parts with your
service's API.

## 2. Identity

```python
class MySwitchesAdapter(ProviderAdapter):
    provider_id = "my_switches"          # stable forever: it is stored with every device the module finds
    display_name = "My switches"         # what the owner sees in Settings
    description = "Wall switches through the My-Switches hub on your network."   # one line, required
```

Import everything from one place: `from hnb.providers.sdk import ...`.

## 3. Settings and credentials

| | Declare | Read in the adapter | Where it is stored | Shown back to the owner? |
|---|---|---|---|---|
| **Settings** (host, port, region) | `config_fields = (ConfigField(name, label, default, help, choices),)` | `self._config.get(name, default)` (always a string) | database | yes, editable in Settings |
| **Credentials** (API key, token) | `secret_fields = (ConfigField(...),)` **and** the same names in `required_secrets` | `self._secrets.get(name)` (may be `None`) | encrypted vault only | never, not even to the owner |

With `choices=("a", "b")` a setting becomes a picker and any other value is
refused. The Settings page draws the form from these declarations. There is no
UI code to write. Saving new settings restarts your adapter.

A missing credential must make `health()` report `AUTH_REQUIRED` and make calls
raise `AuthExpired`. Never treat a missing credential as `DOWN` or as "device off".

## 4. The simulator (`fake_device.py`)

Change the fake so it behaves like your real service: the same requests, the
same replies, the same error cases (bad key, unknown device, the service going
away). Tests and local development run against it. **Never put a real account,
address, device id or key in the repository.** The isolation guard
(`tests/test_isolation_guard.py`) fails the build on private IP addresses, MAC
addresses, tailnet names, private keys and certificates, and tokens in
well-known vendor formats (AWS, GitHub, Slack, Google, Tailscale, `sk-…` keys;
the list is `VENDOR_SHAPES` in `hnb/security/redact.py`). Use obviously fake
values like the example's `acme-dev-key-0001`, and `127.0.0.1` or the
documentation range `192.0.2.x` for addresses.

`python plugins/my_switches/fake_device.py` runs it standalone for trying the
module in a real hub (step 11).

## 5. Transport

Replace the adapter's `_call` with your protocol (HTTP, WebSocket, a vendor SDK).

- **Timeouts.** The core wraps every adapter call in the provider timeout
  (`HNB_PROVIDER_TIMEOUT_S`, default 8 s) and reports a timeout truthfully. Give
  your own requests a shorter timeout (the example uses 5 s) so the owner gets
  your clearer message instead of a generic "no answer".
- **Never block the event loop.** The hub is one asyncio process. Use async
  code: `asyncio` streams (as the example does) or `websockets`, which ships
  with the hub. Wrap a blocking SDK call in `await asyncio.to_thread(...)`.
  `httpx` is installed for the test suite only and is **not** in the image.
  For a plain HTTP/JSON device, the standard library is enough:
  ```python
  import json, urllib.request
  def _get(url: str, key: str) -> dict:                  # blocking; run it in a thread
      req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
      with urllib.request.urlopen(req, timeout=5) as r:
          return json.load(r)
  data = await asyncio.to_thread(_get, f"http://{host}:{port}/api/switches", key)
  ```
  Map `urllib.error.URLError` / `TimeoutError` to `ProviderUnavailable` and an
  HTTP 401/403 to `AuthExpired`.
- **Long-lived connections** (a WebSocket, a subscription) open in
  `async def start(self)` and close in `async def close(self)`. The hub calls
  `start()` once before the first poll and `close()` on shutdown or restart.
- **Retries.** Don't loop. The hub's poller already retries reads with
  exponential backoff (capped at 4 poll intervals) while a provider is failing,
  and it re-checks health after a success. One immediate retry of a *read* on a
  dropped connection is fine. **Do not retry writes** unless the provider's
  write is idempotent and you know the first attempt never arrived. A retried
  "toggle" is a light that flips twice.
- **Third-party libraries** (including `httpx` or `aiohttp`) must be added to
  `requirements.in` and the hash-pinned lock regenerated (`python tools/lock.py`).
  The Docker image installs nothing that isn't in `requirements.lock`, so a
  module can pass every local test and still fail to import on an installed hub.

## 6. Errors: what to raise

Messages are shown to the owner as-is. Write them in plain words and never
include a secret, token or setup code.

| Situation | Raise | What the owner sees |
|---|---|---|
| Can't reach your service at all | `ProviderUnavailable("Can't reach the My-Switches hub.")` | Every device of yours shows *Can't reach* (state `UNKNOWN`, never Off); health `DOWN` |
| The service answered: this device is offline or gone | `DeviceUnavailable("That switch is offline.")` | That device's state becomes `UNKNOWN`; the command `FAILED` |
| Credentials rejected or missing | `AuthExpired("The hub rejected the API key.")` | *Needs sign-in*; the owner re-enters the credential in Settings |
| Any other provider-side error | `ProviderError("The hub reported an error.")` | The command `FAILED` with your message |
| Adding a device with a setup code failed | `PairingError("...")` | Shown in the *Add a device* sheet |

Asking about a device your service doesn't know must raise (usually
`DeviceUnavailable`), never return empty state.

## 7. Discovery

```python
async def discover_devices(self) -> list[DiscoveredDevice]:
    return [DiscoveredDevice(
        ref=DeviceRef(self.provider_id, item["id"]),   # your service's own stable id — never an IP or MAC
        name=item["name"],                             # the name the owner already uses in the vendor's app
        kind=DeviceKind.SWITCH,                        # LIGHT SWITCH PLUG TV SPEAKER CAMERA DOORBELL LOCK THERMOSTAT SENSOR NETWORK OTHER
        capabilities=(CapabilitySpec(CapabilityType.SWITCH),),
        suggested_room=item.get("room"),               # the vendor app's room, if any; only a suggestion
        manufacturer="My-Switches", model=item.get("model"),
    ) for item in (await self._call("list"))["items"]]
```

Return everything your service can see. The core decides what is new, never
duplicates a device, never overwrites the owner's names, rooms or decisions, and
starts every new device as `UNREVIEWED`. Nothing appears on the Home Hub until
the owner says it's theirs.

`kind` decides where the Hub shows the device (lights, entertainment, cameras,
network, or *Locks, climate & more*). The table is in `API.md`, "Device kinds".
Capabilities decide which controls it gets. The two are independent: a
`DeviceKind.SWITCH` with the `POWER` capability is as valid as one with `SWITCH`.

## 8. Capabilities and state

Declare **only** what the device really has. The Hub draws a control for every
declared writable capability and nothing else.

| Capability | Value | Notes |
|---|---|---|
| `POWER` | `true` / `false` | the device itself (lamp, plug, TV) |
| `SWITCH` | `true` / `false` | a wall switch driving a load |
| `BRIGHTNESS` | integer 0–100 | percent |
| `COLOR` | `{"h": 0–360, "s": 0–100}` | |
| `COLOR_TEMPERATURE` | integer kelvin | range in `options={"min": 2700, "max": 6500}` |
| `VOLUME` | integer 0–100 | |
| `MUTE` | `true` / `false` | |
| `APP_LAUNCH` | one of `options["apps"]` | write-only |
| `LOCK` | `"LOCKED"` / `"UNLOCKED"` | HIGH sensitivity: the hub requires an explicit confirmation |
| `THERMOSTAT` | `{"mode": ..., "setpoint_c": float}` | `options={"modes": [...], "min_c": 7, "max_c": 32}` |
| `CAMERA_LIVE`, `CAMERA_EVENT` | read-only | always read-only, whatever the adapter says |

`get_capabilities(ref)` must agree with what discovery declared. `get_state(ref)`
returns a `StateReading(capability, value)` for each **declared, readable**
capability, in exactly these value shapes. The conformance kit validates every
reading against the vocabulary. The authoritative table is generated into
`API.md`, "Capability values".

`freshness_window` (seconds, default 60) is how long a reading stays trustworthy
before the Hub shows *Last known*. The hub, not your module, polls: every
`HNB_POLL_INTERVAL_S` (30 s installed, 15 s in dev). Set the window to at least
twice that (the example uses 120 s) so one slow poll doesn't flag every device.

**`POWER` or `SWITCH`?** `POWER` is the device itself turning on and off (a lamp,
a plug, a TV). `SWITCH` is a wall switch that drives a separate load. Both are the
on/off toggle on the Hub. Pick the one that describes the device, and use that
name in API calls: a module that declares `SWITCH` gets 422 for `POWER`.

## 9. Execute, then verify

```python
async def execute(self, ref, capability, value) -> ExecuteResult:
    # `value` is already validated and normalised by the core for this device.
    if capability is not CapabilityType.POWER:                  # only what you declared
        raise DeviceUnavailable("This device can't do that.")
    await self._call("set", id=ref.external_id, on=bool(value))
    try:
        state = {r.capability: r.value for r in await self.get_state(ref)}   # read the device back
    except ProviderError:
        return ExecuteResult(ExecuteStatus.ACCEPTED)                          # sent, not confirmed
    return ExecuteResult(ExecuteStatus.CONFIRMED, StateReading(capability, state.get(capability)))
```

**"Sent" is not "done".** Return `CONFIRMED` only after reading the device's
state back, and include what it actually reported in `observed`. The core
compares it with the request: a match is *CONFIRMED*, and a mismatch is
*ACCEPTED* with "The device reports a different value than requested." If you
cannot read back (a write-only API, or the read failed), return `ACCEPTED`; the Hub
then shows *Not confirmed yet*. If a device rounds values (levels, mireds), map
its reading to the canonical value before returning it.

Refuse capabilities you didn't declare by raising `DeviceUnavailable`, as above.
The core should never send one, but refuse loudly if it does.

## 10. Health

```python
async def health(self) -> ProviderHealth:
    try:
        await self._call("list")
    except AuthExpired:
        return ProviderHealth(HealthStatus.AUTH_REQUIRED, "The API key was rejected. Update it in Settings.")
    except ProviderUnavailable:
        return ProviderHealth(HealthStatus.DOWN, "Can't reach the My-Switches hub.")
    except Exception:
        return ProviderHealth(HealthStatus.UNKNOWN, "The hub gave an unexpected answer.")
    return ProviderHealth(HealthStatus.OK)
```

Cheap, and it **never raises**. `DEGRADED` exists for "working, but slow or
partially failing". `detail` is shown to the owner.

## 11. Prove it: the conformance kit

`plugins/my_switches/test_my_switches.py` has a **harness** that builds your
adapter against the simulator and knows how to break it:

```python
class Harness:
    @contextlib.asynccontextmanager
    async def adapter(self):                 # start the fake, yield a ready adapter, clean up
        ...
    async def break_provider(self):          # make the service unreachable (stop the fake)
        ...
    async def expire_auth(self):             # make the stored credential fail (change the fake's key)
        ...                                  # set `expire_auth = None` if your module has no credentials
```

Adapt the harness to your fake, then run `python -m pytest plugins/my_switches`.

| Check | Proves |
|---|---|
| `identity` | lowercase `provider_id`, a `display_name` and a `description` |
| `discovery_refs_unique_and_own` | at least one device; refs unique and carrying your id |
| `capabilities_match_discovery` | `get_capabilities` agrees with discovery |
| `state_is_declared_and_valid` | state only for declared capabilities, values valid |
| `write_then_read_back` | each writable capability is set to two different values; CONFIRMED means `get_state` then shows that value |
| `health_reports_trouble_without_raising` | OK when working, DOWN when broken, never an exception |
| `unreachable_raises_instead_of_inventing_state` | an unreachable service raises `ProviderUnavailable`, never reports Off |
| `expired_auth_is_distinguishable` | rejected credentials raise `AuthExpired`; health says `AUTH_REQUIRED` |
| `missing_credentials_are_auth_required` | no credentials → `AUTH_REQUIRED` (needs `FakeSecrets` in the harness) |
| `unknown_device_is_an_error` | an unknown device raises, not empty state |

Checks that don't apply to your module (credentials, for one that has none)
report as *skipped*. The two credential checks need the harness to pass its
credentials as `FakeSecrets({...})` (from `hnb.providers.sdk.testing`), which lets
the kit remove them. The kit accepts `ACCEPTED` from `execute` (honest for
services that can't report state), so **if your service can read state back,
prove it in your own tests**, as the example's
`test_writes_are_confirmed_by_reading_the_device_back` does.

Your own tests use the same harness through `run_check`. The body receives the
harness (`h`, with whatever your harness stored, e.g. `h.hub`) and a started
adapter (`a`):

```python
def test_off_really_turns_it_off():
    async def body(h, a):
        ref = (await a.discover_devices())[0].ref
        result = await a.execute(ref, CapabilityType.POWER, False)
        assert result.status is ExecuteStatus.CONFIRMED
        assert h.hub.plugs[ref.external_id]["on"] is False    # look at the simulator itself (the example's fake)
    run_check(Harness, body)
```

The kit calls nothing before your body except your harness's `adapter()`; if
your module needs `start()` (a long-lived connection), call it there. On Windows
a connection to a closed local port takes about 2 s to be refused, so outage
tests run slower there than on Linux.

Replace the example's own test
(`test_capabilities_follow_the_device`) with tests for your mapping: both
directions of every write, how offline devices and errors surface, and that no
secret appears in a message. `python -m pytest` collects `plugins/`
automatically, so your module runs with the rest of the suite. Keep test file
names unique (`test_<your_id>.py`): plugin folders aren't packages, and two
files with the same name stop pytest from collecting. The core's own tests use
the built-in modules only, so your module never changes their results.

## 12. See it on the Home Hub

```bash
python plugins/my_switches/fake_device.py          # terminal 1: your simulator
python -m hnb.cli dev                              # terminal 2: the hub on http://127.0.0.1:8780
python -m hnb.cli setup-code                       # terminal 3: one-time code for the first sign-in
```

Run all three from the repository root. `setup-code` finds the dev hub's data
in `./var`. To use another port, start the hub with `HNB_PORT=8791` and use that
port below. (In PowerShell, set variables first: `$env:HNB_PORT="8791";
python -m hnb.cli dev`.) If you start the hub with `HNB_DATA_DIR`, give `setup-code` the same
`HNB_DATA_DIR`. To see only your module's devices, start the hub with the demo
off: `HNB_ENABLE_DEMO=0 python -m hnb.cli dev` (on a fresh `./var`). A fresh
hub's health reads *Needs attention* until its first backup; that is unrelated
to your module.

Open `http://127.0.0.1:8780`, create the owner, then **Settings → Services**.
Your module is listed with its description. **Set up** shows your settings and
credential fields. Fill them in (for the simulator, its address and dev key) and
**Connect**. The hub checks health and looks for devices straight away (**Find
devices** looks again later). Mark the new devices *This is mine* under
**Devices**, and they appear on the Home Hub with exactly the controls you
declared. Every tap goes through your `execute()`; the result and the activity
log show whether it was confirmed.

The same through the API (see `API.md`). Create the owner once, keeping the
session cookie in `var/jar` (inside the git-ignored `var/`; a cookie jar anywhere
else in the repository fails the isolation guard) and the CSRF token in `$CSRF`.
On later runs use `/api/auth/login` with `{"username", "passphrase"}` instead:

```bash
HUB=http://127.0.0.1:8780
CSRF=$(curl -s -c var/jar -H 'Content-Type: application/json' \
  -d '{"setup_code":"<from setup-code>","username":"dev","passphrase":"a long dev passphrase","house_name":"Dev"}' \
  $HUB/api/auth/bootstrap | python -c "import sys,json; print(json.load(sys.stdin.buffer)['csrf_token'])")
curl -s -b var/jar -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"provider_id":"my_switches","settings":{"host":"127.0.0.1","port":"7788"},"secrets":{"api_key":"<dev key>"}}' \
  $HUB/api/providers                                     # 200: the provider, its status and device count
curl -s -b var/jar $HUB/api/devices                      # your devices, UNREVIEWED; yours have
                                                         # technical.connections[].provider == "my_switches"
curl -s -b var/jar -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"status":"MINE"}' $HUB/api/devices/<device id>/review
curl -s -b var/jar -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"capability":"POWER","value":true}' $HUB/api/devices/<device id>/actions    # outcome CONFIRMED (POWER for the
                                                         # scaffold's plugs; use your module's capability)
curl -s -b var/jar -X POST -H "X-CSRF-Token: $CSRF" $HUB/api/providers/my_switches/discover   # look again (no body)
curl -s -b var/jar "$HUB/api/audit?limit=5"                  # newest first: COMMAND_CONFIRMED, COMMAND_REQUESTED, …
```

If your module doesn't appear, look at **Settings → Services**: a module that
failed to import or broke the contract is listed there with the reason (also
`GET /api/system` → `provider_load_errors`). One broken module never stops the
hub.

## 13. Optional parts of the contract

| Want | Implement |
|---|---|
| Live updates instead of waiting for the next poll | Call `self._notify_update()` when your service pushes a change (subscription, event). The hub refreshes now; polling stays the safety net. |
| Owner adds devices with a setup code | `supports_pairing = True`, `pairing_timeout_s`, and `async def pair(self, code) -> list[DeviceRef]`. The code is a credential: never log or echo it. New devices still start `UNREVIEWED`. |
| Non-secret local state (caches, pairing metadata) | Write under `self._storage` (a private directory, included in backups). Never put secrets there. |
| Credentials the hub can create itself | `initial_secrets()`. Only the demo uses this; real services get credentials from the owner. |

`reauthenticate()` is part of the contract for a future sign-in flow; this release
doesn't call it. When credentials stop working, the owner enters new ones in Settings.

The contract with every docstring is `hnb/providers/base.py`. The full reference
is `PROVIDERS.md`.

## 14. Ship it

- **In this repository:** keep the folder in `plugins/`. The installer copies
  `plugins/` into the image, so `git pull && sudo ./install.sh` on the hub
  deploys it.
- **As a separate Python package:** declare an entry point, and the hub finds it
  wherever it is installed:
  ```toml
  [project.entry-points."hnb.providers"]
  my_switches = "hnb_my_switches.adapter:MySwitchesAdapter"
  ```
  Add the package to `requirements.in` and regenerate the lock, because the image
  installs only hash-pinned packages.
- **Extra folders during development:** `HNB_PLUGIN_DIRS=/path/one:/path/two`
  (`;`-separated on Windows).

Provider modules run inside the hub with its privileges. Install only code you
trust (see `SECURITY.md`).

## Checklist

- [ ] `provider_id`, `display_name`, `description` set; the id will never change
- [ ] settings in `config_fields`, credentials in `secret_fields` + `required_secrets`
- [ ] only real capabilities declared; state values in the vocabulary's shapes
- [ ] `ProviderUnavailable` / `DeviceUnavailable` / `AuthExpired` raised as described; no secrets in messages
- [ ] `CONFIRMED` only after reading back; `ACCEPTED` otherwise
- [ ] `health()` never raises
- [ ] no blocking calls on the event loop; request timeouts under 8 s; no write retries
- [ ] conformance passes against the simulator; your own mapping tests added in `test_<your_id>.py`
- [ ] no real addresses, ids or keys anywhere in the folder
- [ ] the device appears in Settings → Services, connects, is found, and works on the Home Hub

# Providers

A **provider** is anything that can see or control devices: Matter, Home
Assistant, a brand's local API, a brand's cloud. The hub talks to each through
one **provider module**, an adapter implementing `ProviderAdapter`. This
document is the reference: how modules are found, the contract, and the rules
every module follows. The step-by-step walk-through is `ADDING_A_PROVIDER.md`.

**Scope of this release.** The Starter ships the platform: the SDK, the
conformance kit, a complete example module, and two built-in modules (the
fictional **demo** home, and **Matter** through its own controller container).
Modules for a particular house's brands and services are written by whoever
installs it, against this contract, without changing the core.

## Where modules come from

At start-up the hub loads, in order:

1. **Built-ins**: `demo`, `matter` (`hnb/providers/registry.py`, `BUILTIN`).
2. **Installed packages** that declare an entry point in the `hnb.providers` group:
   ```toml
   [project.entry-points."hnb.providers"]
   acme = "hnb_acme.adapter:AcmeAdapter"
   ```
3. **Plugin folders**: every `plugins/<name>/adapter.py` that defines `ADAPTER`
   (folders starting with `_` or `.` are skipped). `plugins/` ships in the repo
   and the image. More folders can be listed in `HNB_PLUGIN_DIRS`
   (`os.pathsep`-separated).

A module that fails to import, doesn't subclass `ProviderAdapter`, has an invalid
`provider_id` (`^[a-z][a-z0-9_]{1,31}$`) or clashes with an existing id is
skipped and reported (**Settings → Services**, and `GET /api/system` →
`provider_load_errors`). One bad module never stops the hub. Modules run inside
the hub with its privileges, so install only code you trust.

The **catalog** (`hnb/providers/catalog.py`, `KNOWN_PROVIDERS`) lists well-known
services that have no module yet (Alexa, Home Assistant, Google Home, Kasa,
Govee, Tuya, Lutron, Android TV, Roku, ONVIF cameras, Ring). They appear in
Settings as *Provider module not installed* and refuse to connect. The hub never
pretends to reach a service it has no code for. A module whose id matches a
catalog entry replaces that entry.

## The contract

`hnb/providers/base.py`, re-exported with everything a module needs by
`hnb.providers.sdk`.

| Member | Required | Purpose |
|---|---|---|
| `provider_id: str` | ✅ | stable, lowercase id, stored with every device; never change it once shipped |
| `display_name: str` | ✅ | shown to the owner |
| `description: str` | ✅ | one line in Settings → Services |
| `freshness_window: float` | 60 | seconds a `CONFIRMED` reading stays fresh before the Hub shows *Last known* |
| `config_fields: tuple[ConfigField]` | — | non-secret settings (`name, label, default, help, choices`); values are strings in `self._config` |
| `secret_fields: tuple[ConfigField]` | — | credentials the owner types; stored only in the vault |
| `required_secrets: tuple[str]` | — | names read through `self._secrets.get(name)` |
| `supports_pairing: bool`, `pairing_timeout_s: float` | False, 180 | the owner can add devices with a setup code |
| `async discover_devices() -> list[DiscoveredDevice]` | ✅ | everything the service can see |
| `async get_capabilities(ref) -> list[CapabilitySpec]` | ✅ | current capabilities (firmware can change them) |
| `async get_state(ref) -> list[StateReading]` | ✅ | current state of every declared readable capability |
| `async execute(ref, capability, value) -> ExecuteResult` | ✅ | perform a write; `value` is already validated |
| `async health() -> ProviderHealth` | ✅ | cheap liveness and auth check; never raises |
| `async pair(code) -> list[DeviceRef]` | with pairing | add a device with an owner-supplied code |
| `async start()` / `async close()` | — | open / release long-lived connections |
| `self._notify_update()` | — | tell the hub a change was pushed; it refreshes now |
| `initial_secrets()` | — | credentials the hub may mint itself (demo only) |
| `async reauthenticate() -> AuthResult` | — | reserved for a future sign-in flow; this release doesn't call it |

**What a module receives:** its own `DeviceRef(provider, external_id)` values,
a `SecretHandle` that can read only its own vault entries, its settings, and a
private `storage` directory for non-secret state (included in backups). **What
it never sees:** the hub's device ids, rooms, the owner, sessions, or any other
provider's settings or credentials.

**What the core does for every module:** the timeout on every call, per-device
write ordering (one command at a time per device; status reads wait too),
the owner review gate, value validation, retries of reads with backoff, and
the audit trail. Modules don't implement any of these.

## The rules

### Capabilities
Declare only what the device really has. The Hub draws a control for every
declared writable capability and nothing else. Values use the vocabulary's
shapes exactly (table in `API.md`, "Capability values"). `CAMERA_*` are
read-only whatever a module claims. `LOCK` is HIGH sensitivity: the hub
requires an explicit owner confirmation before your `execute` is called.

### Error model
| Raise | When | Result |
|---|---|---|
| `ProviderUnavailable` | the service can't be reached | the module's devices go `UNKNOWN` (*Can't reach*), never Off; health `DOWN` |
| `DeviceUnavailable` | the service answered: this device is offline or unknown | that device's capability goes `UNKNOWN`; the command `FAILED` |
| `AuthExpired` | credentials rejected or missing | health `AUTH_REQUIRED` (*Needs sign-in*) |
| `ProviderError` | anything else the service reports | the command `FAILED` with the message |
| `PairingError` | adding a device failed | shown in the *Add a device* sheet |

Messages are shown to the owner verbatim: plain words, and no secrets, tokens
or setup codes.

### Timeout model
The core wraps every adapter call in `HNB_PROVIDER_TIMEOUT_S` (default 8 s;
pairing gets `pairing_timeout_s` + 10 s). A timed-out write is reported as
*"No answer within 8s. The device may or may not have changed."* and that
capability's state becomes `UNKNOWN`. Modules should time their own requests out
sooner (about 5 s) with a clearer message, and must never block the event loop
(async libraries, or `asyncio.to_thread` for blocking SDKs).

### Retry guidance
The poller polls each connected provider every `HNB_POLL_INTERVAL_S` (30 s,
jittered ±10 %), backs off exponentially while it is failing (at most four
intervals), and re-checks health right after a success. Modules therefore don't
loop. One immediate retry of a **read** on a dropped connection is fine. **Never
retry a write** unless it is idempotent *and* you know the first attempt didn't
arrive: a duplicated toggle is a visible bug.

### State verification
`execute` returns `CONFIRMED` only after reading the device back, with what it
reported in `observed`. The core then records `CONFIRMED` if that equals the
request, or `ACCEPTED` with "The device reports a different value than
requested" if not. Without a read-back, return `ACCEPTED` and the state is shown
as *Not confirmed yet* (`ASSUMED`). Map device-native units (levels, mireds) to
canonical values before returning them, so a rounding step doesn't look like a
disagreement. The Matter module's tolerances are in D-023.

### Health
`health()` is cheap, never raises, and returns `OK`, `DEGRADED` (working but
slow or partial), `AUTH_REQUIRED`, `DOWN` or `UNKNOWN`, with a plain-words
`detail`. Missing credentials are `AUTH_REQUIRED`, not `DOWN`.

### Secrets
Credentials come only from the owner (`secret_fields`) or `initial_secrets()`.
They live only in the vault (XChaCha20-Poly1305, key outside the database and
outside backups) and are never returned by any API. The audit records credential
*names* only. Never write them to `storage`, logs, exceptions or messages.

## Conformance

`hnb.providers.sdk.conformance` holds ten checks every module must pass against
a simulator: identity, discovery, capability/discovery agreement, valid state,
read-back, health without raising, an unreachable service raises, expired
credentials are distinguishable, missing credentials read as `AUTH_REQUIRED`,
and unknown devices raise. A module supplies a small harness
(`adapter()`, `break_provider()`, `expire_auth` or `None`). See
`ADDING_A_PROVIDER.md`, step 11, and `examples/providers/acme_plug/`.
`tests/test_adapter_conformance.py` runs the same checks on the built-in demo
and Matter modules, and includes a deliberately broken adapter to prove the kit
fails when it should.

---

## Demo module

`hnb/providers/demo/`: a fictional Alexa-style house (`fixtures/alexa_style_house.json`)
with 14 devices, served entirely through the real backend with no network and no
real credentials. Its state persists in its `storage` directory. Its one setting,
**Simulate a problem** (`none` / `outage` / `expired sign-in`), switches it
between working, unreachable and needing sign-in, to show how the Hub reports
each case.

## Matter module

`hnb/providers/matter/` — `client.py` (WebSocket client), `mapping.py` (pure
Matter ⇄ canonical mapping), `adapter.py` (the contract).

```
matterjs-server v1.4.0 (own container, API on 127.0.0.1:5580 only)
  ⇅ WebSocket (python-matter-server-compatible API)
MatterServerClient — timeouts, ≤64 in flight, reconnect with backoff, node cache fed by events
MatterAdapter      — ProviderAdapter contract; refs are "<node>-<endpoint>"
  → Starter core → Home Hub
```

- **One Starter device per Matter endpoint that serves On/Off.** A two-gang
  switch is two devices; a bridge contributes one device per bridged endpoint,
  named from its Bridged Device Basic Information.
- **Capabilities only from what the endpoint serves** (Descriptor ServerList, and
  Colour Control feature bits) — never from the device type alone:

| Starter | Matter evidence | Command | Read-back |
|---|---|---|---|
| POWER | On/Off server (lights, plugs) | `On` / `Off` | `ep/6/0` |
| SWITCH | On/Off server on a switch/dimmer-switch device type | `On` / `Off` | `ep/6/0` |
| BRIGHTNESS | Level Control server + CurrentLevel | `MoveToLevelWithOnOff` (1–254 ⇄ 1–100 %) | `ep/8/0` |
| COLOR | Colour Control + HS feature + CurrentHue | `MoveToHueAndSaturation` (0–254 ⇄ °/%) | `ep/768/0`, `/1` |
| COLOR_TEMPERATURE | Colour Control + CT feature; range from physical min/max mireds | `MoveToColorTemperature` (kelvin ⇄ mireds, read as 50 K steps) | `ep/768/7` |

- **Truthful execution:** every command is followed by `read_attribute` from the
  device. CONFIRMED only when the device reports the requested value (within 1–2
  device units of rounding); a disagreeing device is reported with what it
  actually says; an unreadable device is ACCEPTED, never CONFIRMED.
- **Live state:** the Matter server's events (attribute changes, availability)
  trigger an immediate refresh through the contract's update listener; polling is
  the safety net. A powered-off device becomes *Can't reach* when the server marks
  it unavailable (≈5 min: subscription timeout + offline grace period); a command
  to it fails at once and its state becomes UNKNOWN.
- **Adding devices:** `pair(code)` → `commission_with_code` with
  `network_only: true` — the path for devices already on the network and shared
  from Alexa by multi-admin (see `pilot/ALEXA_MATTER_SHARING.md`). Bluetooth
  commissioning of factory-new devices is not part of this release.
- **Proven by:** mapping tests on fixture nodes, a fake Matter server
  (`tests/fake_matter_server.py`), the shared conformance suite, the HTTP owner
  flow, and a VM phase that commissions a real matter.js software light over the
  Matter protocol.

## Research: what is actually possible in an Alexa-heavy home

Researched 2026-09-25. **[V]** = read at the source. **[U]** = inference or a
secondary source. Re-check before relying on a line older than six months.

### Alexa — an adapter at most, never the core

- **Direction of control is Alexa → skill.** The Smart Home API ("add-ons" since
  2026) lets Alexa control *your* cloud's devices; it does not let your code
  control Alexa's. [V] <https://developer.amazon.com/en-US/docs/alexa/smarthome/understand-the-smart-home-skill-api.html>
- **A skill only discovers its own devices** — "the endpoints associated with the
  customer's device cloud account". [V] <https://developer.amazon.com/en-US/docs/alexa/device-apis/alexa-discovery.html>
- **No public API lists or controls arbitrary devices, rooms or groups in a
  consumer's Alexa account.** [V as far as the published docs go; an absence
  cannot be proven]
- The **Endpoints API** that does list devices and rooms is restricted to
  registered *Alexa Smart Properties* partners (hotels, senior living). [V]
  <https://developer.amazon.com/en-US/docs/alexa/alexa-smart-properties/endpoint-api.html>
- **Alexa for Apps** = voice deep-links into a phone app. **Alexa Connect Kit** =
  for device makers. Neither reaches existing devices. [V]
- **Routine trigger pattern (supported, one-way):** a private smart-home skill can
  expose virtual contact/motion sensors; an Alexa routine triggered by them can
  act on *any* Alexa device, including Zigbee paired to an Echo. No state comes
  back. Needs a public HTTPS endpoint + account linking, and each routine is
  built by hand in the Alexa app. [V]
  <https://developer.amazon.com/en-US/docs/alexa/device-apis/alexa-contactsensor.html>
- **Recent changes:** Smart Home API v2 removed 2025-11-01; Alexa Routines Kit
  discontinued 2026-05-13; custom routine triggers are a developer preview
  (en-US/en-CA). [V] <https://developer.amazon.com/en-US/docs/alexa/ask-overviews/deprecated-features.html>
- **Unofficial routes are rejected as a foundation.** `alexa_media_player` (media
  only, "Amazon may cut off access at any time"), `alexa-remote-control` (cookie
  sessions), and Home Assistant's `alexa_devices` (Amazon login + OTP, private
  app endpoints [U]). Fragile, and likely outside Amazon's terms. [V]

### Matter — the primary path for Alexa-controlled devices

- **Multi-admin works out of the Alexa app** for devices that are *Matter*:
  Device → *Other Assistants and Apps* → *Add Another* → 11-digit code, valid
  15 minutes. The hub becomes a second controller; **Alexa keeps working** for
  voice and routines. [V] <https://www.tp-link.com/us/support/faq/3573/>
- **Does not work for:** Zigbee devices paired to an Echo, or devices Alexa
  reaches through a brand's cloud skill. Those need their brand's own interface,
  or re-pairing. [U — follows from how Matter works]
- **Echo Thread border routers** (Thread 1.3): Echo Dot Max, Hub, Plus 2nd gen,
  Show 8 3rd gen, Show 10 3rd gen, Show 11, Show 15 2nd gen, Show 21, Studio,
  eero line. The hub can use them to reach Thread devices. [V]
- **Alexa's Matter support** is at the 1.5 SDK (2026-03) for plugs, lights,
  locks, covers, thermostats, sensors, fans, purifiers, appliances, vacuums —
  **not cameras**. [V] <https://developer.amazon.com/en-US/docs/alexa/smarthome/matter-support.html>
- **Spec:** 1.4 (2024-11) Enhanced Multi-Admin; 1.5 (2025-11) cameras over
  WebRTC and closures; 1.6 (2026-06) Joint Fabric. Matter cameras are only
  starting to ship; no certified Matter doorbell found yet. [V/U]
- **Controller on Debian:** `python-matter-server` is **archived** (8.1.2 final).
  Successor: **`matterjs-server`**, API-compatible, Apache-2.0, Docker image
  `ghcr.io/matter-js/matterjs-server:stable`, Matter 1.6, marked beta. Home
  Assistant switched to it 2026-06-23. [V] <https://github.com/matter-js/matterjs-server>
- **Host requirements:** IPv6 + mDNS/multicast reachable from the container →
  host networking. `chip-tool` is a test tool, not something to run a house on. [V]

### Home Assistant — optional compatibility bridge

- HA's Alexa integration goes **HA → Alexa only** (needs Nabu Casa, or Lambda +
  public HTTPS). It is not a way to read Alexa. [V]
- HA is useful when the owner **already runs it**, or when a brand has only an HA
  integration (GE Cync, Meross via `meross_lan`, Tuya local, Ring/Blink community
  libraries). The hub consumes HA over **WebSocket** (`auth` with a long-lived
  token, `get_states`, `call_service`, `subscribe_events`, registries) and REST. [V]
  <https://developers.home-assistant.io/docs/api/websocket/>
- Trade-off: a second system to update and back up, and HA says its Matter
  Server app must run on HAOS for supported installs. The Starter therefore
  **runs its own matterjs-server** and treats HA as optional, never required.

### Direct local interfaces worth an adapter

| Brand | Interface | Notes |
|---|---|---|
| Lutron Caséta / RA2 Select / RA3 | LEAP, local push | Best-in-class for switches. [V] |
| TP-Link Kasa / Tapo | local | Newer firmware needs TP-Link cloud credentials for local auth; some need "third-party compatibility" on. [V] |
| Govee | documented LAN API, local push | LAN control toggled per device in the Govee app. [V] |
| Leviton Decora 2nd gen | Matter (early-access firmware) | 1st gen will not get Matter. [V] |
| Ring doorbell | **Ring Partner API** (official) | Live view (WebRTC/WHEP, RTSP), motion/button webhooks, snapshots. A *private app* allows up to 5 allow-listed accounts — fits one homeowner. [V] <https://developer.amazon.com/docs/ring/get-started.html> |
| Google/Android TV | Android TV Remote v2, local | Keys, deep links, power. Not Fire TV. [V] |
| Fire TV | ADB over network | Needs developer options + network debugging. [V] |
| Roku | ECP, HTTP :8060 | Roku OS 14.1+: "Control by mobile apps" must be enabled. [V] |
| LG webOS / Samsung Tizen | local websocket | Pairing prompt on TV; Samsung not across VLANs. [V] |

### Recommended bridge architecture (ranked)

1. **Hub owns devices directly** — Matter multi-admin for every Matter device
   Alexa has (Alexa stays co-admin), plus brand-local adapters (Caséta, Kasa/Tapo,
   Govee, TVs). matterjs-server in a sibling container, host networking, IPv6.
2. **Home Assistant adapter** where the owner already runs HA or a brand has no
   better route.
3. **Official cloud APIs** only where nothing local exists (Ring Partner API
   private app, Tuya cloud, Cync).
4. **Move Echo-paired Zigbee** to a hub-owned coordinator (ZHA / Zigbee2MQTT) —
   requires factory-resetting each device; re-expose to Alexa over Matter.
5. **Stopgap:** private smart-home skill + virtual sensors → Alexa routines, for
   anything reachable only by Alexa. One-way; needs a cloud endpoint.
6. **Never:** cookie/private-API Alexa control as a dependency.

## Modules a house may need

Nothing below ships in this release. Each is a provider module written against
the contract above, in priority order for an Alexa-heavy home, following the
research:

| Module | Covers | Notes |
|---|---|---|
| `demo` | the fictional demo home | ✅ built-in |
| `matter` | Matter devices, including ones shared from the Alexa app | ✅ built-in (own controller container) |
| `home_assistant` | anything an existing Home Assistant already controls | designed: `docs/HOME_ASSISTANT_FALLBACK.md` |
| `lutron_leap`, `kasa`, `govee_lan` | switches, plugs, lights with local APIs | whichever brands the house has |
| `android_tv`, `roku` | TV power, volume, apps | local protocols |
| `ring_partner`, `onvif` | doorbells and cameras | live video needs a streaming-capable module |

`tools/classify_inventory.py` sorts a device inventory (CSV, kept outside the
repository) into DIRECT MATTER · VENDOR ADAPTER · HOME ASSISTANT BRIDGE ·
ALEXA-ONLY · UNKNOWN to decide which modules are worth writing first.

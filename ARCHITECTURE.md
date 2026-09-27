# Architecture

Home Network BRAIN Starter is a self-hosted home hub: **one app** for every room,
whatever brand or service controls each device. It runs on a small computer in
the house (Debian 13, Docker, x86_64). The public internet is never required.

## Principle: a provider-independent core

```
HOUSE → ROOM → DEVICE → CONNECTION → CAPABILITY → STATE → ACTION → HISTORY
```

| Concept | What it is | Code |
|---|---|---|
| **House** | The installation. One per hub. | `hnb/core/model.py` `House` |
| **Room** | Owner-named space. Suggested from a provider's rooms/groups, never forced. | `Room` |
| **Device** | One physical thing. Identity is a hub-minted UUID — **never** an IP, MAC or provider id. | `Device` |
| **Connection** | One provider's handle on a device: `(provider, external_id)`, unique per house. A device may have several (e.g. Matter *and* a brand cloud). | `Connection` |
| **Capability** | What the device can do, in the canonical vocabulary. Declared by the adapter. | `hnb/core/capabilities.py` |
| **State** | Last value per capability, with **freshness**: `CONFIRMED · ASSUMED · STALE · UNKNOWN`. | `StateValue` |
| **Action** | A requested write and its truthful outcome: `REJECTED · ACCEPTED · CONFIRMED · FAILED`. | `Action` |
| **History** | Append-only audit record of every discovery, review decision and action. | `HistoryEvent` |

Example — `Living Room → Ceiling Lights → matter:<id> → POWER, BRIGHTNESS → ON, 70% (CONFIRMED)`.

## Layers

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Web app  — Home Hub · Setup wizard · Settings               │  web/
 │  renders HomeView only; knows nothing about providers         │
 ├──────────────────────────────────────────────────────────────┤
 │  HTTP — network policy · session · CSRF · request shapes     │  hnb/api/app.py
 ├──────────────────────────────────────────────────────────────┤
 │  Service — the only orchestrator: one transaction per change │  hnb/service.py
 │  (model + audit events), polling, health, auth               │  hnb/auth.py
 ├──────────────────────────────────────────────────────────────┤
 │  Core — Home: discovery merge · review gate · capability      │  hnb/core/
 │  gate · execute with timeout + per-device lock · freshness    │
 │  · audit events (outbox) · HomeView projection                │
 ├──────────────────────────────────────────────────────────────┤
 │  Provider modules — one per provider, behind one contract     │  hnb/providers/
 │  built-in: demo · matter   ·   plugins/<name>/ · entry points │  plugins/
 ├──────────────────────────────────────────────────────────────┤
 │  Store — SQLite (WAL, FK, migrations) · secret vault · backup │  hnb/store/ hnb/security/ hnb/ops/
 └──────────────────────────────────────────────────────────────┘
```

A light switched from the Hub (M1, as tested end to end):

```
Hub tap → POST /api/devices/{id}/actions  (session + CSRF)
  → HubService.act → Home.prepare_action   validate, review gate → COMMIT (PENDING, COMMAND_REQUESTED)
  → Home.perform_action → adapter.execute  timeout, per-device lock
  → read-back → state CONFIRMED            → COMMIT (outcome, state, COMMAND_CONFIRMED)
  → response → Hub refreshes GET /api/home
```

**Dependency rule:** arrows point down only. Adapters import the core's
vocabulary; the core never imports a specific adapter; the web app never sees a
provider name except on Settings pages (`technical` block).

## The rules the core enforces (and the tests that prove them)

| Rule | Why | Test |
|---|---|---|
| Every discovered device starts **UNREVIEWED** and is invisible on the Hub | A hub on a shared network sees neighbours' TVs, printers, guests' phones | `test_everything_discovered_starts_unreviewed_and_off_the_hub` |
| Only **MINE** devices can be controlled | Same | `test_unclaimed_device_cannot_be_controlled` |
| A write for an undeclared capability is refused **before** any provider call | No fake buttons, no surprise side-effects | `test_unsupported_capability_is_rejected_before_any_provider_call` |
| Re-discovery never overwrites owner decisions or duplicates a device | Canonical identity | `test_rediscovery_never_overwrites_owner_choices_or_duplicates` |
| Provider down → state **UNKNOWN**, never OFF | A cloud outage is not a dark room | `test_provider_down_makes_state_unknown_not_off` |
| Timeout → **FAILED** with "may or may not have changed" | A sent command is not a performed action | `test_timeout_is_a_truthful_failure` |
| Accepted without read-back → **ACCEPTED**, state **ASSUMED** | Same | `test_accepted_without_readback_is_not_reported_as_confirmed` |
| HIGH-sensitivity capabilities (LOCK) need an explicit confirmation | Unlocking a door is not a light switch | `test_high_sensitivity_capability_needs_confirmation` |
| The Hub view exposes only declared controls and no provider names | No jargon on the main page | `test_hub_view_exposes_only_declared_controls_and_no_provider_names` |
| A slow status read never overwrites a newer command result | The poller and the owner act on the same device | `test_slow_poll_cannot_overwrite_a_newer_command_result` |

Concurrency: every provider call is wrapped in `asyncio.wait_for` by the core
(adapters are not trusted to time themselves out), and writes to one device are
serialised by a per-device lock that status reads take too. Background polling runs one task per provider,
with jittered intervals, exponential backoff (max 5 min) on failure, and clean
cancellation on shutdown.

## Capability vocabulary

`POWER · SWITCH · BRIGHTNESS · COLOR · COLOR_TEMPERATURE · VOLUME · MUTE ·
APP_LAUNCH · LOCK · THERMOSTAT · CAMERA_LIVE · CAMERA_EVENT`

Values are validated and normalised once, in the core (`validate()`), before an
adapter sees them. `CAMERA_*` are read-only regardless of what an adapter
claims. `LOCK` is `HIGH` sensitivity. Device-specific bounds (colour
temperature range, the TV's app list, thermostat modes) travel in
`CapabilitySpec.options`, and the UI draws exactly those.

## First-run wizard

Twelve steps. Each is re-entrant: it reads current state from the API, so a
step can be revisited after a reboot without redoing earlier ones.

| # | Step | Server side |
|---|---|---|
| 1 | Welcome | — |
| 2 | Create owner | `POST /api/auth/bootstrap`: only while no owner exists, home network only, one-time setup code; argon2id |
| 3 | Name home | part of bootstrap |
| 4 | Your network | `GET /api/system`: hostname, LAN address, IPv6 (needed by Matter); *Unknown* when the hub can't tell |
| 5 | Services | `GET/POST /api/providers`: installed modules with their settings and credential forms; catalog entries shown as *Provider module not installed* |
| 6 | Find devices | `POST /api/providers/{id}/discover` |
| 7 | Is this yours? | `POST /api/devices/{id}/review`: the gate for everything after |
| 8 | Rooms | `PATCH /api/devices/{id}`, prefilled from provider rooms; add, rename and delete rooms (`/api/rooms`) |
| 9 | Favorites | `PATCH /api/devices/{id}` `{"favorite": true}` |
| 10 | Remote access | off; not part of this release (the hub never opens itself to the internet) |
| 11 | Backup | `POST /api/backups`: first verified backup; nightly timer |
| 12 | Finish | `POST /api/setup/complete`; until then the Hub shows *Setup isn't finished* |

Steps 1–3 are the create-owner screen. Bootstrap is available **only** until an
owner exists, and only from the home network; afterwards it answers 409.

## Home Hub

One aggregated read (`GET /api/home` → `HomeView`, `hnb/core/view.py`), rendered as:

1. **Hero** — house name, one-line status in plain words, lights on, clock
2. **Rooms** — chips; tapping scopes the whole page to that room
3. **Favorites**
4. **Lights & switches** — tile toggles; dimmers/colour in a detail sheet
5. **TV & entertainment** — power, volume, mute, app chips (only those declared)
6. **Cameras** — last event; live video needs a camera module that supports streaming, and the card says so
7. **Locks, climate & more** — locks (with an in-app confirmation), thermostats (setpoint and mode), sensors and other devices, with only their declared controls
8. **Home system** — network devices, devices responding, each service in plain words, items to review, last backup

Large touch targets (≥44 px), rounded cards, phone → tablet → desktop, light and
dark themes. Freshness shows as plain words: *Can't reach*, *Not confirmed yet*,
*Last known*. Everything technical (system health, services, activity log)
lives under **Settings**.

The web app is three static files (`web/`) on the real API, with a strict CSP
and self-hosted fonts (DECISIONS D-018).

## Adding a device (any provider that supports pairing)

```
Settings → Add a service (e.g. Matter) → Add a device → setup code
  → POST /api/providers/{id}/pair            (session + CSRF; one at a time)
  → HubService.pair_device → adapter.pair(code)   (Matter: commission_with_code, network_only)
  → core discovery → new devices UNREVIEWED  → PAIRING_SUCCEEDED (code never recorded)
  → owner review → room → Home Hub
```

The core never learns what "Matter" is: pairing, config fields and adapter
start-up are optional parts of the provider contract.

## Persistence

SQLite in WAL mode with foreign keys on, one file (`/var/lib/hnb/data/hnb.db`).
Numbered SQL migrations (`hnb/store/migrations/`) run each in its own
transaction, with a pre-migration copy taken by SQLite's online backup API.
`quick_check` + `foreign_key_check` run at every start; backups run the full
`integrity_check`. `audit_log` is append-only by trigger. Provider secrets are
ciphertext only (`provider_secrets`); the key lives outside the database.

## Health

`GET /api/health` answers anonymously with only `status`, `version`,
`setup_required` (used by Docker and the installer). Signed in, it adds API
uptime, database (schema, journal mode, checks, size, audit count), vault
(entries, anything sealed with another key), each provider's status in plain
words, backup (age, verified), storage and poller state. Never a secret.

## Extension points

What can be extended without touching the core, and what needs a core change:

| To add | Where | Core change? | Guide |
|---|---|---|---|
| A provider (brand, cloud, bridge) | a provider module in `plugins/<name>/` or a package with an `hnb.providers` entry point | **no** | `ADDING_A_PROVIDER.md` |
| Provider settings and credentials | `config_fields` / `secret_fields` on the module; Settings draws the form | no | same |
| Adding devices by setup code | `supports_pairing` + `pair()` on the module; the *Add a device* sheet appears | no | same |
| Push updates | `_notify_update()` from the module | no | same |
| A service in the "not installed" list | `hnb/providers/catalog.py` | data only | — |
| A device kind | `DeviceKind` + `SECTION_OF`, optionally a card in `web/app.js` | small | `DEVELOPMENT.md` |
| A capability | `CapabilityType` + validator + description, a control in `web/app.js` | yes, the shared vocabulary | `DEVELOPMENT.md` |
| An API endpoint | service method + handler + tests; regenerate `API.md` | yes | `DEVELOPMENT.md` |
| A schema change | a new numbered migration | yes | `DEVELOPMENT.md` |

The hub finds provider modules at start-up (`hnb/providers/registry.py`). A
module that fails to load is reported, not fatal. The conformance kit
(`hnb.providers.sdk.conformance`) is the contract's executable form.

## Out of scope so far

Automations, scenes, energy, presence, floor plans. The existing Home Network
BRAIN has mature designs for all of these (see `DECISIONS.md` D-009); they are
ported only after the vertical slice ships.

# Home Network BRAIN Starter

**One app for the whole house.** Lights, switches, TVs, cameras, locks,
thermostats and the home network in one place, whatever brand or service runs
each device underneath. It is self-hosted on a small computer in the home, with
no public exposure and no mandatory cloud.

> **Release 0.2.0 (release candidate).** This is the platform: a
> provider-independent core, a documented API, a provider SDK with a
> conformance kit, the Home Hub web app, a 12-step setup wizard, auth, audit,
> health, encrypted credentials, backups and a Debian 13 installer. It ships
> with a fictional demo home and a Matter module. **Integrations for a
> particular house's brands are provider modules added by whoever installs it**
> (`ADDING_A_PROVIDER.md`), with no change to the core.

## Try it in two minutes (no Docker)

```bash
git clone <this repository> home-network-brain-starter && cd home-network-brain-starter
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m hnb.cli dev              # → http://127.0.0.1:8780
.venv/bin/python -m hnb.cli setup-code       # in another terminal: one-time code to create the owner
```

(On Windows: `.venv\Scripts\` in PowerShell, `.venv/Scripts/` in Git Bash.) The
wizard walks you through the fictional demo home: 14 devices in five rooms,
including a TV and a printer you'll mark *Not mine* and *Ignore*. **Settings →
Services → Demo home → Simulate a problem** shows how the Hub reports an outage
or an expired sign-in: *Can't reach*, never *Off*.

## Install on a Debian 13 box

```bash
git clone <this repository> home-network-brain-starter
cd home-network-brain-starter && sudo ./install.sh
```

Open the printed address from a phone on the home network and create the owner
with the code from `sudo /opt/hnb/bin/setup-code`. Details: `INSTALL.md`.

## What it does

- **Home Hub**: one page for phone, tablet or wall display, in light or dark.
  Rooms, favourites, lights and switches, TVs, cameras, locks and climate, and a
  home-system panel. Only controls a device really has are shown, and provider
  names stay in Settings.
- **Truthful state**: a command is *confirmed* only after the device is read
  back. An unreachable service shows *Can't reach*, never *Off*. Locks need an
  explicit confirmation.
- **The owner decides**: every discovered device starts unreviewed; only devices
  marked *This is mine* can be controlled.
- **Security by default**: one owner (argon2id), sessions with CSRF protection,
  home-network-only access, provider credentials encrypted in a vault whose key
  is never in the database or backups, an append-only audit log, and a
  non-root, read-only container.
- **Operations**: health in plain words, nightly verified backups, restore,
  upgrades that back up first and keep the previous release.
- **Extensible**: provider modules are one folder in `plugins/`, loaded at
  start-up, proven by a conformance kit.

## Documentation

| Document | For |
|---|---|
| [`RELEASE_NOTES.md`](RELEASE_NOTES.md) | What this release contains, and the handoff: what the recipient gets and must do |
| [`INSTALL.md`](INSTALL.md) | Installing on Debian 13, everyday commands, updating, what runs |
| [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md) | Backups, the vault key, restoring, rebuilding, rolling back |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Symptoms, causes and fixes; where the logs are |
| [`ADDING_A_PROVIDER.md`](ADDING_A_PROVIDER.md) | Step by step: a new provider module from scaffold to the Home Hub |
| [`PROVIDERS.md`](PROVIDERS.md) | Provider reference: loading, contract, error/timeout/retry/verification rules; Matter; research |
| [`API.md`](API.md) | The HTTP API: auth, CSRF, errors, action flow, capabilities, every endpoint, recorded examples ([`docs/openapi.json`](docs/openapi.json)) |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | Running locally, tests, the web app, adding endpoints/capabilities/device kinds, building, releasing |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Domain model, layers, core rules, extension points |
| [`SECURITY.md`](SECURITY.md) | Threat model, controls, known limitations |
| [`DECISIONS.md`](DECISIONS.md) | Why things are the way they are |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed |
| `pilot/` | For an Alexa-heavy home: sharing Matter devices from the Alexa app, and a device inventory template for `tools/classify_inventory.py` |

## How it's built

```
HOUSE → ROOM → DEVICE → CONNECTION → CAPABILITY → STATE → ACTION → HISTORY
```

A provider-independent core (`hnb/core`) talks to devices only through provider
modules (`hnb/providers`, `plugins/`), each implementing one contract. The Home
Hub renders one server-built view and never learns which provider is behind a
button. Python 3.13, FastAPI, SQLite, and three static web files with no build
step. Layout and conventions: `DEVELOPMENT.md`.

## License

MIT (see [`LICENSE`](LICENSE)). Use it, change it, build on it. Provider modules
you write for your own devices are yours.

## Not included in this release

Provider modules for specific brands (Alexa, Home Assistant, Kasa, Govee, Ring,
TVs and others are listed in Settings as *Provider module not installed*), live
camera video, remote access from outside the home, HTTPS on the LAN, multiple
users, automations and scenes. See `RELEASE_NOTES.md`.

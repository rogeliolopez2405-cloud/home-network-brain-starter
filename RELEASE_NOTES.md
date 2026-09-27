# Release notes — 0.2.0 (release candidate)

**Home Network BRAIN Starter** is a self-hosted home hub: one app for every room,
whatever brand or service runs each device. This release completes the
**platform**. Integrations for a particular house are provider modules added on
top of it, without changing the core.

## What is in this release

| Area | What you get |
|---|---|
| Core | Provider-independent domain model (house, rooms, devices, connections, capabilities, state, actions, history); truthful outcomes and freshness; owner review gate; per-device ordering; timeouts |
| API | Documented HTTP API: auth, home, rooms, devices, capabilities, actions, providers, discovery, health, audit, backups, system. OpenAPI schema; recorded examples (`API.md`) |
| Database | SQLite (WAL, foreign keys), numbered migrations with snapshots, integrity checks at start, append-only audit log |
| Provider SDK | Contract, capability vocabulary, error/timeout/retry/verification rules, conformance kit (10 checks), plugin loading, example module and scaffold (`hnb new-provider`) |
| Built-in modules | **demo** (a fictional 14-device house, no network) and **Matter** (own controller container; devices shared from the Alexa app by setup code) |
| Home Hub | One page for phone, tablet and wall display, light and dark; rooms, favourites, lights and switches, TVs, cameras, locks and climate, home system; only declared controls; provider names only in Settings |
| Setup wizard | 12 steps: welcome, owner, home name, network, services, discovery, review, rooms, favourites, remote access, backup, finish |
| Security | One owner (argon2id), HttpOnly sessions + CSRF, home-network-only access, encrypted credential vault (key outside database and backups), log redaction, hardened container, isolation guard against leaking a real home into the repository |
| Operations | Health in plain words, nightly verified backups, restore, upgrades with pre-upgrade backup and rollback, Debian 13 installer |
| Docs | README, INSTALL, BACKUP_RESTORE, TROUBLESHOOTING, ADDING_A_PROVIDER, PROVIDERS, API, DEVELOPMENT, ARCHITECTURE, SECURITY, DECISIONS, CHANGELOG |

## How it was verified

- The full test suite (unit, API, conformance, browser, isolation guard) passes
  in the working tree, in a fresh clone, and in a `git archive` export with no
  `.git` directory.
- The Home Hub was driven in a real browser against the real server: all 12
  setup steps, outage and expired-sign-in states, phone/tablet/desktop, light
  and dark, auth and CSRF probes, action round trips checked in the database
  and audit log. `tests/test_web_browser.py` keeps these checks automated, and
  `tools/ui_mutation.py` proves they catch regressions.
- Three times, a developer with only the repository added a new provider
  module (discovery, POWER, state, read-back, health, conformance, visible
  through the API and Home Hub) by following `ADDING_A_PROVIDER.md`, with no
  core changes and the full suite green with the module present. What each
  round found was fixed before the next; the third found nothing blocking.
- A fresh Debian 13 amd64 VM with no Docker installed the release from a
  `git archive` export and passed install, bootstrap, control, audit, a real
  browser on the installed Home Hub, outage, backup, reboot, restore, re-install
  and Matter commissioning (`.github/workflows/debian13-vm.yml`, 83 checks).
- CI repeats the suite, including the browser tests, from a clean checkout and
  from a `git archive` export, then builds and smoke-tests the image.

## Known limitations

No passphrase reset · single owner · plain HTTP on the home network · no remote
access · provider modules are not sandboxed · no live camera video without a
streaming-capable module · no way to remove a connected service (use
`--no-demo` on a real house). Details: `SECURITY.md`, `TROUBLESHOOTING.md`.

---

## Getting started (for whoever installs it)

### What this project is

A home hub you run on a small computer in your house. It shows every device you
choose in one app, controls them through **provider modules**, and tells the
truth about their state. It does not replace Alexa: Alexa keeps working for voice
and routines.

**The Starter ships the platform. Integrations for your house's specific
devices and services are your responsibility.** That means writing (or
obtaining) provider modules and pairing your own devices. The hub, its API, its
security and its operations are done; your house's brands are not built in.

### Install it

On a mini PC with Debian 13 (amd64), wired to your router:

```bash
git clone <this repository> home-network-brain-starter
cd home-network-brain-starter
sudo ./install.sh --no-demo        # --no-demo: a real house doesn't want the fictional devices
```

Open the address it prints from your phone, and create the owner with the code
from `sudo /opt/hnb/bin/setup-code`. The wizard takes it from there. Full guide:
`INSTALL.md`.

### Run demo mode

To see everything working with no real devices, run the demo on any computer
(no Docker, nothing installed system-wide):

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m hnb.cli dev            # http://127.0.0.1:8780
.venv/bin/python -m hnb.cli setup-code     # one-time code for the first sign-in
```

Or install on a spare box *without* `--no-demo`. **Settings → Services → Demo
home → Simulate a problem** shows an outage and an expired sign-in. A connected
service can't be removed in this release, so keep the demo off the hub you use
for the real house.

### Add a provider

Follow `ADDING_A_PROVIDER.md`:
`python -m hnb.cli new-provider <id>` copies a working example into
`plugins/<id>/`. You replace its protocol with your service's API, prove it with
the conformance kit, and it appears in **Settings → Services**. Rules and
reference: `PROVIDERS.md`. No core code changes.

### Connect real devices later

1. **Matter devices** work today. In the hub: **Settings → Services → Matter →
   Set up → Connect** (keep the default server address), then **Add a device**. In the Alexa app: the device →
   *Other Assistants and Apps* → *Add Another* → give the hub the 11-digit code
   within 15 minutes. Alexa keeps controlling it too. Steps and caveats:
   `pilot/ALEXA_MATTER_SHARING.md`.
2. **Everything else** needs a provider module: a brand's local API (Lutron,
   Kasa, Govee, TVs), a cloud API (Ring), or a bridge (Home Assistant, designed
   in `docs/HOME_ASSISTANT_FALLBACK.md`).
3. To decide what to build first, list your devices (Alexa app → Devices) in a
   CSV kept **outside** the repository (`pilot/inventory-template.csv`) and run
   `python tools/classify_inventory.py <your.csv>`. It sorts each device into
   DIRECT MATTER, VENDOR ADAPTER, HOME ASSISTANT BRIDGE, ALEXA-ONLY or UNKNOWN.
   Zigbee devices paired directly to an Echo can't be reached by any hub without
   re-pairing (`PROVIDERS.md`, research).
4. Every new device starts *unreviewed*. Mark yours *This is mine* and put them
   in rooms, and they appear on the Home Hub.

### Where the logs are

`/var/lib/hnb/logs/hnb.log`, `sudo docker logs hnb-hub-1`,
`sudo docker logs hnb-matter-1`, `journalctl -u hnb`, and in the app
**Settings → Activity** (every change, who made it, what the device said) and
**Settings → System health**. Logs never contain secrets. See
`TROUBLESHOOTING.md`.

### How backups work

Every night at about 3:30 the hub backs itself up, verifies the backup and keeps
the newest 14 (`/var/lib/hnb/backups`). **Settings → Backups → Back up now**
makes one on demand. Backups never contain the encryption key: save it once to a
USB stick with `sudo /opt/hnb/bin/export-key /media/usb/hnb-vault.key` and keep
it apart from the backups. Copy backups off the hub now and then. See
`BACKUP_RESTORE.md`.

### How to update

```bash
cd home-network-brain-starter && git pull && sudo ./install.sh
```

It backs up first, keeps the previous release, migrates safely and waits until
the hub is healthy. Your data, settings and key are kept.

### How to recover

| Problem | Do |
|---|---|
| Something looks wrong | **Settings → System health**, then `TROUBLESHOOTING.md` |
| Bad data or a bad change | `sudo /opt/hnb/bin/restore <backup>` |
| A bad update | roll back: `BACKUP_RESTORE.md`, "Roll back a bad upgrade" |
| The box died | reinstall on new hardware, put the saved key back, restore the newest copied-off backup (`BACKUP_RESTORE.md`, "Rebuild") |
| Forgot the passphrase | start over (`TROUBLESHOOTING.md`); keep it in a password manager |

### Intentionally not included

- Provider modules for your specific brands and services (Alexa has no public
  API for this; Home Assistant, Kasa, Govee, Tuya, Lutron, TVs, Ring and cameras
  are listed as *Provider module not installed*)
- Pairing, configuring or testing your physical devices
- Live camera video (needs a streaming-capable camera module)
- Remote access from outside the home, and HTTPS on the LAN
- Multiple users or roles, a passphrase reset
- Automations, scenes, energy, presence
- Removing a connected service from the app

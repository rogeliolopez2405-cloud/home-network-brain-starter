# Development

Everything a developer needs to run, test, change and ship the Starter. For the
design itself read `ARCHITECTURE.md` first. For adding a provider module,
`ADDING_A_PROVIDER.md`.

## Set up

Python 3.12 or newer (the image and CI use 3.13). No Docker is needed to develop.

```bash
git clone <this repository> home-network-brain-starter && cd home-network-brain-starter
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pip install -e ".[browser]"      # optional: real-browser UI tests (uses an installed Chrome)
```

On Windows use `Scripts` instead of `bin` (`.venv\Scripts\pip` in PowerShell,
`.venv/Scripts/pip` in Git Bash). Commands below write `python` for the
virtual environment's interpreter.

## Run it locally

```bash
python -m hnb.cli dev            # hub on http://127.0.0.1:8780 with the fictional demo home
python -m hnb.cli setup-code     # in another terminal: the one-time code for creating the owner
```

CLI commands read the same `HNB_*` settings as the hub: if you run the hub with
`HNB_DATA_DIR=...`, run `setup-code` with it too.

`dev` keeps data in `./var` (git-ignored), generates a local vault key, binds to
127.0.0.1, logs to the console and polls every 15 s. Delete `./var` to start over. Everything is
configured by environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `HNB_DATA_DIR`, `HNB_BACKUP_DIR` | `./var/data`, `./var/backups` in dev; `/var/lib/hnb/...` installed | the database and provider state; backups |
| `HNB_LOG_DIR` | unset: logs go to the console | when set, `hnb.log` rotates there (5 MB × 5); installed hubs use `/var/lib/hnb/logs` |
| `HNB_VAULT_KEY_FILE` | `./var/secrets/vault.key` in dev (generated); `/run/secrets/vault_key` in the container | the 32-byte key that seals provider credentials |
| `HNB_HOST`, `HNB_PORT` | `0.0.0.0` (`127.0.0.1` in dev), `8780` | where the API listens; the network policy still refuses callers outside the home network |
| `HNB_ENABLE_DEMO` | `1` | connect the fictional demo home on first start |
| `HNB_POLL_INTERVAL_S` | `30` (`15` in dev) | how often providers are polled |
| `HNB_PROVIDER_TIMEOUT_S` | `8` | the limit on every adapter call |
| `HNB_PLUGIN_DIRS` | — | extra provider-module folders (`os.pathsep`-separated) |
| `HNB_ALLOWED_NETWORKS` | home-network ranges | who may connect at all |
| `HNB_BACKUP_KEEP` | `14` | backups kept |
| `HNB_SESSION_IDLE_S`, `HNB_SESSION_ABSOLUTE_S`, `HNB_COOKIE_SECURE` | 7 d, 30 d, `auto` (Secure over HTTPS) | session policy |

The full list with parsing is `hnb/config.py`. Other CLI commands:
`python -m hnb.cli --help` (`serve`, `migrate`, `check`, `backup`,
`list-backups`, `verify-backup`, `restore`, `new-provider`, `provider-config`).

### The demo home

The demo provider (`hnb/providers/demo/`) serves a fictional house from
`fixtures/alexa_style_house.json`: 14 devices in five rooms (lights, a wall
switch, two TVs, a doorbell and a camera, a lock, a thermostat, a router, plus
an unknown TV and a printer for practising *Not mine* and *Ignore*). It goes
through the real backend like any provider. **Settings → Services → Demo home
→ Simulate a problem** switches it to an outage or an expired sign-in, so you
can see how the Hub reports trouble.

## Layout

```
hnb/
  core/          capabilities (vocabulary + validation) · model · home (the aggregate) · view (HomeView)
  providers/     base (the contract) · registry (loading) · catalog · sdk/ (public API + conformance kit)
                 demo/ · matter/ (built-in modules)
  store/         db (SQLite, migrations) · repo · migrations/NNNN_*.sql
  security/      vault · redact · netpolicy
  ops/           backup · system
  auth.py        owner bootstrap, sessions, CSRF, rate limits
  service.py     HubService, the only orchestrator
  api/app.py     HTTP layer (FastAPI)
  cli.py         every command above
web/             index.html · app.js · app.css · fonts/   (no build step)
plugins/         provider modules loaded at start-up (yours go here)
examples/        the example provider new-provider copies
deploy/          Dockerfile · compose.yaml · hnb.env.example · systemd units
bin/             the installed hub's commands (backup, restore, status, setup-code, export-key, hnb)
tools/           gen_api_docs · lock · release_check · ui_mutation · classify_inventory · denylist_add_secret
tests/           unit, API, browser, isolation guard; vm/ = Debian 13 acceptance
```

Layering is strict: HTTP (`hnb/api`) → service (`hnb/service.py`) → core
(`hnb/core`) → adapter (`hnb/providers`). Handlers hold no domain logic, the core
holds no SQL, and the core never imports a specific adapter.

## Tests

```bash
python -m pytest                             # everything: unit, API, conformance, examples, plugins, browser, guard
python -m pytest tests/test_core.py          # one file
python -m pytest plugins/my_switches         # one provider module
```

`pyproject.toml` already sets `-q`; adding another hides the summary line.

| Suite | What it proves |
|---|---|
| `test_core.py` | the core's rules: review gate, capability gate, truthful outcomes, freshness, concurrency |
| `test_api.py`, `test_api_surfaces.py`, `test_matter_api.py` | the HTTP API end to end (TestClient): auth, CSRF, network policy, every endpoint |
| `test_adapter_conformance.py` + `examples/`, `plugins/` | every provider module against the SDK conformance kit |
| `test_store.py`, `test_backup.py`, `test_vault_and_redaction.py` | migrations, integrity, backups, encryption, log redaction |
| `test_web_browser.py` | the Home Hub in Chrome against a live server: layouts at three widths, touch targets, no dead buttons, no console errors, outage wording, CSRF. Skipped unless `.[browser]` is installed |
| `test_api_docs.py` | `docs/openapi.json` matches the code; `API.md` covers every endpoint |
| `test_isolation_guard.py` | nothing from a real home is in the repository (see below) |
| `tests/vm/` | the full install on a fresh Debian 13 VM (run by GitHub Actions) |

Tests are hermetic: everything is built in temporary directories, providers run
against simulators, and nothing touches the network except loopback. The core's
test hubs load the built-in modules only (`adapter_classes=BUILTIN`), so modules
in `plugins/` never change core results; each module is tested by its own
`test_<id>.py`. `tests/test_scaffold.py` proves a scaffolded module passes the
kit next to the example.

`python tools/ui_mutation.py` undoes each guarded UI fix in turn and confirms
the browser tests fail without it.

### The isolation guard

`tests/test_isolation_guard.py` scans every file git would see (tracked, plus
untracked files that aren't git-ignored) and fails the build on private IP
addresses or home subnets, MAC addresses, tailnet names, vendor token shapes
(`VENDOR_SHAPES` in `hnb/security/redact.py`), private keys and certificates,
key/certificate/env files, database files, backup archives, cookie jars and
session cookies, personal home or cloud paths, agent workspace paths, and media
not listed in `tests/allowed-media.txt`. Keep local data in `./var` (ignored). It also reads an optional private denylist of your own
identifiers (`~/.hnb-starter/private-denylist.txt`, one per line, **never
committed**). Add a secret-shaped value to it with
`python tools/denylist_add_secret.py`. Use the documentation ranges
`192.0.2.0/24` and `203.0.113.0/24` in examples. Never weaken the guard to make a
change pass.

## Before you commit

```bash
python -m pytest
python tools/gen_api_docs.py        # after changing any endpoint (test_api_docs.py fails until you do)
node --check web/app.js             # if you have Node: catches syntax errors in the UI
git diff --check
git add <explicit paths>            # never `git add -A`: generated files and local data stay out
```

CI (`.github/workflows/ci.yml`) repeats this from a clean checkout **and** from a
`git archive` export, checks shell syntax, builds the image and smoke-tests it
read-only and non-root.

## The web app

Three static files served by the hub itself: `web/index.html`, `web/app.js`
(one ES module, no framework, no build) and `web/app.css`, plus self-hosted
fonts. The hub sends `Cache-Control: no-cache`, so a browser reload picks up
edits. A strict Content-Security-Policy allows no third-party requests. Keep it
that way: no CDNs.

The UI renders only what the API returns. The Hub reads one payload
(`GET /api/home`, built by `hnb/core/view.py`). Controls come from each device's
declared capabilities, and state words come from freshness (*Can't reach*,
*Not confirmed yet*, *Last known*). Provider names appear only in Settings.

## Common changes

### Add an API endpoint

1. Service method in `hnb/service.py` (raise `ServiceError(status, "plain words")` for failures).
2. Handler in `hnb/api/app.py` with `tags=[...]`, a `summary`, and a docstring. It
   depends on `owner` for reads and writes. CSRF is enforced for every write.
3. Tests in `tests/test_api_surfaces.py`.
4. `python tools/gen_api_docs.py` to regenerate `docs/openapi.json` and `API.md`.

### Add a capability

A capability is part of the vocabulary every provider and the UI share, so this
is a core change:

1. `hnb/core/capabilities.py`: add it to `CapabilityType`, give it a validator
   branch in `validate()`, a `DESCRIPTIONS` entry, and add it to `READ_ONLY` or
   `SENSITIVITY` if it is read-only or needs confirmation.
2. `hnb/providers/sdk/conformance.py`: add a sample value to `SAMPLE_VALUES` if
   it is writable.
3. `web/app.js`: render its control on the card for the device kinds that have
   it. A capability with no control is invisible, which is safe but useless.
4. Tests: validation in `tests/test_core.py`, rendering in `tests/test_web_browser.py`.
5. `python tools/gen_api_docs.py` (the capability table in `API.md` is generated).

The database stores capability names as text, so no migration is needed.

### Add a device kind

1. `hnb/core/model.py`: add it to `DeviceKind`.
2. `hnb/core/view.py`: map it to a Hub section in `SECTION_OF` (`lights`,
   `entertainment`, `cameras`, `network` or `more`). A missing entry fails loudly.
3. `web/app.js`: if it needs its own card or icon, add it where `cardFor`
   picks a card. Otherwise it gets the generic card with its declared controls.
4. Regenerate the API docs. Kinds are stored as text, so no migration is needed.

### Change the database

Add `hnb/store/migrations/NNNN_short_name.sql` (next number, lowercase name).
Each migration runs once, in its own transaction, after an online snapshot of
the database. Never edit a migration that has shipped; write a new one. The
schema version shows in health and in backups. `tests/test_store.py` has the
patterns.

### Change dependencies

Edit `requirements.in`, then `python tools/lock.py`. It resolves hash-pinned
binary wheels for CPython 3.13 on Linux x86_64 and aarch64 into
`requirements.lock`. The image installs with `--require-hashes`, so an
unlocked package can't sneak in.

## Build and deploy

```bash
docker build -f deploy/Dockerfile -t hnb-starter:dev .     # the image the installer builds
```

The real deployment is `install.sh` on Debian 13 (see `INSTALL.md`): it builds
the image, writes the systemd units and starts the hub under Docker Compose with
a read-only, non-root, capability-free container.

**Acceptance on a real VM.** `.github/workflows/debian13-vm.yml` boots the
official Debian 13 cloud image under QEMU/KVM, installs a `git archive` export
with `./install.sh --yes`, then drives the hub: bootstrap, sign-in, control,
audit, backup, reboot, outage, restore, re-install and a Matter commissioning.
It runs on every push to `main` or by hand (`gh workflow run debian13-vm.yml`).
Read the result from the run's `results.jsonl` artifact; one line per check.

## Release

```bash
HNB_REQUIRE_DENYLIST=1 python tools/release_check.py            # check only
HNB_REQUIRE_DENYLIST=1 python tools/release_check.py --package  # also write dist/hnb-starter-<version>.tar.gz
```

It refuses a dirty tree, runs the test suite on a `git archive` export, and runs
the isolation guard with your private denylist required. Bump the version in
`pyproject.toml` and `hnb/__init__.py`, update `CHANGELOG.md` and
`RELEASE_NOTES.md`, commit, then tag.

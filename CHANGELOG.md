# Changelog

## [0.2.0] — 2026-09-26 — Platform completion (release candidate)

The product boundary is settled: the Starter ships the platform, and
integrations for a particular house are provider modules the recipient adds.

### Added — platform
- **Provider SDK** (`hnb.providers.sdk`): the contract in one import, a
  conformance kit of 10 checks, `FakeSecrets` for harnesses.
- **Plugin loading**: `plugins/<name>/adapter.py`, the `hnb.providers` entry-point
  group, or `HNB_PLUGIN_DIRS`; a broken module is reported, never fatal.
- **Provider catalog**: well-known services without a module show *Provider
  module not installed* and refuse to connect.
- **Owner-entered credentials** (`secret_fields`): vault only, never returned.
- **Example provider** (`examples/providers/acme_plug`) with simulator and tests;
  `hnb new-provider <id>` scaffolds it into `plugins/`.
- **API**: rooms rename/delete, device detail and history, discovery, backups,
  system info, capability vocabulary, setup completion; OpenAPI for signed-in
  owners; `docs/openapi.json` and `API.md` generated from the running app with
  recorded examples and a drift test.
- **Web**: all 12 setup steps; Settings with services, per-provider settings and
  credentials, the demo simulator, backups and activity; a room editor; a
  *Locks, climate & more* section with an in-app lock confirmation and
  thermostat controls.
- **Demo**: backyard camera, front-door lock, thermostat; outage and expired
  sign-in selectable in Settings.
- **Browser tests** (`tests/test_web_browser.py`, `.[browser]` extra) and
  `tools/ui_mutation.py`, which proves each guarded UI fix is load-bearing.
- **Docs**: `ADDING_A_PROVIDER.md`, `DEVELOPMENT.md`, `TROUBLESHOOTING.md`,
  `RELEASE_NOTES.md`; `DISASTER_RECOVERY.md` became `BACKUP_RESTORE.md`.

### Fixed — found by three fresh-developer provider tests
Developers with only the repository built a provider module from
`ADDING_A_PROVIDER.md` (no core changes, each round's findings fixed first):
- The scaffold's test file collided with the example's and stopped pytest
  collecting; it is now `test_<id>.py`, with its own simulator port and no stale
  references (`tests/test_scaffold.py`).
- Core test hubs loaded the developer's `plugins/`, so one committed module
  broke the suite; test hubs, browser tests and the docs generator now use the
  built-in modules only.
- The SDK write check could pass an adapter whose writes did nothing; it now
  writes two different values. The example proves read-back in both directions,
  so every scaffold starts with that test.
- `setup-code` after `dev` looked in the installed data folder; it now finds a
  dev hub in `./var` and names the folder when it can't.
- Provider outages and expired credentials were in the audit log but not the
  hub log; health changes are now logged.
- Many doc fixes (POWER vs SWITCH, writing your own tests, stdlib HTTP, ports,
  cookie jars under `var/`, Windows and PowerShell notes, API examples).

### Added — release gates
- Isolation guard also fails on database files, backup archives, cookie jars
  and session cookies, the private denylist, agent worktree/scratchpad paths
  and personal cloud paths.
- Debian 13 VM acceptance has a `browser` phase: Chrome on the runner drives the
  installed hub after the reboot (sign-in, rendering at phone and desktop width,
  taps verified in the API and audit, CSRF refusal, no console errors).
- CI runs the browser tests from a clean checkout with `HNB_REQUIRE_BROWSER=1`.

### Fixed — found by real-browser validation
- Locks, thermostats and other devices never appeared on the Hub; a favourite
  lock rendered as a camera card.
- Rooms couldn't be added, renamed or deleted in the app.
- Switches and sliders were under the 44 px touch minimum; a disabled mute
  button looked tappable.
- The activity log showed raw JSON; room events had no name.
- During an outage the header claimed "0 lights on" while all were unknown.
- Every Hub refresh fetched `/api/home` twice.
- The wrong-setup-code message named a different command than the setup screen.
- A Matter test failed whenever a random UUID contained "1111".

### Added — Matter (M2 preparation)
- **Matter adapter** (`hnb/providers/matter/`): WebSocket client for
  matterjs-server, pure Matter ⇄ canonical mapping (POWER, SWITCH, BRIGHTNESS,
  COLOR, COLOR_TEMPERATURE — only what an endpoint serves), device read-back on
  every command, reconnect with backoff, event-fed state cache.
- **Pairing seam in the provider contract** (`supports_pairing`, `pair(code)`,
  `config_fields`, `start()`), provider-neutral: paired devices still start
  UNREVIEWED. API: `POST /api/providers`, `PATCH /api/providers/{id}`,
  `POST /api/providers/{id}/pair`. Audit: PROVIDER_SETTINGS_CHANGED,
  PAIRING_REQUESTED / SUCCEEDED / FAILED — the setup code is never stored.
- **Settings UI:** *Add a service* and *Add a device* (setup-code sheet with the
  Alexa sharing steps).
- **Matter server container** (matterjs-server v1.4.0, digest-pinned, loopback
  API, non-root, read-only); installer sets up IPv6/Thread route acceptance and
  the Matter data directory; hub moves to host networking.
- **Fake Matter server** for hermetic tests; conformance suite now runs the demo
  and Matter adapters; VM phase commissions a real matter.js software light.
- `pilot/ALEXA_MATTER_SHARING.md`, `docs/HOME_ASSISTANT_FALLBACK.md`,
  `tools/classify_inventory.py`.

### Fixed — Matter
- A refused Matter pairing always told the owner to "get a fresh code". The
  VM run showed the real cause can be device attestation (the CI light's test
  certificate): messages now say what happened and the server's reason is logged.

- After the Matter server restarted, its devices showed "Can't reach" for up to
  two minutes even while commands were confirmed (stale provider health). A
  successful command or pairing now re-checks that provider at once (VM run).
- A mistyped setup code now says "That code has a typo".
- A device that went offline was only noticed at the next poll, and a failed
  command left its old state on screen. Matter server events now refresh the Hub
  immediately (also making Alexa-made changes appear live), and a command the
  device didn't carry out marks its state UNKNOWN (VM run of e977676).

### Changed — Matter
- The GitHub evidence artifact that contained an obsolete VM test cookie was
  deleted (source and CI history untouched).


## [0.1.0] — 2026-09-25 — M1: installable server

### Added
- **API server** (FastAPI): `/api/health`, `/api/session`, `/api/home`,
  `/api/devices`, `/api/rooms`, `/api/providers`, `/api/audit`,
  `/api/auth/{bootstrap,login,logout}`, `POST /api/devices/{id}/review`,
  `POST /api/devices/{id}/actions`, `PATCH /api/devices/{id}`, `POST /api/rooms`.
  Layering: HTTP → `HubService` → core → adapter.
- **Owner auth**: setup-code bootstrap that closes permanently, argon2id, hashed
  session tokens, idle/absolute expiry, CSRF tokens, rate limiting.
- **SQLite persistence**: WAL, foreign keys, numbered transactional migrations
  with pre-migration snapshots, integrity checks, append-only audit triggers.
- **Audit log**: actor, action, target, requested value, result, time and source
  for every discovery, review, room/name/favorite change, command
  (REQUESTED → CONFIRMED / ACCEPTED / FAILED / REJECTED), provider health change,
  sign-in, backup and restore.
- **Encrypted provider-secret vault** (XChaCha20-Poly1305) with the key outside the
  database; log redaction with self-test.
- **Demo provider as a real adapter**: vault token, persistent simulated world,
  failure injection through provider config.
- **Background polling** per provider with timeouts and backoff; an outage shows
  *Can't reach*, never *Off*, and degrades only that provider's devices.
- **Web app** on the real API: first-owner setup, sign-in, guided review, Home Hub,
  rooms, devices, Settings (health, services, activity).
- **Installer** for Debian 13 x86_64; Docker Compose deployment (non-root, read-only,
  no capabilities, bounded); systemd service and nightly backup timer.
- **Backup / restore** (`bin/backup`, `bin/restore`): verified archives with a
  SHA-256 manifest, key excluded; `DISASTER_RECOVERY.md`.
- **Isolation gate**: generic checks + private out-of-repo denylist;
  `tools/release_check.py` packaging gate.
- **Debian 13 VM acceptance** workflow (install → use → reboot → outage → restore →
  re-install) and CI (tests from checkout and `git archive`, image build, container
  smoke test).
- A site-survey checklist for the first pilot home (kept with the pilot, not published).

### Fixed
- **Parallel requests could corrupt reads (HTTP 500):** the auth dependency ran in
  a worker thread and shared the event loop's SQLite connection. The dependency
  is now async and the connection refuses cross-thread use, so any regression
  fails loudly in tests (found in local browser testing of 41e8c8e).
- Hub kept showing an ended outage after navigating back: Home and Rooms now
  always refetch; HTML/JS/CSS are served with `Cache-Control: no-cache` so an
  upgraded hub never runs stale JavaScript.
- During an outage the hero said "0 lights on"; it now reports unknown lights
  separately, and unreachable devices show a neither-on-nor-off toggle.
- Poll backoff capped at 4 × the poll interval so a recovered service shows as
  working within ~2 minutes instead of up to 5.
- A status poll that started before a command could land after it and restore
  the old value (found in review; per-device lock now covers reads).
- Installer listed Docker bridge addresses as places to open the hub; it now
  prints the default-route LAN address (and `.local` only when mDNS runs).
- VM evidence upload no longer includes the test session cookie.
- After a provider outage, capabilities without readable state (TV app launch,
  camera live view) stayed UNKNOWN forever and the device looked unreachable.


## [0.0.1] — 2026-09-25 — Phase 0

### Added
- Canonical domain model: House, Room, Device, Connection, CapabilitySpec,
  StateValue (with freshness), Action (with truthful outcome), HistoryEvent.
- Capability vocabulary with one validator: POWER, SWITCH, BRIGHTNESS, COLOR,
  COLOR_TEMPERATURE, VOLUME, MUTE, APP_LAUNCH, LOCK (HIGH sensitivity),
  THERMOSTAT, CAMERA_LIVE, CAMERA_EVENT (read-only).
- `ProviderAdapter` contract and a conformance suite every adapter must pass.
- `Home` core: discovery merge that never overwrites owner decisions, review
  gate (UNREVIEWED → MINE / NOT_MINE / IGNORED), capability gate, per-device
  write lock, core-enforced timeouts, append-only history.
- `HomeView` projection — the only shape the Hub renders.
- Demo adapter + fictional Alexa-style house fixture; failure injection
  (down / auth / slow / no read-back).
- Web shell: Home Hub, 12-step setup wizard, Settings (activity log, outage
  simulation, theme), light and dark themes, phone → desktop.
- Isolation guard test with positive and negative controls.
- Docs: README, ARCHITECTURE, PROVIDERS (with Alexa/Matter/HA research),
  INSTALL, SECURITY, pilot notes, DECISIONS, CHANGELOG.

### Not yet
- API server, owner auth, SQLite, vault, installer, real adapters — see
  `ARCHITECTURE.md` milestones.

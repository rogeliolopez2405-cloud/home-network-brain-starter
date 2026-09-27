# Decisions

Newest last. Each entry: what was decided, why, and what would change it.

### D-001 — Clean-room repository, production untouched (2026-09-25)
New repo; no files copied from the production Home Network BRAIN. Production
was read once, read-only, to inventory reusable patterns (see D-009).
**Why:** the template must be installable in a stranger's home with nothing of
the author's home inside it. **Enforced by** `tests/test_isolation_guard.py`.

### D-002 — Python 3.12+ core, same family as production (2026-09-25)
**Why:** production HNB is Python 3.13 + FastAPI; the Matter/HA ecosystem is
Python/TS-friendly; patterns port without translation. Core (`hnb/core`,
`hnb/providers/base.py`) uses the standard library only.

### D-003 — Alexa is an adapter, not the core; Matter is the primary path (2026-09-25)
**Why:** Amazon publishes no API for a third party to list or control devices in
a consumer's Alexa account (PROVIDERS.md). Matter multi-admin is supported by
the Alexa app and leaves Alexa working. **Rejected:** cookie/private-API Alexa
control — fragile and likely outside Amazon's terms. **Would change if:** Amazon
opens a consumer device-inventory/control API.

### D-004 — Run our own matterjs-server; Home Assistant optional (2026-09-25)
**Why:** `python-matter-server` is archived; `matterjs-server` is its
API-compatible successor. Requiring HA doubles what the owner must run and
update. HA stays a first-class *optional* adapter.

### D-005 — Truthful state and outcomes are part of the domain model (2026-09-25)
Freshness `CONFIRMED/ASSUMED/STALE/UNKNOWN`; outcomes
`REJECTED/ACCEPTED/CONFIRMED/FAILED`. **Why:** a provider outage is not a dark
room; a sent command is not a performed action.

### D-006 — Phase 0 web shell is one static page; production stack decided in M1 (2026-09-25)
**Why:** the shell's job in Phase 0 is to fix the information design and the
HomeView contract, not the framework. Production HNB's web app is React 18 +
TypeScript + Vite; M1 either adopts that stack (to reuse its sanitised generic
components) or keeps no-build static pages if the Hub stays this small. The
HomeView JSON contract does not change either way.

### D-007 — POWER and SWITCH are both kept (2026-09-25)
Both boolean. SWITCH marks a wall switch driving a load (possibly a dumb bulb);
POWER is the device itself. **Why:** owners describe them differently, and a
switch may later gain a "load type". The Hub renders both as the same toggle.

### D-008 — In-memory store in Phase 0; SQLite in M1 behind the same `Home` API (2026-09-25)
**Why:** get the rules right and tested before committing a schema.

### D-009 — Reuse by re-implementation, after sanitisation review (2026-09-25)
Production code classified SAFE GENERIC is re-implemented or ported file-by-file
with a review, never bulk-copied. Anything touching auth/crypto
(`passkeys`, `secrets_store`, `secretbox`) gets a line-by-line review first.

### D-010 — Licence undecided (2026-09-25)
Marked proprietary / all rights reserved until the owner chooses. Decide before
the first external install. *Superseded by D-034.*

### D-011 — Vault crypto: XChaCha20-Poly1305 via PyNaCl (revised in M1, 2026-09-25)
*Supersedes the Phase 0 plan (AES-256-GCM via `cryptography`).* libsodium's AEAD
has 192-bit nonces (random nonces are safe without a counter), ships wheels for
every platform we build or develop on — so vault tests run everywhere, not only
in Linux CI — and supports associated data for slot binding. Reviewed against
production's AES-GCM secret box: kept versioned framing, key id and fail-closed
behaviour; fixed an ambiguous AAD separator, a key file written without
permissions, and a silent key overwrite.

### D-012 — FastAPI + stdlib sqlite3, no ORM (2026-09-25)
Same family as production, smallest dependency set (18 locked packages). The
repository layer (`hnb/store/repo.py`) holds all SQL; the domain never imports it.

### D-013 — In-memory aggregate, write-through per change (2026-09-25)
`HubService` mutates the in-memory `Home`, then persists the changed objects and
the emitted audit events in one SQLite transaction. If the commit fails the model
is reloaded from disk. One process owns the database (lock file).
**Would change if:** several processes need to write.

### D-014 — Commands are persisted before the provider is called (2026-09-25)
`prepare_action` → commit (PENDING + COMMAND_REQUESTED) → provider call → commit
the outcome. A crash mid-call leaves a PENDING row that start-up turns into
FAILED ("may or may not have changed") — never a silent gap.

### D-015 — First-owner bootstrap needs a setup code from the hub's disk (2026-09-25)
LAN-only was not enough: anyone on the Wi-Fi could race the owner to the setup
page. The code proves admin access to the hub. The installer never prints it;
`sudo bin/setup-code` shows it on request.

### D-016 — Network policy enforced in the app (2026-09-25)
Docker-published ports bypass the host firewall, and binding to one LAN address
breaks when DHCP changes it after a reboot. The app refuses any caller outside
loopback / RFC 1918 / link-local / ULA / 100.64/10, judged from the socket peer only.

### D-017 — The demo provider is a real provider (2026-09-25)
Connected like any other (registration, vault token, polling, audit); keeps its
simulated devices' state in its own storage so restarts behave like a real home.
The frontend has no demo code paths.

### D-018 — Web app stays no-build (2026-09-25)
Resolves D-006: three static files served by the hub, a strict CSP with no inline
script or style, self-hosted OFL fonts. Revisit if the UI outgrows one module.

### D-019 — The isolation guard has a private, out-of-repo tier (2026-09-25)
Real identifiers (hostnames, tailnet, device serials, volume names) cannot be
listed in the repository that must not contain them. They live in
`~/.hnb-starter/private-denylist.txt` (secrets as SHA-256 only); findings print
line numbers, never values; packaging refuses to run without the file.

### D-020 — Debian 13 VM acceptance runs on GitHub Actions (2026-09-25)
The development machine is Windows on ARM with no virtualization. A private
repository's x86_64 runner boots the checksum-pinned official Debian 13 cloud
image under QEMU/KVM on every push to `main`.

### D-021 — Hub and Matter server on host networking; Matter API on loopback (2026-09-25)
Matter needs host networking (IPv6 link-local, mDNS, UDP to devices; bridge
networking doesn't work). matterjs-server's WebSocket API is unauthenticated, so
it binds to 127.0.0.1 only, and the hub joins the host network to reach it. The
hub's port stays guarded in the app (D-016). Verified in the VM run with `ss`.

### D-022 — M2 adds devices by network commissioning only (2026-09-25)
The pilot keeps Alexa; Matter devices are shared from the Alexa app with a setup
code and are already on the network, so `network_only: true` is sufficient and
needs no Bluetooth. BLE commissioning of factory-new devices is a later release.

### D-023 — Read-back tolerance and 50 K colour-temperature steps (2026-09-25)
Matter units (levels 0–254, mireds) don't map one-to-one onto percent and kelvin.
A command is CONFIRMED when the device reports the sent value within 1–2 device
units, and kelvin readings are snapped to 50 K, so a light set to 3000 K or
6500 K doesn't read back as 3003 K / 6490 K on the next poll.

### D-024 — Home Assistant adapter designed, not built (2026-09-25)
`docs/HOME_ASSISTANT_FALLBACK.md`. Built only if the pilot inventory shows
important devices that Matter and brand adapters can't reach.

### D-025 — Matter fabric storage is a credential, kept out of ordinary backups (2026-09-25)
`/var/lib/hnb/matter` holds the hub's Matter operational keys. Like the vault
key it is excluded from nightly backups; losing it means re-sharing devices from
the Alexa app (about a minute each), not losing the house.

### D-026 — Production attestation policy stays strict; CI opts in to test certificates (2026-09-25)
matterjs-server refuses devices whose attestation certificate is a Matter *test*
certificate. The CI virtual light has one, so the VM run first proves the refusal,
then applies `tests/vm/compose.test-dcl.yaml` (`ENABLE_TEST_NET_DCL=true`) for the
rest of the test. The production compose file has no such switch.

### D-027 — The Starter ships the platform; house integrations are provider modules (2026-09-26)
Integrations for a specific house (its switch brand, doorbell, TVs) are not
part of the product. The release delivers the core, API, SDK, conformance kit,
example module, Home Hub, wizard, security, operations and docs; the recipient
adds provider modules for their devices. The product stops depending on any one
home's inventory, and "done" is measurable without physical access to a house.

### D-028 — Provider modules load from folders and entry points, in-process (2026-09-26)
`plugins/<name>/adapter.py` (ships in the image) and the `hnb.providers`
entry-point group, plus `HNB_PLUGIN_DIRS` for development. A module that fails
to import or breaks the contract is skipped and reported (Settings, `/api/system`),
never fatal. Modules run in the hub's process: the contract already scopes what
they receive (own refs, settings, vault entries), and a process sandbox is a
large design for a single-owner home hub. Documented as a limitation in
`SECURITY.md`; there is no API for uploading code.

### D-029 — Credentials are declared by modules and entered by the owner (2026-09-26)
`secret_fields` makes Settings draw credential inputs; values go straight to the
vault, are never returned by any API (not even to the owner), and a blank field
keeps the stored value. The audit records credential names, never values.

### D-030 — Uninstalled providers are listed, not hidden (2026-09-26)
The catalog shows well-known services as *Provider module not installed*, and
connecting one is refused (409). An owner looking for "Alexa" learns the honest
state, and nothing pretends to connect.

### D-031 — API docs are generated from the running app (2026-09-26)
`tools/gen_api_docs.py` starts a throwaway hub, makes real requests and records
the responses into `API.md`; `docs/openapi.json` is the app's own schema. A test
fails when either drifts. Environment facts (hostname, LAN address) are pinned
to fictional values and tokens are redacted before recording, because recorded
examples otherwise capture the machine that generated them. Documentation
addresses use RFC 5737 ranges, which the isolation guard allows.

### D-032 — The UI is tested in a real browser, against a live server (2026-09-26)
API tests could not see that locks never rendered or that touch targets were too
small. `tests/test_web_browser.py` drives Chrome (Playwright, the system Chrome
where present) against uvicorn on a real port. It is an optional extra and skips
without Playwright, so the core suite stays dependency-light. CI installs it.
`tools/ui_mutation.py` reverts each guarded fix to prove the tests catch it.

### D-033 — No passphrase reset in this release (2026-09-26)
A reset path is an authentication bypass unless it proves physical control of
the hub. Until one is designed (likely a new setup code read on the hub itself),
forgetting the passphrase means starting over, which is documented in
`TROUBLESHOOTING.md`.

### D-034 — Open source under the MIT licence (2026-09-27)
*Supersedes D-010.* The Starter is a platform other people are meant to build
on, so it is published openly under the MIT licence: anyone may use, change and
redistribute it, including commercially, with the copyright notice kept. The
public edition leaves out notes about the first private pilot installation.

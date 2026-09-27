# Security

This is built as a distributable product that runs inside strangers' homes.
Defaults are the security posture — most owners will never change them.

## Threat model (short)

| Asset | Threat | Control |
|---|---|---|
| Physical devices (locks, doors, lights) | Unauthenticated or forged writes | Owner auth on every write; CSRF tokens; HIGH-sensitivity capabilities need an explicit confirmation step |
| Provider tokens (Ring, TP-Link, HA…) | Theft from disk or backups | Encrypted vault, key outside DB and backups, least-privilege scopes |
| The hub | Exposure to the internet | Binds LAN + loopback only; no port forwarding; remote access only via Tailscale, opt-in |
| Neighbours' devices | Hub controls something that isn't the owner's | Every discovery starts UNREVIEWED; only MINE devices are controllable |
| Audit trail | Tampering / silent actions | Append-only history of every discovery, review and action, with actor |
| Provider modules | Malicious or buggy module code | Modules load only from the release (`plugins/`, the image) or installed packages; there is no upload API; each sees only its own refs, settings and vault entries; a broken module is reported, not fatal. **Not sandboxed**: see limitations |
| This repo | Leaking a real home into the template | `tests/test_isolation_guard.py`: private IPs/subnets, MACs, tailnet names, credential shapes, key/cert files, personal paths, unlisted media, plus a private out-of-repo denylist |

## Requirements and where they live

| Requirement | Design | Status |
|---|---|---|
| No embedded secrets | Nothing secret in repo, image or config; isolation guard (generic checks + private out-of-repo denylist) with positive and negative controls; `tools/release_check.py` refuses to package without the denylist | ✅ M1 |
| Owner authentication | One owner; argon2id (t=3, 64 MiB, p=4), NFC-normalised 10–256 character passphrase, dummy-hash timing for unknown names, hash upgrade on login | ✅ M1 |
| First-owner bootstrap | Only while no owner exists, only from the home network, only with the one-time setup code the hub writes to its own data directory (`sudo bin/setup-code`); code deleted on use; a DB trigger refuses a second owner | ✅ M1 |
| Sessions | 256-bit token in an `HttpOnly; SameSite=Strict` cookie (`Secure` over HTTPS), only its SHA-256 stored, 7-day idle / 30-day absolute expiry, logout deletes it | ✅ M1 |
| Authenticated writes / CSRF | Every write needs a session **and** its CSRF token in `X-CSRF-Token`; cross-origin `Origin` refused; login and bootstrap require JSON | ✅ M1 |
| Brute force | Failed logins/bootstraps limited per client (5 per 15 min), uniform failure message, audited | ✅ M1 |
| Encrypted provider tokens | XChaCha20-Poly1305 (libsodium), random 192-bit nonce, associated data binds each ciphertext to its (provider, name) slot, key fingerprint per row; key file 0400 outside the DB; the DB or a backup alone cannot decrypt; values never returned by any API | ✅ M1 |
| Log redaction | Managed-value + credential-shape redaction on every log line and traceback; self-test at start; lines withheld if the self-test fails | ✅ M1 |
| Least privilege | Container non-root, read-only filesystem, `cap_drop: ALL`, `no-new-privileges`, memory/PID limits; adapters see only their own refs and secrets | ✅ M1 |
| Network exposure | The app answers only home-network/loopback/overlay callers; forwarded headers never trusted; no port forwarding; strict CSP, no framing, no third-party requests | ✅ M1 |
| Audit trail | Append-only `audit_log` enforced by DB triggers: actor, action, target, requested value, result, time, source | ✅ M1 |
| Safe backups | WAL-safe online copy, `integrity_check` + `quick_check` + FK check, SHA-256 manifest, 0600 archives, key excluded, traversal-safe restore | ✅ M1 |
| Rollback-able updates | Pre-upgrade verified backup; previous release kept; pinned image digest and dependency hashes | ✅ M1 (manual rollback) · automatic M4 |
| Owner-entered credentials | Modules declare `secret_fields`; values go straight to the vault, are never returned by any API (not even to the owner), a blank field keeps the stored value, and the audit records credential *names* only | ✅ M2 |
| API schema | The OpenAPI schema is served only to a signed-in owner; generated docs redact tokens | ✅ M2 |
| UI verification | The Home Hub is tested in a real browser against a live server: no console errors, no failed requests, no dead controls, CSRF refused from the page | ✅ M2 |
| Matter controller | Separate container; unauthenticated API bound to 127.0.0.1 only; non-root, read-only, no capabilities; fabric credentials excluded from ordinary backups; setup codes never stored or logged | ✅ M2 prep |
| Optional remote access | Tailscale, opt-in | M4 |
| HTTPS on the LAN | Not in M1, see below | M4 |

## Known limitations (this release)

- **Plain HTTP on the home network.** The passphrase and session cookie cross
  the LAN unencrypted. Acceptable for a first pilot on a trusted home network;
  HTTPS arrives with Tailscale Serve / local certificates in M4.
- **Single owner.** Household members share the owner sign-in until roles land.
- **No TOTP or passkeys yet.**
- **Rate limits reset on restart** (kept in memory).
- **No passphrase reset.** Forgetting it means starting over (`TROUBLESHOOTING.md`).
- **Provider modules run in the hub's process** with its privileges (inside the
  hardened container). A module can read what the hub can. Install only modules
  you have reviewed; a sandbox is future work.
- **No remote access.** The hub answers only on the home network; Tailscale is planned.

## Truthfulness is a security property

The hub never reports a door as locked because it *sent* a lock command. Outcomes
are `ACCEPTED` until state is read back (`CONFIRMED`). A provider that can't be
reached makes state `UNKNOWN`, not OFF/UNLOCKED. See `ARCHITECTURE.md`.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Security → Report a
vulnerability** on this repository, not in a public issue. Include what you
found, how to reproduce it, and what it affects. Reports are acknowledged, fixed
in a release, and credited if you wish.

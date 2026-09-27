# Install

## Supported host

| | Supported | Notes |
|---|---|---|
| OS | **Debian 13 (trixie)** | Other Debian-likes: `--allow-unsupported`, untested |
| CPU | **x86_64 (amd64)** | arm64 images build (the lockfile covers it) but are untested |
| Runtime | Docker Engine + Compose v2 | From Debian's own packages; the installer offers to install them |
| Hardware | mini PC / NUC, 1 GB RAM free, 2 GB disk free | The hub uses < 150 MB RAM |
| Network | wired Ethernet on the home LAN | IPv6 on the LAN is needed for Matter devices |

## Install

```bash
git clone <this repository> home-network-brain-starter
cd home-network-brain-starter
sudo ./install.sh
```

The installer tells you what it will do, asks before installing packages
(`docker.io docker-cli docker-compose docker-buildx`, nothing else), and ends
with the address to open. `--yes` answers yes; `--no-demo` starts without the
fictional demo home; `--port N` changes the web port.

Open the address from a phone or computer **on your home network**. The first
screen asks for a one-time **setup code**; show it on the hub with:

```bash
sudo /opt/hnb/bin/setup-code
```

Create the owner (sign-in name + passphrase), then tell the hub which of the
discovered devices are yours.

## What the installer does

| Step | What | Idempotent? |
|---|---|---|
| Preflight | Debian 13, amd64, root, 2 GB free, release files present | read-only |
| Docker | Installs the four Debian packages **only with your approval**; enables `docker.service` | skipped if present |
| Account | System user `hnb` (no login shell) | created once |
| Directories | `/var/lib/hnb/{data,backups,logs}` 0750, `/var/lib/hnb/secrets` 0700, owned by `hnb` | yes |
| Vault key | 32 bytes from `/dev/urandom` → `/var/lib/hnb/secrets/vault.key` (0400). **Never overwritten, never printed.** | created once |
| Release | Copies this release to `/opt/hnb` (previous kept as `/opt/hnb.prev`) | replaced each run |
| Config | `/etc/hnb/hnb.env` from `deploy/hnb.env.example` (root:hnb 0640, nothing secret) | created once; version kept current |
| Image | Builds `hnb-starter:<version>`: base pinned by digest, Python deps by SHA-256 | each run |
| Upgrade safety | If a hub already exists: **verified backup first**, then stop it | each run |
| Database | Create or migrate (in a transaction, with a pre-migration snapshot), integrity check | yes |
| Services | `hnb.service` (starts at boot), `hnb-backup.timer` (nightly) | yes |
| Health | Waits for the container to report healthy | — |

## Everyday commands

```bash
sudo /opt/hnb/bin/status                 # service, health, newest backup
sudo /opt/hnb/bin/backup                 # backup now (verified)
sudo /opt/hnb/bin/restore <file>         # restore (stops, verifies, restores, restarts)
sudo /opt/hnb/bin/hnb list-backups
sudo /opt/hnb/bin/hnb check              # full database integrity check
sudo /opt/hnb/bin/export-key <file>      # copy the vault key for disaster recovery
sudo systemctl restart hnb               # restart
```

## Updating

```bash
cd home-network-brain-starter && git pull && sudo ./install.sh
```

The installer takes a **verified backup first**, keeps the previous release in
`/opt/hnb.prev`, migrates the database in a transaction (with a snapshot), and
waits for the hub to report healthy. Your owner account, rooms, devices, history,
settings and the vault key are kept. To go back, see
[`BACKUP_RESTORE.md`](BACKUP_RESTORE.md), "Roll back a bad upgrade".

## Where things live

| Path | What |
|---|---|
| `/opt/hnb` | the installed release (previous one in `/opt/hnb.prev`) |
| `/etc/hnb/hnb.env` | configuration: port, options (nothing secret) |
| `/var/lib/hnb/data` | the database (`hnb.db`) and provider modules' non-secret state |
| `/var/lib/hnb/backups` | verified nightly backups (newest 14) |
| `/var/lib/hnb/logs` | `hnb.log` |
| `/var/lib/hnb/secrets/vault.key` | the key that decrypts saved provider credentials (0400; never in backups) |
| `/var/lib/hnb/matter` | the hub's Matter credentials (never in backups) |

Backups, the key and recovery: [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md). Problems and
logs: [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

## What runs

Two containers, both on host networking:

- `hnb-matter-1` — the Matter controller (matterjs-server v1.4.0, digest-pinned).
  Its API listens on **127.0.0.1 only** (it has no authentication); non-root,
  read-only, no capabilities. Its data (`/var/lib/hnb/matter`) holds the hub's
  Matter credentials. The installer enables IPv6 route advertisements on the
  LAN interface (`/etc/sysctl.d/60-hnb-matter.conf`) so Thread devices behind an
  Echo border router are reachable.
- `hnb-hub-1` — the hub:

- runs as the `hnb` account (non-root), read-only root filesystem, all Linux
  capabilities dropped, `no-new-privileges`, 512 MB memory and 128-process
  limits, logs capped at 3 × 10 MB;
- listens on port 8780 (host network) and **answers only callers on the home network**
  (loopback, RFC 1918, link-local, IPv6 ULA, Tailscale's 100.64/10) — enforced
  in the app, because Docker-published ports bypass the host firewall;
- makes no outbound connections except to providers you connect (the demo
  home makes none). Fonts and scripts are served by the hub itself.

## Develop or try it without Docker

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
python -m hnb.cli dev          # http://127.0.0.1:8780, data in ./var
python -m hnb.cli setup-code   # in another terminal, for the first sign-in
```

More in [`DEVELOPMENT.md`](DEVELOPMENT.md).

## Verified on

The acceptance run boots the official Debian 13 genericcloud amd64 image in a
fresh VM with no Docker, installs a `git archive` export with
`./install.sh --yes`, and checks bootstrap → sign-in → Hub → control → SQLite →
audit → backup → **reboot** → health/auth/state/audit preserved → outage →
restore → re-install → Matter commissioning of a software light.
See `.github/workflows/debian13-vm.yml` and `tests/vm/`.

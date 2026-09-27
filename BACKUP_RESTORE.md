# Backup and restore

How the hub protects its data, how to back up and restore, and how to rebuild a
hub on new hardware.

## What makes a hub

| Piece | Where (installed hub) | In a backup? |
|---|---|---|
| **Database**: owner account, rooms, devices, review decisions, state, full history | `/var/lib/hnb/data/hnb.db` | ✅ yes (WAL-safe online copy, integrity-checked) |
| **Configuration**: port, account ids, options (nothing secret) | `/etc/hnb/hnb.env` | ✅ yes (`config/hnb.env`) |
| **Provider state**: each provider module's non-secret local files (caches, pairing metadata) | `/var/lib/hnb/data/providers/` | ✅ yes (`providers/…`) |
| **Vault key**: decrypts saved provider credentials | `/var/lib/hnb/secrets/vault.key` | ❌ **never**, on purpose |
| **Matter credentials**: the hub's Matter fabric (operational keys) | `/var/lib/hnb/matter/` | ❌ **never**, like the key |

The vault key is left out deliberately. A backup that gets copied or lost must
not unlock the services the home is signed into. Keep the key somewhere
**different** from the backups.

Each archive (`hnb-backup-<UTC time>.tar.gz`, mode 0600) holds `manifest.json`
with a SHA-256 for every file, the database's `integrity_check` / `quick_check` /
foreign-key results, the app version, the schema version and the vault key's
*fingerprint* (never the key).

## Automatic backups

`hnb-backup.timer` runs every night at 03:30 (± 30 min). Each backup is verified
after it is written, and the newest 14 are kept (`HNB_BACKUP_KEEP` in
`/etc/hnb/hnb.env`). The Home Hub's *Home system* panel and **Settings → System
health** show the last backup; health turns *Needs attention* if backups stop or
fail verification. Every backup is recorded in the activity log.

## Back up now

| From | How |
|---|---|
| The app | **Settings → Backups → Back up now** (also setup step 11). **Check** re-verifies any listed backup. |
| The hub's terminal | `sudo /opt/hnb/bin/backup` |
| The API | `POST /api/backups`, `GET /api/backups`, `POST /api/backups/{file}/verify` (see `API.md`) |
| Development | `python -m hnb.cli backup`, `list-backups`, `verify-backup <file>` |

## Save the key once, offline

```bash
sudo /opt/hnb/bin/export-key /media/usb/hnb-vault.key
```

Store that USB stick (or a printout of `xxd -p hnb-vault.key`) somewhere safe and
offline. The command never prints the key.

## Copy backups off the hub

A hub that dies takes its local backups with it. Copy them off now and then:

```bash
scp <you>@<hub>:/var/lib/hnb/backups/hnb-backup-*.tar.gz ./
```

## Restore

Restoring replaces the whole database, so it runs on the hub, not from the app:

```bash
sudo /opt/hnb/bin/restore                          # lists available backups
sudo /opt/hnb/bin/restore hnb-backup-<UTC>.tar.gz  # or a path to a copied-in archive
```

It verifies the archive first, stops the hub, snapshots the current database to
`backups/pre-restore-<time>.db`, restores, starts the hub and waits until it is
healthy. The owner signs in with the same name and passphrase as at backup time.
When the hub starts again it records the restore in the activity log
(*backup restored*, with the archive's details). A restore refuses to run while
the hub is running (it holds `hnb.lock`). To undo a restore,
restore the backup taken just before it; the `pre-restore-*.db` snapshot is a
last-resort copy of the database that was replaced.

In development: `python -m hnb.cli restore <file>` with the dev hub stopped.

## Rebuild on new hardware

1. Install Debian 13 and run the installer as usual (see `INSTALL.md`).
2. **If you saved the key**, put it back *before* restoring:
   ```bash
   sudo systemctl stop hnb
   sudo install -m 0400 -o hnb -g hnb /media/usb/hnb-vault.key /var/lib/hnb/secrets/vault.key
   ```
3. Copy the newest backup to the hub and restore it:
   ```bash
   sudo /opt/hnb/bin/restore /path/to/hnb-backup-<UTC>.tar.gz
   ```

**Without the key** everything still comes back (owner, rooms, devices,
history), but saved provider credentials show *Needs sign-in* and must be
entered again in Settings. Health reports the vault as `locked` until they are.

**Matter devices** on a rebuilt hub are shared again from the app that controls
them (for Alexa: Other Assistants and Apps → Add Another → code, about a minute
each). To avoid that, copy `/var/lib/hnb/matter/` offline together with the
vault key while the hub is stopped.

## Roll back a bad upgrade

Every re-run of `install.sh` takes a verified backup first and keeps the previous
release in `/opt/hnb.prev`. To go back:

```bash
sudo systemctl stop hnb
sudo mv /opt/hnb /opt/hnb.bad && sudo mv /opt/hnb.prev /opt/hnb
sudo sed -i "s/^HNB_VERSION=.*/HNB_VERSION=<previous version>/" /etc/hnb/hnb.env
sudo /opt/hnb/bin/restore <the pre-upgrade backup>
```

The previous image is still on the host (`docker images hnb-starter`), so nothing
is rebuilt.

## Checks

```bash
sudo /opt/hnb/bin/status                                    # service, container health, newest backup, timer
sudo /opt/hnb/bin/hnb check                                 # full SQLite integrity check
sudo /opt/hnb/bin/hnb verify-backup /var/lib/hnb/backups/<file>
```

What is verified on every run is proven by `tests/test_backup.py` and by the
Debian 13 VM acceptance run (backup → outage → restore → state preserved).

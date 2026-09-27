# Troubleshooting

Start with what the hub says about itself:

```bash
sudo /opt/hnb/bin/status        # service, container health, newest backup, backup timer, /api/health
```

In the app, **Settings → System health** shows each part in plain words, and
**Settings → Activity** shows every change, who made it and what the device said.

## Where the logs are

| What | Where |
|---|---|
| The hub's own log | `/var/lib/hnb/logs/hnb.log` (rotates at 5 MB, 5 kept) |
| Hub container output | `sudo docker logs hnb-hub-1` (capped at 3 × 10 MB) |
| Matter controller | `sudo docker logs hnb-matter-1` |
| Service start/stop | `journalctl -u hnb` · `journalctl -u hnb-backup` |
| Development | the terminal running `python -m hnb.cli dev` |

Logs never contain passphrases, tokens, setup codes or provider credentials.
Values that look like secrets are redacted before a line is written.

## Reaching the hub

| Symptom | Cause | Fix |
|---|---|---|
| The page doesn't load at all | Wrong address, hub down, or the phone isn't on the home network | Use the address the installer printed (`http://<hub-ip>:8780`). Check `sudo /opt/hnb/bin/status`. Put the phone on the home Wi-Fi, not mobile data. |
| *"This hub only answers on the home network."* (403) | The caller's address is outside the allowed ranges (loopback, 10/8, 172.16/12, 192.168/16, link-local, IPv6 ULA, 100.64/10) | Connect from the home network. For an unusual LAN range, set `HNB_ALLOWED_NETWORKS` in `/etc/hnb/hnb.env` and `sudo systemctl restart hnb`. Never add public ranges. |
| After an update the app looks old | Browser cache | Reload the page. The hub sends `no-cache`, so one reload is enough. |

## Signing in

| Symptom | Cause | Fix |
|---|---|---|
| *"That setup code isn't right."* | Typo, or an old code | Read it again: `sudo /opt/hnb/bin/setup-code`. It is 12 characters, works once, and needs no dashes. |
| *"Setup is closed: this hub already has an owner."* | The owner was created already | Sign in instead. |
| *"Too many attempts. Try again in N minutes."* (429) | 5 failures in 15 minutes from that device | Wait. The limit is per device and resets on success. |
| Forgot the passphrase | There is no reset in this release | Starting over is the only way: `sudo systemctl stop hnb`, move `/var/lib/hnb/data` aside, and run `sudo ./install.sh` again from the release folder. It creates a new database and setup code. Rooms, devices and history start fresh, and provider credentials must be entered again. Keep the passphrase in a password manager. |
| Signed out unexpectedly | 7 days idle or 30 days since sign-in | Sign in again. |

## Devices and services

| What the Hub says | What it means | What to do |
|---|---|---|
| *Can't reach* | The hub can't get a trustworthy reading, because the service or the device doesn't answer. It never means "off". | Check **Settings → Services**. If the whole service is *Can't reach*, the problem is between the hub and that service (network, the service's own hub, its cloud). If only one device is, check its power and Wi-Fi. |
| *Needs sign-in* | The service rejected the stored credential (expired, rotated, revoked) | **Settings → Services → the service → Set up**: enter the new credential and **Save**. A blank field keeps the stored one. |
| *Not confirmed yet* | The command was sent but the device hasn't been read back | Usually clears at the next poll. If it stays, the provider module can't read that value back. |
| *Last known* | The last reading is older than the service's freshness window | The hub is still polling; it retries with backoff while a service struggles. |
| A command says *"No answer within 8s. The device may or may not have changed."* | The provider timed out | Look at the device. The hub marks its state unknown until the next good reading. |
| *"This needs a confirmation step first."* | Locks need an explicit confirmation | Use the lock's button, which asks first. API callers send `"confirm": true`. |
| A service shows *Provider module not installed* | The hub has no code for that service yet | That is expected. A developer adds it as a provider module (`ADDING_A_PROVIDER.md`). Matter devices can often be shared to the hub instead (below). |
| A device isn't on the Home Hub | It hasn't been marked *This is mine*, or it's *Not mine* / *Ignored* | **Devices** → mark it *This is mine*. Nothing appears on the Hub until you do. |
| A device appears twice | Two services see the same device | Mark the copy you don't use *Ignore*. |

## Provider modules (developers)

| Symptom | Cause | Fix |
|---|---|---|
| My module isn't listed in Settings | It failed to load. Settings lists it with the reason, as does `GET /api/system` → `provider_load_errors`. | Common reasons: `adapter.py` doesn't define `ADAPTER`; `provider_id` isn't 2–32 lowercase letters, digits or `_`; the id clashes with another module; an import error. Modules load at start-up, so restart the hub after fixing. |
| Connect says *"Check …"* or 400 | A setting isn't one of the module's `choices`, an unknown setting was sent, or a credential is missing | Match the module's `config_fields` / `secret_fields`. |
| Conformance check fails | The adapter breaks a contract rule | The check's name says which (`ADDING_A_PROVIDER.md`, step 11). |
| The hub feels stuck while my module works | A blocking call on the event loop | Use async libraries or `asyncio.to_thread`. |

## Matter

| Symptom | Cause | Fix |
|---|---|---|
| *Add a device* fails with a code | Codes from the Alexa app last 15 minutes and work once | Generate a new code (Alexa app → device → Other Assistants and Apps → Add Another) and add it within the window. |
| Refused for attestation | The device's certificate isn't a production Matter certificate | The hub keeps attestation strict on purpose. Use a certified device. |
| Pairing times out | The hub can't reach the device over IPv6 / mDNS | Setup step 4 (network) shows whether IPv6 is on. The hub needs wired Ethernet on the same LAN as the devices, and IPv6 enabled on the router. |
| A powered-off Matter device still shows its old state for a few minutes | The Matter server marks a device unavailable only after its subscription times out (about 5 minutes) | Expected. Commands to it fail immediately, and its state becomes unknown. |
| All Matter devices *Can't reach* | The Matter controller container is down | `sudo docker logs hnb-matter-1`; `sudo systemctl restart hnb`. |

Background: `pilot/ALEXA_MATTER_SHARING.md` and `PROVIDERS.md`, "Matter".

## Health says *Needs attention*

**Settings → System health** names the part:

| Part | Typical cause | Fix |
|---|---|---|
| Services | A service is *Can't reach* or *Needs sign-in* | See "Devices and services" above. |
| Backup | No verified backup in the last 48 hours. A new hub reads this way until its first backup. | **Settings → Backups → Back up now** or `sudo /opt/hnb/bin/backup`; check `journalctl -u hnb-backup`. See `BACKUP_RESTORE.md`. |
| Saved sign-ins: *locked* | Credentials were sealed with a different vault key (restored onto a hub without the original key) | Put the original key back (`BACKUP_RESTORE.md`), or enter the credentials again in Settings. |
| Storage | Under 1 GB or 5 % free | Free disk space. Old backups beyond `HNB_BACKUP_KEEP` are deleted automatically. |
| Database: *error* | The database failed its integrity checks | Stop and restore the newest verified backup (`BACKUP_RESTORE.md`). |

## After a power cut or reboot

The hub starts on boot (`hnb.service`). If it doesn't come back within a couple
of minutes:

```bash
systemctl status hnb docker
sudo journalctl -u hnb -n 50
sudo docker logs hnb-hub-1 --tail 50
```

The database is checked at every start (`quick_check` and foreign keys). Rooms,
devices, history and the owner's session survive restarts. That is part of the
VM acceptance test.

## An update went wrong

Every `sudo ./install.sh` takes a verified backup first and keeps the previous
release in `/opt/hnb.prev`. Roll back with the steps in `BACKUP_RESTORE.md`,
"Roll back a bad upgrade".

## Development

| Symptom | Fix |
|---|---|
| `python -m hnb.cli dev` says the port is in use | Another hub is running: stop it, or set `HNB_PORT`. |
| Start fresh | Stop the dev hub and delete `./var`. |
| `test_isolation_guard` fails | A tracked file contains something that looks like real home data (a private IP, MAC, key or credential shape, a personal path). Use documentation addresses (`192.0.2.x`) and fake values. Don't weaken the guard. |
| `test_api_docs` fails | An endpoint changed: `python tools/gen_api_docs.py`. |
| Browser tests are skipped | `pip install -e ".[browser]"`. They use an installed Chrome, or `python -m playwright install chromium`. |

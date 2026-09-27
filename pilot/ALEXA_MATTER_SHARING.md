# Keep Alexa, add the Home Hub — sharing Matter devices

**Nothing is removed from Alexa.** A Matter device can have more than one
controller at the same time ("multi-admin"). Sharing a device with the Home
Network BRAIN hub adds the hub as a *second* controller: Alexa voice commands,
routines and the Alexa app keep working exactly as before, and the hub can
control the same device directly on your home network.

## Which devices can be shared this way

| The device… | Can the hub control it via Matter? |
|---|---|
| shows **"Other Assistants and Apps"** in its Alexa settings | ✅ Yes — it's a Matter device |
| has a Matter logo (the three-arrow symbol) on it or its box | ✅ Almost always — check for the Alexa option above |
| is a **Zigbee** device paired straight to an Echo (no brand app) | ❌ No. Alexa is its only controller; Matter sharing doesn't exist for it |
| is only in Alexa through a brand's skill (e.g. an older Wi-Fi plug with its own app) | ❌ Not via Matter. It may be reachable through that brand's own interface later |

We do **not** claim the hub can control Alexa-only Zigbee devices or skill-only
cloud devices. An inventory (`pilot/inventory-template.csv` + `tools/classify_inventory.py`) sorts every device into
what's possible.

## Share one device (about a minute)

On the phone that has the Alexa app, and the hub open in a browser:

1. **Home Hub:** Settings → Connected services → **Matter** → **Add a device**.
   (First time only: Settings → *Matter* → **Connect**.)
2. **Alexa app:** **Devices** → pick the device → tap the **gear** →
   **Other Assistants and Apps** → **Add Another**.
3. Alexa shows an **11-digit setup code**. It's valid for **15 minutes**.
4. **Home Hub:** type the code → **Add device**. Adding can take up to two
   minutes.
5. The device appears under **Devices → Waiting for review**. Tap **This is mine**,
   choose its room, and it shows up on the Home Hub.

Try it: flip it from the hub, then ask Alexa — both work.

## If it doesn't work

| What you see | What to do |
|---|---|
| No "Other Assistants and Apps" option | The device isn't Matter; it can't be shared this way. |
| "Get a fresh code…" | Codes expire after 15 minutes or after one use — tap **Add Another** again for a new one. |
| "The device didn't answer in time" | Make sure it's powered and online in Alexa; try again from the same room as the hub's network. |
| Thread devices (some bulbs/plugs) won't add | They are reached through an Echo that acts as a Thread border router (Echo 4th gen, Echo Hub, Show 8/10/15…). Keep that Echo on and try again. Wi-Fi Matter devices are the simplest first test. |

## To stop sharing later

Alexa app → the device → gear → **Other Assistants and Apps** → remove the
hub's entry. Alexa keeps full control; only the hub loses it.

## What the hub does and doesn't see

- It sees the device's own reported state (on/off, brightness, colour) and
  confirms every change by reading the device back — the Hub never shows a
  change as done just because it sent the command.
- Changes made with Alexa show up on the Hub within a second or two.
- A device that loses power shows **Can't reach** after about five minutes —
  that's Matter's own offline detection (a missed check-in plus a grace period so
  brief Wi-Fi blips don't flap). Tapping it before then fails straight away and
  says so; the Hub never shows it as off.
- It doesn't see Alexa's rooms, groups or routines (Amazon offers no API for
  that). Rooms are set once in the hub.
- The setup code is used once and never stored or logged.

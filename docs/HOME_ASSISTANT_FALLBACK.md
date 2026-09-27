# Home Assistant adapter — fallback architecture (designed, not built)

**Build it only if the pilot inventory shows that important devices can't be
reached through Matter or a direct brand adapter** (typical cases: GE Cync,
Meross, Tuya/Smart Life, Wyze, Blink). Until then it stays a design: an
unbuilt adapter costs nothing, a half-built one is a maintenance and security
liability. The decision gate is `tools/classify_inventory.py` — if its
"HOME ASSISTANT BRIDGE" group holds devices the owner uses daily, build this.

## Shape

```
Home Assistant (owner-run, separate box or container)
   ⇅ WebSocket API, long-lived access token
HomeAssistantAdapter  → ProviderAdapter contract → Starter core → Home Hub
```

The Starter never becomes a Home Assistant add-on and never requires one; HA is
one provider among others, exactly like Matter.

## Contract mapping

| ProviderAdapter | Home Assistant WebSocket API |
|---|---|
| `start()` | connect `ws://<ha>:8123/api/websocket` → `auth` with the token → `subscribe_events` (`state_changed`) |
| `discover_devices()` | `config/entity_registry/list` + `config/device_registry/list` + `config/area_registry/list` → one Starter device per `light.*` / `switch.*` entity; **HA areas become `suggested_room`** |
| `get_capabilities()` | from the entity's `supported_color_modes` / `supported_features` (below) |
| `get_state()` | cached from `get_states` + `state_changed` events |
| `execute()` | `call_service` (`light.turn_on` / `turn_off`, `switch.turn_on` / `turn_off`), then read the new state from the next `state_changed` (bounded wait) → CONFIRMED, else ACCEPTED |
| `health()` | connection up + auth ok → OK; auth rejected → AUTH_REQUIRED; unreachable → DOWN |
| `reauthenticate()` | NEEDS_USER: the owner pastes a fresh long-lived token in Settings |
| `pair()` | not supported — devices are added in Home Assistant itself |

| Starter capability | HA entity evidence | Service call |
|---|---|---|
| POWER | `light.*`, or `switch.*` (plug) | `turn_on` / `turn_off` |
| SWITCH | `switch.*` whose device class is `switch` / `outlet` in a wall | `switch.turn_on` / `turn_off` |
| BRIGHTNESS | `supported_color_modes` ⊄ {`onoff`} | `light.turn_on` `brightness_pct` |
| COLOR | `hs`, `xy`, `rgb*` in `supported_color_modes` | `light.turn_on` `hs_color: [h, s]` |
| COLOR_TEMPERATURE | `color_temp` in `supported_color_modes`; range from `min_color_temp_kelvin` / `max_color_temp_kelvin` | `light.turn_on` `color_temp_kelvin` |

HA state `unavailable` / `unknown` → the core's UNKNOWN (*Can't reach*), never OFF.

## Security

- The long-lived token is a full-access credential to the owner's HA. It is
  entered once in Settings, stored only in the Starter vault
  (`required_secrets = ("access_token",)`), never returned by any API, and
  registered with the log redactor.
- Recommend the owner creates a **dedicated HA user** for the hub (non-admin
  where HA allows) so the token can be revoked without affecting anything else.
- HA's address is validated like other provider settings (`ws://` / `wss://`);
  prefer `wss://` when HA has TLS.

## Tests required before it ships

Same bar as Matter: a fake HA WebSocket server fixture (auth ok/fail, entities
with each colour mode, `state_changed` events, unavailable entities), the shared
conformance suite, the HTTP owner flow, and a VM acceptance phase against a real
Home Assistant container with a demo integration.

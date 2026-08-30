# Location Zones — Design Spec (2026-08-30)

Report a device's coarse location — a labeled zone like `House: home`,
`away`, or `unknown` — to Home Assistant, computed entirely on the iOS
device from admin-configured geography. GPS coordinates never leave the
phone; the only thing ever transmitted is the zone label string.

Spans two codebases:

- **casa-provisioner** (HA custom integration + admin panel)
- **casa-mobile-app / casa-ios** (Casa iOS app)

## Requirements (as decided)

| Topic | Decision |
|---|---|
| Consumers | One HA `sensor` per device + admin panel display. No `device_tracker`. |
| Geography | Multiple anchor points (lat/long), each with an ordered list of labeled radius rings. Site-wide — same config for every device. |
| State | `"<anchor name>: <ring label>"` for the innermost containing ring across all anchors; `away` when outside every ring; `unknown` with a `reason` (`permission_denied` / `no_fix` / `stale`) when undeterminable. |
| Privacy | No GPS, no distance, no coordinates ever sent or stored server-side. Report schema has no location fields; endpoint rejects unknown keys. |
| Detection | Native iOS geofences (`CLCircularRegion`). Trust iOS dampening; no custom hysteresis. Static region budget: ≤18 rings total, enforced server-side and in the panel (Approach 1). |
| Reporting | Immediate best-effort POST on zone change (device_key-encrypted, works in background) + zone fields on every heartbeat as reconciler. |
| Config delivery | Saving zones in the panel enqueues a `location` update to every device via the existing queued-update + encrypted-push path; provisioning payload seeds new devices; heartbeat response carries `config_version` and a mismatch re-enqueues. |
| Staleness | Server flips sensor to `unknown`/`stale` after `stale_after_minutes` (configurable, 0 = never) without a report. |
| Permission UX | Onboarding step for new devices; explainer sheet on first config arrival for existing devices; anything short of Always-authorization reports `unknown`/`permission_denied`. |
| Panel UI | "Location Zones" card inside the Settings view with a vendored-Leaflet map picker (OSM tiles), plus zone chips in the devices list and fields in the device editor. |
| Release | Ships with a `CASA_VERSION` / `PANEL_VERSION` bump; zones pane refuses to save against a version-skewed backend. |

## Server: data model

New HA `Store(hass, 1, "casa_location_zones")`, managed like `wg_data`
(in-memory dict + `async_delay_save`), registered in `async_setup_entry`:

```json
{
  "config_version": "a1b2c3d4",
  "stale_after_minutes": 30,
  "anchors": [
    {
      "id": "uuid4",
      "name": "House",
      "latitude": 49.0,
      "longitude": -123.0,
      "rings": [
        {"label": "home",   "radius_m": 150},
        {"label": "nearby", "radius_m": 1000}
      ]
    }
  ]
}
```

`config_version` is a short stable hash (e.g. first 8 hex of sha256) of
the canonical-JSON `anchors` list; recomputed on every save.
`stale_after_minutes` changes do NOT bump the version (devices don't
care about it).

Validation on save (server-side, mirrored client-side):

- per anchor: radii strictly ascending, ring labels non-empty and
  unique within the anchor, `away`/`unknown` rejected as labels
  (reserved), lat/long in range;
- total rings across all anchors ≤ 18;
- anchor names non-empty and unique.

Per-device state on the existing device record in `stored_data`
(alongside `wireguard_connected` etc.):

```
location_state: str            # "House: home" | "away" | "unknown"
location_reason: str | None    # "permission_denied" | "no_fix" | "stale" | None
location_reported_at: str      # iso8601
location_config_version: str   # version the device last confirmed applying
```

## Server: endpoints

### `CasaLocationZonesView` — `/api/casa/admin/location_zones` (admin-gated)

- `GET` → full config.
- `PUT` → wholesale replace. Validates, bumps `config_version` when
  anchors changed, delay-saves, then **enqueues a `location`/`update`
  entry for every registered device** (payload: full config +
  version) and kicks `_deliver_updates_in_background` (encrypted
  silent push, no notify). Response includes the device count so the
  panel can toast "Saved — pushing to N devices".
- Version-skew guard in the panel API wrapper (same pattern as
  `saveProvisionTemplate`).

### `CasaLocationReportView` — `POST /api/casa/location_report` (`requires_auth = False`)

Body: `{"device_id": ..., "payload": <base64 AES-GCM blob>}` encrypted
with the site `device_key`, device_id bound into the AAD (mirror of
`_encrypt_push_payload` in the app→server direction; server gets a
`_decrypt_report_payload` counterpart). Decrypted inner:

```json
{"state": "House: home", "reason": null, "config_version": "a1b2c3d4", "ts": 1793500000}
```

- Reject: unknown device_id, decrypt failure, AAD mismatch, `ts`
  outside ±300 s, unknown top-level keys, any lat/long-looking field.
- Accept: update the four device-record fields, fire the sensor
  dispatcher signal, delay-save.

### Heartbeat changes

- Request: three optional fields `location_state`, `location_reason`,
  `location_config_version` — applied identically to a report (no ts
  check needed; the call is session-authenticated).
- Response: adds `location_config_version` (server's current, or
  absent when no zones configured). If the device-supplied version
  differs from the server's, the handler enqueues a fresh
  `location`/`update` entry for that device (deduping: drop any older
  queued `location` entries for the device first) — the device's
  normal `updates: true` pull path delivers it.

### Queue semantics

`location` entries are superseding: whenever a new one is enqueued for
a device, older queued `location` entries for that device are dropped.
`_prune_stale_queued_updates` needs no changes beyond that helper.

## Server: sensor + staleness

- `CasaDeviceLocationSensor` joins the seven existing per-device
  sensors in `sensor.py` (same dispatcher pattern). State =
  `location_state` (default `unknown`), attributes: `reason`,
  `reported_at`, `config_version`.
- Staleness sweep: `async_track_time_interval` every 60 s — for each
  device, if `stale_after_minutes > 0`, `location_reported_at` older
  than that, and state ≠ `unknown`: set `unknown`/`stale`, signal,
  delay-save. Registered in `async_setup_entry`, unsubscribed in
  `async_unload_entry`.

## Provisioning payload

The payload builder appends a `location_zones` blob (config +
version) whenever zones are configured. Not a template field —
site-wide state, like the device key. iOS provisioning parse stores it
through the same single apply path as pushed updates.

## iOS: components

### `ZoneEvaluator` (new, pure Swift — unit-testable, no CoreLocation)

- Input: config (anchors/rings) + a coordinate.
- Output: innermost containing ring across all anchors →
  `"<anchor>: <label>"`; none → `"away"`.
- Haversine distance; innermost = smallest radius among containing
  rings (ties: first anchor in config order).

### `LocationZoneManager` (new singleton, `CLLocationManagerDelegate`)

- **Config apply** — single entry point `applyLocationConfig(_:)`:
  persist to App Group, update in-memory copy, re-register regions,
  then immediately `requestLocation()` → evaluate → report. Both the
  update path and provisioning call this and nothing else (lesson
  from the 2026-08-30 stale-`wireguardConfig` bug: one write path).
- **Regions**: clear all regions with ID prefix `casa-zone:`, register
  one `CLCircularRegion` per ring (`casa-zone:<anchorId>:<ringIndex>`,
  entry+exit). ≤18 guaranteed by server cap.
- **Events**: on any region enter/exit → one `requestLocation()` fix →
  `ZoneEvaluator` → change-only report. Never trust the region event
  alone (concentric regions fire in messy orders). No fix →
  `unknown`/`no_fix`.
- **Reporting**: encrypt inner JSON with stored `device_key`
  (encrypt mirror of `CasaDecryptor`), POST to
  `/api/casa/location_report` via URLSession — fits the ~10 s
  background window a region event grants. Failures swallowed; the
  heartbeat reconciles. Change-only against last *sent* state, so
  iOS event replays are no-ops.
- **Permission**: anything short of `.authorizedAlways` → state
  `unknown`/`permission_denied`; `locationManagerDidChangeAuthorization`
  re-registers and re-evaluates when the user later grants.
- **Cold start**: instantiated in `CasaApp` init so the delegate is
  live before iOS delivers a relaunch region event; config read from
  App Group.
- Wipe paths (`wipeAll`) clear the stored config and unregister
  regions.

### Apply path (`ContentView.applyUpdateEntry`)

New case `("location", "update")` → decode config →
`LocationZoneManager.shared.applyLocationConfig(...)` → return true
(ack). Also handles the pending-permission flag below.

### Heartbeat JS (`FullScreenWebView`)

Heartbeat body gains `location_state`, `location_reason`,
`location_config_version` injected from native state. Response
handling: if server `location_config_version` ≠ ours, nothing extra to
do client-side — the server enqueues and the existing `updates: true`
pull applies it.

### Permission UX

- **Onboarding**: a location step (only when the provisioning payload
  contains zones): explainer → `requestWhenInUseAuthorization` →
  `requestAlwaysAuthorization`. Skippable → `unknown`/`permission_denied`.
- **Existing devices**: first `location` config arrival with status
  `.notDetermined` sets a pending flag; next foreground shows an
  explainer sheet (pending-WireGuard pattern) running the same
  two-step request.
- **Settings sheet**: read-only row — current zone state, permission
  status, deep link to iOS Settings when permission blocks.
- Info.plist: `NSLocationWhenInUseUsageDescription` and
  `NSLocationAlwaysAndWhenInUseUsageDescription`, both stating that
  location never leaves the device — only the zone name does.

## Panel

### Settings view — "Location Zones" card (`views/location-zones.js`, loaded by `settings.js`)

- Vendored Leaflet (BSD-2) as a panel asset; OSM raster tiles with
  attribution (needs internet in the admin's browser; fields degrade
  gracefully without it).
- Draggable pin per anchor; each ring a `L.circle` redrawn live from a
  radius number-input/slider (no drag-handle plugins). "Use HA home
  location" seeds the first anchor from `hass.config`.
- Add/remove anchors; add/remove/reorder rings; inline labels.
  Client-side validation mirrors (ascending radii, reserved labels,
  ≤18 rings, uniqueness) with inline errors.
- `stale_after_minutes` numeric field on the card.
- Save → `PUT` wholesale → toast "Saved — pushing to N devices".
  Refuses to save on version skew (existing guard pattern).

### Device surfaces

- Devices list: zone chip (`House: home` green, `away` neutral,
  `unknown` amber with reason tooltip) beside the WireGuard chips.
- Device editor: the four `location_*` fields join `RECORD_KEYS` and
  the info grid; config-version mismatch rendered amber (same visual
  language as `provisioning_pending_push`).
- Admin devices API serializes the four new fields.

### `api.js`

`getLocationZones()`, `saveLocationZones(config)` with the skew guard.

## Versioning

Bump `CASA_VERSION` (const.py) and `PANEL_VERSION` (panel/version.js)
together per the release convention.

## Edge cases

- **Empty `anchors`** is valid: devices unregister all regions and stop
  reporting; sensors go `unknown`/`stale` after the timeout.
- **No `device_key` on device**: crossing-POSTs skipped (can't
  encrypt); heartbeat fields still flow (session-authenticated).
- **Clock skew**: POST rejected outside ±5 min; heartbeat path is the
  net.
- **Anchor moved while device away**: config apply always re-registers
  and immediately re-evaluates from a fresh fix.
- **iOS event replays**: change-only reporting makes them no-ops.

## Testing

- **Server** (pytest; repo currently has no test suite — add a minimal
  `tests/` for this feature): config validation matrix, report
  decrypt/replay/AAD rejection, unknown-key rejection, heartbeat
  piggyback + version-mismatch enqueue + supersede-dedupe, staleness
  flip, sensor state/attributes.
- **iOS**: `ZoneEvaluator` unit tests (innermost-across-anchors, ties,
  away, empty config). Manual device test plan: grant/deny/revoke
  permission, cross boundaries, kill-and-cross, airplane-mode crossing
  then heartbeat reconcile.
- **E2E smoke**: two rings around the real site; drive out; watch
  `home → nearby → away` on the sensor and panel chips.

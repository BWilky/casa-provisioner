# Casa app integration — queued updates & device_key

How the iOS app consumes profile / WireGuard updates from the Casa Home Assistant
integration. Two channels deliver the same updates:

- **Pull (durable, source of truth):** the heartbeat flags pending work; the app pulls
  plaintext updates from `/profile_updates`, applies them, and acks each by id.
- **Push (optional fast path):** an encrypted silent push carries the same update so the
  app can apply it without waiting for the next heartbeat. If it can't be decrypted, the
  pull path is the safety net — nothing is ever lost.

All endpoints are authenticated with the device's normal HA bearer token, except `/api/casa/location_report`, which is unauthenticated at the HTTP layer and relies on the device-key-encrypted body.

---

## 1. Heartbeat — `POST /api/casa/heartbeat`

Request body (only `device_id` is required; omitted fields leave the stored value alone):

| Field | Type | Meaning |
|---|---|---|
| `device_id` | string | Required. |
| `app_version` | string | |
| `current_url` | string | Page the WebView is showing. |
| `provisioned_at` | string (ISO 8601) | When the current profile was installed; a value newer than a pending expiry override drops that override. |
| `expires_at` | int (unix s) | The device's session expiry; omit when it never expires. |
| `wireguard_configured` / `wireguard_connected` | bool | |
| `alias` | string | See "Device alias flow". |
| `ip_address`, `last_12_token` | string | Optional overrides of what the server derives from the request. |
| `location_state`, `location_reason`, `location_config_version` | string | Zone report piggy-backed on the heartbeat (same rules as `/api/casa/location_report`). |

Response fields:

```json
{
  "status": "success",
  "reregister": false,
  "updates": true,
  "device_key": "<64 hex chars>",
  "device_key_id": "<8 hex chars>",
  "require_alias": false,
  "has_alias": true,
  "heartbeat_interval_seconds": 300,
  "profile_report_interval_seconds": 3600,
  "location_config_version": "<string>|null",
  "site_id": "<32 chars>",
  "relay_url": "https://push.bonjour.casa",
  "expires_at": 1767225600
}
```

- `profile_report_interval_seconds` is the site's cadence for `POST /api/casa/profile_report` (below). Always present.
- `location_config_version` is always present; `null` when the site has no location
  zones (an empty anchor list has no config version). The server only re-enqueues the
  zone config when it has a non-null version and the heartbeat's
  `location_config_version` differs from it (or is missing), so a site without zones
  never re-sends anything.
- `site_id` / `relay_url` (26.10.01): the relay this site is registered with and its
  site_id there. If either differs from what the app stored at provisioning, the app
  re-registers with that relay and re-POSTs `register_device` with the new proxy token.
  Older servers omit both.
- `expires_at` is present only while an admin override is pending (`0` = permanent); omitted otherwise.
- `reregister: true` means: re-register with the relay and POST `register_device`. Sent
  when `/reconcile` found the relay lost the proxy token, and (26.10.01) when the
  heartbeat's device_id is recorded under a different HA user — a heartbeat never takes
  a device record over from another user (unless it presents the session token pinned
  on that record, or that record's own session is gone); `register_device` moves it.
  Such a heartbeat also answers `updates: false` and applies no location fields.
- **Persist `device_key` and `device_key_id` on every heartbeat.** `device_key` is the
  shared secret used to decrypt pushes; `device_key_id` is its fingerprint.
- If `updates == true`, call the pull endpoint (§2).
- `device_key`/`device_key_id` are only ever `null` if the site key isn't set yet
  (shouldn't happen in normal operation).
- `heartbeat_interval_seconds` is the site's admin-configured cadence (default 300,
  range 60–3600). Always present. The app should apply it as its new heartbeat
  interval going forward, and reset to the 300s default on reprovision — a custom
  interval must never carry over from a previous site/session.
- `400 {"error": "Device is being removed."}` while an admin purge of this device is in
  flight; the session is about to be revoked anyway.

### Register — `/api/casa/register_device`

- `POST {"device_id": "<id>", "push_token": "<64 hex>"?}` → `{"status": "success"}`. The
  push token is the relay proxy token (64 hex chars, else 400). Re-posting merges onto
  the existing record. A device_id lives under exactly one HA user: registering one
  that is recorded under another user moves the record to the caller and drops its
  queued updates and pending reauthentication (they were addressed to the old user).
- `GET ?device_id=<id>` → `{"registered": bool, "push_token", "registered_at", "last_seen_at"}`
  (`registered` means push-registered).
- `DELETE ?device_id=<id>` clears only the push registration; the record stays.
- WebSocket fallback: the `casa.register_device` service (`device_id`, `push_token`,
  both required) supports a response —
  `hass.callService("casa", "register_device", data, undefined, false, true)` resolves
  with `res.response.status == "success"`.

### Device alias flow

The request body accepts an optional `alias` (string) alongside the usual fields. The
server accepts it **only while the stored alias is empty** (trimmed, capped at 60
chars) — an admin-set alias always wins and is never overwritten.

- `require_alias` is the **site-wide** flag only. The per-profile flag arrives in the
  provisioning payload (`require_alias`) and in `profile` update payloads
  (`fields.require_alias`); the app ORs the two sources.
- `has_alias` reflects whether the device currently has a non-empty alias. When the
  requirement is active and `has_alias` is `false`, the app blocks with a name prompt
  and sends the entered value as `alias` on every heartbeat until `has_alias` flips
  `true` (also the signal to clear any locally pending submission).

## 2. Pull updates — `GET /api/casa/profile_updates?device_id=<id>`

- Must use the **same HA session** the device heartbeats with: the server matches the
  bearer token's stable refresh-token id against the device record.
  - `401` — no / invalid bearer
  - `403` — valid bearer but wrong session for this device (re-authenticate)
  - `404` — device_id not registered (re-register)
- Returns plaintext:

```json
{
  "updates": [
    {
      "id": "<32 alphanumerics>",
      "type": "wireguard | profile | auth | location",
      "action": "update | revoke",
      "payload": { ... },
      "created_at": "<iso8601>",
      "created_by": "<admin name>"
    }
  ]
}
```

- `(type, action)` pairs: `wireguard/update`, `wireguard/revoke`, `profile/update`,
  `auth/reauthenticate`, `location/update` (schemas in §4).
- `auth` / `reauthenticate` is never acked by the device (see the section after §4).
- `location` / `update` carries the location-zone config and replaces any older `location` entries in the queue.
- Ack every entry you cannot apply too (unknown type/action, malformed payload, a
  WireGuard config the parser rejects, a `profile` entry with no `fields`) and log why;
  only `auth/reauthenticate` stays unacked. (26.10.01) The server also drops non-auth
  entries unacknowledged for 30 days and caps a device's queue at 50 entries (oldest
  non-auth first), so a device that never acks can't grow its queue forever.

### Profile report — `POST /api/casa/profile_report`

Body: `{"device_id": "<id>", "fields": { ... }}`. `fields` must be an object (400 otherwise);
only live provisioning fields are kept, unknown keys are dropped. `401` no user, `404`
device not registered — or (26.10.01) not the caller's device: only the device's
pinned session or its owning user may report. Returns `{"status": "success"}`. Sent every
`profile_report_interval_seconds` or immediately on a `request_profile_report` push (§5).

Report `immersive_level` as the level only (`"2"`) with `theme_color_mode` and
`custom_color` (`"#rrggbb"`) as their own keys. A legacy `"level,mode,color"` triple is
still accepted and split server-side. Bools may be JSON bools or `"true"`/`"false"`;
numbers are stored as strings.

## 3. Acknowledge — `POST /api/casa/profile_updates`

```json
{ "device_id": "<id>", "ids": ["<id1>", "<id2>"] }   // or single: "id": "<id>"
```

Removes the entries from the queue. Returns `{ "status": "ok", "remaining": <n> }`.
**Always ack after applying**, whether the update came from the pull path or a push.

---

## Self-deprovision — `POST /api/casa/deprovision` (26.09.30)

Lets a device remove its own server-side record when the user signs out or resets the app.

Auth: the device's own HA session (Bearer access token).

Request body: `{"device_id": "<id>"}`

| Status | Meaning |
|---|---|
| 200 | `{"status": "success", "access_revoked": bool}`. The record is removed, the relay proxy token unregistered, queued updates dropped, and this device's refresh token revoked (`access_revoked` is true when a token was found and revoked). |
| 400 | `device_id` missing or empty. |
| 401 | No authenticated user. |
| 404 | `{"error": "Device not found"}`. Returned when the device does not exist, belongs to another user, or the caller is a deleted managed user. It is never 403, so other users' device ids are not confirmed. |

The HA user account is never deleted; only this device's record and session are removed. Treat any 2xx, 4xx, or timeout as done and continue the local wipe, since the access token may already be revoked.

## 4. Update schema & how to apply

| `type`      | `action`         | `payload`                                                      | Apply                              |
|-------------|------------------|----------------------------------------------------------------|------------------------------------|
| `wireguard` | `update`         | `{ "config": "<wg .conf text>", "excluded_wifi": "<ssid|''>" }` | install / replace the tunnel       |
| `wireguard` | `revoke`         | `{}`                                                           | remove the tunnel                  |
| `profile`   | `update`         | `{ "profile_id": "...", "name": "...", "fields": { ... } }`     | apply `fields` (same as provisioning) |
| `auth`      | `reauthenticate` | `{ "username": "...", "password": "..." }`                     | log out and auto-login as the new user (below) |
| `location`  | `update`         | `{ "anchors": [ ... ], "config_version": "<string>" }`         | replace the zone config; empty `anchors` (`config_version: ""`) means tear down all regions and clear the config, without prompting for location permission |

### `profile` / `update` fields

`fields` is sparse — only the keys being changed are present (template applies carry
what the template sets; the device editor pushes what the admin changed). Keys and
types the app must accept:

| Key | Type |
|---|---|
| `host_url` | string. A change to a different origin is refused (log + ack, keep the current server): the device has no credentials there; the panel warns that this needs a re-provision. Same-origin changes apply. |
| `default_dashboard`, `allowed_pages`, `allowed_wifi`, `wireguard_excluded_wifi`, `push_notifications` (`"false"`, `"true"`, `"mandatory"`) | string |
| `welcome_url` | string; `""` means no welcome page (same as absent) |
| `immersive_level` | int, numeric string, or a legacy `"level,mode,color"` triple |
| `theme_color_mode` (`inherit`, `custom`, `inherit_with_fallback`), `custom_color` (`#rrggbb`) | string |
| `cache_control_hours` | number or numeric string (servers send a string; `""` = app default) |
| `allow_all_pages`, `require_alias`, `allow_wireguard` | bool (also `"true"`/`"false"`) |

WireGuard config never travels inside a profile update: when a template apply or a
device-editor push resolves to a WireGuard config (`wireguard_config` or
`wireguard_profile_id`), the server strips those keys and enqueues a separate
`wireguard/update` entry. An app that still finds them in `fields` ignores them.

### `auth` / `reauthenticate` — remote credential rotation

Admin-initiated switch of the account a device logs in as, without a full
re-provision. On applying:

1. Keep everything except the HA session: `device_id`, push registration,
   `device_key`/`device_key_id`, server URL, and all provisioning fields
   stay untouched.
2. **Do not ack this entry** (unlike every other type). Tear down only the
   web session: unmount the WebView, wipe WebKit data (drops the old
   `hassTokens`), remount the same server URL, and let the auto-login
   injector sign in with the new `username`/`password`.
3. A failed login lands the device on the HA login screen and eventually
   follows the normal auth-failure wipe paths — there is no rollback.

Server-side lifecycle: the queue entry is **auto-dequeued** on the device's
first authenticated contact (register or heartbeat) as the new user, which is
also when the server moves the device record, rebinds `refresh_token_id`, and
revokes the old session token. That is why the device must NOT ack: leaving
the entry queued means a broken apply simply re-delivers on the next
heartbeat pull until the reauth completes (or the device wipes). Apps that
predate this type ignore the entry and never ack it either — the server
clears it at completion or on admin cancel. The push copy travels only inside
the encrypted `casa_update` envelope (§5), never in plaintext APNs payload.

(26.10.01) A queued entry always carries the target account's current password:
any later rotation of that password (another provision, a single-use or timer
scramble, `casa.scramble_guest_password`, a reauth that rotates it) drops the
now-stale entry and its pending marker server-side, so a device never pulls
credentials that can no longer log in. A push copy delivered before the rotation
can't be recalled; its login simply fails.

---

## 5. Push payloads

Delivered via APNs through the relay — same channel as today's WireGuard pushes.

Silent pushes are sent with `apns-push-type: background` / `apns-priority: 5`
and a `content-available: 1` payload with no `alert` key (requires a relay
built after 26.07; older relays degrade to an empty-alert push that only
reaches a foregrounded app). The app must declare the `remote-notification`
background mode to be woken for these while suspended.

### Silent update push (new, ackable)

```
command:        "casa_update"
encrypted:      true
update_payload: "<base64: nonce||ciphertext||tag>"
update_id:      "<id>"
device_key_id:  "<8 hex>"
title:          ""        // silent
message:        ""
```

Decrypt `update_payload` (§6) → inner JSON `{ "id", "type", "action", "payload", "ts" }`.
The inner `id` **always equals the envelope `update_id`** (and equals the queue entry id),
so ack either one. The envelope `update_id` lets you dequeue even when `device_key_id`
doesn't match and you can't decrypt yet.

### WireGuard push (legacy `update_wireguard` service — fire-and-forget)

```
command:           "wireguard_update" | "wireguard_revoke"
encrypted:         true
wireguard_payload: "<base64: nonce||ciphertext||tag>"
device_key_id:     "<8 hex>"
title / message:   always present; "" when silent
```

(26.10.01) Always encrypted: the service's `encrypt_config: false` is deprecated and
ignored with a warning. Older servers could send `encrypted: false` with
`base64(plaintext)`.

Inner JSON: `{ "action", "config", "excluded_wifi", "ts" }` (update) or
`{ "action": "revoke", "ts" }`. **No `update_id` and nothing to ack** — these have no
queue entry. Apply and stop. (The admin panel's WireGuard button uses the `casa_update`
path above, which *is* ackable; the legacy service path is for automations.)

### Visible notify push (admin "Notify via Push")

A normal alert notification with no payload:

```
title / message: <set by admin>
data: { "update_id": "...", "type": "...", "action": "..." }
```

Treat as a nudge: heartbeat + pull.

### `casa.notify_user` pushes

A visible alert with the caller's `title`/`message`; any `data` dict is passed through
as-is (no `command`, never routed through command dispatch). Keys the app acts on:

| Key | Type | Effect |
|---|---|---|
| `nav_path` | string | Navigate the WebView to this path. |
| `sheet_path` | string | Open this path in a bottom sheet. |
| `stream_url` | string | Open a native stream player sheet. |
| `sheet_height` | number (0–1) | Sheet height fraction (default 0.6 for `sheet_path`, 0.5 for `stream_url`). |
| `timeout` | number (s) | Auto-dismiss the sheet / stream after this long. |
| `haptic` | string | `light`, `medium`, `heavy`, `success`, `warning`, `error` (`none` = off). |
| `open_on_delivery` | bool | Act immediately when delivered in the foreground (banner suppressed) instead of on tap. |

### Command pushes (`deprovision`, `clear_cache_and_reload`)

Content-free silent pushes (`title`/`message` `""`) with `data: { "command": ... }`:

- `deprovision` → wipe local provisioning state and sign out (the app may also call
  `POST /api/casa/deprovision`; see "Self-deprovision" above).
- `clear_cache_and_reload` → clear the web cache and reload the WebView.

### Check-in nudge pushes (`request_heartbeat` / `request_profile_report`)

Content-free silent pushes asking the app to act immediately instead of
waiting for its next scheduled tick. No encryption, no queue entry:

```
title / message: ""   // silent
data: { "command": "request_heartbeat" | "request_profile_report" }
```

- `request_heartbeat` → send a heartbeat right now. If the response's
  `updates` flag is true, this naturally triggers the normal pull path
  (§2) — the same mechanism the periodic heartbeat timer already uses.
  Sent automatically by the server after most admin actions that change a
  device's or site's state (provisioning field edits, profile/WireGuard
  queue pushes, expiration changes, device-key rotation), and also
  available as a standalone `casa.request_heartbeat` HA service call.
- `request_profile_report` → POST the current provisioning snapshot to
  `/api/casa/profile_report` right now, instead of waiting up to
  `profile_report_interval_seconds`. Available as `casa.request_device_report`.

---

## 6. Crypto

AES-256-GCM. The key is HKDF-SHA256–derived per device:

- **IKM**    = UTF-8 bytes of `device_key` (use the hex *string* as-is — do **not** hex-decode)
- **salt**   = UTF-8 bytes of `device_id`
- **info**   = `"casa-update-v1"`
- **length** = 32 bytes

The base64 blob is `nonce(12) || ciphertext || tag(16)` — exactly CryptoKit's "combined"
SealedBox layout, so decryption is a one-liner:

```swift
import CryptoKit

func deriveKey(deviceKey: String, deviceId: String) -> SymmetricKey {
    HKDF<SHA256>.deriveKey(
        inputKeyMaterial: SymmetricKey(data: Data(deviceKey.utf8)),
        salt: Data(deviceId.utf8),
        info: Data("casa-update-v1".utf8),
        outputByteCount: 32
    )
}

func decryptPush(_ b64: String, deviceKey: String, deviceId: String) throws -> Data {
    let box = try AES.GCM.SealedBox(combined: Data(base64Encoded: b64)!)
    return try AES.GCM.open(box, using: deriveKey(deviceKey: deviceKey, deviceId: deviceId))
}
```

---

## 7. Key rotation

The admin can rotate `device_key` at any time (non-destructive). Therefore:

- Before decrypting a push, compare its `device_key_id` to the stored one. **If they don't
  match (or no key is stored yet), do not attempt decryption** — trigger a heartbeat (which
  refreshes the key and the `updates` flag) and take the pull path instead.
- Because the queue is durable, a missed or undecryptable push is never a lost update.

---

## 8. Recommended client state & flow

Persist: `device_id`, `device_key`, `device_key_id`.

**Golden path**
1. Heartbeat → store the key fields. If `updates`, `GET /profile_updates`.
2. Apply each entry by `type` / `action`.
3. `POST /profile_updates` with the applied ids.

**Fast path (optional)**
- Silent `casa_update` push whose `device_key_id` matches → decrypt → apply → ack `update_id`.
- On any mismatch or failure, fall back to the golden path.

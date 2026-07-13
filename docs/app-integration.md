# Casa app integration — queued updates & device_key

How the iOS app consumes profile / WireGuard updates from the Casa Home Assistant
integration. Two channels deliver the same updates:

- **Pull (durable, source of truth):** the heartbeat flags pending work; the app pulls
  plaintext updates from `/profile_updates`, applies them, and acks each by id.
- **Push (optional fast path):** an encrypted silent push carries the same update so the
  app can apply it without waiting for the next heartbeat. If it can't be decrypted, the
  pull path is the safety net — nothing is ever lost.

All endpoints are authenticated with the device's normal HA bearer token.

---

## 1. Heartbeat — `POST /api/casa/heartbeat`

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
  "heartbeat_interval_seconds": 300
}
```

- **Persist `device_key` and `device_key_id` on every heartbeat.** `device_key` is the
  shared secret used to decrypt pushes; `device_key_id` is its fingerprint.
- If `updates == true`, call the pull endpoint (§2).
- `device_key`/`device_key_id` are only ever `null` if the site key isn't set yet
  (shouldn't happen in normal operation).
- `heartbeat_interval_seconds` is the site's admin-configured cadence (default 300,
  range 60–3600). Always present. The app should apply it as its new heartbeat
  interval going forward, and reset to the 300s default on reprovision — a custom
  interval must never carry over from a previous site/session.

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
      "type": "wireguard | profile",
      "action": "update | revoke",
      "payload": { ... },
      "created_at": "<iso8601>",
      "created_by": "<admin name>"
    }
  ]
}
```

## 3. Acknowledge — `POST /api/casa/profile_updates`

```json
{ "device_id": "<id>", "ids": ["<id1>", "<id2>"] }   // or single: "id": "<id>"
```

Removes the entries from the queue. Returns `{ "status": "ok", "remaining": <n> }`.
**Always ack after applying**, whether the update came from the pull path or a push.

---

## 4. Update schema & how to apply

| `type`      | `action` | `payload`                                                      | Apply                              |
|-------------|----------|----------------------------------------------------------------|------------------------------------|
| `wireguard` | `update` | `{ "config": "<wg .conf text>", "excluded_wifi": "<ssid|''>" }` | install / replace the tunnel       |
| `wireguard` | `revoke` | `{}`                                                           | remove the tunnel                  |
| `profile`   | `update` | `{ "profile_id": "...", "name": "...", "fields": { ... } }`     | apply `fields` (same as provisioning) |

---

## 5. Push payloads

Delivered via APNs through the relay — same channel as today's WireGuard pushes.

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
encrypted:         true | false
wireguard_payload: "<base64: nonce||ciphertext||tag>"   // or base64(plaintext) if encrypted=false
device_key_id:     "<8 hex>"
title / message:   present only when not silent
```

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

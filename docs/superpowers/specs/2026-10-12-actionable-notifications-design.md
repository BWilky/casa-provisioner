# Actionable Notifications — Design Spec (2026-10-12)

Add action buttons to Casa push notifications. An automation attaches a list
of actions to `casa.notify_user`; the iOS app shows them as buttons on the
notification; a press is reported back to Home Assistant, which fires a bus
event and optionally runs a service call the sender attached to that action.

Scope: **casa-provisioner** (HA integration + admin panel + docs),
**push-notifications-relay** (one optional field), **casa-ios** (one new
app-extension target, press handling, native callback).

Today nothing actionable exists in any of the three projects: no
`aps.category` is sent, no `UNNotificationCategory` is registered, the app
ignores `response.actionIdentifier`, and HA has no endpoint for "the user
chose X".

## Requirements (as decided)

| Topic | Decision |
|---|---|
| What a press does | Always fires an HA bus event. Optionally also runs a service call stored server-side, opens the app at a dashboard path, or returns typed text. |
| HA surface | Bus event `casa_notification_action` only. No device trigger in v1. |
| Action definition | Inline per notification (`data.actions`), like the official companion app. No pre-registered categories. |
| iOS mechanism | One fixed category `CASA_DYNAMIC` registered at launch; a Notification Content Extension sets `extensionContext.notificationActions` from the payload. This is how the official app does it. |
| Callback path | Native, no WebView: encrypted with the site device key (same scheme as `location_report`, new HKDF info string), to a new unauthenticated endpoint. Works from the lock screen with the app not running. |
| Authorisation of service calls | HA stores the full action list when it sends; the device only names an action id. Service names never leave HA. |
| Per-action options | `service` (+ `target`, `data`), `nav_path`, `text_input`, `destructive`, `auth_required`. |
| Plain tap / dismiss | No event. Existing tap behaviour (`nav_path`, `sheet_path`, `stream_url` on the notification itself) unchanged. |
| Record lifetime | 24 hours. A press after that is rejected silently. |
| Duplicates | First valid press consumes the record. A retried callback returns 200 and fires nothing. |
| Panel | Test-push forms get an optional Data (JSON) box with an "Insert actions example" link. No pending/press history page. |
| Release | `CASA_VERSION` (const.py) + `PANEL_VERSION` (panel/version.js) bumped together. Relay protocol stays at 1 (optional field only). |

## 1. Contract

### 1.1 Service input

```yaml
service: casa.notify_user
data:
  username: bryce            # or device_id, as today
  title: Front door
  message: Someone is at the door
  data:
    tag: front_door                  # optional string, echoed in the event
    action_data: {camera: front}     # optional dict, echoed in the event
    actions:
      - action: UNLOCK               # required; unique in the list
        title: Unlock                # required
        service: lock.unlock         # optional; domain.name; script.x and automation.trigger work
        target: {entity_id: lock.front_door}   # optional, with service
        data: {}                     # optional, with service
        auth_required: true          # optional; device must be unlocked
      - action: VIEW
        title: View camera
        nav_path: /lovelace/doors    # optional; opens the app at this path
      - action: REPLY
        title: Reply
        text_input: true             # optional; typed text returns as reply_text
      - action: IGNORE
        title: Ignore
        destructive: true            # optional; red button
```

Validation (`_validate_actions`, raises `HomeAssistantError` before any push
is sent):

- `actions` is a list of 1–10 objects.
- `action`: non-empty string, ≤ 64 chars, unique within the list.
- `title`: non-empty string, ≤ 64 chars.
- `service`: string matching `^[a-z0-9_]+\.[a-z0-9_]+$` if present.
- `target`, `data`: dicts if present; only meaningful with `service`.
- `nav_path`: string ≤ 512 chars if present.
- `text_input`, `destructive`, `auth_required`: booleans if present.
- Unknown keys inside an action are rejected.
- `tag`: string ≤ 128 chars if present. `action_data`: dict if present.

Without `actions`, `notify_user` behaves exactly as today.

### 1.2 HA → relay

For each target device, HA creates a pending record (see §3.2) with its own
random `notification_id` (`secrets.token_urlsafe(24)`), then posts the
existing `/send` body plus:

- top-level `category: "CASA_DYNAMIC"`
- `data.notification_id`
- `data.actions`: the list with `service`, `target`, `data` **removed** from
  every entry. All other caller `data` keys pass through as today.

### 1.3 Relay → APNs

`/send` and `/send_batch` accept an optional `category: str` (1–64 printable
ASCII characters). When present the relay sets `aps.category` on both the
plaintext payload and the encryptor's outer payload. Absent → no change.
Background pushes ignore it. Protocol stays at 1.

### 1.4 Device → HA

`POST /api/casa/notification_action`, no bearer token, body
`{"device_id": "...", "payload": "<b64>"}`. The payload is AES-256-GCM
(`nonce || ct || tag`, base64) under
`HKDF-SHA256(ikm=utf8(device_key), salt=utf8(device_id), info="casa-action-v1")`.
Inner JSON, keys whitelisted:

```json
{"notification_id": "...", "action": "UNLOCK", "reply_text": "...", "ts": 1760000000}
```

`reply_text` is optional and capped at 1,000 characters. `ts` must be within
±300 s. Every failure returns `403 {"error": "Rejected"}`.

### 1.5 Event

```yaml
event_type: casa_notification_action
data:
  action: UNLOCK
  notification_id: "..."
  tag: front_door          # omitted if not sent
  action_data: {...}       # omitted if not sent
  device_id: "..."
  username: bryce
  user_id: "..."
  reply_text: "..."        # only for text_input actions
```

The event fires before the stored service call so an automation observes the
press even if the call fails.

Example automation (goes in README):

```yaml
- action: casa.notify_user
  data: {username: bryce, title: Garage, message: Closing in 60 s,
         data: {tag: garage_close, actions: [{action: CANCEL, title: Keep open}]}}
- wait_for_trigger:
    - trigger: event
      event_type: casa_notification_action
      event_data: {tag: garage_close, action: CANCEL}
  timeout: "00:01:00"
  continue_on_timeout: true
- if: "{{ wait.trigger is none }}"
  then: {action: cover.close_cover, target: {entity_id: cover.garage}}
```

## 2. iOS

### 2.1 Category registration

In `AppDelegate.didFinishLaunching`, right after setting the notification
delegate, register one `UNNotificationCategory(identifier: "CASA_DYNAMIC",
actions: [], intentIdentifiers: [], options: [])`. iOS persists registered
categories; the extension (below) supplies the buttons at display time.

### 2.2 New target `CasaNotificationContent`

Notification Content Extension, bundle id
`com.kelby.casa.CasaNotificationContent`, deployment target matching the app.
Info.plist: `UNNotificationExtensionCategory = CASA_DYNAMIC`,
`UNNotificationExtensionDefaultContentHidden = false`,
`UNNotificationExtensionInitialContentSizeRatio` at the minimum so no custom
UI area is visible. The view controller's `didReceive(_:)` parses the actions
from `userInfo` (flat or under `data`) and assigns
`extensionContext?.notificationActions`. No network, no keys, no app-group
access.

### 2.3 Shared parser in `CasaCore`

`NotificationActionSpec` (`action`, `title`, `navPath`, `textInput`,
`destructive`, `authRequired`) with `static func parse(userInfo:) -> [Spec]`.
Entries missing `action` or `title` are skipped; the list is capped at 10.
A helper builds the `UNNotificationAction` /
`UNTextInputNotificationAction` with options: `.destructive`,
`.authenticationRequired`, and `.foreground` when `navPath` is set. Both the
app and the extension use this type.

### 2.4 Press handling

`AppDelegate.userNotificationCenter(_:didReceive:)`:

- `UNNotificationDefaultActionIdentifier` or
  `UNNotificationDismissActionIdentifier` → existing behaviour, unchanged.
- Otherwise: find the spec whose `action` equals `response.actionIdentifier`;
  take `userText` if the response is `UNTextInputNotificationResponse`; start
  a `beginBackgroundTask`; call `NotificationActionReporter.report(...)`; if
  the spec has `navPath`, post the existing `.casaPushActionNav` path. Call the
  completion handler when the report finishes or the background task expires.

### 2.5 `NotificationActionReporter` (main app)

Mirrors `LocationZoneManager.sendReport`: reads `serverURL`
(UserDefaults), `casa_device_id` (UserDefaults) and the device key
(`SecureStore`, after-first-unlock). Encrypts
`{notification_id, action, reply_text?, ts}` via `CasaReportCrypto` with info
`casa-action-v1`, posts to `/api/casa/notification_action` with a 15 s
timeout.

Retry queue: on transport error or 5xx, append the report to a
UserDefaults-backed queue (`casa_pending_action_reports`). Flush on app
foreground and on each heartbeat. Drop entries older than 24 h. A 403 is
final (not queued). A 200 removes the entry.

### 2.6 Crypto change

`CasaReportCrypto.encryptReport(_:deviceKey:deviceId:info:)` gains an `info`
parameter defaulting to `"casa-report-v1"`, so the existing location call
site is untouched.

## 3. HA integration

### 3.1 `handle_notify_user`

After parsing `data`: if `actions` present, run `_validate_actions`. For each
device with a push token, create a pending record, then build the relay body
per §1.2. Logging stays as today. The service response shape is unchanged.

### 3.2 Pending store

`Store(hass, 1, "casa_notification_actions")`, shape
`{"pending": {notification_id: record}}` where record =
`{device_id, user_id, username, tag?, action_data?, actions: [full entries],
created_at, consumed_at?}`.

- Created on send; saved with `async_delay_save` (short delay) so a burst of
  sends is one write.
- Pruned on every write and by an hourly `async_track_time_interval`: drop
  records older than 24 h, and consumed records older than 1 h.
- Loaded at setup alongside the other stores.

### 3.3 `CasaNotificationActionView`

`url = "/api/casa/notification_action"`, `requires_auth = False`. Steps:

1. Parse JSON; require `device_id` and string `payload`, else 400.
2. `_find_device_record`; need device + site `device_key`, else 403.
3. `decrypt_report_payload(..., info=b"casa-action-v1")`; else 403.
4. Inner keys ⊆ `{notification_id, action, reply_text, ts}`; `ts` within
   ±300 s; `reply_text` str ≤ 1,000 if present; else 403.
5. Record must exist, `record.device_id == device_id`, and `action` must be
   one of its entries; else 403 (logged at info with the reason).
6. If already consumed → 200 `{"status": "ok", "duplicate": true}`, no event.
7. Mark consumed, save, fire `casa_notification_action` (§1.5), then if the
   entry has `service`: `hass.services.async_call(domain, name, data,
   target=target, blocking=False, context=Context())`, catching and logging
   any exception. Return 200. The system `Context()` is safe because
   `notify_user` refuses, at send time, any action carrying `service` when
   the call came from a non-admin user (`call.context.user_id` set and not an
   admin); automations, scripts and system calls (`user_id` None) are allowed.

### 3.4 Crypto helper refactor (`location.py`)

`_derive_report_key`, `encrypt_report_payload`, `decrypt_report_payload` take
`info: bytes = _REPORT_INFO`. The action view imports them. No duplication.

### 3.5 Docs and version

- `services.yaml`: `notify_user.data` description gains the actions example.
- `README.md`: actions section + the wait_for_trigger example.
- `docs/app-integration.md` §5: actions payload, `notification_id`,
  `category`; new §5.x for the callback.
- `docs/provisioning-protocol.md` §8 endpoint table (+ `notification_action`,
  "encryption is the authentication"), §9 note that `CASA_DYNAMIC` visibles
  still never go through command dispatch.
- Bump `CASA_VERSION` and `PANEL_VERSION` together.

## 4. Panel

- Devices-list test-push modal (`panel/views/devices.js`) and the device
  editor's "Push & Notifications" test form (`panel/views/device-editor.js`):
  add an optional **Data (JSON)** textarea and an **Insert actions example**
  link that fills it with three actions (`nav_path`, `service: script.turn_on`
  with a placeholder target, destructive `IGNORE`). Parse client-side; a parse
  error goes through `ui.errMsg`. Pass the object as `data` to
  `api.notifyUser`. Help text: presses arrive as the
  `casa_notification_action` event (Developer Tools → Events).
- Relay `README.md`: add `category` to the `/send` field table; reword the
  `"data": {"category": "alarm"}` example so it no longer implies
  `aps.category`.

## 5. Error handling and edge cases

- Relay: invalid `category` → existing 422 validation shape.
- Record missing/expired at press time → 403; app drops the report; HA logs
  at info. The user sees nothing (accepted for v1).
- Device key rotated between send and press → decrypt fails → 403, as for
  location reports.
- `service` no longer exists at press time → warning logged; event already
  fired.
- Payload size: the per-field caps in §1.1 alone do not (10 actions at the
  64/64/512 caps exceed 4 KB), so `validate_actions` also rejects any list
  whose device-safe copy (`device_actions`, compact JSON) is over 2 KB
  ("actions are too large to push"). That keeps the push well under the 4 KB
  APNs limit and the relay's 10 KB body cap; server-only keys (`service`,
  `target`, `data`) never leave HA and do not count.
- Foreground press uses the same path as a background press.
- Security note (carried from `location_report`): the device key is
  site-wide, so another provisioned device on the same site could forge a
  press only if it also knew the random `notification_id`, which is delivered
  only to the target device over APNs. Accepted for v1.

## 6. Testing

- **Provisioner (pytest):** `_validate_actions` (each rule); pending store
  create/consume/duplicate/prune; endpoint: valid press fires event with the
  §1.5 shape and calls the service; wrong device; unknown action; expired;
  replayed (duplicate → 200, one event); bad `ts`; oversize `reply_text`;
  `notify_user` strips server-side keys and adds `category` +
  `notification_id`; no-actions path unchanged.
- **Relay (pytest):** `category` lands in `aps.category` for plaintext and
  encrypted payloads, absent when not sent, ignored for background, rejected
  when invalid; README field table updated.
- **iOS (XCTest in CasaCore):** parser (all flags, text input, bad entries
  skipped, cap at 10, flat vs nested `data`); reporter queue persistence and
  24 h drop.
- **Manual (Fairwater):** expand notification → buttons appear; press with
  app killed from lock screen → event in Developer Tools; `auth_required`
  prompts unlock; text input returns `reply_text`; `nav_path` action opens
  the app at the path; off-LAN press over WireGuard; press after HA restart
  (store persisted); press after 24 h → nothing.

## Out of scope

Device triggers, Android, sound/badge/interruption-level passthrough,
end-to-end encryption of `notify_user` title/body, a panel history of presses,
dismiss events.

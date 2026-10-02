# Casa Provisioner

Home Assistant custom integration for provisioning and managing [Casa](https://bonjour.casa) iOS devices. Handles user creation, encrypted provisioning (QR / BLE / deep link / manual entry), push notifications, WireGuard VPN configuration, device lifecycle management (expiration, remote deprovision), and an admin sidebar panel.

## Installation (HACS)

1. Add custom repository: `https://github.com/bwilky/casa-provisioner` → Category: **Integration**
2. Download **Casa Provisioner** in HACS
3. Restart Home Assistant
4. Add via **Settings → Devices & Services → Add Integration → Casa**

## Integration Options

| Option | Default | Description |
|--------|---------|-------------|
| Admin / System Only | `true` | Restrict service calls to admin users and automations |
| Create Devices | `true` | Register Casa devices in the HA Device Registry |
| Show Panel | `false` | Add the **Casa** admin panel to the sidebar (admin users only) |
| Relay Base URL (`relay_base_url`) | blank (`https://push.bonjour.casa`) | The push relay this site talks to; blank uses the default. Must be `https://`, or `http://` only for localhost, private LAN addresses or `.local` hosts. Site credentials are kept per relay, so switching to a lab relay and back never discards the production site key. Phones still register with the production relay until the app learns this URL; use only for testing pushes from a lab relay. Reload the integration after changing it. |
| Regenerate Site ID | — | Regenerates both the site ID and site key (breaks existing push registrations) |

---

## Admin Panel

Enable **Show Panel** in the integration options to get a **Casa** sidebar entry (admins only). The panel is a full app in the style of the ESPHome Device Builder:

- **Device list** (default page) — searchable, sortable table or card grid with status dots (heartbeat recency), app-version chips, filter and column pickers, select-multiple bulk actions (reload / delete / deprovision), pagination, and a per-device menu (edit, test push, reload, push WireGuard / apply template, re-provision (one click: new login over push, or a fresh QR for a wiped/replaced phone), delete record, deprovision). An **Accounts** tab manages guest users with one-time-credential dialogs. Each account's menu can provision a new device straight onto it. Home Assistant devices are named after the device alias (falling back to "Casa Device (<account>)").
- **Provision wizard** ("+ Provision device") — three steps. **Account:** create a new account named after the device (username suggested as `casa-<name>`, live availability check; a taken Casa username offers "use this account instead") or pick an existing Casa account — accounts can be shared by several devices. **Template:** pick a saved template or *Configure manually*; per-device tweaks sit under *Customize for this device* (optionally *Also save as new template*), and PIN / Wi-Fi join / sign out other devices under *Advanced*. **Done:** QR code plus universal and `hascasa://` deep links with copy buttons. Passwords are generated server-side and never shown. BLE beacons, window length, password scramble and QR files remain available on the `casa.provision` service (as does `method: manual`).
- **Device editor** — three-pane page per device: section navigator (Overview, Session & Expiration, Push, VPN/WireGuard, Pending Updates, Danger Zone), forms in the middle, and a live read-only `device.json` pane on the right. Expiration changes (set/extend/permanent/expire-now) apply on the device's next heartbeat and show as *pending* until confirmed.
- **Template editor** — same three-pane layout for provision templates. Templates are **partial**: they store only the fields you explicitly set (each set field gets an × reset button; unset fields render dimmed with the system default), and the admin fills in the blanks at provision time. A **live `payload.json` preview** of the v2 provision profile updates as you type (provision-time values shown as placeholders), with dirty-state Save. Templates stamp values at provision time — editing one never changes already-provisioned devices; use **Apply to devices…** (template list or editor) to push a template's set device fields to selected devices as a one-time bulk apply.
- **Settings** (header menu) — Site ID / encryption-key rotation, site regeneration (danger), and WireGuard profile management with in-use checks.

The panel is served as versioned ES modules from `/casa_static/`; updates are picked up automatically after a Home Assistant restart (no hard refresh needed).

---

## Services

### `casa.provision`

Generates an encrypted provisioning payload. Supports four methods: `qr`, `ble`, `deep_link`, and `manual`. Returns response data — `qr` and `deep_link` include both a `deep_link` (`hascasa://setup?data=…`) and a `universal_link` (`https://bonjour.casa/setup?d=…`); `manual` returns the resolved plaintext `fields` for the app's manual entry sheet (no payload is built) plus an `unsupported` map of settings manual entry cannot carry over (PIN, push/site binding, WireGuard).

**Connection**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `method` | ✅ | — | `qr`, `ble`, `deep_link`, or `manual` |
| `profile` | | — | Provision template (ID or name) whose saved fields seed this provision; also accepted as `template` |
| `host_url` | ✅ | — | HA URL the device connects to (e.g., `http://192.168.1.100:8123`) |
| `username` | ✅ | — | Target HA guest user account (matched by display name or login username) |
| `password` | | auto-generated | Specific password (otherwise random 12-char) |
| `device_alias` | | — | Alias auto-applied when the device first registers (within 30 min); never overwrites an existing alias |
| `pin` | | — | Max 6-digit PIN required before provisioning completes |

**App UI**

| Field | Default | Description |
|-------|---------|-------------|
| `default_dashboard` | — | Dashboard path to load on startup (e.g., `/lovelace/home`) |
| `welcome_url` | — | URL shown in a pop-up sheet after initial provisioning |
| `immersive_level` | `1` | `1` = standard, `2` = transparent status bar, `3` = fullscreen |
| `theme_color_mode` | `inherit` | `inherit`, `custom`, or `inherit_with_fallback` |
| `custom_color` | `#000000` | Hex color for status bar when using custom mode |

**Access Control**

| Field | Default | Description |
|-------|---------|-------------|
| `deauthenticate_existing` | `false` | Force-logout all active sessions for this user |
| `allow_all_pages` | `false` | Grant access to all dashboards (`/*`) |
| `allowed_pages` | `[]` | List of allowed paths (e.g., `/dashboard-1/*`) |
| `allowed_wifi` | `[]` | Wi-Fi SSIDs the app is restricted to |
| `require_alias` | `false` | Block the app with a name prompt until the device has an alias |

**Push Notifications & VPN**

| Field | Default | Description |
|-------|---------|-------------|
| `push_notifications` | `false` | `false`, `true`, or `mandatory` |
| `allow_wireguard` | `false` | Enable WireGuard VPN for this profile |
| `wireguard_config` | — | Paste the client's WireGuard config file content |
| `wireguard_excluded_wifi` | — | Comma-separated SSIDs where WireGuard stays off |

**Timing & Security**

| Field | Default | Description |
|-------|---------|-------------|
| `timeout_minutes` | `5` | QR/BLE scanning window in minutes. `0` = permanent |
| `expiration_hours` | `336` | App session duration in hours (14 days). `0` = permanent |
| `password_scramble` | `true` | Scramble the user's password on first redemption (the link works once), with the `password_scramble_in` timer as the fallback for links nobody uses. BLE beacons scramble on the timer only |
| `password_scramble_in` | `0` | Minutes until scramble. `0` = inherit from timeout |
| `delete_qr_after_window` | `true` | Delete QR image file after timeout (QR only) |
| `cache_control_hours` | — | Custom app asset cache duration (app defaults to 48h) |

**Method-Specific**

| Field | Applies To | Description |
|-------|-----------|-------------|
| `qr_filename` | QR | Custom filename (auto: `qr_[user]_[timestamp].png`) |
| `esphome_service` | All | List of ESPHome services to push the payload to (required for `ble`; with other methods, broadcast in addition to the primary output, and `successful_targets` is included in the response) |
| `connect_wifi_ssid` | All | Wi-Fi SSID the device should auto-join |
| `connect_wifi_password` | All | Password for the above network |
| `payload_version` | All | `2` (default, JSON + hybrid encryption) or `1` (legacy pipe-delimited) |
| `payload_decrypted` | All | `true` = plaintext payload (debugging only) |

> **Universal links:** the `universal_link` output only opens the app directly if `https://bonjour.casa/.well-known/apple-app-site-association` is served (JSON, no redirect) with the app's ID and a path matching `/setup`, and `/setup` should serve a fallback page (App Store link) for devices without the app. This is relay-server configuration, outside this integration.

---

### `casa.create_user`

Creates a local HA user account.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | ✅ | — | Display name |
| `username` | ✅ | — | Login username |
| `password` | | auto-generated | Account password |
| `local_only` | | `true` | Restrict to local network |

---

### `casa.remove_user`

Deletes a user account created via this integration and updates the internal tracker. Returns `{ status, username, user_id }`.

| Field | Required | Description |
|-------|----------|-------------|
| `username` | ✅ | Username to remove |

---

### `casa.notify_user`

Sends a push notification via the relay (`push.bonjour.casa`) to all registered devices of a user, or to a single device. Returns `{ success, sent_count, failed_count }`.

| Field | Required | Description |
|-------|----------|-------------|
| `username` | * | Target user (all their devices). Provide this OR `device_id` |
| `device_id` | * | Target a single device UUID |
| `title` | ✅ | Notification title |
| `message` | ✅ | Notification body |
| `data` | | Custom payload object/dictionary to pass with the notification |

---

### `casa.reload_device`

Sends a silent background push to clear cache and reload the default URL on a specific device.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | ✅ | Target device UUID |

---

### `casa.set_device_expiration`

Sets, extends, or clears a device's session expiration after provisioning. Delivered on the device's next heartbeat (up to ~5 minutes); shown as *pending* in the panel until the device confirms. Provide exactly one of the three value fields.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | ✅ | Target device UUID |
| `expires_at` | * | Absolute expiry (unix epoch seconds); `0` = never |
| `expires_in_hours` | * | Relative expiry: hours from now |
| `permanent` | * | `true` = remove the expiration entirely |

---

### `casa.deprovision_device`

**Destructive.** Remotely wipes a device and removes it from the server: sends a silent `deprovision` push (the app immediately wipes its session), revokes the device's HA refresh token, unregisters its relay proxy token, drops queued updates, and deletes the record. Offline or push-less devices are wiped lazily the next time they contact the server and their revoked session fails. Returns `{ status, push_sent, access_revoked }`.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | ✅ | Target device UUID |

---

### `casa.delete_device`

Removes a device's server-side record and revokes its access **without** wiping the app: revokes the HA session token, unregisters the relay proxy token, drops queued updates, and deletes the record. The app keeps its local session until its revoked token next fails. Use for stale/orphaned records; use `casa.deprovision_device` to remotely wipe. Returns `{ status, access_revoked }`.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | ✅ | Target device UUID |

---

### `casa.update_wireguard`

Pushes a new WireGuard configuration (or revokes the existing one) to a device or all of a user's devices. End-to-end encrypted by default so the relay can never read the config.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | * | Target device UUID. Provide this OR `username` |
| `username` | * | Target all devices of this user |
| `action` | | `update` (default) or `revoke` |
| `wireguard_config` | | Client config file content (required for `update`) |
| `wireguard_excluded_wifi` | | Comma-separated SSIDs where WireGuard stays off |
| `encrypt_config` | | Default `true`; disable only for debugging |
| `silent` | | Default `true`; disable to also show a notification |

---

### `casa.reconcile`

Diffs the relay's live proxy tokens against HA's device records, unregisters orphans, and flags devices needing re-registration. Also runs automatically once a day.

---

### `casa.regenerate_site`

**Destructive.** Rotates the site ID and site key on the relay. Breaks all existing push registrations; devices must re-register.

---

### `casa.register_device`

Registers a device for push notifications. Validates token format (64-char hex). Max 100 devices per user. Supports both integration-managed and native HA users.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | ✅ | Unique device identifier |
| `push_token` | ✅ | APNs device token (hex) |

---

### `casa.view_casa_users`

Lists users created via the integration with metadata. Returns response data.

| Field | Default | Description |
|-------|---------|-------------|
| `include_deleted` | `false` | Include deleted user records |

---

### `casa.list_tokens`

Lists all active refresh tokens/sessions for a user.

| Field | Required | Description |
|-------|----------|-------------|
| `username` | ✅ | Target user |

---

### `casa.remove_token`

Forcefully logs out a session by revoking its refresh token.

| Field | Required | Description |
|-------|----------|-------------|
| `username` | ✅ | Target user |
| `token_id` | ✅ | Token ID, or `*` to revoke all |

---

### `casa.scramble_guest_password`

Randomizes a user's password immediately.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `username` | ✅ | — | Target user |
| `deauthenticate` | | `true` | Also revoke all active sessions |

---

### `casa.clear_ble_beacon`

Stops ESPHome beacons from broadcasting.

| Field | Required | Description |
|-------|----------|-------------|
| `esphome_service` | ✅ | List of ESPHome services to clear |

---

### `casa.housekeeping`

Deletes old QR code images from the `www` folder.

| Field | Default | Description |
|-------|---------|-------------|
| `hours_old` | `24` | Delete files older than X hours |
| `prefix` | `qr_` | Filename prefix to target |

---

## HTTP API Endpoints

These are called by the Casa iOS app directly (authenticated via HA long-lived or refresh tokens).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/casa/register_device` | Register/update a device for push notifications |
| `GET` | `/api/casa/register_device?device_id=X` | Check if a device is registered |
| `DELETE` | `/api/casa/register_device?device_id=X` | Unregister push for a device (record retained — use `casa.delete_device` / `casa.deprovision_device` to remove a device) |
| `POST` | `/api/casa/heartbeat` | Device heartbeat. Response carries `reregister`, `updates`, `device_key`/`device_key_id`, and — while an admin override is pending — `expires_at` (the app applies it; `0` = permanent) |
| `GET`/`POST` | `/api/casa/profile_updates` | Pull / acknowledge queued profile & WireGuard updates |
| `POST` | `/api/casa/profile_report` | Device reports its current provisioning fields (`{device_id, fields}`) |
| `POST` | `/api/casa/location_report` | Encrypted location-state report; unauthenticated at the HTTP layer, body encrypted with the device key |
| `POST` | `/api/casa/deprovision` | Device removes its own record and session (`{device_id}`) |

Admin-only endpoints (used by the panel): `/api/casa/admin/summary`, `/api/casa/admin/device` (alias + `expires_at_override`), `/api/casa/admin/wireguard_profiles`, `/api/casa/admin/provision_profiles`, `/api/casa/admin/queue_update`, `/api/casa/admin/regenerate_device_key`, `/api/casa/admin/location_zones`, `/api/casa/admin/settings`, `/api/casa/admin/sessions`, `/api/casa/admin/check_username`, `/api/casa/admin/reauth_device`.

**Protocol:** see [`docs/provisioning-protocol.md`](docs/provisioning-protocol.md).

**Deploying to a test HA:** `CASA_DEPLOY_PASS=... ./deploy.sh` (optional `CASA_DEPLOY_HOST`, `CASA_DEPLOY_USER`; the script refuses to run without the password variable).

---

## Events

| Event | Fired When |
|-------|------------|
| `casa_code_redeemed` | A provisioned user logs in during the scanning window. Includes `username`, `client_name`, `ip_address`, `method`. |

---

## Provisioning Payload Format

**v2 (default):** a JSON profile object, DEFLATE-compressed and hybrid-encrypted (AES-256-GCM body, AES key RSA-OAEP-wrapped with the bundled public key), base64url-encoded. Field names match the JSON keys shown in `casa.provision`.

**v1 (legacy, `payload_version: 1`):** a pipe-delimited string of 21 fields, base64-encoded (or RSA-encrypted with the bundled public key):

| Index | Field |
|-------|-------|
| 0 | Server URL |
| 1 | Username |
| 2 | Password |
| 3 | Site ID |
| 4 | PIN |
| 5 | Default Dashboard |
| 6 | Welcome URL |
| 7 | Immersive Level |
| 8 | Theme Color Mode |
| 9 | Custom Color |
| 10 | Session Expiration (unix) |
| 11 | Code Expiration (unix) |
| 12 | Cache Control Hours |
| 13 | Allowed Paths |
| 14 | Allowed Wi-Fi |
| 15 | Push Notifications |
| 16 | WireGuard Enabled |
| 17 | WireGuard Config (base64) |
| 18 | WireGuard Excluded Wi-Fi |
| 19 | Connect Wi-Fi SSID |
| 20 | Connect Wi-Fi Password |

---

## Deprecated Services

These still work but forward to `casa.provision` internally:

- `casa.generate_qr` → use `casa.provision` with `method: qr`
- `casa.provision_ble_beacon` → use `casa.provision` with `method: ble`

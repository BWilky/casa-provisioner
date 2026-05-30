# Casa Provisioner

Home Assistant custom integration for provisioning and managing [Casa](https://bonjour.casa) iOS devices. Handles user creation, encrypted provisioning (QR / BLE / deep link), push notifications, WireGuard VPN configuration, and device lifecycle management.

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
| Regenerate Site ID | — | Regenerates both the site ID and site key (breaks existing push registrations) |

---

## Services

### `casa.provision`

Generates an encrypted provisioning payload. Supports three methods: `qr`, `ble`, and `deep_link`. Returns response data.

**Connection**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `method` | ✅ | — | `qr`, `ble`, or `deep_link` |
| `host_url` | ✅ | — | HA URL the device connects to (e.g., `http://192.168.1.100:8123`) |
| `username` | ✅ | — | Target HA guest user account |
| `password` | | auto-generated | Specific password (otherwise random 12-char) |
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
| `password_scramble` | `true` | Scramble the user's password after the window closes |
| `password_scramble_in` | `0` | Minutes until scramble. `0` = inherit from timeout |
| `delete_qr_after_window` | `true` | Delete QR image file after timeout (QR only) |
| `cache_control_hours` | — | Custom app asset cache duration (app defaults to 48h) |

**Method-Specific**

| Field | Applies To | Description |
|-------|-----------|-------------|
| `qr_filename` | QR | Custom filename (auto: `qr_[user]_[timestamp].png`) |
| `esphome_service` | BLE | List of ESPHome services to push payload to |
| `connect_wifi_ssid` | All | Wi-Fi SSID the device should auto-join |
| `connect_wifi_password` | All | Password for the above network |
| `payload_decrypted` | All | `true` = plaintext payload (debugging only) |

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

Sends a push notification to all registered devices for a user via the relay (`push.bonjour.casa` with automatic failover to `push2.bonjour.casa`). Returns `{ success, sent_count, failed_count }`.

| Field | Required | Description |
|-------|----------|-------------|
| `username` | ✅ | Target user |
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
| `DELETE` | `/api/casa/register_device?device_id=X` | Unregister a device |
| `POST` | `/api/casa/heartbeat` | Device heartbeat with metadata (IP, token, URL) |

---

## Events

| Event | Fired When |
|-------|------------|
| `casa_code_redeemed` | A provisioned user logs in during the scanning window. Includes `username`, `client_name`, `ip_address`, `method`. |

---

## Provisioning Payload Format

The payload is a pipe-delimited string of 21 fields, base64-encoded (or RSA-encrypted with the bundled public key):

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

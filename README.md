# Casa Provisioner

Home Assistant custom integration to provision and manage Casa devices.

## Installation (HACS)

1. Add custom repository: `https://github.com/bwilky/casa-provisioner` (Category: Integration)
2. Download **Casa Provisioner** in HACS.
3. Restart Home Assistant.
4. Add integration via **Settings > Devices & Services > Add Integration**.

---

## Architecture: Unified Provisioning

All provisioning flows are consolidated into a single service: `casa.provision`.
This service handles both QR code generation and ESPHome BLE beacon broadcasting via the `method` parameter (`qr` or `ble`).

---

## Service Calls

### `casa.provision`
Generates an encrypted provisioning payload. Returns response data.

| Field | Type | Description |
| :--- | :--- | :--- |
| `method` | string | **Required**. `qr` or `ble`. |
| `host_url` | string | **Required**. Home Assistant connection URL. |
| `username` | string | **Required**. Target Home Assistant guest user. |
| `password` | string | Optional. Specific password (otherwise auto-generated). |
| `pin` | string | Optional. Max 6-digit PIN code required by app. |
| `connect_wifi_ssid` | string | Optional. Auto-connect Wi-Fi SSID. |
| `connect_wifi_password` | string | Optional. Wi-Fi password. |
| `timeout_minutes` | integer | Scanning/broadcast window (mins). Default: `0` (permanent) for QR, `5` for BLE. |
| `delete_qr_after_window`| boolean| Delete QR file after timeout. Default: `true` (QR only). |
| `password_scramble` | boolean| Scramble user password after timeout. Default: `true`. |
| `password_scramble_in` | integer | Minutes to scramble password (0 = inherit from timeout). |
| `qr_filename` | string | Custom QR filename (QR only). |
| `esphome_service` | list | Target ESPHome services (e.g., `esphome.lobby_beacon_update_payload`) (BLE only). |
| `deauthenticate_existing`| boolean| Force logout of active sessions. Default: `false`. |
| `allow_all_pages` | boolean| Grant access to all dashboards. Default: `false`. |
| `allowed_pages` | list | Paths allowed (e.g., `/dashboard-1/*`). |
| `allowed_wifi` | list | Wi-Fi SSIDs the app is restricted to. |
| `default_dashboard` | string | Path to load on startup (e.g., `/lovelace/home`). |
| `welcome_url` | string | Pop-up URL to display after provisioning. |
| `immersive_level` | string | Status bar styling: `1` (default), `2` (transparent), `3` (fullscreen). |
| `theme_color_mode` | string | Status bar color: `inherit` (default), `custom`, `inherit_with_fallback`. |
| `custom_color` | string | Hex color for status bar (default: `#000000`). |
| `expiration_hours` | integer | App session duration. Default: `336` (14 days). `0` for permanent. |

### `casa.create_user`
Creates a local Home Assistant user account.

| Field | Type | Description |
| :--- | :--- | :--- |
| `name` | string | **Required**. Display name. |
| `username` | string | **Required**. Login username. |
| `password` | string | Optional password (auto-generated if empty). |
| `local_only` | boolean| Restrict user to local network. Default: `true`. |

### `casa.remove_token`
Forcefully logs out a user session.

| Field | Type | Description |
| :--- | :--- | :--- |
| `username` | string | **Required**. Target user. |
| `token_id` | string | **Required**. Specific token ID, or `*` to delete all. |

### `casa.list_tokens`
Lists active tokens/sessions for a user.

| Field | Type | Description |
| :--- | :--- | :--- |
| `username` | string | **Required**. Target user. |

### `casa.scramble_guest_password`
Manually scrambles a user's password.

| Field | Type | Description |
| :--- | :--- | :--- |
| `username` | string | **Required**. Target user. |
| `deauthenticate` | boolean| Delete all active sessions. Default: `true`. |

### `casa.clear_ble_beacon`
Stops specific ESPHome beacons from broadcasting.

| Field | Type | Description |
| :--- | :--- | :--- |
| `esphome_service` | list | **Required**. Target ESPHome services to clear. |

### `casa.housekeeping`
Deletes old QR codes from `www` folder.

| Field | Type | Description |
| :--- | :--- | :--- |
| `hours_old` | number | Delete files older than X hours. Default: `24`. |
| `prefix` | string | Filename prefix. Default: `qr_`. |

### Deprecated Services
* `casa.generate_qr` (Use `casa.provision` with `method: qr`)
* `casa.provision_ble_beacon` (Use `casa.provision` with `method: ble`)

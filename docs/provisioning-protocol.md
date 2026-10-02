# Casa provisioning protocol

Status: contract for release 26.10.01; items marked (26.09.30) or (26.10.01) landed in that release.

## 1. Overview

The provisioner (this Home Assistant integration) produces provisioning links and QR codes. The iOS app consumes them: it decrypts the payload with the private half of `casa_public.pem`, logs in to Home Assistant, then talks to the device endpoints in section 8. The Lovelace card and the admin panel are front ends to the `casa.provision` service (`_provision_internal`); they display the link or QR and never build payloads themselves. The panel's payload preview mirrors the server's v2 field order.

## 2. Links

Built by `build_links(payload, version)`. `UNIVERSAL_LINK_SETUP_URL` is `https://bonjour.casa/setup`.

| Version | Deep link | Universal link | Payload encoding |
|---|---|---|---|
| v2 | `hascasa://setup?data=<P>` | `https://bonjour.casa/setup?d=<P>` | `P` is padding-stripped base64url of the envelope; inserted untouched, never percent-encoded |
| v1 (frozen) | `hascasa://setup?data=<quote(P)>` | `https://bonjour.casa/setup?d=<quote(P, safe='')>` | `P` is standard base64; the deep link leaves `/` raw (`urllib.parse.quote` default), the universal link also percent-encodes `/` |

Any other version raises `ValueError`. The v1 rules are frozen and will not change.

The app accepts `data` and `d` on either scheme.

## 3. v2 envelope

Produced by `_encrypt_payload_hybrid`. Before base64url:

```
0x02 || RSA-OAEP-SHA256(aes_key)[256] || nonce[12] || AES-256-GCM(raw-DEFLATE(json)) || tag[16]
```

- RSA public key: `casa_public.pem`, RSA-2048, OAEP with SHA-256 for both the hash and MGF1, no label. RSA wraps only the 32-byte AES key, so the JSON has no size limit.
- AES-256-GCM with a 12-byte random nonce and no AAD. The 16-byte tag is the last 16 bytes of the ciphertext.
- Compression is raw DEFLATE (wbits -15, level 9): no zlib header and no Adler-32 trailer.
- The JSON is compact (`separators=(",", ":")`).
- Encoding is base64url with the trailing `=` stripped.

## 4. v2 payload fields

Keys are emitted in this order (the `profile` dict in `_provision_internal`).

| Key | Type | Meaning |
|---|---|---|
| `v` | int | Payload format version, `2`. |
| `server_version` | string | `CASA_VERSION` of the provisioner. Display only. |
| `server_url` | string | Home Assistant URL the app connects to. |
| `username` | string | Login username. |
| `password` | string | Login password. |
| `site_id` | string | Identifier of this HA instance at the push relay. |
| `pin` | string | Setup PIN for the device. |
| `default_dashboard` | string | Dashboard path opened by default. |
| `welcome_url` | string | Optional welcome page URL; empty if none. |
| `immersive_level` | string | Immersive display level. |
| `theme_color_mode` | string | Theme color mode. |
| `custom_color` | string | Custom color, hex. |
| `session_expiration` | int | Session expiry, unix seconds; `0` means none. |
| `expiration` | int | Link expiry, unix seconds; `0` means none. |
| `cache_control_hours` | string | Cache-control hours. |
| `allowed_pages` | string | Allowed page patterns, e.g. `/*`. |
| `allowed_wifi` | string | Wi-Fi allow list; empty for any. |
| `require_alias` | bool | Device must be given an alias at setup. |
| `push_notifications` | string | Normalized push setting. |
| `wireguard` | object | `allowed` (bool), `config` (string), `excluded_wifi` (string). |
| `connect_wifi` | object | `ssid` (string), `password` (string). |
| `location_zones` | object, optional | `anchors` (list), `config_version`; present only when zones are configured. |
| `relay_url` | string, optional (26.10.01) | Push relay base URL without trailing slash, e.g. `https://relay.example`. Present only when the site uses a non-default relay; absent means `https://push.bonjour.casa`. `site_id` is only valid at this relay. |
| `device_alias` | string, optional (26.10.03) | Name the admin gave the device in the wizard or a re-provision. Present only when set. Apps treat the device as named (no name prompt) and send it as the heartbeat `alias`; the server keeps any alias it already has. |

`pin` (26.10.01): when non-empty, the app prompts for it before provisioning for every
method (QR, deep link, manual, BLE) and for v1 and v2 payloads alike; a wrong PIN allows
a retry, cancel aborts, and five wrong attempts abort.

## 5. Version policy

`v` is the payload format version. Adding an optional key never bumps it (apps ignore keys they don't know, such as `relay_url` on older builds); removing, renaming, retyping a key, or changing the envelope does. `server_version` is `CASA_VERSION` for display only. The app supports a set of `v` values and shows an "Update Casa" alert naming both versions when it sees one it does not support — it does not attempt a partial parse. The server never emits a `v` the current app cannot parse without also bumping the app first; `payload_version: 1` stays available for app builds that predate v2.

## 6. Single use (26.09.30)

When `password_scramble` is on, the login password in a link is scrambled on first redemption: the login listener fires `casa_code_redeemed`, then the window closes — the password is scrambled, any QR file is retired, and the listener and fallback timer stop. Unredeemed links are scrambled by the timer at the scramble deadline.

(26.10.01) The window (user id, login, scramble deadline, scanning-window end, listener deadline, known session tokens, QR file) is persisted in the integration store, keyed by HA user id, and re-armed when the integration loads. A reload or restart no longer cancels single use or expiry; a window whose deadline passed while HA was down is closed immediately, and a phone that redeemed the link meanwhile is detected by its new session token. A deleted user or login closes the window without error. `casa_code_redeemed` carries `user_id` and `provision_id` (also returned by `casa.provision`) so a client can match its own code.

Listener TTL: while single use is armed, the listener runs for the scramble window plus 30 s (`password_scramble_in`, or the scanning window when that is 0), capped at 24 h. It polls every 2 s for the first 30 minutes, then every 10 s. Without single use the listener keeps its 30-minute cap.

BLE is excluded: for `method: ble` the password is not scrambled on redemption (a scrambled password would strand the beacon still broadcasting it); the timer alone scrambles it. With `password_scramble` off, the link stays multi-use until its window ends.

## 7. QR delivery (26.09.30)

For `method: qr` the response carries `qr_data_uri` (`data:image/png;base64,...`), the PNG of the deep link rendered in memory. Clients use it.

(26.10.01) No file is written by default: `/local/` is served without authentication, so a QR image there exposes live credentials. `filename`/`url_path` are `null` unless the caller passes `qr_filename`, which is reduced to a safe basename (`[A-Za-z0-9._-]`, `.png`; paths and `..` are rejected) and written to `www/` for the window only — it is deleted when the window closes (or overwritten with an EXPIRED image when `delete_qr_after_window: false`). The shared `www/casa_qr.png` is no longer written. `url_path` remains deprecated.

## 8. Device endpoints

All use the device's HA bearer token unless noted. Queue and update mechanics are in [app-integration.md](app-integration.md); they are not repeated here.

| Endpoint | Methods | Notes |
|---|---|---|
| `/api/casa/register_device` | POST, GET, DELETE | Register, read, and remove the device record. (26.10.01) One HA user owns a device_id; registering it as another user moves the record. Also callable as the `casa.register_device` service with a response. |
| `/api/casa/heartbeat` | POST | Check-in; flags pending updates. (26.10.01) Response adds `site_id` and `relay_url`. |
| `/api/casa/profile_updates` | GET, POST | Pull queued updates; acknowledge by id. |
| `/api/casa/profile_report` | POST | Device reports its current profile fields. |
| `/api/casa/location_report` | POST | No bearer token. The body is encrypted with the device key, and that encryption is the authentication. |
| `/api/casa/deprovision` (26.09.30) | POST | Body `{"device_id": "<id>"}`. The device must belong to the calling user, otherwise 404 `{"error": "Device not found"}`; missing `device_id` is 400. Removes the device record and revokes its access; the HA user account stays. Response `{"status": "success", "access_revoked": bool}`. The app treats any 2xx, 4xx, or timeout as done and continues its local wipe. |

## 9. Push commands

Sent through the relay as `data.command`.

| Command | Purpose |
|---|---|
| `casa_update` | Deliver an encrypted queued update. |
| `request_heartbeat` | Ask the device to check in. |
| `request_profile_report` | Ask the device to send a profile report. |
| `deprovision` | Tell the device to wipe itself. |
| `clear_cache_and_reload` | Clear the web cache and reload. |
| `wireguard_update` | Push a WireGuard configuration. |
| `wireguard_revoke` | Revoke the WireGuard configuration. |

Visible notifications from `casa.notify_user` carry no `data.command`; any `data` is caller-supplied and the app must not route them through command dispatch. `wireguard_update` / `wireguard_revoke` may be sent as visible alerts (push_type alert) when not silent.

## 10. Relay

The push relay protocol is documented in the sibling repository, section "Protocol 1" of `push-notifications-relay/README.md`. Not copied here. (26.09.30) At setup the provisioner probes the relay with `GET <relay_base>/health` (5 s timeout). It records `relay_version` and `relay_protocol` and reports them in `/api/casa/admin/summary`. A protocol other than 1 logs one warning; a relay that omits the fields yields `relay_protocol: null` with no warning. The probe never blocks setup.

## 11. Test vectors

`tests/vectors/provisioning-v2.json`, regenerated by `tests/vectors/generate_vectors.py`:

- `plaintext`: a complete v2 object with test values.
- `test_public_key_pem`, `test_private_key_pem`: a throwaway RSA-2048 pair. The private half is committed so the iOS tests can decrypt the envelope.
- `envelope_b64url`: `plaintext` encrypted to the test public key.
- `deep_link`, `universal_link`: the links built from `envelope_b64url`.

The vectors are never generated with the production key.

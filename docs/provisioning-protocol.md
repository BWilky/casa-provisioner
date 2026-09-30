# Casa provisioning protocol

Status: contract for release 26.09.30; items marked (26.09.30) land in that release.

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

## 5. Version policy

`v` is the payload format version. Adding an optional key never bumps it; removing, renaming, retyping a key, or changing the envelope does. `server_version` is `CASA_VERSION` for display only. The app supports a set of `v` values and shows an "Update Casa" alert naming both versions when it sees one it does not support. The server never emits a `v` the current app cannot parse without also bumping the app first.

## 6. Single use (26.09.30)

When `password_scramble` is on, the login password in a link is scrambled on first redemption: the login listener fires `casa_code_redeemed`, then scrambles the password and stops listening, and the pending cleanup timer for that user is cancelled. A link is usable exactly once. The cleanup timer remains the fallback for links nobody redeems. With `password_scramble` off, the link stays multi-use until its window ends.

## 7. QR delivery (26.09.30)

For `method: qr` the response carries `qr_data_uri` (`data:image/png;base64,...`), the PNG of the deep link rendered in memory. Clients prefer it. `url_path` (a `/local/<file>.png` path) is deprecated this release: it is still returned, with `url_path_deprecated: true`, and will be removed in the next release.

## 8. Device endpoints

All use the device's HA bearer token unless noted. Queue and update mechanics are in [app-integration.md](app-integration.md); they are not repeated here.

| Endpoint | Methods | Notes |
|---|---|---|
| `/api/casa/register_device` | POST, GET, DELETE | Register, read, and remove the device record. |
| `/api/casa/heartbeat` | POST | Check-in; flags pending updates. |
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
| `notify_user` | Visible notification (the `casa.notify_user` service). |

## 10. Relay

The push relay protocol is documented in the sibling repository, section "Protocol 1" of `push-notifications-relay/README.md`. Not copied here. (26.09.30) At setup the provisioner probes the relay with `GET <relay_base>/health` (5 s timeout). It records `relay_version` and `relay_protocol` and reports them in `/api/casa/admin/summary`. A protocol other than 1 logs one warning; a relay that omits the fields yields `relay_protocol: null` with no warning. The probe never blocks setup.

## 11. Test vectors

`tests/vectors/provisioning-v2.json`, regenerated by `tests/vectors/generate_vectors.py`:

- `plaintext`: a complete v2 object with test values.
- `test_public_key_pem`, `test_private_key_pem`: a throwaway RSA-2048 pair. The private half is committed so the iOS tests can decrypt the envelope.
- `envelope_b64url`: `plaintext` encrypted to the test public key.
- `deep_link`, `universal_link`: the links built from `envelope_b64url`.

The vectors are never generated with the production key.

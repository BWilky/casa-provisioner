# Provisioning parity — provisioner and card design

Date: 2026-09-30
Status: written under the user's autonomy grant ("go ahead with your own
recommendations"); every choice below was the recommended option in the
earlier conversation or is marked **Ruling**.
Sub-project 2 of 3 (relay → **provisioner + card** → iOS app)

## Why

The provisioner is the source of every provisioning link, QR and payload,
and the server the app talks to. Today it has no written contract for that
protocol, no tests for link generation, a manifest version that disagrees
with its own version constant, and a few behaviours the app and card have
drifted from (the card reads an expiry key the server never sends; the QR is
an unauthenticated PNG on disk; links never become single-use; the payload
version is emitted but nobody checks it). The app work in sub-project 3
needs one new server endpoint (device self-deprovision) and a payload the
app can version-gate. This sub-project delivers those and the parity fixes.

## Scope

In scope:

1. Protocol contract document plus machine-readable test vectors.
2. Payload version fields and the version policy.
3. Device self-deprovision endpoint.
4. Links become single-use: scramble on first redemption.
5. QR delivered inline as a data URI; files kept one release, deprecated.
6. Version alignment: `manifest.json` = `CASA_VERSION` = `PANEL_VERSION` = `26.09.30`, enforced by a test.
7. Panel payload preview parity (`require_alias`, `location_zones`).
8. Relay protocol probe at setup, surfaced in the admin summary.
9. Relay base URL configurable per site (integration option).
10. Card fixes: expiry key, README, data-URI rendering, no external QR service, version banner.
11. Doc fixes to `docs/app-integration.md` from the audit.
12. `deploy.sh` reads the host password from the environment, never from the file.
13. Tests for 1–9 that run with plain `pytest` and the existing `conftest.py` stubs.

Out of scope: moving the private key out of the app, signing payloads,
authentication for `/local`, BLE provisioning changes, HA user deletion on
self-deprovision (the account stays; admins decide).

## 1. Protocol contract and vectors

`docs/provisioning-protocol.md` is the single source of truth for:

- **Link grammar.** `hascasa://setup?data=<P>` and
  `https://bonjour.casa/setup?d=<P>`. For v2, `P` is padding-stripped
  base64url of the envelope, not percent-encoded. For legacy v1 the grammar
  is documented as-is (standard base64, percent-quoted) and marked frozen.
  The app must accept both `data` and `d` on either scheme (sub-project 3).
- **v2 envelope.** `0x02 ‖ RSA-OAEP-SHA256(AES key)[256] ‖ nonce[12] ‖
  AES-256-GCM(raw-DEFLATE(JSON))‖tag[16]`. Public key `casa_public.pem`,
  RSA-2048. No AAD.
- **v2 JSON fields.** Every key with type, required/optional, and meaning,
  in the order the server emits them, including `v`, `server_version`
  (new), `require_alias`, `location_zones`.
- **Version policy.** `v` is the payload format version. Adding an optional
  key never bumps it; removing, renaming, retyping a key, or changing the
  envelope does. `server_version` is `CASA_VERSION` for display only. The
  app supports a set of `v` values and shows an "Update Casa" alert naming
  both versions when it sees one it does not support. The server never
  emits a `v` the current app cannot parse without also bumping the app
  first.
- **Device endpoints.** Auth, request, response for `register_device`
  (POST/GET/DELETE), `heartbeat`, `profile_updates` (GET/POST),
  `profile_report`, `location_report`, and the new `deprovision`. Copies
  what `docs/app-integration.md` has and corrects the audit drift (see 11).
- **Push commands.** `casa_update`, `request_heartbeat`,
  `request_profile_report`, `deprovision`, `clear_cache_and_reload`,
  `wireguard_update`, `wireguard_revoke`, `notify_user`.
- **Relay protocol.** Pointer to the relay README "Protocol 1" and how the
  provisioner probes it (section 8).

`tests/vectors/provisioning-v2.json` holds:

- `plaintext`: a complete v2 JSON object (test values, no real secrets).
- `test_public_key_pem` / `test_private_key_pem`: a throwaway RSA-2048 pair
  generated once for the vectors and committed. Never the production key.
- `envelope_b64url`: the plaintext encrypted with the test public key,
  committed so the Swift tests (sub-project 3) can decrypt it with the same
  test private key and assert field-by-field equality.
- `deep_link`, `universal_link`: the two links built from `envelope_b64url`.

Python tests: envelope structure (`0x02`, length ≥ 269, base64url with no
`=`), round-trip decrypt with the test private key equals `plaintext`, links
match the grammar byte-for-byte.

## 2. Payload version fields

- v2 JSON gains `"server_version": CASA_VERSION` right after `"v": 2`.
  Additive, so `v` stays 2.
- `panel/payload-preview.js` mirrors it.
- **Ruling:** no `min_app_version` field. The app's supported-set check on
  `v` is the gate; a minimum-app-version knob is YAGNI until a real
  incompatibility exists.

## 3. Device self-deprovision endpoint

`POST /api/casa/deprovision` — HA session auth (the device's own bearer).

Request: `{"device_id": "<id>"}`.

Authorization: the device must belong to the calling user — present in
`stored_data["users"][user.id]["devices"]` (and that user not `deleted`), or
in `stored_data["native_devices"][user.id]`. Anything else → 404
`{"error": "Device not found"}` (not 403, to avoid confirming other users'
device ids). Missing `device_id` → 400.

Effect: `_purge_device(hass, device_id)` (pops the record, unregisters the
relay proxy token, revokes the device's own refresh token, drops its queued
updates) then `_remove_registry_device(hass, device_id)`. The HA user
account is untouched. Response `{"status": "success", "access_revoked":
bool}`. The response is sent on the request that carried the now-revoked
token; HA has already authenticated it, so that is fine, and the app treats
any 2xx/4xx/timeout as "done, continue the local wipe".

Logged at info: `CASA: Device '<id>' self-deprovisioned by user '<name>'.`

Registered alongside the other views in `async_setup_entry`. Documented in
the contract and in `docs/app-integration.md`.

A pure helper `_device_owned_by(stored_data, user_id, device_id) -> bool`
carries the authorization rule so it is unit-testable without aiohttp.

## 4. Scramble on first redemption

Today `_login_listener` fires `casa_code_redeemed` when a new refresh token
appears and does nothing else; the password scrambles only when the
`_cleanup_sequence` timer fires.

Design: extract the scramble body of `_cleanup_sequence` into
`async def _scramble_and_close(hass, username, auth_provider)` which
generates a new password, saves it, and cancels the login listener. The
listener receives `on_redeemed: Callable[[], Awaitable[None]] | None`; when
`password_scramble` is true the provisioner passes a closure that awaits
`_scramble_and_close` and also cancels the pending cleanup timer for that
user (`hass.data[DOMAIN]["timers"]`). Order inside the listener: fire the
event first (the card's success pane depends on it), then scramble.

Result: a link is usable exactly once when scrambling is on; the timer
remains as the fallback for links nobody redeems. `password_scramble=false`
keeps today's behaviour (multi-use until the window ends). `ble` keeps its
current behaviour too; only the listener path changes and it is shared.

Documented in the contract under "Single use".

## 5. QR as data URI

`_provision_internal` for `method == "qr"`:

- Renders the PNG in memory (`qrcode.make(deep_link)` → `BytesIO`) and
  returns `"qr_data_uri": "data:image/png;base64,<...>"` in the response.
- Keeps writing the files and returning `url_path` **this release only**,
  and adds `"url_path_deprecated": true`. The removal is noted in the
  contract for the next release.
- The `_cleanup_sequence` QR-file actions are unchanged this release.

Panel (`provision.js`, `provision-guided.js`): `<img src>` uses
`qr_data_uri` when present, else `url_path`.

Card (`index.js`): same preference; the `url_path`-missing error only
triggers when neither key is present.

## 6. Version alignment

- `CASA_VERSION = "26.09.30"`, `PANEL_VERSION = "26.09.30"`,
  `manifest.json` `"version": "26.09.30"`.
- New test `tests/test_versions.py` reads all three and asserts equality
  (regex over `const.py` and `version.js`, JSON for the manifest). This is
  what makes the memory note about bumping them together enforceable.

## 7. Panel payload preview

`panel/payload-preview.js` `buildV2PayloadPreview` adds `server_version`,
`require_alias`, and `location_zones` (when zones exist) in the same
positions the server emits them.

## 8. Relay protocol probe

At setup, after site verification, the provisioner `GET`s
`<relay_base>/health` with a 5 s timeout. It stores
`relay_version`/`relay_protocol` in `hass.data[DOMAIN]` (not persisted) and
surfaces both in `/api/casa/admin/summary`. Unknown protocol (≠ 1) → one
warning log; older relays without the fields → `relay_protocol: null`, no
warning. Never blocks setup.

## 9. Relay base URL option

New integration option `relay_base_url` (default `https://push.bonjour.casa`)
in the options flow next to `admin_system_only`. `const.py` keeps
`RELAY_BASE_URL` as the default only; every relay call goes through
`relay_url(hass, path)` which reads the option. Changing the option and
reloading the entry re-verifies the site against the new relay. Purpose:
test relay changes from Fairwater against a local or staging relay without
touching production. Documented in README.

## 10. Card

- Read `expires_at` (fall back to `qr_expires_at` / `ble_expires_at` for
  older servers) for both QR and BLE countdowns.
- Render `qr_data_uri` when present.
- Remove the `api.qrserver.com` dependency: the app-links pane shows the
  two store links as buttons; no QR for them. **Ruling:** the card has no
  QR library and pulling one in for a static store link is not worth the
  weight; a link is enough on a phone.
- `const CARD_VERSION = "26.09.30"` and the customary `console.info`
  banner.
- README: `casa.provision` with `method: ble` replaces the non-existent
  `casa.start_ble`; `timeout_minutes` replaces `duration`; document
  `intro: true`; fix the editor's duplicate `domain` key.
- `hacs.json` unchanged; tag the commit `v26.09.30` so HACS can pin.

## 11. `docs/app-integration.md` fixes

From the audit: `location_report` has no bearer auth (device-key
encryption is the auth); heartbeat response also carries
`profile_report_interval_seconds`, `location_config_version` (always
present, may be null), and conditional `expires_at`; queued update types
also include `auth` and `location`; `profile_report` body `{device_id,
fields}`; document push commands `deprovision` and `clear_cache_and_reload`;
legacy WireGuard push sends `title`/`message` as `""` when silent; add the
`deprovision` endpoint. The contract document links here rather than
duplicating the queue/update mechanics.

## 12. deploy.sh

`PASS="${CASA_DEPLOY_PASS:?set CASA_DEPLOY_PASS}"`; `HOST` and `USER`
overridable the same way with today's defaults. README gains a one-line
"deploying to a test HA" note. `.vscode/sftp.json` stays git-ignored and is
not touched.

## 13. Tests

All under `tests/`, runnable with `pytest` using the existing
`conftest.py` stubs (HA, aiohttp, qrcode are stubbed when absent; only
`cryptography` and `pytest` are needed):

- `test_protocol_vectors.py` (section 1).
- `test_links.py`: the module-level `build_links(payload_b64url,
  version)` helper returns exactly the grammar (extracted from
  `_provision_internal` so it is testable; v1 keeps the two different
  quoting rules, documented as frozen).
- `test_deprovision_auth.py`: `_device_owned_by` truth table (managed user,
  native user, deleted user, other user's device, unknown id).
- `test_versions.py` (section 6).
- `test_scramble_on_redeem.py`: the listener with a fake `hass.auth`
  yields a new token → `on_redeemed` awaited once, event fired first.
- `test_relay_url.py`: `relay_url` uses the option when set, default
  otherwise.

## Deploy and verification

`deploy.sh` to Fairwater (authorized test host). Then:

- panel shows version `26.09.30` with no skew banner;
- `casa.provision` (deep_link) returns `server_version`, and for `qr`
  returns `qr_data_uri`;
- redeem a link once in the simulator, confirm the password scrambled
  immediately (second attempt with the same link fails auth) and the
  `casa_code_redeemed` event fired;
- `POST /api/casa/deprovision` from the device removes it from the summary
  and revokes its token;
- admin summary shows `relay_version`/`relay_protocol` (null until the
  relay deploy lands).

Barnabas is not touched in this sub-project.

## Interfaces changed (consumers: card, panel, iOS app)

- v2 payload: `+server_version` (additive).
- `casa.provision` response: `+qr_data_uri`, `+url_path_deprecated`.
- New endpoint `POST /api/casa/deprovision`.
- Admin summary: `+relay_version`, `+relay_protocol`.
- New integration option `relay_base_url`.
- `deploy.sh` requires `CASA_DEPLOY_PASS`.

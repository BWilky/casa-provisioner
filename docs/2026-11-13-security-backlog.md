# Security, parity and lean backlog (2026-11-13)

Source: four read-only reviews on 2026-11-13: relay, provisioner, iOS, and a cross-repo parity pass. Items marked ✔ were re-checked by hand against the code.
Repos:
- relay: `push-notifications-relay`, 1.2.0
- casa: `casa-provisioner`, 26.11.13
- iOS: `casa-mobile-app/casa-ios`, 1.95 build 21, uncommitted work in tree

## Decisions (from Bryce, 2026-11-13)

- **Keep old clients working.** Real installs exist, so every protocol change ships in two steps:
  1. The new side accepts old and new.
  2. A later release removes the old path.

  Nothing that old apps (1.93/1.94) or older casa versions depend on is removed in step 1.
- **Provisioning link keeps its key in the link.** HA encrypts each link with a random per-link key and carries that key in the link. There is no relay mailbox.
- **Payload encryption must work with the relay down.** No key can live in the relay. Provisioning links and any notification-content encryption are keyed by HA and the device only.
- **Remove the `push2.bonjour.casa` fallback** from the app.
- **Relay sign-up stays open.** Add format validation and expiry so junk ages out.

## Open questions (ask before starting the item)

- **Q1, BLE provisioning beacons. ANSWERED 2026-11-13: leave as is for now and do not change them.** BLE is out of scope for X1, C2 and Phase 5.
- **Q2, `admin_system_only` option.** Options: remove it (services always admin or system), or keep it but always guard the dangerous services? Default if unanswered: keep the option, always guard scramble/provision/remove_token.
- **Q3, relay expiry period.** Default: 180 days since last use for sites and registrations.
- **Q4, WireGuard key rotation after provisioning.** The link is a bearer secret that carries the WG private key. Rotating the device's WG key once it first heartbeats would make a captured link useless afterwards. Do we want that? Needed for item X1.

## Phase 0: relay outage (ops, in progress)

- [ ] **0.1 Find out why billing was disabled (Nov 8 → Nov 13).** The relay answered 500 "billing is disabled" from 2026-11-08 22:24 UTC to 2026-11-13 18:46 UTC; the last good `/send` was Nov 8 08:30 UTC. Find why billing lapsed, and set up a budget alert or billing alert email.
- [ ] **0.2 Fix Google's front-end 503s.** After billing came back, both run.app and push.bonjour.casa mostly return Google front-end 503 "not available yet", with an occasional 200. The service is Ready, the APIs are enabled, `allUsers` has invoker, and requests never reach the container. Check the Cloud console for a project notice. Re-check after any redeploy.
- [x] **0.3 Flipped `APNS__USE_SANDBOX=false`** (revision push-relay-00015). Xcode debug builds no longer receive pushes. Dev tokens get BadDeviceToken and are pruned.

## Phase 1: relay hardening (backward compatible, protocol stays 1). DONE 2026-11-13

Shipped as relay 1.3.0: merged to main, tag v1.3.0, deployed as revision push-relay-00017, Firestore TTL policies on `expire_at` enabled. Still to do: confirm a real HA send arrives on a TestFlight phone.

Plan: `push-notifications-relay/docs/superpowers/plans/2026-11-13-relay-hardening.md`

- [x] **R1 ✔ Real body cap.** Count bytes actually received; the current check trusts Content-Length, so a chunked request bypasses it (`main.py:138`).
- [x] **R2 ✔ 422 responses stop echoing the request.** Today they include `site_key` in the response and in Cloud Logging (`main.py:344`).
- [x] **R3 Strict field formats.**
  - Hex device and proxy tokens.
  - A safe `site_id` charset everywhere.
  - Length caps.
  - `push_type` limited to alert or background, with priority derived from it.
  - Drop `topic` and `collapse_key`. Pydantic ignores unknown fields, so old HA keeps working.
- [x] **R4 ✔ Prune only on real token errors.** Prune on BadDeviceToken, Unregistered and DeviceTokenNotForTopic only. Other APNs 400s become 502. APNs results never penalise the caller's IP (`apns_client.py:124`).
- [x] **R5 Remove dead surface.** Remove:
  - `/send_batch` (nothing calls it).
  - Silent mode.
  - The relay encryptor (no iOS counterpart; it breaks the relay-down rule).
  - API-key auth.
  - APNs certificate auth.
  - The FastAPI docs routes.
  - `config.example.yaml` from the image.
- [x] **R6 Firestore is the only production store.** Remove `none` (an open raw-token APNs relay) and SQLite. Keep in-memory for tests only.
- [x] **R7 Site keys.**
  - Atomic create.
  - Store SHA-256 at rest, migrating existing keys lazily.
  - Give the per-instance key cache a TTL, so a removed site stops working everywhere.
- [x] **R8 Expiry.** Add `expire_at` to sites and registrations, refreshed on use (at most daily), plus a Firestore TTL policy. See Q3.
- [x] **R9 Rate limiter.** Bound dict sizes and bucket IPv6 by /64.
- [x] **R10 Release.**
  - Pin dependencies.
  - Pin the base image by digest.
  - Bump version to 1.3.0, update the README, deploy, verify.
  - Enable TTL policies.

## Phase 2: casa provisioner fixes (no protocol change). Built 2026-11-13, not yet released

On branch `security-phase2` as casa 26.11.14. Plan: `docs/superpowers/plans/2026-11-13-security-phase2.md`.
- **Done:** C1–C7, C10–C14, plus the final-review fixes.
- **Partial, by ruling:**
  - C2 detects a reused link and alerts the admin; it does not revoke the extra session.
  - C9 covers uninstall only. Regen rows, auth entries and the WireGuard views keep their secrets.
  - C10 still stores `last_12_token` and keeps its sensors.
  - C13 leaves the Reload button open to any HA user.
- **Deferred to Phase 4:** C8.
- **Deferred minors:** the remove_user race, `site_id` churn on a 5xx during the heal, a startup de-dupe of double-pinned records, stripping `action_data` without actions, removing stores through the live Store instances, and the missing tests.

- [x] **C1 ✔ Claim takeover.** A fresh provisioning claim may only move the device it was issued for, `replaces_device_id`. Drop the time-based path for foreign records (`__init__.py:7054`, `:769`).
- [ ] **C2 True single-use links.** On redemption, revoke every new session except the first and alert the admin if there were several (`:446-496`, `:987`).
- [x] **C3 Admin panel XSS.** Render `current_url` as a link only when it is http(s) (`panel/views/device-editor.js:489`).
- [x] **C4 Service authorization.**
  - scramble, provision and remove_token are admin or system only, regardless of the option (see Q2).
  - Restrict them to Casa-managed users.
  - Never allow removing an admin's or the owner's tokens.
- [x] **C5 Strip reserved keys from `notify_user` data.** Strip `command`, `casa_command`, `aps`, `casa_update`, `update_payload`, `wireguard_payload`, `encrypted` and `device_key_id`. Stop forwarding `action_data` to the device; it is already stored server-side. Cap the full push estimate at 4 KB before creating the pending record.
- [x] **C6 React to relay 403s.**
  - "registration invalid or expired": mark the device `needs_reregister` right away.
  - "Invalid site key": re-verify or re-register the site, then reconcile.

  Today recovery waits up to about 24 h for the daily reconcile.
- [x] **C7 Keep `site_id` only after `/register_site` succeeds.** Retry with backoff instead of only on restart (`:328`).
- [ ] **C8 WireGuard updates go through the queue.**
  - `update_wireguard` uses the acked `profile_updates` queue; the push becomes only a nudge.
  - Don't mark the update applied on relay 200 (`:6630-6650`).
  - Drop the `encrypt_config` legacy path once old apps are gone (Phase 5).
- [ ] **C9 Secrets at rest.**
  - Uninstall removes all six casa stores.
  - Never persist WireGuard configs outside the profiles store.
  - Strip the Wi‑Fi password, PIN and WG config from regen, pending and expired rows.
  - TTL on undelivered auth queue entries.
  - Redact configs in the WG profiles GET.
- [ ] **C10 Logging and identity.**
  - No `site_key` or full push token in logs, including DEBUG (`:6277`).
  - Use `request.remote`, not X-Forwarded-For or a body-supplied `ip_address`.
  - Stop storing `last_12_token`; drop the token-suffix and push-token sensors.
- [x] **C11 Deleted users.** `remove_user` purges the user's device records, their relay tokens, `provision_claims`, `provision_opened` and `server_passwords`. `notify_user` by device_id honours the deleted flag.
- [x] **C12 Sibling re-pin.** Re-pin only when the caller presents the current pinned token or a fresh claim (`:1928`, `:4896`).
- [ ] **C13 Native device auto-create.** Non-admin users can't mass-create native device records (cap per user; admin approval?). The Reload button is admin only.
- [x] **C14 Location report replay.** Reject a repeated (device_id, ts) within the ±300 s window.

## Phase 3: iOS fixes (no protocol change). Built 2026-11-13, uncommitted, not device-tested

- **Where:** in the casa-ios working tree, on top of the uncommitted 1.95 work (Bryce: do not commit).
- **Patch and notes:** `casa-ios/docs/patches/2026-11-13-phase3-security.patch` and `casa-ios/docs/2026-11-13-phase3-security-notes.md` (change list, device checklist, deferred items).
- **Done:** I1–I9, plus the fixes from the independent review:
  - Certificate pinning remembers a public-CA host as "system", pins the name for a CA certificate reached by IP (Let's Encrypt renewals keep working), and turns a refusal into a visible error rather than a hang.
  - Redirecting servers are handled.
  - IPv6 literals are handled.
  - The fail-closed WiFi lock is seeded at launch.

- [x] **I1 ✔ Web-view bridges.**
  - Drop messages unless they come from the main frame and the HA origin.
  - Block cross-origin main-frame navigation; check the host, not only the path.
  - Check the web view's origin before every `evaluateJavaScript` that carries secrets.
  - Inject auto-login only on the HA host.
- [x] **I2 TLS.** Run normal certificate evaluation first. Fall back only to a certificate whose key hash was pinned at provisioning, or trusted on first use. Remove `InsecureSessionDelegate` from the reachability probes.
- [x] **I3 WireGuard keys to the Keychain.** Move the private key and preshared key from app-group UserDefaults to SecureStore (AfterFirstUnlockThisDeviceOnly), with a one-time migration.
- [x] **I4 Log redaction.** No full push payloads, APNs token or query strings in AppLogStore. `jsLogger` logs the main frame only.
- [x] **I5 Share deep link.** Remove the `hascasa://share` route; the App Group handoff stays.
- [x] **I6 https-only `relay_url`.**
- [x] **I7 Remove the `push2.bonjour.casa` fallback** (`RelayEndpoints.swift`, `AppState.swift:1014`).
- [x] **I8 WiFi lock fails closed** while an allow-list exists (uncommitted 1.95 code, `NetworkHelper.swift:11`).
- [x] **I9 Entitlements and plists.**
  - Drop the unused external-accessory entitlement.
  - Remove ATS arbitrary loads from CasaShare.
  - Remove the obsolete Bluetooth peripheral string.

## Phase 4: cross-repo protocol changes (two-step, compatible)

Order: casa first (it emits both), then iOS (accepts both), then flip the casa default after the app ships.

- [ ] **X1 Provisioning link v3: per-link key, no embedded private key.**
  - Payload v3 is AES-256-GCM under a random per-link key. The key travels in the link: the universal-link `#fragment`, so bonjour.casa never sees it, and in the `hascasa://` query.
  - The app accepts v2 and v3. Casa keeps emitting v2 until the new app is out, then emits v3 (admin option, default flipped one release later).
  - Phase 5 removes v2, the v1 pipe format and the embedded RSA key.
  - Also: the link stays a bearer secret, so see Q4 (rotate the WG key after first heartbeat). Remove BLE beacons or make them single-use (Q1).
- [ ] **X2 Authenticated commands.**
  - `deprovision`, reload and clear-cache move inside the device-key-encrypted `casa_update` envelope (timestamped).
  - The new app accepts the encrypted form. It keeps accepting plaintext only from casa versions that predate it, based on the server version the heartbeat reports.
  - Casa sends the encrypted form to apps that report support.
- [x] **X3 Per-device keys** (casa 26.11.15, branch `security-phase4-step1`; design `docs/superpowers/specs/2026-11-13-security-phase4-design.md`). No app change. A device's pushes switch to its own key once it has fetched it and never go back; the site key is refused for reports from a device once it has reported under its own key; per-device rotate in the device editor and on owner change. Casa issues each device its own random key, delivered once to its pinned session through the heartbeat. Location reports, action presses and encrypted pushes use it. The site-wide `device_key` stays only for apps that haven't picked up a per-device key, and is removed in Phase 5. The heartbeat stops returning keys to non-owned or unpinned callers right away.
- [ ] **X4 Notification content encryption (optional, last in Phase 4).**
  - **How it works with the app closed:** iOS runs a Notification Service Extension (NSE) for any push marked `aps.mutable-content: 1`, whether the app is running, backgrounded or force-quit, and gives it about 30 s to rewrite the content.
  - **Flow:**
    1. Casa sends a placeholder alert ("New notification") plus `enc` = AES-GCM(title, body, actions, nav data) under the per-device key (X3).
    2. The NSE decrypts and replaces title and body, and puts the decrypted actions into `userInfo`. CasaNotificationContent then sees them as today.
  - **Needs:**
    - A new NSE target.
    - The device key in a Keychain access group shared with the NSE.
    - A relay option to set `mutable-content: 1` (casa strips caller `aps` since Phase 2). Old apps keep receiving plaintext, based on the app version from the heartbeat.
  - **Limits:**
    - Before the first unlock after a reboot the Keychain is unreadable, and a decrypt failure or timeout also leaves the content unchanged. Either way the placeholder shows.
    - Silent pushes skip the NSE; they are already casa-encrypted.
  - Works with the relay down; the relay and Apple only ever see ciphertext.

- [x] **C8 WireGuard via the queue** (casa 26.11.15). `casa.update_wireguard` queues `wireguard` updates (durable, acked) instead of the legacy `wireguard_update` push; `silent`/`title`/`message` are ignored.
- [ ] **X2 next:** app 1.96 (signed `device/deprovision` handled headless in the AppDelegate path), then casa sends it to ≥ 1.96. Only admin "Deprovision device" sends a wipe.

## Phase 5: removals once old clients are gone

- [ ] Remove:
  - v1 and v2 payloads, the embedded RSA key and `CasaDecryptor` RSA code.
  - `_encrypt_payload`, `payload_decrypted` and the legacy `generate_qr` / `provision_ble_beacon` services (Q1).
  - `qr_filename` / `www` PNGs, the `housekeeping` service and `url_path_deprecated`.
  - The legacy flat template body.
  - The duplicate `register_device`, `list_tokens` and `remove_token` services.
  - Plaintext commands and the site-wide `device_key`.
  - The legacy `update_wireguard` push path.
  - On iOS: commands casa never sends (`clear_cache_and_reload`, `refresh`, the `casa_command` alias), nested `data.*` lookups, and the 408/429 retry branch.
- [ ] Remove iOS weight:
  - WireGuardKit app sources (1.8 MB) and `WireGuard.xcodeproj`.
  - The WireGuardKit link from the main app target.
  - `Casa.xcodeproj.survived-backup/`.
  - Root `test.swift` and `zone-evaluator-tests.swift` (move them into the CasaCore tests).
  - Duplicate haptics and the second log store.
  - The notification extension's dependency on all of CasaCore; it only needs `NotificationActionSpec`.
- [ ] Split casa `__init__.py` (7.4k lines) into views and services modules.

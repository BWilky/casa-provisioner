# Security Phase 4: per-device keys, signed commands, WireGuard via queue, encrypted notifications

**Status:** approved by Bryce 2026-11-13 ("go ahead and build"); builder-check calls folded in below.
**Backlog:** `docs/2026-11-13-security-backlog.md`, Phase 4.
**Repos:** casa-provisioner (casa), casa-ios (app), push-notifications-relay (relay).

## Why

The site-wide `device_key` is one secret shared by every phone at a site, handed to any HA user's app on heartbeat. Whoever holds it can:
- forge any device's location reports and notification-button presses;
- decrypt any device's encrypted update pushes, including re-login credentials and WireGuard configs.

It has also leaked: Fairwater's key appeared in full in the app's logs and in a support conversation.

Separately:
- **Destructive push commands are unauthenticated.** A plaintext `deprovision` wipes a phone, so anyone who can send a push to it (any `notify_user` caller before 26.11.14, the relay operator) can wipe it.
- **WireGuard updates can be lost.** The legacy `update_wireguard` push is not applied when the app is in the background, but HA records it as applied.
- **Notification text is readable in transit.** Titles, bodies and button labels pass through the relay and Apple in plaintext.

## Goals

1. **G1 Per-device keys.** Each device has its own random key. The shared site key stops being handed out, and eventually stops being accepted.
2. **G2 Signed commands.** The app obeys a wipe only when it arrives inside an encrypted, timestamped update under its own key. A plaintext `deprovision` push is ignored by new apps.
3. **G3 WireGuard through the queue.** Every WireGuard change goes through the durable, acknowledged update queue. HA records an assignment as applied only when the device acknowledges it.
4. **G4 Encrypted notification text.** For apps that support it, the relay and Apple see only a placeholder and ciphertext. The phone shows the real title, body and buttons, including when the app isn't running.

## Non-goals

- **No change to the provisioning link format.** The in-app link key stays (Bryce, 2026-11-13). A used link's WireGuard key keeps working until it is changed on the router, and that is accepted.
- **No relay involvement in VPN delivery.**
- **No "exposed profile" flagging.** Bryce declined it.
- **No removals yet.** Removing the old paths (the site key, plaintext commands, the legacy WireGuard push) is Phase 5, after old apps age out.

## Anti-goals (failure even if it "works")

- **An old app (1.93 to 1.95) loses push, location reports, button presses or WireGuard updates** because of this change.
- **Anything depends on the relay holding a key,** or stops working while the relay is down. The relay outage is an accepted gap only for push delivery itself.
- **A legitimate phone gets locked out of its own key.** For example, a phone that signs in again with a fresh session must not lose it.

## Constraints

- **Two-step compatibility.** Casa decides per device from the `app_version` the device reports on heartbeat, using a numeric dotted-version compare. Casa sends:
  - the new form only to apps at or above the version that supports it (`CASA_MIN_APP_SIGNED_COMMANDS`, `CASA_MIN_APP_ENCRYPTED_ALERTS`; the app release number is set at implementation time, expected 1.96);
  - the old form to everything else.
- **Crypto stays as it is.**
  - HKDF-SHA256 with the device_id as salt, AES-256-GCM, a random 12-byte nonce, base64 of nonce‖ct‖tag.
  - The existing info strings `casa-update-v1`, `casa-report-v1` and `casa-action-v1` are kept, plus `casa-alert-v1` for G4.
  - Only the input key material changes: the per-device key instead of the site key.
- **Push shapes.** Background pushes use `push_type: background` with `data.command = "casa_update"` (existing). Alerts use relay `/send`.
- **Releases.**
  - casa: bump `CASA_VERSION` and `PANEL_VERSION` together, tag `V.yy.mm.dd`, deploy to Fairwater with the Marketplace (HACS) connector recipe.
  - App: keep using the uncommitted working tree (Bryce: build on top, don't commit).
  - Relay: semver bump. Adding an optional field keeps protocol 1.
- **Tests.** casa uses plain pytest and the fakes; the relay uses pytest; the app uses CasaCore XCTest for logic plus an `xcodebuild` device build.

## Design

### G1 Per-device keys (casa only; works with every app)

- **Storage.** Each device record (managed and native) gets `device_key` (`secrets.token_hex(32)`) and `device_key_id` (the existing `_device_key_id` fingerprint). It is created lazily the first time the device is served a key.
- **Heartbeat response.** `device_key` and `device_key_id` carry the device's own key, and only when the caller owns the record and its session is the record's pinned session (`refresh_token_id`).
  - The heartbeat may re-pin first (`_may_repin`: a session of the owning account that isn't pinned to another device). So another login to the same account can take the pin and the key. That is accepted: it is the same trust as the queue pull, which already serves that session the plaintext payloads the key protects (builder check; my call).
  - Otherwise both are `null`. Today any authenticated caller gets the site key.
  - Apps already store whatever key the heartbeat returns (`device-key-snapshot`), so every app version switches over on its next check-in with no app change.
- **Encrypting for a device** (update pushes, the legacy WireGuard push while it remains, G4 alerts):
  - use the device's own key once the device has fetched it;
  - otherwise use the site key.

  The push carries `device_key_id`. An app holding the other key already refuses to decrypt, checks in, and pulls the queue (existing behaviour), so a mismatch only delays the update.
  - "Fetched" is a per-record flag set when the heartbeat response delivered the per-device key: `device_key_delivered_at`.
- **Decrypting from a device** (location reports, button presses):
  - try the device's own key first, then the site key;
  - if the site key is the one that worked, count it in a per-device `legacy_key_uses` counter and log once at INFO.

  Phase 5 removes the site-key fallback.
- **Rotation.** The existing admin "rotate device key" action (site key) stays. Add a per-device "rotate key" action in the device editor (admin only): it generates a new key, clears `device_key_delivered_at`, and nudges the device to check in. Per-device rotation is also done automatically when a device record moves to another user (the claim or reauth paths in `_move_device_record`).
- **Cleanup.** Purge and forget paths drop the key with the record (it lives in the record).
- **Uninstall.** Nothing extra is needed; it is in `casa_users`.

### G2 Signed commands (casa + app)

- **New queue update type** `("device", "deprovision")`, payload `{}`.
  - Delivered like every queued update: an encrypted `casa_update` push under the device's key, with id, type, action and ts inside. The app's existing replay check (`ts` ≤ last applied is rejected) applies.
  - Also pulled through `profile_updates`, so it works when the push is missed.
- **Casa wipe path:** admin "Deprovision device" (`handle_deprovision_device`), the only path that sends a wipe push today. User deletion and expiry send none today and keep sending none (builder check; my call):
  - **App at or above the minimum version:** enqueue `device/deprovision` and send the encrypted push. Do not send the plaintext command.
  - **Older app:** send the plaintext `{"command":"deprovision"}` exactly as today.
  - **Either way,** HA still revokes the session and purges the record afterwards, exactly as today.
  - **Ordering.** Enqueue and push first, then purge after a short grace period (the builder decides the mechanism; 30 s is suggested), so a device that is online can pull the queue before its session is revoked. A device that misses it wipes on its next 401, as today.
- **App (new version, 1.96):**
  - **The applier.** `("device","deprovision")` is handled in the AppDelegate push path, not only in ContentView's `applyUpdateEntry`, so it wipes a suspended or not-running app the way the plaintext wipe does today (background task, `AppState.handleSilentDeprovision`). It acks first, then wipes. The foreground queue pull handles the same entry (builder check; my call: the signed wipe must not be weaker than the plaintext one).
  - **Version.** G2 ships as 1.96, not inside the uncommitted 1.95, so no 1.95 build on a phone can receive an entry type it doesn't know (my call).
  - **Plaintext commands.** When the heartbeat reports a casa version at or above the release that sends `device/deprovision`, `CasaApp` ignores plaintext `deprovision`, `reload`, `refresh`, `clear_cache_and_reload` and `casa_command` aliases, and logs them as ignored. It still acts on `request_heartbeat` and the other non-destructive nudges, which only make it check in. Against an older casa (another site not yet upgraded) the plaintext commands keep working, as backlog X2 decided (two-step compatibility).
- **Unauthenticated wipe paths remain** (a 401, expiry) and are not commands; they stay as they are.

### G3 WireGuard through the queue (casa only; works with every app ≥ June 2026)

- **`casa.update_wireguard`** (legacy service) and every other path that today sends a `wireguard_update` push use `_enqueue_and_push_update` (type `wireguard`, action `update`/`revoke`) instead.
- **Assignment tracking.** As built (plan ruling R3): the assignment is recorded when the update is queued, as the panel's paths already do. A pending and an applied assignment count the same for the conflict check, and the panel shows the device's own reported `wireguard_configured`, so recording on ack would change nothing visible. The fix is that the update is now durable and acknowledged instead of a fire-and-forget push recorded on relay 200.
  - **Revoke** clears both the assignment and any pending one at enqueue time, as today, so a lost phone's config can go to its replacement at once (my call; the router key can't be rotated anyway).
  - **An ack means delivered, not applied.** The app also acks entries it rejects (an unparseable config) and duplicates. Casa generates the config, so a rejection is a bug, not a normal path; the ack protocol does not change (my call).
- **The legacy push builder** (`command: wireguard_update|wireguard_revoke`) is no longer used for new sends. Its code is removed in Phase 5.
- **Excluded-WiFi and the other existing WireGuard payload fields** carry over unchanged in the queue payload.

### G4 Encrypted notification text (relay + casa + app; optional, last)

- **Relay 1.4.0.** `/send` accepts an optional boolean `mutable_content`. When true and `push_type` is alert, the relay sets `aps.mutable-content = 1`. No other change; the relay never sees the key.
- **Casa.** For devices with app ≥ the minimum version, `notify_user` sends:
  - title "Casa" and body "New notification" (the placeholder; the builder may localise later);
  - `mutable_content: true`;
  - `data = { "enc": base64(AES-GCM under HKDF(device key, device_id, "casa-alert-v1")), "device_key_id": … }`.

  The ciphertext holds `{title, message, data}`, where `data` is the stripped, size-checked data including actions, `notification_id` and `nav_path`. The 4 KB check runs on the encrypted envelope. Older apps get today's plaintext alert.
  - **Too big once encrypted:** the message text inside the ciphertext is shortened (ending in "…") until the envelope fits. If it still doesn't fit with an empty message, the send fails with today's size error. It never falls back to plaintext (my call).
- **App.**
  - **A new Notification Service Extension** (`CasaNotificationService`). It decrypts `enc` with the device key, then:
    - replaces the title and body;
    - merges the decrypted `data` into `userInfo`;
    - sets `categoryIdentifier` to `CASA_DYNAMIC` when actions are present.

    If decryption fails or the 30 s limit is reached, the placeholder shows.
  - **Keychain sharing.** The device key must be readable by the extension, so `SecureStore` gains a shared Keychain access group used by the app and the service extension. The existing `deviceKey` item is migrated into it once. Before first unlock the key is unreadable, so the placeholder shows (accepted).
  - **Tap handling.** It already reads `userInfo` after the extension has run, so `nav_path` and the button view keep working.
- **Bryce's part.** Adding a target needs an App ID, a provisioning profile and the Keychain-group capability in the Apple developer account. Either Bryce adds the target in Xcode (File > New > Target > Notification Service Extension, named `CasaNotificationService`) and the builder fills it, or the builder edits the project file and Bryce confirms signing.

## Order of work

1. **casa: G1 and G3** together. No app change; release, then deploy to Fairwater.
2. **App: G2 applier and plaintext-command ignore,** plus the version constant. Casa then sends `device/deprovision` to that version. This needs a casa release with the G2 sending side, gated on the version.
3. **G4** across relay, casa and app, last.

## Left to the builder

- How the deprovision grace period is implemented (task, `async_call_later`, or immediate purge on the device's ack).
- The exact log wording and the per-device rotate button's placement in the device editor.
- Whether `legacy_key_uses` is shown in the panel or only logged.
- Test structure, as long as each goal's behaviour and its old-app path are both covered.

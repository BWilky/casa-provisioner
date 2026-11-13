# Casa Security Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the casa integration's confirmed security gaps that need no protocol change, so every shipped iOS app (1.93+) keeps working.

**Architecture:** Each task is a local fix inside `custom_components/casa`, with tests in `tests/` against the existing fakes (`tests/fakes.py`: FakeHass, FakeRequest, make_user, bind_view). There is no wire-format change toward the app or the relay. Release at the end bumps `CASA_VERSION` and `PANEL_VERSION` together.

**Tech Stack:** Python 3 (Home Assistant custom integration), plain pytest with stubs (`tests/conftest.py`), panel ES modules (`panel/`), with node tests in `tests/js` where they exist.

**Spec:** `docs/2026-11-13-security-backlog.md`, Phase 2 (C1–C14) and the decisions at its top.

## Global Constraints

- **Compatibility:** no change to any request or response shape the iOS app (1.93+) or relay 1.3.x uses. Old apps must keep provisioning, heartbeating, registering, receiving pushes and pressing actions.
- **Q1:** BLE beacons are out of scope; don't change them.
- **Q2 default:** keep the `admin_system_only` option, but scramble, provision and remove_token are always admin or system only.
- **Tests:** run `python3 -m pytest -q` from the repo root. 305 tests pass at 743b363.
- **Release:** `CASA_VERSION` (const.py) and `PANEL_VERSION` (panel/version.js) are bumped together; the tag is `V.yy.mm.dd`.
- **Logs:** never log a secret (site_key, push token, passwords, WireGuard keys) at any level.

## Review Focus

1. A phone reset and re-provisioned to another user whose teardown never reached HA must still register once the admin deletes the stale record. It must never silently take over a live device.
2. A notify_user call whose data includes `command: "deprovision"` must deliver a normal notification, with no command.
3. A panel device whose `current_url` is `javascript:...` must render as text.
4. A relay 403 on a send must flag the device for re-registration right away, not at the next daily reconcile.
5. A non-admin with `admin_system_only` off must not be able to read any password.

---

### Task 1 (C1): A provisioning claim cannot take over a live device

**Files:** Modify `custom_components/casa/__init__.py` (`_claim_device_for_caller` at about line 7054; add `_peek_replacement_claim` next to `_consume_replacement_claim` at about line 798). Test `tests/test_device_ownership.py`.

**Rule.** A fresh claim moves a foreign record only when one of these holds:
- (a) the record's pinned `refresh_token_id` no longer exists on its owner (a dead record), or
- (b) the caller's unconsumed replacement claim names this device_id.

Pinned-session and reauth-target moves are unchanged.

- [ ] **Step 1: Write failing tests.**
  - `test_fresh_claim_cannot_take_live_device`: owner A's pinned rA exists, B has a recorded claim. The claim returns False, the record stays with A, and rA is not revoked.
  - `test_fresh_claim_moves_dead_record`: rA is removed from A's tokens, B has a claim. The record moves to B.
  - `test_replacement_claim_moves_named_device`: `replacement_claims["rB"] = {"replaces_device_id": "PHONE", ...}` lets the move happen while rA is alive, and does not consume the replacement claim.
  - `test_time_window_claim_cannot_take_live_device`: the same as the first test, using the time-based path.
- [ ] **Step 2: Run them.** The live-device tests fail (today they move).
- [ ] **Step 3: Implement** `_peek_replacement_claim(hass, refresh_token_id) -> str | None` (no pop; ignores claims older than `_CLAIM_WINDOW_SECONDS`) and the rule above. Log `CASA: Device '%s' is still active under another user; not moved` at warning level.
- [ ] **Step 4: Update** `test_redeemed_provisioning_claim_moves_purges_and_revokes_old_session` to the dead-record form, so it keeps the purge and revoke assertions. Run the full suite; it passes.
- [ ] **Step 5: Commit** with message `fix(security): a provisioning claim can no longer take over a live device`.

### Task 2 (C3): Panel never renders a non-http(s) URL as a link

**Files:** Modify `panel/views/device-editor.js` (about line 489) and any other `href="${...}"` built from device-reported data (grep `href=` in `panel/`). Test with node `tests/js` if that harness exists; otherwise add a pure helper `safeHttpUrl(u) -> string|null` in `panel/lib` (or the panel's util module) with a node test.

- [ ] **Step 1: Write the failing test** `safeHttpUrl`:
  - `"javascript:alert(1)"` gives null.
  - `"data:text/html,x"` gives null.
  - `"https://h/x"` stays as is.
  - `"http://h"` stays as is.
  - `" JaVaScRiPt:..."` gives null.
- [ ] **Step 2: Run it.** It fails because the helper is missing.
- [ ] **Step 3: Implement** the helper and use it. A null result renders the escaped text with no anchor.
- [ ] **Step 4: Run the tests.** They pass.
- [ ] **Step 5: Commit** with message `fix(security): panel renders device-reported URLs as links only when http(s)`.

### Task 3 (C5): notify_user cannot smuggle control keys; push stays under 4 KB

**Files:** Modify `__init__.py` (`_notify_relay_body` and `_action_push_fields`, about lines 1150–1215). Test `tests/test_notify_actions.py`.

**Interfaces:**
- `RESERVED_PUSH_KEYS = frozenset({"aps", "command", "casa_command", "casa_update", "update_payload", "wireguard_payload", "encrypted", "device_key_id", "update_id"})` is stripped from caller data, with a warning that names the keys.
- `action_data` is no longer copied into the device push; it stays in the pending record and the event.
- `MAX_PUSH_BYTES = 3800`. When the JSON-encoded relay body's `title` + `message` + `data` exceed it, raise `ServiceValidationError` before any pending record is written.

- [ ] **Step 1: Write failing tests.**
  - `test_notify_strips_reserved_keys`: data `{"command": "deprovision", "aps": {...}, "foo": 1}` produces a relay body whose data has `foo` but no `command` or `aps`.
  - `test_notify_does_not_forward_action_data`.
  - `test_notify_rejects_oversize_push_before_pending_record`.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): notify_user strips reserved push keys, keeps action_data server-side, caps push size`.

### Task 4 (C2): A provisioning link yields one working session

**Files:** Modify `__init__.py` (redemption listener at about 446–496 and `_end_provision_window` at about 966–1000). Test `tests/test_scramble_on_redeem.py` and `tests/test_pending_provisions.py`.

**Rule.** When the window ends with redemption, keep the earliest refresh token created during the window. Revoke the others and record only the kept one as a claim. If more than one was created, log at warning level and fire a `persistent_notification` naming the user.

- [ ] **Step 1: Write the failing test** `test_second_session_from_same_link_is_revoked`: two tokens are created inside the window; after the window ends only the earlier one survives and only it is in `provision_claims`.
- [ ] **Step 2: Run it.** It fails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): provisioning links yield exactly one session`.

### Task 5 (C4): Credential-revealing services are always admin-only

**Files:** Modify `__init__.py` (`_check_authorization` at about 5099; the services `scramble_guest_password`, `provision`, `remove_token`). Test `tests/test_password_rotation.py` and a new `tests/test_service_auth.py`.

**Rule:**
- These three services require admin or system context even when `admin_system_only` is off.
- scramble and provision target only Casa-managed users (present in `stored_data["users"]`), never admins or the owner.
- remove_token never targets an admin's or the owner's tokens.

- [ ] **Step 1: Write failing tests.**
  - `test_non_admin_cannot_scramble_when_option_off`.
  - `test_non_admin_cannot_provision_when_option_off`.
  - `test_scramble_refuses_non_casa_user`.
  - `test_remove_token_refuses_admin_target`.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): password-revealing and token-removal services are admin-only and casa-scoped`.

### Task 6 (C10): No secrets in logs; client IP from the socket

**Files:** Modify `__init__.py` (about 6273–6283 relay debug logging, 1893 and 2076 X-Forwarded-For, the body `ip_address` override). Test `tests/test_relay_sites.py` and `tests/test_view_dispatch.py`.

- [ ] **Step 1: Write failing tests.**
  - `test_relay_send_logs_no_site_key_or_push_token`: caplog at DEBUG around a send contains neither.
  - `test_heartbeat_ip_ignores_forwarded_header_and_body`: the recorded `ip_address` is `request.remote`.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): no site_key or push token in logs; device IP from the socket`.

### Task 7 (C11): Deleting a user cleans up everything that user had

**Files:** Modify `__init__.py` (`remove_user` handler, about 6090; `_reconcile_site` about 6767; notify_user device resolution about 6210). Test `tests/test_queue_hygiene.py`.

- [ ] **Step 1: Write failing tests.**
  - `test_remove_user_purges_devices_and_relay_tokens`: the user's device records are gone, `/unregister` is called for each proxy token, and `provision_claims[uid]`, `provision_opened[uid]` and `server_passwords[uid]` are gone.
  - `test_notify_by_device_id_skips_deleted_user`.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement** using `_purge_device` per device.
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): removing a user purges its devices, relay tokens and claims`.

### Task 8 (C12): A sibling device cannot re-pin another device's session

**Files:** Modify `__init__.py` (register_device GET at about 1928 and the heartbeat re-pin at about 4896). Test `tests/test_device_ownership.py`.

- [ ] **Step 1: Write the failing test** `test_sibling_on_shared_account_cannot_repin`: two devices share user A, and device 2's token calls with device 1's id; device 1's pin is unchanged.
- [ ] **Step 2: Run it.** It fails.
- [ ] **Step 3: Implement.** Re-pin only when the record is unpinned, its pinned token no longer exists, or the caller holds a fresh claim (C1 rule).
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): devices on a shared account cannot re-pin each other`.

### Task 9 (C6, C7): React to relay 403s at once; keep site_id only once registered

**Files:** Modify `__init__.py` (send paths at about 3517–3528 and 6285–6300; `_register_site` at about 328; `_ensure_site_registration`). Test `tests/test_relay_sites.py`.

- [ ] **Step 1: Write failing tests.**
  - `test_send_403_registration_invalid_flags_needs_reregister`.
  - `test_send_403_invalid_site_key_reverifies_and_reregisters_site`.
  - `test_register_site_failure_keeps_no_site_id`.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix: react to relay 403s immediately; persist site_id only after the relay accepts it`.

### Task 10 (C9): Secrets at rest

**Files:** Modify `__init__.py` (`async_remove_entry` about 7257; regen and pending rows about 5607 and 1016; auth queue prune about 1627; WG profiles GET; device editor pasted-config merge about 2828). Test `tests/test_queue_hygiene.py` and `tests/test_wireguard_one_per_device.py`.

- [ ] **Step 1: Write failing tests.**
  - `test_remove_entry_deletes_all_casa_stores`.
  - `test_expired_pending_row_has_no_secrets`.
  - `test_undelivered_auth_entry_expires`.
  - `test_wireguard_profiles_get_redacts_private_keys`.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): fewer secrets at rest; uninstall removes every casa store`.

### Task 11 (C13, C14): Native device auto-create cap; location replay

**Files:** Modify `__init__.py` (native record creation about 4689 and 4827; location report view about 2613–2668; `button.py:90`). Test `tests/test_location.py` and `tests/test_device_ownership.py`.

- [ ] **Step 1: Write failing tests.**
  - `test_non_admin_native_device_cap`: a non-admin user is capped at 5 native records (decision recorded as a ruling).
  - `test_location_report_replay_rejected`: the same (device_id, ts) twice is accepted once.
- [ ] **Step 2: Run them.** They fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.** It passes.
- [ ] **Step 5: Commit** with message `fix(security): cap native device records per non-admin; reject replayed location reports`.

### Task 12: Release

- [ ] **Step 1: Bump** `CASA_VERSION` and `PANEL_VERSION` to the release date (`26.11.14` or the actual day). Run the full suite.
- [ ] **Step 2: Commit.** Bryce decides the merge, the `V.yy.mm.dd` tag, and the deploy to Fairwater.

C8 (WireGuard updates through the queue) is deferred to Phase 4. It changes what old apps must support, so it needs the two-step rollout.

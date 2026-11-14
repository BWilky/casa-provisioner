# Security Phase 4, step 1: per-device keys and WireGuard via the queue (casa only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every device gets its own encryption key in place of the shared site key, and every WireGuard change goes through the durable update queue. This needs no app change.

**Architecture:** A small new module, `device_keys.py`, holds the key rules: create, choose for encrypt, decrypt with fallback, and rotate. `__init__.py` calls it at each place that today reads `stored_data["device_key"]`. `casa.update_wireguard` is rebuilt on `_enqueue_and_push_update` through a module-level helper so it can be tested.

**Tech Stack:** Python 3.13, Home Assistant custom integration, `cryptography` (HKDF and AES-GCM, unchanged), plain pytest with `tests/fakes.py`, node tests for the panel (`tests/js`).

**Spec:** `docs/superpowers/specs/2026-11-13-security-phase4-design.md` (G1 and G3).

## Global Constraints

- Crypto is unchanged: HKDF-SHA256, salt = device_id, info `casa-update-v1` / `casa-report-v1` / `casa-action-v1`, AES-256-GCM, base64(nonce‖ct‖tag). Only the input key changes.
- The key id is `_device_key_id(key)` (sha256 hex, first 8 characters) for both kinds of key.
- An old app (1.93 to 1.95) must not lose push, location reports, button presses or WireGuard updates.
- The per-device key appears only in the owner's pinned-session heartbeat response. It never appears in logs, in admin APIs or in sensor attributes.
- A null `device_key` in a heartbeat response leaves the app's stored key in place (the app's JS only snapshots strings). This is relied on.
- Release: bump `CASA_VERSION` (const.py) and `PANEL_VERSION` (panel/version.js) together, to 26.11.15 or the release date.
- Branch: `security-phase4-step1` off `main`. Test commands are `python -m pytest -q` and `node --test tests/js`.

## Rulings made while planning (they refine the approved design)

- **R1. No return to the site key.** Once a device's key has been delivered, pushes to it never fall back to the site key, including after a per-device rotation. Rotation makes a new key and keeps `device_key_delivered_at`, so the push carries the new key id, the app sees a mismatch, checks in and pulls the queue. This is stricter than the design, which cleared the flag and so fell back to the leaked site key. If this is wrong, a rotated device's next push is delayed until its next check-in.
- **R2. The site key stops working once a device proves it has its own key.** The first report or button press that decrypts under a device's own key sets `device_key_confirmed_at`. After that, the site key is refused for that device. Before that, the site key is still accepted and counted in `legacy_key_uses` (logged once at INFO). This is stricter than the design, which accepted the site key until Phase 5. If this is wrong, a report encrypted with the site key just after the switch is refused, but the heartbeat carries location anyway.
- **R3. The WireGuard assignment is still recorded when the update is queued, not when the device acks it.** For the conflict check, a pending assignment and an applied one count the same, and the panel shows the device's own reported `wireguard_configured`. So recording on ack would change nothing anyone can see. What G3 does fix is that `update_wireguard` used to record the assignment when the relay accepted a push the app might never apply; the update now goes through the queue. The design doc is updated to match.
- **R4. `update_wireguard` ignores `silent`, `title` and `message`.** These fields are undocumented in services.yaml. Queued updates go out as silent pushes, and the service logs a warning when any of these fields is given.

## Review Focus

1. **Re-sign-in.** The same phone signs in again with a fresh session: `_may_repin` moves the pin and the next heartbeat returns the key. Test: `test_resigned_in_phone_still_gets_its_key`.
2. **Sibling or foreign session.** A session pinned to another device, or a caller that doesn't own the record, gets `device_key: null` and the record's key is not created. Test: `test_unpinned_session_gets_null_key`.
3. **Lost heartbeat response.** The key is marked delivered but the phone still holds the site key, so the push id mismatches and the queue still delivers. Covered by R1. Test: `test_push_uses_device_key_once_delivered` asserts the key id.
4. **Key leakage.** The per-device key is not in any log line or admin summary. Test: `test_admin_summary_has_no_device_keys`, plus a caplog assertion in the heartbeat test.
5. **`update_wireguard` to a device with no push token.** It is still queued, where before it was skipped and lost. Test: `test_update_wireguard_queues_without_push_token`.

---

### Task 1: `device_keys.py` key rules

**Files:**
- Create: `custom_components/casa/device_keys.py`
- Test: `tests/test_device_keys.py`

**Interfaces:**
- Produces:
  - `ensure_device_key(device_info: dict) -> str` creates `device_key = secrets.token_hex(32)` if it is missing, then returns it.
  - `encryption_key(stored_data: dict, device_info: dict) -> str | None` returns the device's key if `device_key_delivered_at` is set, otherwise `stored_data.get("device_key")`.
  - `decrypt_from_device(stored_data, device_info, device_id, payload_b64, *, info=REPORT_INFO) -> dict` tries the device key first, then the site key (refused once `device_key_confirmed_at` is set; R2). On a device-key success it sets `device_key_confirmed_at` if missing. On a site-key success it increments `legacy_key_uses` and logs INFO the first time. It raises `ValueError` when nothing decrypts. It wraps `location.decrypt_report_payload`.
  - `rotate_device_key(device_info: dict) -> str` sets a new key, pops `device_key_confirmed_at`, keeps `device_key_delivered_at` (R1) and returns the new key id.
  - `public_key_state(device_info) -> dict` returns `{"device_key_id", "delivered", "legacy_key_uses"}` for the panel. It never includes the key.

- [ ] **Step 1: Write the failing tests** in `tests/test_device_keys.py`, using a payload built with the test-side encryptor `_enc(key, did, obj, info)` (HKDF and AES-GCM, as in `test_location.py`):
  - `test_ensure_creates_once`: two calls return the same 64-hex string.
  - `test_encryption_key_is_site_key_until_delivered`: it returns the site key, then the device key after `device_key_delivered_at` is set.
  - `test_decrypt_prefers_device_key_and_confirms`: it returns the dict and sets `device_key_confirmed_at`.
  - `test_site_key_accepted_and_counted_before_confirmation`: `legacy_key_uses == 1`.
  - `test_site_key_refused_after_confirmation`: it raises `ValueError`.
  - `test_rotate_keeps_delivered_and_clears_confirmed`: the new id differs from the old, delivered is kept and confirmed is gone.
  - `test_public_state_never_contains_key`: the key string is not in `json.dumps(public_key_state(info))`.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_device_keys.py`. Expected: an ImportError (the module is missing).
- [ ] **Step 3: Implement** `device_keys.py` per the Interfaces block. Import `_device_key_id`'s logic as a local `key_id(key)` and have `__init__._device_key_id` call it, so there is one definition.
- [ ] **Step 4: Run** the test file. Expected: 7 passed.
- [ ] **Step 5: Commit** with the message `feat(keys): per-device key rules module`.

### Task 2: heartbeat delivers the device's own key only to its pinned session

**Files:**
- Modify: `custom_components/casa/__init__.py` (heartbeat response, about lines 2391 to 2397)
- Test: `tests/test_device_keys_heartbeat.py`

**Interfaces:**
- Consumes: `ensure_device_key`, `key_id`.
- Behaviour: after `heartbeat_func` returns, if `result.get("owned", True)`, `device_info` is found, `refresh_token_id` is truthy, and `device_info.get("refresh_token_id") == refresh_token_id`, then the view ensures the key, sets `device_key_delivered_at` (an ISO time, only if missing, then `_save_stored_data`) and returns that key and id. Otherwise it returns `device_key: None, device_key_id: None`.

- [ ] **Step 1: Write the failing tests.** Drive `CasaHeartbeatView` with a stub `heartbeat_func` that returns `{"owned": True}` (or `{"owned": False}`), records pre-seeded under `users.u1.devices.D1` with `refresh_token_id: "rt1"`, and headers from `bearer("rt1")`:
  - `test_pinned_session_gets_own_key`: the response key equals the record's `device_key`, differs from the site key, and `device_key_delivered_at` is set.
  - `test_unpinned_session_gets_null_key`: with `bearer("rt2")`, both fields are None and `"device_key" not in record`.
  - `test_not_owned_gets_null_key`.
  - `test_resigned_in_phone_still_gets_its_key`: the stub heartbeat sets the record's pin to `rt2` (simulating `_may_repin`) and `bearer("rt2")` gets the same key as before.
  - `test_key_not_logged`: with caplog at DEBUG, the key is not in `caplog.text`.
- [ ] **Step 2: Run** the file. Expected: failures (the site key is returned).
- [ ] **Step 3: Implement** in the heartbeat view.
- [ ] **Step 4: Run** the file, then the full suite. Expected: all pass. An existing test that expects the site key from the heartbeat is updated, with a ledger note.
- [ ] **Step 5: Commit** with the message `feat(keys): heartbeat hands each device its own key`.

### Task 3: encrypt for a device with its key

**Files:**
- Modify: `custom_components/casa/__init__.py`: `_send_encrypted_update_push` (3811), `_enqueue_and_push_update` (3857), admin summary `push_ready` (2569, 2614), queue view (4122), reauth `_deliver_result` (4414), reprovision `push_ready` (4500).
- Test: `tests/test_device_keys_push.py`

**Interfaces:**
- Consumes: `encryption_key(stored_data, device_info)`.
- Every `stored_data.get("device_key")` used to encrypt for, or decide push readiness of, a specific device becomes `encryption_key(stored_data, dinfo)`. The push's `device_key_id` is the id of the key actually used.

- [ ] **Step 1: Write the failing tests**, capturing `_send_push_to_relay` with monkeypatch:
  - `test_push_uses_site_key_before_delivery`: the decrypted `update_payload` matches under the site key, and `device_key_id` is the site key's id.
  - `test_push_uses_device_key_once_delivered`: it decrypts under the device key and `device_key_id` is the device key's id.
  - `test_admin_summary_has_no_device_keys`: no device key string appears in `json.dumps` of the summary response.
- [ ] **Step 2: Run.** Expected: the second test fails.
- [ ] **Step 3: Implement** the swaps.
- [ ] **Step 4: Run** the file and the full suite. Expected: all pass.
- [ ] **Step 5: Commit** with the message `feat(keys): encrypt pushes with the device's own key once it has it`.

### Task 4: decrypt reports and presses with the device's key

**Files:**
- Modify: `custom_components/casa/__init__.py`: `CasaLocationReportView.post` (2894), `CasaNotificationActionView.post` (2955).
- Test: extend `tests/test_location.py` and `tests/test_notification_action_view.py`.

**Interfaces:**
- Consumes: `decrypt_from_device(..., info=REPORT_INFO | ACTION_INFO)`. The "no key" 403 now means neither a device key nor a site key exists. A changed record is saved with `_save_stored_data`, because of the confirm and counter fields.

- [ ] **Step 1: Write the failing tests:**
  - `test_report_under_device_key_accepted_and_confirms`
  - `test_report_under_site_key_after_confirm_rejected` (403)
  - `test_press_under_device_key_runs_action`
  - `test_old_app_press_under_site_key_still_works`
- [ ] **Step 2: Run.** Expected: the device-key tests fail (403).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both files and the full suite. Expected: all pass.
- [ ] **Step 5: Commit** with the message `feat(keys): reports and presses decrypt with the device's own key`.

### Task 5: per-device rotation (admin action and owner change)

**Files:**
- Modify: `custom_components/casa/__init__.py`: `CasaAdminRegenerateDeviceKeyView.post` (about 4706) accepts an optional `{"device_id"}`. `_move_device_record` (7302) calls `rotate_device_key(info)` when the record has a key.
- Modify: `panel/api.js` (`rotateDeviceKey(deviceId)`), `panel/views/device-editor.js` (a "Rotate encryption key" card under "Reload app", using `ui.showConfirm`), and `panel/version.js`. The panel version bump itself happens in Task 7.
- Test: `tests/test_device_keys_rotate.py`

**Interfaces:**
- POST `casa/admin/regenerate_device_key` with `device_id` rotates only that record, nudges it with `_nudge_device_checkin`, and returns `{"status":"ok","device_id","device_key_id"}`. An unknown device returns 404 and a non-admin 403. Without `device_id`, the site-key behaviour is unchanged.

- [ ] **Step 1: Write the failing tests:**
  - `test_admin_rotates_one_device`: the key id changes, other devices are untouched, and the site key is unchanged.
  - `test_rotate_requires_admin`
  - `test_rotate_unknown_device_404`
  - `test_moving_a_record_rotates_its_key`: call `_move_device_record` directly.
- [ ] **Step 2: Run.** Expected: they fail.
- [ ] **Step 3: Implement** the backend and the panel card. The confirm text is: "Give this device a new encryption key? It picks up the new key on its next check-in; until then updates wait in its queue."
- [ ] **Step 4: Run** the file, the full suite and `node --test tests/js`. Expected: all pass.
- [ ] **Step 5: Commit** with the message `feat(keys): rotate one device's key; rotate on owner change`.

### Task 6: `update_wireguard` through the queue (G3)

**Files:**
- Modify: `custom_components/casa/__init__.py`. Add a module-level `async def _queue_wireguard_change(hass, targets, action, config, excluded_wifi, created_by) -> dict`. `handle_update_wireguard` keeps its target resolution and conflict log, then calls it. Remove the legacy push building in the handler.
- Test: `tests/test_update_wireguard_queue.py`

**Interfaces:**
- `targets` is `[(device_id, device_info, uid)]`.
- For each target it calls `_enqueue_and_push_update(..., "wireguard", action, payload, created_by)`, where the payload is `{"config", "excluded_wifi"}` for update and `{}` for revoke. It calls `_set_wireguard_assignment(dinfo, config if update else None)` at enqueue (R3). When the push wasn't sent it nudges with `_nudge_device_checkin`. It saves `qu_store` and stored data once.
- Returns `{"success": True, "queued_count", "sent_count", "skipped_count", "failed_count": 0}`, where `sent_count` counts pushes that went out and `skipped_count` counts devices with no push token (still queued).
- The handler logs a warning when `silent`, `title` or `message` is given (R4).

- [ ] **Step 1: Write the failing tests:**
  - `test_update_wireguard_enqueues_wireguard_update`: one queue entry, type `wireguard`, payload `{"config", "excluded_wifi"}`, `wireguard_key_fp` set.
  - `test_update_wireguard_queues_without_push_token`: `queued_count == 1` and `skipped_count == 1`.
  - `test_revoke_enqueues_and_frees`
  - `test_no_legacy_wireguard_push_sent`: no captured relay payload has `data.command` in (`wireguard_update`, `wireguard_revoke`).
- [ ] **Step 2: Run.** Expected: an ImportError.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and the full suite. Expected: all pass.
- [ ] **Step 5: Commit** with the message `feat(wireguard): update_wireguard goes through the durable queue`.

### Task 7: version, docs, backlog

**Files:**
- Modify: `custom_components/casa/const.py` (`CASA_VERSION`), `panel/version.js` (`PANEL_VERSION`), `custom_components/casa/manifest.json` (if it carries the version), `docs/2026-11-13-security-backlog.md` (Phase 4 step 1 status), and the design doc (R3 wording under G3).
- [ ] **Step 1:** Bump both versions to the same value and run `python -m pytest -q tests/test_versions.py`. Expected: pass.
- [ ] **Step 2:** Update the docs.
- [ ] **Step 3:** Run the full suite and `node --test tests/js`. Expected: all pass.
- [ ] **Step 4: Commit** with the message `chore: 26.11.x — Phase 4 step 1 (per-device keys, WireGuard via queue)`.

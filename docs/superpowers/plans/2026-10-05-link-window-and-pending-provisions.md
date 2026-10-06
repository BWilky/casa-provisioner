# Link Window + Pending Provisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Setup links made in the admin wizard stay valid for 24 hours by default (admin-selectable), the device-naming window follows the link, and every unredeemed provision is visible in the Devices tab with its QR/links, Revoke and Regenerate.

**Architecture:** The server already persists one open provisioning window per HA user (`stored_data["pending_provisions"]`) and drives it with a timer + redemption listener. We extend that record with the links, the wizard's device name/template, and a sanitized copy of the request for Regenerate; we persist the pending profile (today in-memory) with a validity that follows the window; we leave a tombstone when a window expires unredeemed; and we expose a slim list in `/api/casa/admin/summary` plus a new admin endpoint for links/revoke/regenerate/dismiss. The panel's Devices view merges pending rows into the device table and reuses the existing result view for the QR/links modal. The wizard sends `timeout_minutes` explicitly (default 1440) and labels the chip as link validity, separate from session length.

**Tech Stack:** Python (HA custom integration, plain-pytest with fakes in `tests/fakes.py`), vanilla ES modules panel (node:test for pure logic).

**Spec:** Conversation 2026-10-05 (summarised in this plan's Global Constraints / task notes).

## Global Constraints

- Server default `timeout_minutes` stays 5 (kiosk QR / service callers unchanged). Only the wizard sends 1440.
- Wizard "Link valid for" choices: 15 minutes, 1 hour, 24 hours (default). Max 24 h (listener cap `min(scramble+30, 86400)`).
- Result key `expires_at` keeps its name (app + tests rely on it); only the panel label changes to "Link valid until".
- Results additionally carry `session_expiration_hours` so the panel can show session length separately.
- Pending rows never carry links in the 30 s summary poll; links come from `GET /api/casa/admin/pending_provision?user_id=`.
- Stored links contain the encrypted temp password: the record is deleted when the window closes (redeemed/revoked) and the tombstone never stores links.
- Bump `CASA_VERSION`, `PANEL_VERSION`, `manifest.json` together → `26.10.06`.
- No attribution lines in commits.

## Review Focus

1. A phone that redeems at hour 3 of a 24 h link must still be named and get its profile recorded (pending profile validity follows the window + 30 min grace, persisted across restarts) → Task 1 test.
2. A window that expires unredeemed leaves an "expired" row; one that was redeemed does not → Task 2 test.
3. A new provision for the same account (wizard, regenerate) replaces the pending row and clears the expired tombstone → Task 2/3 tests.
4. Revoke scrambles the password immediately (link dead) and the row disappears; the Accounts entry stays → Task 3 test.
5. Service callers that never send `timeout_minutes` still get 5 minutes → Task 4 test (wizard request builder sends it; server default untouched, asserted in existing behaviour).

---

### Task 1: Persist the pending profile with a window-following validity

**Files:**
- Modify: `custom_components/casa/__init__.py` (`_apply_pending_provision` ~836, stash at ~4768, `_end_provision_window`)
- Test: `tests/test_pending_profile.py` (new)

**Interfaces:**
- Produces: `stored_data["pending_profiles"][user_id] = {profile_id, profile_name, device_alias, expiration_hours, set_at, valid_until}`; helper `_stash_pending_profile(hass, user_id, *, profile_id, profile_name, device_alias, expiration_hours, valid_until)`; `_pending_profile_valid_until(now, scramble_at, window_ends_at, listen_until)` → window end + 1800 grace (fixed 1800 when the window never ends).

- [ ] Test: profile stashed with `valid_until` 10 h ahead is applied at +3 h (monkeypatch `time.time`), consumed, alias set.
- [ ] Test: profile older than `valid_until` is dropped, not applied.
- [ ] Test: stash is in `stored_data` (survives "restart" = fresh `hass.data` with same stored_data).
- [ ] Implement; remove `pending_profile_by_user` in-memory dict.
- [ ] Run `python3 -m pytest -q`; commit `feat(provision): persist pending profile; validity follows the link window`.

### Task 2: Extend the pending record; tombstone expired windows

**Files:**
- Modify: `custom_components/casa/__init__.py` (record at ~5020, `_end_provision_window`, results at ~5040-5110)
- Test: `tests/test_pending_provisions.py` (extend)

**Interfaces:**
- Record gains: `deep_link`, `universal_link`, `expires_at`, `device_alias`, `profile_id`, `profile_name`, `expiration_hours`, `regen` (service_data minus `password`), `host_url`.
- `stored_data["expired_provisions"][user_id] = {provision_id, login_username, method, device_alias, profile_name, created_at, expired_at, regen, replaces_device_id}`.
- `_end_provision_window(..., reason)` writes the tombstone only for `reason == "timer"` with no new tokens.
- `_clear_expired_provision(hass, user_id)`; new provisions and successful `_apply_pending_provision` call it.
- Results (`qr`, `deep_link`, `manual`, `ble`) gain `"session_expiration_hours": expiration_hours`.

- [ ] Test: timer close with no new tokens → tombstone present, pending gone.
- [ ] Test: timer close with a new token → no tombstone.
- [ ] Test: `_end_provision_window(reason="revoked")` → no tombstone.
- [ ] Implement; run tests; commit `feat(provision): keep links on the pending window; tombstone expired links`.

### Task 3: Summary list + admin endpoint

**Files:**
- Modify: `custom_components/casa/__init__.py` (summary view ~2095; new `CasaAdminPendingProvisionView`; registration ~4461; `remove_user` cleanup)
- Test: `tests/test_pending_provision_view.py` (new)

**Interfaces:**
- Summary: `"pending_provisions": [{user_id, username, name, profile_name, method, created_at, expires_at, state: "awaiting"|"expired", has_links, replaces_device_id}]`, `stats.awaiting_setup`.
- `GET /api/casa/admin/pending_provision?user_id=` → `{user_id, username, name, method, created_at, expires_at, deep_link, universal_link, qr_data_uri, session_expiration_hours}`; 404 when none/expired.
- `POST` body `{user_id, action: "revoke"|"dismiss"|"regenerate", host_url?}`; regenerate returns the provision result (`method: "qr"`).

- [ ] Tests: GET 403 non-admin; GET 404 unknown; GET returns links + data-uri (monkeypatch `_qr_png_data_uri`); POST revoke → pending gone, password scrambled; POST dismiss → tombstone gone; POST regenerate → provision_func called with regen data + `user_id`, tombstone cleared.
- [ ] Test: summary lists awaiting + expired rows (FakeHass; may need `qu_data`).
- [ ] Implement; register view; prune tombstones older than 7 days at summary time; run tests; commit `feat(admin): pending provisions in summary + links/revoke/regenerate endpoint`.

### Task 4: Wizard link window + copy

**Files:**
- Modify: `panel/views/provision-logic.js` (`buildProvisionRequest`), `panel/views/provision.js` (state, template step control, result copy), `panel/views/provision-result.js` (chip + session line), `panel/views/reprovision.js` (pass-through only)
- Test: `tests/js/provision-logic.test.mjs`

**Interfaces:**
- `buildProvisionRequest({..., linkMinutes})` adds `timeout_minutes: linkMinutes` when a positive integer.
- `LINK_WINDOW_OPTIONS = [{minutes: 15, label: "15 minutes"}, {minutes: 60, label: "1 hour"}, {minutes: 1440, label: "24 hours"}]`, default 1440 (`state.linkMinutes`).
- `setupResultHtml(r, {esc, fmtExpiry})` renders "Link valid until …" and, when `r.session_expiration_hours` is a number, "Session: N days after setup" / "Session: permanent".

- [ ] Node test: request includes `timeout_minutes` when given, omits otherwise.
- [ ] Implement select on Template step (above Generate), result copy ("named … when it connects, as long as the link is valid").
- [ ] `node --test tests/js/provision-logic.test.mjs`; commit `feat(panel): link validity picker (24 h default) and clearer result copy`.

### Task 5: Devices tab pending rows

**Files:**
- Create: `panel/views/pending-provision.js` (modal + actions)
- Modify: `panel/api.js`, `panel/views/devices.js`, `panel/styles.js`

**Interfaces:**
- `api.getPendingProvision(userId)`, `api.pendingProvisionAction(userId, action, extra = {})`.
- `openPendingModal(app, p)`: loads links, shows `setupResultHtml`, buttons Copy link / Regenerate / Revoke / Close. `regeneratePending(app, p)`, `revokePending(app, p)`, `dismissPending(app, p)`.
- Devices rows: `row.kind === "pending"`, key `pending:<user_id>`; status dot `status-dot--pending`; filters gain status "awaiting"; select-mode skips pending rows; meta line "N awaiting setup".

- [ ] Implement; manual check in browser against Lamplighter after deploy (not automatable here).
- [ ] Commit `feat(panel): show unredeemed provisions in Devices with QR/links, revoke, regenerate`.

### Task 6: Version bump + docs

- [ ] `26.10.06` in `const.py`, `panel/version.js`, `manifest.json`; `python3 -m pytest -q` (test_versions).
- [ ] Note in `docs/provisioning-protocol.md` if it documents `expires_at`/window (grep first).
- [ ] Commit `chore: 26.10.06 — link window + pending provisions`.

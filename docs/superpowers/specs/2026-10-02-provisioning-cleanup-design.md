# Provisioning Cleanup — Design Spec (2026-10-02)

Make provisioning easy and concise in the Casa admin panel. Today there are
three overlapping entry points (scenario popup, classic wizard, guided flow)
and none of them supports "pick an existing account + pick an existing
template": the classic wizard's Username is a free-text box, the Accounts tab
has no provision action, and the only preset-account path ("Re-provision
user") requires an existing device.

This spec replaces all of that with **one new-device wizard** and a
**one-click re-provision** action on devices.

Scope: **casa-provisioner** (HA integration + admin panel). No iOS app
changes.

## Requirements (as decided)

| Topic | Decision |
|---|---|
| Scenarios | New account + template; existing account + template; manual config (no template). Re-provisioning is a device action, not a wizard scenario. |
| Account model | Shared accounts allowed — several devices may sign in as one account. |
| Wizard shape | One unified 3-step wizard: **Account → Template → Done**. No scenario popup. Replaces the classic wizard and the guided flow. |
| New account naming | Device name doubles as the account display name. Username suggested as `casa-<slug>`, editable (prefix not enforced). |
| Username conflict | Offer "use this existing account instead?" which flips to *Use existing* with it selected. |
| Existing-account picker | Casa-managed accounts only (Accounts tab). Device name still **required**. |
| Template step | Saved templates + one **Configure manually** row (merges "Create new template" and "One-off"). Full form hidden behind a collapsed **Customize for this device**; changes apply to this device only unless the **Also save as new template** checkbox (off by default) is ticked. |
| Passwords | Always auto-generated, never shown, for new and existing accounts. No custom-password UI. |
| Per-run options | Collapsed **Advanced** on the Template step: PIN, Wi-Fi join, "Sign out all other devices on this account" (deauthenticate_existing). |
| Hidden from UI | BLE beacon, setup window length (default 5 min), password scramble, QR filename, delete-QR-after-window. `casa.provision` service still accepts all of them. |
| Delivery | No Deliver step. "Generate setup" lands on Done: QR + universal link + deep link, always. |
| Entry points | Devices "+ Provision device"; Accounts row ⋮ "Provision device"; Templates row "Provision with this template"; "Provision a device now" after Create account. Preselected items are prefilled; the wizard always starts at step 1. |
| Re-provision location | Device row ⋮ menu **and** a button on the device page. |
| Re-provision UX | Near-touchless: one confirm. Auto method — push if push-registered, else QR/link. A "Phone wiped or replaced? Show QR instead" link forces QR. No other options. |
| Re-provision account | Always the device's current account. Account switching (Reauthenticate…) is **removed entirely**. |
| Re-provision password | Always rotated (as every provision does today). |
| Re-provision sessions | Revoke **only this device's** session. Push path: after the device signs back in. QR path: immediately on generate. Other devices on the account untouched. |
| Re-provision settings | Push: login only, settings unchanged. QR: seeded from the device's reported `provisioning_fields` → else its original template (`provisioning_profile_id`) → else system defaults. |
| Replaced phone | A QR re-provision redeemed by a phone with a new `device_id` replaces the old record (inherits alias + lineage; old record deleted). |
| Out of scope | "Apply template…" device action, template editor, `casa.provision` service schema. |
| Release | `CASA_VERSION` (const.py) + `PANEL_VERSION` (panel/version.js) bumped together. |

## Panel: new-device wizard (`views/provision.js`, rewritten)

### Routes

| Route | Behavior |
|---|---|
| `/provision` | Wizard, nothing preselected. |
| `/provision/account/:username` | *Use existing account* preselected with that account. |
| `/provision/template/:templateId` | That template preselected on step 2. |
| `/provision/user/:username` | Legacy alias — same view and behavior as `/provision/account/:username`. |
| `/provision/profile/:templateId` | Legacy alias — same as `/provision/template/:templateId`. |
| `/provision/guided` | Legacy alias — same as `/provision`. |

(The panel router has no redirects; legacy patterns map to the same view.)

Unknown preselected account/template → toast ("That account no longer
exists." / "That provision template no longer exists.") and start with
nothing preselected.

### Step ① Account

```
● Create new account
    Device name *  [ Kitchen iPad        ]   (also the account name)
    Username *     [ casa-kitchen-ipad   ]   ✓ available
○ Use existing account
    Account *      [ Mobile Bryce (mobile-bryce) · 0 devices ▾ ]
    Device name *  [ Kitchen iPad        ]
                                              [ Continue ]
```

- Username auto-slugs from device name with the `casa-` prefix until the admin
  edits the username field (same `usernameEdited` rule the guided flow uses).
  Validation: `username-utils.js` `USERNAME_RE` (`^[a-z0-9][a-z0-9-]*$`),
  so `casa-` fits without changing the rule.
- Live availability via `username-utils.js` `attachUsernameField` / the
  existing availability API (advisory; `create_user` is authoritative).
- On conflict where the taken username is a Casa-managed account: the hint
  shows "`<username>` already exists — *use this account instead?*". Clicking
  selects *Use existing account* with that account, keeping the typed device
  name. If the conflict is a non-Casa or admin user, plain "already in use"
  error (they can't be picked).
- Picker lists accounts from the existing accounts summary (`casa_managed`),
  sorted by name, each showing its device count.
- If the run already created an account (retry after failure), step ① is
  read-only with the existing guided-flow notice ("Account X was already
  created for this run…").

### Step ② Template

```
● Mobile Owners     Push required · VPN · Permanent · All pages
○ Kiosk Tablet      All pages
○ Configure manually

▸ Customize for this device
▸ Advanced (PIN, Wi-Fi join, sign out other devices)
                                   [ Back ]  [ Generate setup ]
```

- Template rows use `payload-preview.js` `profileChips`. Search box shown when
  there are more than ~8 templates.
- **Customize for this device** (collapsed; auto-opens for *Configure
  manually*): the `profile-fields.js` sections Connection (host_url only —
  username/password/pin/deauth are not in it), App UI, Access Control,
  Push & VPN, Timing & Security (`expiration_hours`, `cache_control_hours`).
  Seeded `DEFAULTS` + selected template's fields; `host_url` defaults to
  `window.location.origin` when unset. Switching template re-seeds the form
  (confirm if the admin had edited it).
- Inside Customize: `☐ Also save as new template` + name field (required when
  ticked). Saved sparse: the base template's set keys + keys the admin
  changed (guided flow's fork logic, minus the prompt). For *Configure
  manually* the saved template stores only touched keys (`newSetKeys` logic).
- **Advanced** (collapsed): PIN (≤6 chars), Wi-Fi SSID + password,
  `☐ Sign out all other devices on this account` (`deauthenticate_existing`,
  default off).
- Validation on Generate: host_url required; template name required if the
  save box is ticked.

### Generate (deploy order, retry-safe)

1. **Create account** — new-account branch only; skipped if `createdUser` set.
2. **Save template** — only if the save box is ticked; skipped if
   `savedTemplateId` set.
3. **`api.provision`** with: `method: "qr"`, `user_id`, `username`,
   `device_alias`, the coerced fields, process fields (pin, Wi-Fi,
   deauthenticate_existing), `profile` lineage (rules below).
   No `password` (server generates).

Lineage: selected template unchanged → its id; saved-as-new → the new id;
customized but not saved, or Configure manually unsaved → no `profile`.

Failure at any step → error bar on step ②; Generate retries from the first
incomplete step. Leaving after the account was created keeps the account
(existing behavior; the unload/leave guard warns).

### Step ③ Done

QR image (`qr_data_uri`), Universal link + Deep link with Copy, "valid until
…" chip. Buttons: `Provision another` (fresh state, strips preset path) /
`Done` (refresh + go to Devices).

### Entry points

- `views/devices.js`: "+ Provision device" → `/provision` (unchanged).
- `views/accounts.js`: row ⋮ menu gains "Provision device" →
  `/provision/account/<username>`. Create-account success dialog gains a
  "Provision a device now" button → same route.
- `views/templates.js`: row ⋮ "Provision with this template" →
  `/provision/template/<id>` — already exists, no change.

## Panel: re-provision (`views/reprovision.js`, new)

Exported `openReprovisionModal(app, device)`, lazy-loaded by:

- `views/devices.js` row ⋮ menu: "Re-provision" (replaces "Re-provision
  user"; "Reauthenticate…" removed).
- `views/device-editor.js`: a "Re-provision" card + button in the
  **Overview** section (the app shell has no header-action slot).
- Hidden for native (non-Casa) devices in both places.

### Confirm dialog

Push-registered (`device.push_registered`):

> **Re-provision Kitchen iPad?**
> Sends a new login to the device over push. Other devices on mobile-bryce
> stay signed in.
> `[Cancel] [Re-provision]` — *Phone wiped or replaced? Show QR instead*

Not push-registered:

> **Re-provision Kitchen iPad?**
> This signs the device out now and gives you a new QR/link to set it up
> again. Other devices on mobile-bryce stay signed in.
> `[Cancel] [Re-provision]`

"Show QR instead" switches the dialog to the second wording before confirm.

### Result

- Push: "Sent — Kitchen iPad will sign back in when it gets it (queued if
  it's offline)." + Done. The pending reauth appears in the device's Pending
  Updates as today.
- QR: same QR + links + validity block as wizard step ③ (shared render
  helper extracted so both views use it).

### Removed

`openReauthModal` and its markup in `device-editor.js`, the
"Reauthenticate…" menu item, and `api.reauthDevice` if nothing else uses it.

## Server: `POST /api/casa/admin/reprovision_device`

Admin-only view, like the other `casa/admin/*` views.

Request: `{ "device_id": str, "method": "auto" | "qr", "host_url": str }`
(`host_url` = panel origin, used only as the last-resort fallback).

Errors (JSON `error`, 4xx): device not found (404); device owned by an
admin or non-Casa account (400 — re-provision is for Casa-managed accounts);
account has no homeassistant credentials (400).

State changes run under the device's lock (`_lock_for(hass, "device",
device_id)`), taking the account's user lock inside it for the password
change (device-before-user order). Push delivery / relay calls happen after
the locks are released, as `reauth_device`'s prepare/`_deliver` split
already does.

### Method resolution

`auto` → `push` when the device has a `push_token` and the site has a
`device_key`; otherwise `qr`. `qr` → `qr`.

### Push path

Reuse the reauth internals (`CasaAdminReauthDeviceView._reauth` +
`_deliver`, with `_deliver` split so its result dict is reusable) with:
target user = the device's current owner, no account switch, no
`scramble_old`, `send_update_push = true`, no password supplied. As in
reauth today, if another device's still-queued reauth to the same account
already holds a server-set password, that password is reused instead of
rotating (rotating would strand that device's entry); otherwise the password
is rotated. The password is never returned to the panel.

Effects (all existing behavior of those internals): rotate the account
password via `_set_account_password` (closes other open windows for the
login, invalidates stale queued reauths), replace any pending reauth, enqueue
a durable `auth/reauthenticate` update, stamp `reauth_pending` with
`old_refresh_token_id`, push or nudge. `_complete_pending_reauth` revokes the
old token after the device signs in with the new password. Settings are not
touched.

Response: `{ "status": "ok", "method": "push", "update_id", "pushed" }`.

### QR path

1. Cancel a pending reauth for the device (dequeue its update, clear
   `reauth_pending`).
2. Resolve fields, per key over `PROFILE_KEYS`:
   device `provisioning_fields` (normalized) → else template
   `provisioning_profile_id` fields (if the template still exists) → else
   system defaults. `host_url`: resolved value, else request `host_url`.
3. Call `_provision_internal` with `user_id` = owner, `method: "qr"`, the
   resolved fields, `device_alias` = current alias, `profile` = the device's
   `provisioning_profile_id` when the template still exists, and the new
   `replaces_device_id = device_id` (see below). No password → rotated.
   `deauthenticate_existing` false.
4. Revoke the device's `refresh_token_id` immediately
   (`hass.auth.async_remove_refresh_token`), if present. No other tokens.

Response: the normal provision result (`qr_data_uri`, `deep_link`,
`universal_link`, `expires_at`) plus `"method": "qr"`.

### Replacement on redeem (`replaces_device_id`)

- `_provision_internal` gains a keyword-only `replaces_device_id` argument
  (not read from service_data, so the `casa.provision` service can't set it)
  and stores it on the `pending_provisions[user_id]` record. The reprovision
  view reaches `_provision_internal` through `hass.data[DOMAIN]["provision_func"]`
  via `_entry_func`, the same pattern the register/heartbeat views use.
- When the window's listener records the redeeming token ids
  (`_record_provision_claims`), it also records
  `stored_data["replacement_claims"][token_id] = {replaces_device_id, at}`
  (24 h TTL, `_CLAIM_WINDOW_SECONDS`). Keyed by refresh token — not by account —
  so a shared account can't route the replacement to the wrong device.
- In `async_register_device`, pop `replacement_claims[refresh_token_id]`:
  - registering `device_id` == `replaces_device_id` → nothing extra (record
    kept, token rebound by the normal merge).
  - different `device_id` and the old record still exists → copy `alias`,
    `provisioning_profile_id`, `provisioning_profile_name` onto the new
    record (alias only if the new one has none), then delete the old record
    through the same path "Delete record" uses (revoke its token if still
    live, unregister its push relay token, purge its queued updates).
- Stale/missing claim → normal registration, no replacement.

Known constraint (unchanged): `pending_provisions` is keyed by account, and
`_set_account_password` closes other open windows for the login — so on a
shared account only one setup link/QR is live at a time; generating a new one
invalidates the previous one.

## Removals

- `views/provision-guided.js` and its route (redirect kept).
- Scenario popup (`openScenarioModal`) and the classic wizard internals:
  Deploy step, method cards, BLE targets, QR options, process-timing
  fields.
- Reauthenticate UI (above).
- `app.js` route table updated per the Routes table.

Unchanged: `casa.provision` service parameters, "Apply template…",
template editor, `reauth_device` endpoint (kept; its internals are shared
with the push path).

## Testing

pytest (`tests/`, using `fakes.py`), new `tests/test_reprovision.py`:

- push path: same-account reauth queued, `reauth_pending.old_refresh_token_id`
  set, no token revoked yet, password rotated.
- push path when not push-registered with `auto` → falls to QR.
- QR seed precedence: reported fields win; template used when no reported
  fields; defaults when template deleted / none; `host_url` fallback.
- QR path revokes only the device's token; another session on the same
  account survives.
- QR path cancels a pending reauth.
- rejects unknown device, admin-owned device, non-Casa account.
- replacement: redeem + register with new `device_id` → old record deleted,
  alias/lineage copied; same `device_id` → record kept; no claim → no-op.

Manual on Fairwater test HA:

- Existing account + existing template (Mobile Bryce + Mobile Owners).
- New account: `casa-` suggestion, conflict → "use this account instead".
- Configure manually + "Also save as new template".
- Entry points: Accounts ⋮, Templates row, after Create account.
- Re-provision via push (device signs back in, old session gone, other
  device on same account still signed in) and via "Show QR instead".
- Replaced phone: QR re-provision redeemed on a different phone replaces the
  record.

## Docs

Update `docs/provisioning-protocol.md` only if any app-observable behavior
changes (none expected — replacement is server-side).

# Provisioning Follow-ups Implementation Plan (2026-10-02)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix the post-provision name prompt (server applies the wizard's name on first contact + name rides the QR/deep-link payload), rename auto-generated entity ids on a fresh provision, and let admins enlarge dense QR codes.

**Architecture:** Server: one `_apply_pending_provision` helper consumed by whichever of register/heartbeat arrives first from the redeeming session; optional `device_alias` payload key; an entity-registry rename that runs at apply time and, for ~10 minutes, on entity creation. Panel: click-to-enlarge QR modal. iOS: decode `device_alias`, never prompt when present.

**Tech Stack:** Python HA custom integration (plain pytest + `tests/fakes.py`), vanilla ES-module panel (`node --test tests/js/*.mjs`), Swift (CasaCore package tests via `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test` in `Packages/CasaCore`).

**Spec:** design approved in chat 2026-10-02 (no spec file — bounded follow-up to `docs/superpowers/specs/2026-10-02-provisioning-cleanup-design.md`).

## Global Constraints

- casa-provisioner repo `/Users/bryce/Documents/casa/casa-provisioner`, branch `provisioning-followups` (create from `main` in Task 1). iOS repo `/Users/bryce/Documents/casa/casa-mobile-app/casa-ios`, branch `provisioning-followups` (create from `main` in Task 5).
- Baselines: pytest 156 passed; node 8 passed; CasaCore 54 tests.
- Payload rule (docs/provisioning-protocol.md §5): adding an optional key never bumps `v`; apps ignore unknown keys.
- An admin/wizard-set alias always wins over a device-submitted one (existing heartbeat rule).
- Entity rename touches only entities whose object id starts with `casa_device_`; never ids the user changed.
- Never hold `_lock_for` locks across network calls.
- Release: provisioner `26.10.03` (const.py `CASA_VERSION`, panel/version.js `PANEL_VERSION`, manifest.json `version` — `tests/test_versions.py` enforces). iOS build `CURRENT_PROJECT_VERSION` 14 → 15 (all occurrences in `Casa.xcodeproj/project.pbxproj`), `MARKETING_VERSION` stays 1.8.

## Review Focus

1. A second device on a shared account heart-beating before the new device must not consume the new device's pending name (fresh-claim guard) — test in Task 2.
2. Heartbeat applying the pending name must still let the later register see it as already applied (no double-apply, no overwrite) — test in Task 2.
3. Entity rename collision: target id already taken → HA-style `_2` suffix via `async_generate_entity_id`, never an exception — test in Task 3.
4. Payload without `device_alias` (older server) → iOS behaves exactly as before — test in Task 5.

---

### Task 1: Click-to-enlarge QR (panel)

**Files:** Modify `custom_components/casa/panel/views/provision-result.js`; Test `tests/js/provision-logic.test.mjs` (append).

**Interfaces:** `setupResultHtml(r, {esc, fmtExpiry})` unchanged signature; `bindCopyButtons(root, ui)` additionally binds the zoom.

- [ ] **Step 0:** `cd /Users/bryce/Documents/casa/casa-provisioner && git checkout main && git pull --ff-only && git checkout -b provisioning-followups`
- [ ] **Step 1: failing test** — append to `tests/js/provision-logic.test.mjs`:

```js
test("setupResultHtml marks the QR as zoomable", () => {
  const html = setupResultHtml({ qr_data_uri: "data:image/png;base64,AA" }, { esc, fmtExpiry: () => "" });
  assert.match(html, /data-qr-zoom/);
  assert.match(html, /Click to enlarge/);
});
```

Run `node --test tests/js/*.mjs` → the new test fails.

- [ ] **Step 2: implement** — in `setupResultHtml`, render the QR `<img>` inside a `<button type="button" data-qr-zoom="<escaped qr>" title="Click to enlarge" style="border:none; background:none; padding:0; cursor:zoom-in;">`, and add under it `<div class="muted" style="font-size:12px; margin-top:6px;">Click to enlarge</div>`. Add `image-rendering:pixelated;` to the img style. In `bindCopyButtons(root, ui)`, after the copy loop, add:

```js
  for (const btn of root.querySelectorAll("[data-qr-zoom]")) {
    btn.addEventListener("click", () => {
      const body = document.createElement("div");
      body.style.cssText = "display:flex; justify-content:center;";
      const img = document.createElement("img");
      img.src = btn.dataset.qrZoom;
      img.alt = "Provisioning QR code";
      img.style.cssText = "width:min(720px, 80vw, 75vh); height:auto; image-rendering:pixelated; background:#fff; padding:24px; box-sizing:border-box; border-radius:var(--casa-radius-sm);";
      body.appendChild(img);
      ui.openModal({ title: "Scan with the Casa app", bodyEl: body, wide: true, buttons: [{ label: "Close", variant: "primary" }] });
    });
  }
```

Update the file's header comment to mention the zoom.
- [ ] **Step 3:** `node --check custom_components/casa/panel/views/provision-result.js`; `node --test tests/js/*.mjs` → 9 pass; `python3 -m pytest -q tests` → 156.
- [ ] **Step 4: commit** `feat(panel): click a setup QR to enlarge it`

---

### Task 2: Server applies the wizard's name on first contact + `device_alias` payload key

**Files:** Modify `custom_components/casa/__init__.py` (new module-level helper after `_apply_device_replacement`; `async_register_device`; `async_heartbeat`; `_provision_internal` v2 `profile` dict); `docs/provisioning-protocol.md` §4; Test `tests/test_pending_apply.py` (create).

**Interfaces:**
- Produces `async _apply_pending_provision(hass, user_id: str, device_info: dict, refresh_token_id: str | None) -> bool` — True when it consumed the redeeming session's pending entry (a fresh provision). Task 3 calls its rename hook from inside it.

- [ ] **Step 1: failing tests** — create `tests/test_pending_apply.py`:

```python
import asyncio
import time

from custom_components.casa import _apply_pending_provision, _record_provision_claims
from tests.fakes import FakeHass, make_user


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="casa-bryce-mobile", tokens=["t-new", "t-other"]))
    hass.casa["pending_profile_by_user"] = {"u1": {
        "profile_id": "t1", "profile_name": "Mobile Owners", "device_alias": "Bryce Mobile",
        "expiration_hours": 0, "set_at": time.time(),
    }}
    return hass


def test_redeeming_session_applies_name_lineage_expiration():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is True
    assert info["alias"] == "Bryce Mobile"
    assert info["provisioning_profile_id"] == "t1"
    assert info["provisioning_profile_name"] == "Mobile Owners"
    assert info["provisioning_expiration_hours"] == 0
    assert "u1" not in hass.casa["pending_profile_by_user"]


def test_second_contact_is_a_noop():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new"))
    info["alias"] = "Kept"
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is False
    assert info["alias"] == "Kept"


def test_other_device_on_shared_account_does_not_consume():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    other = {}
    # t-other is an old session: not a recorded claim and created before the window opened.
    hass.casa["stored_data"]["provision_opened"] = {"u1": time.time() + 60}
    assert asyncio.run(_apply_pending_provision(hass, "u1", other, "t-other")) is False
    assert other == {}
    assert "u1" in hass.casa["pending_profile_by_user"]


def test_existing_alias_is_not_overwritten():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {"alias": "Admin set"}
    asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new"))
    assert info["alias"] == "Admin set"


def test_stale_pending_is_dropped_without_applying():
    hass = _hass()
    hass.casa["pending_profile_by_user"]["u1"]["set_at"] = time.time() - 3600
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is False
    assert info == {}
```

Run `python3 -m pytest -q tests/test_pending_apply.py` → ImportError.

- [ ] **Step 2: implement the helper** (module level, after `_apply_device_replacement`):

```python
async def _apply_pending_provision(hass, user_id: str, device_info: dict, refresh_token_id: str | None) -> bool:
    """Apply what _provision_internal stashed for user_id (template lineage,
    session length, the name typed in the wizard) to this device record —
    on whichever of register/heartbeat arrives first, and only from the
    session that redeemed the window (on a shared account another device's
    contact must not take it). Entries older than 30 min are dropped.
    Returns True when it consumed the entry (a fresh provision)."""
    pending_by_user = hass.data[DOMAIN].setdefault("pending_profile_by_user", {})
    pending = pending_by_user.get(user_id)
    if not pending or not await _has_fresh_claim(hass, user_id, refresh_token_id):
        return False
    pending_by_user.pop(user_id, None)
    if time.time() - pending.get("set_at", 0) > 1800:
        return False
    device_info["provisioning_profile_id"] = pending.get("profile_id")
    device_info["provisioning_profile_name"] = pending.get("profile_name")
    if "expiration_hours" in pending:
        device_info["provisioning_expiration_hours"] = pending["expiration_hours"]
    alias = str(pending.get("device_alias") or "").strip()
    if alias and not str(device_info.get("alias") or "").strip():
        device_info["alias"] = alias[:DEVICE_ALIAS_MAX_LEN]
    return True
```

- [ ] **Step 3: use it.** In `async_register_device`, replace the whole pending block (from `pending_by_user = hass.data[DOMAIN].get("pending_profile_by_user", {})` through the alias assignment `devices[device_id]["alias"] = pending_alias[:DEVICE_ALIAS_MAX_LEN]`, including its comments) with:

```python
        # Fresh provision: template lineage, session length and the wizard's
        # name — applied by whichever of register/heartbeat comes first.
        await _apply_pending_provision(hass, user_id, devices[device_id], refresh_token_id)
```

In `async_heartbeat`, right after the `device_info = devices.setdefault(device_id, {...})` statement and before the `if last_12_token is not None:` line, add:

```python
        # A fresh provision's first heartbeat can beat its registration;
        # apply the wizard's name now so has_alias is true from the start.
        await _apply_pending_provision(hass, user_id, device_info, refresh_token_id)
```

Then, still in `async_heartbeat`, re-run the purge guard after that await (copy the existing idiom): `if _device_being_purged(hass, device_id): raise HomeAssistantError("Device is being removed.")`.

- [ ] **Step 4: payload key.** In `_provision_internal`'s v2 `profile` dict construction, immediately after the dict literal closes (before `if lz_anchors:`), add:

```python
            # Optional (26.10.03): the name typed in the wizard / re-provision,
            # so the app knows it is named and never prompts.
            payload_alias = str(service_data.get("device_alias", "") or "").strip()[:DEVICE_ALIAS_MAX_LEN]
            if payload_alias:
                profile["device_alias"] = payload_alias
```

(Confirm the dict variable is named `profile` and is what gets encrypted for both QR and deep link; if the deep link builds its own payload, add the key there too.)

In `docs/provisioning-protocol.md` §4 table, add after the `relay_url` row:

```
| `device_alias` | string, optional (26.10.03) | Name the admin gave the device in the wizard or a re-provision. Present only when set. Apps treat the device as named (no name prompt) and send it as the heartbeat `alias`; the server keeps any alias it already has. |
```

Add a test to `tests/test_protocol_vectors.py` only if it builds payloads from a function you can call with `device_alias`; otherwise skip and say so in the report.

- [ ] **Step 5:** `python3 -m pytest -q tests` → 161 passed; ast parse check `python3 -c "import ast; ast.parse(open('custom_components/casa/__init__.py').read())"`.
- [ ] **Step 6: commit** `fix: apply the wizard's device name on first contact; carry device_alias in the setup payload`

---

### Task 3: Rename auto-generated entity ids on a fresh provision

**Files:** Modify `custom_components/casa/__init__.py` (helpers after `_sync_ha_device_name`; call from `_apply_pending_provision`; listener in `async_setup_entry`); Test `tests/test_entity_rename.py` (create).

**Interfaces:**
- Consumes `_apply_pending_provision` (Task 2), `_find_device_record`, `_ha_device_name`.
- Produces `_rename_casa_entity(registry, entry, alias: str) -> str | None`, `_rename_device_entities(hass, device_id: str) -> None`, `ENTITY_RENAME_WINDOW_SECONDS = 600`.

- [ ] **Step 1: failing tests** — create `tests/test_entity_rename.py`:

```python
import sys
import time
import types
from types import SimpleNamespace

from custom_components.casa import _rename_casa_entity, _rename_device_entities, _apply_pending_provision
from tests.fakes import FakeHass


class FakeEntityRegistry:
    def __init__(self, entries):
        self.entries = {e.entity_id: e for e in entries}
        self.updates = []

    def async_generate_entity_id(self, domain, suggested_object_id, known_object_ids=None):
        base = suggested_object_id.lower().replace(" ", "_").replace("&", "").replace("__", "_").strip("_")
        cand, n = f"{domain}.{base}", 1
        while cand in self.entries:
            n += 1
            cand = f"{domain}.{base}_{n}"
        return cand

    def async_update_entity(self, entity_id, new_entity_id):
        e = self.entries.pop(entity_id)
        e.entity_id = new_entity_id
        self.entries[new_entity_id] = e
        self.updates.append((entity_id, new_entity_id))
        return e


def _entry(entity_id, name, device_reg_id="reg-D1", unique_id="casa_D1_ip"):
    return SimpleNamespace(entity_id=entity_id, original_name=name, name=None, platform="casa",
                           device_id=device_reg_id, unique_id=unique_id)


def test_auto_id_is_renamed_from_alias():
    reg = FakeEntityRegistry([_entry("sensor.casa_device_guest_ip_address_2", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.casa_device_guest_ip_address_2"], "Bryce Mobile") == "sensor.bryce_mobile_ip_address"


def test_custom_id_is_left_alone():
    reg = FakeEntityRegistry([_entry("sensor.my_phone_ip", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.my_phone_ip"], "Bryce Mobile") is None
    assert reg.updates == []


def test_collision_gets_suffix():
    reg = FakeEntityRegistry([
        _entry("sensor.bryce_mobile_ip_address", "IP Address", unique_id="casa_OTHER_ip"),
        _entry("sensor.casa_device_x_ip_address", "IP Address"),
    ])
    assert _rename_casa_entity(reg, reg.entries["sensor.casa_device_x_ip_address"], "Bryce Mobile") == "sensor.bryce_mobile_ip_address_2"


def test_already_named_correctly_is_noop():
    reg = FakeEntityRegistry([_entry("sensor.bryce_mobile_ip_address", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.bryce_mobile_ip_address"], "Bryce Mobile") is None
```

Also add one test for `_rename_device_entities` that installs fake `homeassistant.helpers.entity_registry` (with `async_get` returning the fake registry and `async_entries_for_device(registry, device_id)` returning entries whose `device_id` matches) and fake `homeassistant.helpers.device_registry` (as in `tests/test_ha_device_name.py`), seeds a managed device `D1` with `alias: "Bryce Mobile"` and `entity_rename_until: time.time() + 600`, calls `_rename_device_entities(hass, "D1")`, and asserts the `casa_device_*` entity was renamed; and a second asserting nothing is renamed when `entity_rename_until` is in the past. Follow the monkeypatch pattern in `tests/test_ha_device_name.py`.

- [ ] **Step 2: implement** (after `_sync_ha_device_name`):

```python
ENTITY_RENAME_WINDOW_SECONDS = 600


def _rename_casa_entity(registry, entry, alias: str) -> str | None:
    """Give one auto-generated Casa entity an id from the device name
    ("sensor.casa_device_guest_ip_address_2" -> "sensor.bryce_mobile_ip_address").
    Ids that don't start with casa_device_ were chosen by a person and are
    left alone. Collisions get HA's _2/_3 suffix. Returns the new id or None."""
    domain, object_id = entry.entity_id.split(".", 1)
    if not object_id.startswith("casa_device_") or not alias:
        return None
    label = entry.original_name or entry.name or ""
    target = registry.async_generate_entity_id(domain, f"{alias} {label}".strip())
    if target == entry.entity_id:
        return None
    registry.async_update_entity(entry.entity_id, new_entity_id=target)
    return target


def _rename_device_entities(hass, device_id: str) -> None:
    """While a freshly provisioned device's rename window is open, rename its
    auto-generated entity ids after its alias (see _rename_casa_entity)."""
    stored_data = hass.data[DOMAIN]["stored_data"]
    device_info, _uid, _username = _find_device_record(stored_data, device_id)
    if not device_info or (device_info.get("entity_rename_until") or 0) < time.time():
        return
    alias = str(device_info.get("alias") or "").strip()
    if not alias:
        return
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, device_id)})
    if device is None:
        return
    registry = er.async_get(hass)
    for entry in list(er.async_entries_for_device(registry, device.id)):
        if getattr(entry, "platform", DOMAIN) == DOMAIN:
            _rename_casa_entity(registry, entry, alias)
```

In `_apply_pending_provision` (Task 2), just before `return True`, add `device_info["entity_rename_until"] = time.time() + ENTITY_RENAME_WINDOW_SECONDS` (only when the record ends up with an alias).

- [ ] **Step 3: run it at the right moments.**
  - In `async_register_device` and `async_heartbeat`, right after each one's device-registry `async_get_or_create(...)` call (inside the `if create_devices:` block), add `_rename_device_entities(hass, device_id)` — covers entities that already exist (same phone re-provisioned).
  - In `async_setup_entry`, register once per entry (store the unsubscribe in `hass.data[DOMAIN]` and call it in `async_unload_entry` next to the other unsubscribes — grep `reconcile_unsub` to find the pattern) a listener for newly created entities:

```python
    from homeassistant.helpers import entity_registry as er

    @callback
    def _on_entity_registry_updated(event):
        if event.data.get("action") != "create":
            return
        entry = er.async_get(hass).async_get(event.data.get("entity_id"))
        if not entry or entry.platform != DOMAIN or not entry.device_id:
            return
        from homeassistant.helpers import device_registry as dr
        device = dr.async_get(hass).async_get(entry.device_id)
        ident = next((i for d, i in (device.identifiers if device else ()) if d == DOMAIN), None)
        if ident:
            _rename_device_entities(hass, ident)

    hass.data[DOMAIN]["entity_rename_unsub"] = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_updated
    )
```

  (`callback` — check how it is imported in `__init__.py`; if absent, `from homeassistant.core import callback`. If the conftest stubs lack `callback`, add a passthrough to the stub `homeassistant.core` in `tests/conftest.py`.)
- [ ] **Step 4:** `python3 -m pytest -q tests` → all pass (161 + new); ast parse check.
- [ ] **Step 5: commit** `feat: freshly provisioned devices get entity ids from their name`

---

### Task 4: Provisioner release 26.10.03

**Files:** `custom_components/casa/const.py`, `custom_components/casa/panel/version.js`, `custom_components/casa/manifest.json`, `README.md`.

- [ ] Bump all three versions `26.10.02` → `26.10.03` (`sed -i ''` as in the earlier release); `python3 -m pytest -q tests/test_versions.py` → 2 pass.
- [ ] README: in the **Provision wizard** bullet, after "Done: QR code plus universal and `hascasa://` deep links with copy buttons" add "(click the QR to enlarge it)"; append to the bullet: "The device name travels in the setup payload, so the app doesn't ask for one, and a freshly provisioned device's auto-generated entity ids are renamed after it (e.g. `sensor.bryce_mobile_location`)."
- [ ] Full suites: `python3 -m pytest -q tests`, `node --test tests/js/*.mjs`.
- [ ] Commit `chore: 26.10.03 — provisioning follow-ups`.

---

### Task 5: iOS — read `device_alias`, never prompt when named

**Files:** `Packages/CasaCore/Sources/CasaCore/URLParser.swift` (V2 payload struct + `PayloadData`); `Casa/ContentView.swift` (~line 2165 where `requireAliasFromProfile = data.requireAlias`); `Casa/OnboardingView.swift` (~line 1052, same); CasaCore tests; `Casa.xcodeproj/project.pbxproj` build number.

- [ ] **Step 0:** `cd /Users/bryce/Documents/casa/casa-mobile-app/casa-ios && git checkout main && git pull --ff-only && git checkout -b provisioning-followups`
- [ ] **Step 1: failing test** — in the CasaCore test target, add a test that decodes a v2 payload JSON containing `"device_alias": "Bryce Mobile"` and asserts `data.deviceAlias == "Bryce Mobile"`, and one without the key asserting `deviceAlias == nil`. Find how existing v2 decode tests construct payloads (grep `requireAlias` / `relay_url` in `Packages/CasaCore/Tests`) and follow that pattern exactly (e.g. if there is an internal JSON → PayloadData entry point used by tests, use it).
- [ ] **Step 2: implement** — `V2Payload` gains `let deviceAlias: String?` (decoder uses snake_case conversion if the struct relies on it — match how `relayUrl` maps from `relay_url`); `PayloadData` gains `public var deviceAlias: String? = nil` (defaulted var like `pin`); the v2 mapping sets `parsed.deviceAlias = (p.deviceAlias ?? "").trimmingCharacters(in: .whitespaces).isEmpty ? nil : p.deviceAlias!.trimmingCharacters(in: .whitespaces)`.
- [ ] **Step 3: app behavior** — at both sites that set `appState.requireAliasFromProfile = data.requireAlias`, add right after:

```swift
        // The admin named this device in the wizard (payload `device_alias`):
        // it is named — never prompt; the name rides the next heartbeat as
        // `alias` until the server confirms has_alias (server keeps its own).
        if let name = data.deviceAlias {
            appState.hasAlias = true
            appState.pendingAliasSubmission = name
            appState.pendingAliasSubmittedAt = Date()
        }
```

  Then verify that nothing between this point and the dashboard (notably `AppState.commitFreshProvisioning` and any reset it calls) clears `hasAlias`/`pendingAliasSubmission`; if something does, apply the same three assignments at the end of `commitFreshProvisioning` from a stashed value instead, and explain in the report.
- [ ] **Step 4:** bump `CURRENT_PROJECT_VERSION = 14;` → `15;` everywhere in `Casa.xcodeproj/project.pbxproj` (check the count before/after; ignore `Casa.xcodeproj.survived-backup`).
- [ ] **Step 5:** run CasaCore tests (`cd Packages/CasaCore && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test 2>&1 | grep -E "Executed [0-9]+ tests"` → 56+ tests, 0 failures) and an app build: `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild build -project Casa.xcodeproj -scheme Casa -destination 'generic/platform=iOS Simulator' ARCHS=arm64 -quiet` → succeeds with no new warnings.
- [ ] **Step 6: commit** `feat: device name from the setup payload — no name prompt when the admin named the device` (one commit for code + one `chore: build 15` for the pbxproj, or one combined).

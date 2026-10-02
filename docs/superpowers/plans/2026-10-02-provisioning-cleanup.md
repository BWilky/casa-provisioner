# Provisioning Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scenario popup + classic wizard + guided flow with one 3-step new-device wizard (Account → Template → Done) that supports existing accounts, and add a one-click, server-side "Re-provision" device action (push when possible, QR otherwise) that replaces Reauthenticate….

**Architecture:** Backend gains a `POST /api/casa/admin/reprovision_device` view that reuses the reauth internals (push path) or `_provision_internal` (QR path, reached via `hass.data[DOMAIN]["provision_func"]` + `_entry_func`), plus `replaces_device_id` plumbing so a QR re-provision redeemed by a new phone replaces the old record. The panel's `views/provision.js` is rewritten around a pure, node-tested `views/provision-logic.js`; a shared `views/provision-result.js` renders QR + links for both the wizard and the new `views/reprovision.js` modal.

**Tech Stack:** Python 3 (Home Assistant custom integration, plain pytest with `tests/fakes.py`), vanilla ES-module JS panel (no build step; `node --test` for pure helpers, Node ≥ 22).

**Spec:** `docs/superpowers/specs/2026-10-02-provisioning-cleanup-design.md`

## Global Constraints

- Work on branch `review-fixes-2026-10` in `casa-provisioner` (current branch; do not create another).
- Run pytest from the repo root: `cd /Users/bryce/Documents/casa/casa-provisioner && python3 -m pytest -q tests` (baseline: 122 passed).
- Run JS tests from the repo root: `node --test tests/js/` (Node ESM auto-detect; files under `custom_components/casa/panel` use `export` without a package.json).
- Panel views never static-import sibling views — load them with `app.loadModule("views/<file>.js")`. Pure helper modules may be imported by node tests directly.
- New usernames are suggested as `casa-<slug>`; validation stays `USERNAME_RE = /^[a-z0-9][a-z0-9-]*$/` (username-utils.js) — do not change it.
- Passwords are never shown in the wizard or re-provision UI.
- Re-provision is only for Casa-managed accounts (`stored_data["users"]`); hide it for native devices (`device.native`).
- `casa.provision` service parameters are unchanged; BLE / timeout / scramble / qr_filename / delete_qr_after_window are only hidden from the panel.
- Bump `CASA_VERSION` (`custom_components/casa/const.py`), `PANEL_VERSION` (`custom_components/casa/panel/version.js`) and `manifest.json` `version` together to `26.10.02` (enforced by `tests/test_versions.py`).
- Panel error text goes through `ui.errMsg(err)`; for `callApi` errors whose body is `{"error": "..."}` read `err.body.error` first.
- Lock order: `_lock_for(hass, "device", id)` before `_lock_for(hass, "user", id)`; never hold either across relay/network calls.

## Review Focus

1. **Shared account with another device's queued reauth** — push re-provision of D1 must not strand D2's pending reauth on the same account (reauth internals reuse the queued server-set password). Test: `test_push_reuses_other_devices_queued_password` (Task 4).
2. **Provision failure on the QR path** — if `_provision_internal` returns `{"error": ...}`, the device's session must NOT be revoked (otherwise the device is cut off with no QR). Test: `test_qr_provision_error_revokes_nothing` (Task 4).
3. **Re-provision of a device whose original template was deleted and that never reported settings** — must fall to system defaults with the panel's origin as `host_url`, not error. Test: `test_seed_falls_back_to_defaults_and_origin` (Task 3).
4. **Expired / foreign replacement claim** — a registration whose token has a stale (>24 h) claim, or no claim, must not purge any record. Tests: `test_replacement_ignores_expired_claim`, `test_replacement_noop_without_claim` (Task 1).
5. **Username suggestion from a name with no slug-able characters** (e.g. emoji only) — must suggest `""`, not `"casa-"`, so validation shows "required" rather than an invalid username. Test: `suggestUsername returns empty for unsluggable names` (Task 5).

---

### Task 1: Replacement claims (backend helpers)

**Files:**
- Modify: `custom_components/casa/__init__.py` — `_record_provision_claims` (≈line 739) and add two helpers right after `_consume_provision_claim` (≈line 778–785)
- Test: `tests/test_device_replacement.py` (create)

**Interfaces:**
- Consumes: `_find_device_record(stored_data, device_id) -> (info, owner_uid, username)`, `_purge_device(hass, device_id) -> dict`, `_save_stored_data(hass)`, `_CLAIM_WINDOW_SECONDS` (24 h).
- Produces:
  - `_record_provision_claims(hass, user_id: str, token_ids, replaces_device_id: str | None = None) -> None` — additionally writes `stored_data["replacement_claims"][token_id] = {"replaces_device_id": str, "at": float}` when `replaces_device_id` is set.
  - `_consume_replacement_claim(hass, refresh_token_id: str | None) -> str | None`
  - `async _apply_device_replacement(hass, new_device_id: str, new_info: dict, refresh_token_id: str | None) -> str | None` — returns the purged old device id, or `None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_device_replacement.py`:

```python
import asyncio
import time

from custom_components.casa import (
    _apply_device_replacement,
    _consume_replacement_claim,
    _record_provision_claims,
)
from tests.fakes import FakeHass, make_user


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="kitchen", tokens=["r-old", "r-new"]))
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kitchen", "devices": {
        "OLD": {"alias": "Kitchen iPad", "provisioning_profile_id": "t1",
                "provisioning_profile_name": "Mobile Owners", "refresh_token_id": "r-old"},
        "NEW": {"refresh_token_id": "r-new"},
    }}
    return hass


def _devices(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]


def test_claim_records_replacement_per_token():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    claim = hass.casa["stored_data"]["replacement_claims"]["r-new"]
    assert claim["replaces_device_id"] == "OLD"
    assert "r-new" in hass.casa["stored_data"]["provision_claims"]["u1"]


def test_claim_without_replacement_records_none():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"})
    assert hass.casa["stored_data"].get("replacement_claims", {}) == {}


def test_consume_is_single_use():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    assert _consume_replacement_claim(hass, "r-new") == "OLD"
    assert _consume_replacement_claim(hass, "r-new") is None


def test_new_phone_replaces_old_record():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    new_info = _devices(hass)["NEW"]
    replaced = asyncio.run(_apply_device_replacement(hass, "NEW", new_info, "r-new"))
    assert replaced == "OLD"
    assert "OLD" not in _devices(hass)
    assert new_info["alias"] == "Kitchen iPad"
    assert new_info["provisioning_profile_id"] == "t1"
    assert new_info["provisioning_profile_name"] == "Mobile Owners"
    assert "r-old" in hass.auth.removed_tokens


def test_new_alias_is_not_overwritten():
    hass = _hass()
    _devices(hass)["NEW"]["alias"] = "Typed on phone"
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new"))
    assert _devices(hass)["NEW"]["alias"] == "Typed on phone"


def test_same_phone_keeps_record():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    replaced = asyncio.run(_apply_device_replacement(hass, "OLD", _devices(hass)["OLD"], "r-new"))
    assert replaced is None
    assert "OLD" in _devices(hass)
    assert hass.casa["stored_data"]["replacement_claims"] == {}


def test_replacement_noop_without_claim():
    hass = _hass()
    replaced = asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new"))
    assert replaced is None
    assert set(_devices(hass)) == {"OLD", "NEW"}


def test_replacement_ignores_expired_claim():
    hass = _hass()
    hass.casa["stored_data"]["replacement_claims"] = {
        "r-new": {"replaces_device_id": "OLD", "at": time.time() - 2 * 86400},
    }
    replaced = asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new"))
    assert replaced is None
    assert "OLD" in _devices(hass)


def test_replacement_noop_when_old_record_gone():
    hass = _hass()
    _devices(hass).pop("OLD")
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    assert asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new")) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_device_replacement.py`
Expected: FAIL — `ImportError: cannot import name '_apply_device_replacement'`

- [ ] **Step 3: Implement**

In `custom_components/casa/__init__.py`, replace `_record_provision_claims` with:

```python
def _record_provision_claims(hass, user_id: str, token_ids, replaces_device_id: str | None = None) -> None:
    """Remember the refresh tokens that redeemed a provisioning window for
    user_id: proof that the session came from that user's provisioning link,
    which lets it take over its device record from a previous owner.

    replaces_device_id (QR re-provision only): the device record the window
    was issued for. Recorded per redeeming token so the registration that
    follows can tell a replacement phone (new device_id) apart — keyed by
    token, not account, so a shared account can't route it to the wrong
    record (see _apply_device_replacement)."""
    if not token_ids:
        return
    stored_data = hass.data[DOMAIN]["stored_data"]
    claims = stored_data.setdefault("provision_claims", {}).setdefault(user_id, {})
    now = time.time()
    for tid in token_ids:
        claims[tid] = now
    for tid in [t for t, ts in claims.items() if now - ts > _CLAIM_TTL_SECONDS]:
        claims.pop(tid, None)
    if replaces_device_id:
        rclaims = stored_data.setdefault("replacement_claims", {})
        for tid in token_ids:
            rclaims[tid] = {"replaces_device_id": replaces_device_id, "at": now}
        for tid in [t for t, c in rclaims.items() if now - c.get("at", 0) > _CLAIM_WINDOW_SECONDS]:
            rclaims.pop(tid, None)
    _save_stored_data(hass)
```

Immediately after the `_consume_provision_claim` function, add:

```python
def _consume_replacement_claim(hass, refresh_token_id: str | None) -> str | None:
    """Pop the replacement claim (if any) a QR re-provision left for this
    session token. Single use; claims older than _CLAIM_WINDOW_SECONDS are
    dropped without effect. Returns the device id to replace, or None."""
    if not refresh_token_id:
        return None
    stored_data = hass.data[DOMAIN]["stored_data"]
    claims = stored_data.get("replacement_claims") or {}
    claim = claims.pop(refresh_token_id, None)
    if not claim:
        return None
    _save_stored_data(hass)
    if time.time() - claim.get("at", 0) > _CLAIM_WINDOW_SECONDS:
        return None
    return claim.get("replaces_device_id") or None


async def _apply_device_replacement(hass, new_device_id: str, new_info: dict, refresh_token_id: str | None) -> str | None:
    """A QR re-provision redeemed by a different phone (wiped or replaced, so
    a new device_id): the new record inherits the old one's alias (only if
    it has none) and template lineage, and the old record is purged the way
    "Delete record" does it. Same device_id → nothing to do (the normal
    register merge already rebinds the token). Returns the purged device id."""
    old_device_id = _consume_replacement_claim(hass, refresh_token_id)
    if not old_device_id or old_device_id == new_device_id:
        return None
    stored_data = hass.data[DOMAIN]["stored_data"]
    old_info, _old_uid, _old_name = _find_device_record(stored_data, old_device_id)
    if not old_info:
        return None
    if not str(new_info.get("alias") or "").strip() and str(old_info.get("alias") or "").strip():
        new_info["alias"] = old_info["alias"]
    for key in ("provisioning_profile_id", "provisioning_profile_name"):
        if not new_info.get(key) and old_info.get(key):
            new_info[key] = old_info[key]
    await _purge_device(hass, old_device_id)
    _LOGGER.info("CASA: Device '%s' replaced '%s' after a QR re-provision.", new_device_id, old_device_id)
    return old_device_id
```

(`_purge_device` and `_find_device_record` are module-level functions defined later in the file; that's fine — they're resolved at call time.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest -q tests/test_device_replacement.py` → all pass.
Run: `python3 -m pytest -q tests` → 131 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/__init__.py tests/test_device_replacement.py
git commit -m "feat: replacement claims let a re-provisioned new phone replace its old record"
```

---

### Task 2: Wire `replaces_device_id` through provision, redeem and register

**Files:**
- Modify: `custom_components/casa/__init__.py`
  - `_arm_pending_provision` (≈line 860–890): pass the record's `replaces_device_id` to the claims callback
  - `async_register_device` closure (≈line 3690–3800): call `_apply_device_replacement`
  - `_provision_internal` closure (≈line 4156): keyword-only `replaces_device_id`, stored on the pending record; publish as `hass.data[DOMAIN]["provision_func"]`
- Test: `tests/test_device_replacement.py` (append)

**Interfaces:**
- Consumes: Task 1's `_record_provision_claims(..., replaces_device_id=)`, `_apply_device_replacement(...)`.
- Produces:
  - `_provision_internal(service_data: dict, users: list = None, *, replaces_device_id: str | None = None) -> dict`
  - `hass.data[DOMAIN]["provision_func"]` → `_provision_internal` (used by Task 4 via `_entry_func(hass, "provision_func")`).
  - `pending_provisions[user_id]["replaces_device_id"]` (str | None).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_device_replacement.py`:

```python
import custom_components.casa as casa
from custom_components.casa import _arm_pending_provision


def test_armed_window_forwards_replaces_device_id(monkeypatch):
    hass = _hass()
    now = time.time()
    hass.casa["stored_data"]["pending_provisions"] = {"u1": {
        "provision_id": "p1", "login_username": "kitchen", "method": "qr", "single_use": True,
        "scramble_at": now + 300, "window_ends_at": now + 300, "listen_until": now + 330,
        "known_token_ids": ["r-old"], "qr_file": None, "qr_expire_mode": "delete",
        "created_at": now, "replaces_device_id": "OLD",
    }}
    captured = {}

    def fake_listener(*args, **kwargs):
        captured.update(kwargs)

        async def noop():
            return None
        return noop()

    monkeypatch.setattr(casa, "_login_listener", fake_listener)

    async def scenario():
        _arm_pending_provision(hass, "u1")
        captured["on_tokens"]({"r-new"})

    asyncio.run(scenario())
    assert hass.casa["stored_data"]["replacement_claims"]["r-new"]["replaces_device_id"] == "OLD"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_device_replacement.py::test_armed_window_forwards_replaces_device_id`
Expected: FAIL — `KeyError: 'replacement_claims'`

- [ ] **Step 3: Implement**

(a) In `_arm_pending_provision`, change the `on_tokens=` line from

```python
            on_tokens=lambda tids: _record_provision_claims(hass, user_id, tids),
```

to

```python
            on_tokens=lambda tids: _record_provision_claims(
                hass, user_id, tids, replaces_device_id=rec.get("replaces_device_id"),
            ),
```

(b) Change the `_provision_internal` signature from

```python
    async def _provision_internal(service_data: dict, users: list = None) -> dict:
```

to

```python
    async def _provision_internal(service_data: dict, users: list = None, *, replaces_device_id: str | None = None) -> dict:
        # replaces_device_id is keyword-only and never read from service_data,
        # so the casa.provision service can't set it — only the admin
        # reprovision view passes it (see _apply_device_replacement).
```

and in the `pending_provisions[target_user.id] = { ... }` dict, add after `"created_at": now_ts,`:

```python
            "replaces_device_id": replaces_device_id or None,
```

(c) Immediately before `    async def handle_provision(call: ServiceCall):` add:

```python
    # The admin reprovision view (registered once per process) reaches the
    # current entry's provision closure through hass.data, like register/heartbeat.
    hass.data[DOMAIN]["provision_func"] = _provision_internal

```

(d) In `async_register_device`, find:

```python
            if pending_alias and not str(devices[device_id].get("alias") or "").strip():
                devices[device_id]["alias"] = pending_alias[:DEVICE_ALIAS_MAX_LEN]

        _save_stored_data(hass)
```

and insert between the `if` block and `_save_stored_data(hass)`:

```python

        # QR re-provision redeemed by a different phone (wiped / replaced):
        # the new record takes over the old one's identity and the old record
        # is purged. No claim (the normal case) → no-op.
        replaced_device_id = await _apply_device_replacement(hass, device_id, devices[device_id], refresh_token_id)
        if replaced_device_id:
            _remove_registry_device(hass, replaced_device_id)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest -q tests` → 132 passed.
Run: `python3 -c "import ast,sys; ast.parse(open('custom_components/casa/__init__.py').read())"` → no output.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/__init__.py tests/test_device_replacement.py
git commit -m "feat: provision windows carry replaces_device_id through redeem to register"
```

---

### Task 3: Re-provision field seeding

**Files:**
- Modify: `custom_components/casa/__init__.py` — add `_reprovision_service_data` right after `_normalize_reported_fields` (≈line 2573)
- Test: `tests/test_reprovision.py` (create)

**Interfaces:**
- Consumes: `_normalize_reported_fields(fields) -> dict`, `LIVE_PROVISIONING_FIELDS` (const), `hass.data[DOMAIN]["pp_data"]["profiles"]` (templates: `{"id", "name", "fields": {...}}`).
- Produces: `_reprovision_service_data(hass, device_info: dict, fallback_host_url: str) -> dict` — `casa.provision` service_data with `method="qr"`, `deauthenticate_existing=False`, reported LIVE fields, optional `profile` (template id, only if it still exists), optional `device_alias`, and `host_url` = panel origin only when neither the reports nor the template carry one. Caller adds `user_id`/`username`.

Precedence note (document, don't change): `_provision_internal`'s `get_field` treats `""` as unset, so a reported empty string falls through to the template's value, then the default.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reprovision.py`:

```python
from custom_components.casa import _reprovision_service_data
from tests.fakes import FakeHass

ORIGIN = "http://192.168.1.21:8123"


def _hass(templates=()):
    hass = FakeHass()
    hass.casa["pp_data"]["profiles"] = list(templates)
    return hass


def test_seed_uses_reported_fields_and_live_template():
    hass = _hass([{"id": "t1", "name": "Mobile Owners", "fields": {"host_url": "https://t"}}])
    info = {"alias": "Kitchen iPad", "provisioning_profile_id": "t1",
            "provisioning_fields": {"host_url": "https://h", "default_dashboard": "/d", "bogus": 1}}
    data = _reprovision_service_data(hass, info, ORIGIN)
    assert data["method"] == "qr"
    assert data["deauthenticate_existing"] is False
    assert data["host_url"] == "https://h"
    assert data["default_dashboard"] == "/d"
    assert "bogus" not in data
    assert data["profile"] == "t1"
    assert data["device_alias"] == "Kitchen iPad"


def test_seed_normalizes_legacy_reports():
    data = _reprovision_service_data(_hass(), {"provisioning_fields": {
        "host_url": "https://h", "immersive_level": "2,custom,#ff0000"}}, ORIGIN)
    assert data["immersive_level"] == "2"
    assert data["theme_color_mode"] == "custom"


def test_seed_without_reports_uses_template_host():
    hass = _hass([{"id": "t1", "name": "T", "fields": {"host_url": "https://t"}}])
    data = _reprovision_service_data(hass, {"provisioning_profile_id": "t1"}, ORIGIN)
    assert data["profile"] == "t1"
    assert "host_url" not in data  # the template supplies it via get_field


def test_seed_falls_back_to_defaults_and_origin():
    data = _reprovision_service_data(_hass(), {"provisioning_profile_id": "deleted"}, ORIGIN)
    assert "profile" not in data
    assert data["host_url"] == ORIGIN
    assert "device_alias" not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_reprovision.py`
Expected: FAIL — `ImportError: cannot import name '_reprovision_service_data'`

- [ ] **Step 3: Implement**

After `_normalize_reported_fields` add:

```python
def _reprovision_service_data(hass, device_info: dict, fallback_host_url: str) -> dict:
    """casa.provision service_data for a QR re-provision of an existing device.

    Per-key precedence (applied by _provision_internal's get_field): the
    device's own reported live settings, then its original template (only if
    it still exists), then system defaults. host_url falls back to the
    panel's origin only when neither source has one. get_field treats "" as
    unset, so a reported empty string falls through to the template.
    The caller adds user_id / username."""
    data = {}
    reported = device_info.get("provisioning_fields")
    if isinstance(reported, dict) and reported:
        data = {k: v for k, v in _normalize_reported_fields(reported).items() if k in LIVE_PROVISIONING_FIELDS}
    template = None
    profile_id = device_info.get("provisioning_profile_id")
    if profile_id:
        profiles = (hass.data[DOMAIN].get("pp_data") or {}).get("profiles", [])
        template = next((p for p in profiles if p.get("id") == profile_id), None)
    if template:
        data["profile"] = template["id"]
    template_host = str(((template or {}).get("fields") or {}).get("host_url") or "").strip()
    if not str(data.get("host_url") or "").strip() and not template_host:
        data["host_url"] = fallback_host_url
    data["method"] = "qr"
    data["deauthenticate_existing"] = False
    alias = str(device_info.get("alias") or "").strip()
    if alias:
        data["device_alias"] = alias
    return data
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest -q tests/test_reprovision.py` → 4 passed. `python3 -m pytest -q tests` → 136 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/__init__.py tests/test_reprovision.py
git commit -m "feat: seed QR re-provision from reported settings, then template, then defaults"
```

---

### Task 4: `reprovision_device` admin endpoint

**Files:**
- Modify: `custom_components/casa/__init__.py`
  - `CasaAdminReauthDeviceView._deliver` (≈line 3437–3480): split into `_deliver_result` (returns dict) + thin `_deliver`
  - Add `class CasaAdminReprovisionDeviceView` right after `CasaAdminReauthDeviceView` (before `class CasaAdminRegenerateKeyView`)
  - View registration block (≈line 4053): register it
- Test: `tests/test_reprovision.py` (append)

**Interfaces:**
- Consumes: `CasaAdminReauthDeviceView._reauth(request, user, body, device_id) -> (response | None, push_kwargs | None)`, `_reprovision_service_data` (Task 3), `hass.data[DOMAIN]["provision_func"]` (Task 2) via `_entry_func`, `_dequeue_update(qu_data, device_id, update_id)`, `_find_device_record`, `_lock_for`, `_save_stored_data`.
- Produces:
  - `CasaAdminReauthDeviceView._deliver_result(**push) -> dict` (same keys `_deliver` returned before).
  - `POST /api/casa/admin/reprovision_device` body `{device_id: str, method: "auto"|"qr", host_url: str}`.
    - push → `200 {"status": "ok", "method": "push", "update_id", "pushed", "push_skipped", "username", ...}` (never `password`)
    - qr → `200 {..._provision_internal result, "method": "qr"}` (`qr_data_uri`, `deep_link`, `universal_link`, `expires_at`)
    - errors → `{"error": str}` with 400/403/404.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reprovision.py`:

```python
import asyncio

import custom_components.casa as casa
from custom_components.casa import CasaAdminReprovisionDeviceView
from tests.fakes import FakeRequest, bind_view, make_user

QR_RESULT = {"method": "qr", "provision_id": "p1", "qr_data_uri": "data:image/png;base64,AA",
             "deep_link": "hascasa://setup?d=x", "universal_link": "https://bonjour.casa/setup#x",
             "expires_at": 1900000000}


def _site(push=False):
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="mobile-bryce", tokens=["r1", "r2"]), password="pw")
    d1 = {"refresh_token_id": "r1", "alias": "Kitchen iPad",
          "provisioning_fields": {"host_url": "https://h"}}
    if push:
        d1["push_token"] = "a" * 64
        hass.casa["stored_data"]["device_key"] = "k" * 44
    hass.casa["stored_data"]["users"]["u1"] = {"username": "mobile-bryce", "devices": {
        "D1": d1, "D2": {"refresh_token_id": "r2"},
    }}
    calls = []

    async def provision(service_data, users=None, *, replaces_device_id=None):
        calls.append((dict(service_data), replaces_device_id))
        return dict(QR_RESULT)

    return hass, provision, calls


def _post(hass, provision, body, admin=True):
    view = bind_view(CasaAdminReprovisionDeviceView(hass, provision))
    return asyncio.run(view.post(FakeRequest(make_user("admin", admin=admin), body)))


def test_qr_path_provisions_and_revokes_only_this_device():
    hass, provision, calls = _site()
    status, resp = _post(hass, provision, {"device_id": "D1", "method": "auto", "host_url": ORIGIN})
    assert status == 200 and resp["method"] == "qr"
    assert resp["qr_data_uri"] == QR_RESULT["qr_data_uri"]
    (service_data, replaces), = calls
    assert replaces == "D1"
    assert service_data["user_id"] == "u1" and service_data["username"] == "mobile-bryce"
    assert service_data["method"] == "qr" and service_data["host_url"] == "https://h"
    assert hass.auth.removed_tokens == ["r1"]
    assert "r2" in hass.auth.users["u1"].refresh_tokens


def test_qr_cancels_pending_reauth():
    hass, provision, _ = _site()
    hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["reauth_pending"] = {"update_id": "e1"}
    hass.casa["qu_data"]["updates"]["D1"] = [{"id": "e1", "type": "auth", "action": "reauthenticate",
                                             "payload": {"username": "mobile-bryce", "password": "x"}}]
    status, _ = _post(hass, provision, {"device_id": "D1", "method": "qr", "host_url": ORIGIN})
    assert status == 200
    assert "reauth_pending" not in hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]
    assert hass.casa["qu_data"]["updates"].get("D1", []) == []


def test_qr_provision_error_revokes_nothing():
    hass, _, _ = _site()

    async def failing(service_data, users=None, *, replaces_device_id=None):
        return {"error": "User not found"}

    status, resp = _post(hass, failing, {"device_id": "D1", "method": "qr", "host_url": ORIGIN})
    assert status == 400 and resp["error"] == "User not found"
    assert hass.auth.removed_tokens == []


def test_push_path_queues_same_account_reauth(monkeypatch):
    hass, provision, calls = _site(push=True)
    pushed = []

    async def fake_push(*args, **kwargs):
        pushed.append(args)
        return True

    monkeypatch.setattr(casa, "_send_encrypted_update_push", fake_push)
    status, resp = _post(hass, provision, {"device_id": "D1", "method": "auto", "host_url": ORIGIN})
    assert status == 200 and resp["method"] == "push" and resp["pushed"] is True
    assert "password" not in resp
    assert calls == []  # no QR provision
    assert hass.auth.removed_tokens == []  # old session revoked only after the new login
    marker = hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["reauth_pending"]
    assert marker["target_user_id"] == "u1" and marker["old_refresh_token_id"] == "r1"
    entry, = [e for e in hass.casa["qu_data"]["updates"]["D1"] if e["type"] == "auth"]
    assert entry["payload"]["password"] == hass.auth.provider.data.passwords["mobile-bryce"]
    assert entry["payload"]["password"] != "pw"  # rotated


def test_push_reuses_other_devices_queued_password(monkeypatch):
    hass, provision, _ = _site(push=True)

    async def fake_push(*args, **kwargs):
        return True

    monkeypatch.setattr(casa, "_send_encrypted_update_push", fake_push)
    # D2 already has a queued reauth carrying the server-set password.
    from custom_components.casa import _set_account_password
    current = asyncio.run(_set_account_password(hass, hass.auth.provider, "mobile-bryce"))
    hass.casa["qu_data"]["updates"]["D2"] = [{"id": "e2", "type": "auth", "action": "reauthenticate",
                                             "payload": {"username": "mobile-bryce", "password": current}}]
    hass.casa["stored_data"]["users"]["u1"]["devices"]["D2"]["reauth_pending"] = {"update_id": "e2"}
    status, _ = _post(hass, provision, {"device_id": "D1", "method": "auto", "host_url": ORIGIN})
    assert status == 200
    assert hass.auth.provider.data.passwords["mobile-bryce"] == current
    assert [e["id"] for e in hass.casa["qu_data"]["updates"]["D2"]] == ["e2"]


def test_forced_qr_skips_push_even_when_registered():
    hass, provision, calls = _site(push=True)
    status, resp = _post(hass, provision, {"device_id": "D1", "method": "qr", "host_url": ORIGIN})
    assert status == 200 and resp["method"] == "qr" and len(calls) == 1


def test_rejects_non_admin_unknown_native_and_bad_method():
    hass, provision, _ = _site()
    assert _post(hass, provision, {"device_id": "D1"}, admin=False)[0] == 403
    assert _post(hass, provision, {"device_id": "NOPE"})[0] == 404
    assert _post(hass, provision, {"device_id": "D1", "method": "ble"})[0] == 400
    hass.auth.add_user(make_user("n1", login="native", tokens=["rn"]))
    hass.casa["stored_data"]["native_devices"] = {"n1": {"N1": {"refresh_token_id": "rn"}}}
    status, resp = _post(hass, provision, {"device_id": "N1"})
    assert status == 400 and "Casa-managed" in resp["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_reprovision.py`
Expected: FAIL — `ImportError: cannot import name 'CasaAdminReprovisionDeviceView'`

- [ ] **Step 3: Split `_deliver`**

In `CasaAdminReauthDeviceView`, rename `async def _deliver(self, device_id, update_id, ...)` to `async def _deliver_result(self, device_id, update_id, login_username, login_password, revealed_password, created_user, scrambled_old, send_update_push, created_by, old_label):` (same parameters), and change its last line from `return self.json(resp)` to `return resp`. Then add right after it:

```python
    async def _deliver(self, **push):
        return self.json(await self._deliver_result(**push))
```

(`post` already calls `self._deliver(**push)`, so reauth behavior is unchanged. `tests/test_password_rotation.py` covers it.)

- [ ] **Step 4: Add the view**

Insert after the `CasaAdminReauthDeviceView` class:

```python
class CasaAdminReprovisionDeviceView(HomeAssistantView):
    """Admin-only one-click re-provision of an existing device, always on its
    current (Casa-managed) account.

    method "auto": over encrypted push when the device is push-registered —
    the reauth internals rotate the account password (or reuse one another
    device's queued reauth already carries), queue `auth/reauthenticate`, and
    revoke this device's old session only once it signs back in. Settings are
    untouched. Otherwise (or method "qr"): a fresh QR/link seeded from the
    device's reported settings → original template → defaults, tagged with
    replaces_device_id so a replacement phone takes over this record, and the
    device's current session is revoked immediately. Other sessions on the
    account are never touched. Passwords are never returned.
    """

    url = "/api/casa/admin/reprovision_device"
    name = "api:casa:admin:reprovision_device"

    def __init__(self, hass: HomeAssistant, provision_func):
        self.hass = hass
        self.provision_func = provision_func

    async def post(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = str(body.get("device_id", "")).strip()
        if not device_id:
            return self.json({"error": "device_id is required"}, status_code=400)
        method = str(body.get("method", "auto") or "auto").strip().lower()
        if method not in ("auto", "qr"):
            return self.json({"error": "method must be 'auto' or 'qr'"}, status_code=400)
        host_url = str(body.get("host_url", "") or "").strip()

        hass = self.hass
        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info, owner_uid, _username = _find_device_record(stored_data, device_id)
        if not device_info:
            return self.json({"error": "Device not found"}, status_code=404)
        udata = (stored_data.get("users") or {}).get(owner_uid)
        if not udata or udata.get("deleted", False):
            return self.json({"error": "Re-provision is only available for Casa-managed accounts"}, status_code=400)
        owner = await hass.auth.async_get_user(owner_uid)
        if not owner or getattr(owner, "is_admin", False):
            return self.json({"error": "Cannot re-provision a device on an admin account"}, status_code=400)

        push_ready = bool(device_info.get("push_token")) and bool(stored_data.get("device_key"))
        if method == "auto" and push_ready:
            return await self._via_push(request, user, device_id, owner_uid)
        return await self._via_qr(device_id, host_url)

    async def _via_push(self, request, user, device_id, owner_uid):
        reauth = CasaAdminReauthDeviceView(self.hass)
        reauth.json = self.json
        reauth.json_message = self.json_message
        body = {"device_id": device_id, "user_id": owner_uid, "send_update_push": True}
        async with _lock_for(self.hass, "device", device_id):
            response, push = await reauth._reauth(request, user, body, device_id)
        if push is None:
            return response
        result = await reauth._deliver_result(**push)
        result.pop("password", None)
        result["method"] = "push"
        return self.json(result)

    async def _via_qr(self, device_id, host_url):
        hass = self.hass
        data = hass.data[DOMAIN]
        stored_data = data["stored_data"]
        async with _lock_for(hass, "device", device_id):
            # Re-resolve under the lock: the record may have moved meanwhile.
            device_info, owner_uid, username = _find_device_record(stored_data, device_id)
            if not device_info:
                return self.json({"error": "Device not found"}, status_code=404)

            service_data = _reprovision_service_data(hass, device_info, host_url)
            service_data["user_id"] = owner_uid
            service_data["username"] = username
            result = await self.provision_func(service_data, replaces_device_id=device_id)
            if not isinstance(result, dict) or result.get("error"):
                # Nothing changed on failure: pending reauth and session kept.
                return self.json({"error": (result or {}).get("error") or "Provisioning failed"}, status_code=400)

            # A still-pending push reauth would fight the new QR's password.
            prev = device_info.pop("reauth_pending", None)
            if prev and prev.get("update_id"):
                _dequeue_update(data["qu_data"], device_id, prev["update_id"])
                data["qu_store"].async_delay_save(lambda: data["qu_data"], 2.0)

            # Cut this device off now; it comes back through the new QR/link.
            rtid = device_info.get("refresh_token_id")
            if rtid:
                owner = await hass.auth.async_get_user(owner_uid)
                token = owner.refresh_tokens.get(rtid) if owner else None
                if token:
                    hass.auth.async_remove_refresh_token(token)
            _save_stored_data(hass)
        return self.json({**result, "method": "qr"})
```

- [ ] **Step 5: Register the view**

In the view-registration block, after `hass.http.register_view(CasaAdminReauthDeviceView(hass))` add:

```python
        hass.http.register_view(CasaAdminReprovisionDeviceView(hass, _entry_func(hass, "provision_func")))
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest -q tests/test_reprovision.py tests/test_password_rotation.py` → all pass.
Run: `python3 -m pytest -q tests` → 143 passed.

- [ ] **Step 7: Commit**

```bash
git add custom_components/casa/__init__.py tests/test_reprovision.py
git commit -m "feat: reprovision_device endpoint — push via same-account reauth, else QR revoking only this device"
```

---

### Task 5: Summary `user_id`, API client, and pure wizard logic

**Files:**
- Modify: `custom_components/casa/__init__.py` — summary `accounts.append({...})` (≈line 1925): add `"user_id": uid`
- Modify: `custom_components/casa/panel/api.js` — add `reprovisionDevice`
- Create: `custom_components/casa/panel/views/provision-logic.js`
- Create: `custom_components/casa/panel/views/provision-result.js`
- Test: `tests/test_reprovision.py` (append), `tests/js/provision-logic.test.mjs` (create)

**Interfaces:**
- Produces:
  - summary `accounts[i]` = `{name, username, user_id, created_at, created_by, device_count}`
  - `api.reprovisionDevice({device_id, method, host_url}) -> Promise<object>`
  - `provision-logic.js` exports:
    - `USERNAME_PREFIX = "casa-"`
    - `suggestUsername(deviceName: string, slugify: (s)=>string) -> string`
    - `changedKeys(fields: object, baseline: object) -> string[]`
    - `templateFieldsToSave({fields, baseSetKeys: string[], changed: string[]}) -> object`
    - `lineageFor({templateId: string|null, customized: boolean, savedTemplateId: string|null}) -> string|null`
    - `buildProvisionRequest({fields, form, account: {username, user_id?, password?}, deviceName, lineage}) -> object`
  - `provision-result.js` exports `setupResultHtml(r, {esc, fmtExpiry}) -> string`, `bindCopyButtons(root, ui) -> void`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reprovision.py`:

```python
from custom_components.casa import CasaAdminSummaryView


def test_summary_accounts_carry_user_id():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "mobile-bryce", "name": "Mobile Bryce", "devices": {}}
    view = bind_view(CasaAdminSummaryView(hass))
    status, resp = asyncio.run(view.get(FakeRequest(make_user("admin", admin=True))))
    assert status == 200
    assert resp["accounts"][0]["user_id"] == "u1"
```

Create `tests/js/provision-logic.test.mjs`:

```js
import test from "node:test";
import assert from "node:assert/strict";
import { slugify } from "../../custom_components/casa/panel/views/username-utils.js";
import {
  suggestUsername,
  changedKeys,
  templateFieldsToSave,
  lineageFor,
  buildProvisionRequest,
} from "../../custom_components/casa/panel/views/provision-logic.js";
import { setupResultHtml } from "../../custom_components/casa/panel/views/provision-result.js";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

test("suggestUsername prefixes the slug", () => {
  assert.equal(suggestUsername("Kitchen iPad", slugify), "casa-kitchen-ipad");
});

test("suggestUsername returns empty for unsluggable names", () => {
  assert.equal(suggestUsername("🙂🙂", slugify), "");
  assert.equal(suggestUsername("   ", slugify), "");
});

test("changedKeys compares stringified values", () => {
  assert.deepEqual(changedKeys({ a: "1", b: true, c: 3 }, { a: "1", b: false, c: "3" }), ["b"]);
});

test("templateFieldsToSave keeps base keys, changed keys and host_url", () => {
  const fields = { host_url: "https://h", a: "x", b: "y", c: "z" };
  assert.deepEqual(templateFieldsToSave({ fields, baseSetKeys: ["a"], changed: ["c"] }), { host_url: "https://h", a: "x", c: "z" });
});

test("lineageFor", () => {
  assert.equal(lineageFor({ templateId: "t1", customized: false, savedTemplateId: null }), "t1");
  assert.equal(lineageFor({ templateId: "t1", customized: true, savedTemplateId: null }), null);
  assert.equal(lineageFor({ templateId: "t1", customized: true, savedTemplateId: "t2" }), "t2");
  assert.equal(lineageFor({ templateId: null, customized: false, savedTemplateId: null }), null);
});

test("buildProvisionRequest — existing account sends no password", () => {
  const data = buildProvisionRequest({
    fields: { host_url: "https://h" },
    form: { pin: " 1234 ", connect_wifi_ssid: "", connect_wifi_password: "pw", deauthenticate_existing: false },
    account: { username: "mobile-bryce", user_id: "u1" },
    deviceName: "Kitchen iPad",
    lineage: "t1",
  });
  assert.deepEqual(data, {
    method: "qr", host_url: "https://h", username: "mobile-bryce", user_id: "u1",
    device_alias: "Kitchen iPad", pin: "1234", profile: "t1",
  });
});

test("buildProvisionRequest — new account carries its password, wifi and deauth", () => {
  const data = buildProvisionRequest({
    fields: {},
    form: { pin: "", connect_wifi_ssid: "Home", connect_wifi_password: "secret", deauthenticate_existing: true },
    account: { username: "casa-kitchen-ipad", user_id: "u9", password: "gen" },
    deviceName: "Kitchen iPad",
    lineage: null,
  });
  assert.equal(data.password, "gen");
  assert.equal(data.connect_wifi_ssid, "Home");
  assert.equal(data.connect_wifi_password, "secret");
  assert.equal(data.deauthenticate_existing, true);
  assert.equal("profile" in data, false);
});

test("setupResultHtml renders QR, both links and validity, escaped", () => {
  const html = setupResultHtml(
    { qr_data_uri: "data:image/png;base64,AA", universal_link: "https://bonjour.casa/setup#\"x", deep_link: "hascasa://s", expires_at: 1 },
    { esc, fmtExpiry: () => "11:20 PM" },
  );
  assert.match(html, /<img src="data:image\/png;base64,AA"/);
  assert.match(html, /Universal Link/);
  assert.match(html, /hascasa:\/\/s/);
  assert.match(html, /&quot;x/);
  assert.match(html, /valid until 11:20 PM/);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_reprovision.py::test_summary_accounts_carry_user_id` → FAIL (`KeyError: 'user_id'`).
Run: `node --test tests/js/` → FAIL (`Cannot find module .../provision-logic.js`).

- [ ] **Step 3: Implement backend + API**

In the summary view's `accounts.append({`, add as the first key: `"user_id": uid,`.

In `panel/api.js`, after `reauthDevice(body) { ... }` add:

```js
  // One-click re-provision of an existing device on its current account
  // ({device_id, method: "auto" | "qr", host_url}). Push when registered,
  // else a fresh QR/link — see CasaAdminReprovisionDeviceView.
  reprovisionDevice(body) {
    const backend = this.summary && this.summary.version;
    if (this.panelVersion && backend && backend !== this.panelVersion) {
      return Promise.reject(new Error("Casa was updated on disk — restart Home Assistant before re-provisioning devices."));
    }
    return this._hass.callApi("POST", "casa/admin/reprovision_device", body);
  }
```

- [ ] **Step 4: Create `panel/views/provision-logic.js`**

```js
// Casa admin panel — pure decision logic for the provision wizard
// (views/provision.js): username suggestion, change detection, sparse
// template saves, lineage and the casa.provision request body. No DOM, no
// imports — unit-tested under node (tests/js/provision-logic.test.mjs).

export const USERNAME_PREFIX = "casa-";

// "Kitchen iPad" → "casa-kitchen-ipad"; a name with nothing slug-able
// suggests "" so the form shows "required" rather than a bare prefix.
export function suggestUsername(deviceName, slugify) {
  const slug = slugify(deviceName || "");
  return slug ? USERNAME_PREFIX + slug : "";
}

// Keys whose collected value differs from the seeded baseline. Values are
// compared as strings (collectFields coerces types; the baseline may hold
// raw template values).
export function changedKeys(fields, baseline) {
  return Object.keys(fields).filter((k) => String(fields[k] ?? "") !== String((baseline || {})[k] ?? ""));
}

// Sparse body for "Also save as new template": the base template's set keys
// plus whatever the admin changed — and always host_url, which every
// provision needs.
export function templateFieldsToSave({ fields, baseSetKeys, changed }) {
  const keep = new Set([...(baseSetKeys || []), ...(changed || []), "host_url"]);
  const out = {};
  for (const key of Object.keys(fields)) {
    if (keep.has(key)) out[key] = fields[key];
  }
  return out;
}

// Template lineage stamped on the device: a newly saved template wins; an
// unchanged saved template is recorded; customized-but-unsaved or manual
// carries none (the fields alone describe the device).
export function lineageFor({ templateId, customized, savedTemplateId }) {
  if (savedTemplateId) return savedTemplateId;
  if (templateId && !customized) return templateId;
  return null;
}

// casa.provision service_data. A new account's generated password is sent
// explicitly so a retry never rotates it; an existing account sends none
// and the server rotates it. Process-only inputs ride along only when set.
export function buildProvisionRequest({ fields, form, account, deviceName, lineage }) {
  const data = { method: "qr", ...fields, username: account.username };
  if (account.user_id) data.user_id = account.user_id;
  if (account.password) data.password = account.password;
  data.device_alias = deviceName;
  const pin = String(form.pin ?? "").trim();
  if (pin) data.pin = pin;
  const ssid = String(form.connect_wifi_ssid ?? "").trim();
  if (ssid) {
    data.connect_wifi_ssid = ssid;
    data.connect_wifi_password = String(form.connect_wifi_password ?? "");
  }
  if (form.deauthenticate_existing) data.deauthenticate_existing = true;
  if (lineage) data.profile = lineage;
  return data;
}
```

Note the expected object in the "existing account" test lists `device_alias` before `pin`; `assert.deepEqual` ignores key order, so this is fine.

- [ ] **Step 5: Create `panel/views/provision-result.js`**

```js
// Casa admin panel — shared "setup is ready" block: QR image, universal +
// deep link with Copy buttons, and the validity chip. Used by the provision
// wizard's Done step (views/provision.js) and the re-provision result modal
// (views/reprovision.js). Loaded lazily via app.loadModule.

function linkRow(label, value, esc) {
  return `
    <div class="field">
      <label>${esc(label)}</label>
      <div class="field-row">
        <input class="input mono" readonly value="${esc(value)}">
        <button class="btn btn--outlined" data-copy="${esc(value)}" style="flex:none;">Copy</button>
      </div>
    </div>`;
}

export function setupResultHtml(r, { esc, fmtExpiry }) {
  const qr = r.qr_data_uri || r.url_path;
  return `
    ${qr ? `
      <div style="text-align:center; margin-bottom:16px;">
        <div class="muted" style="font-size:13px; margin-bottom:12px;">Scan with the Casa app, or send a setup link.</div>
        <img src="${esc(qr)}" alt="Provisioning QR code"
          style="width:220px; height:220px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); padding:12px; background:#fff;">
      </div>` : ""}
    ${r.universal_link ? linkRow("Universal Link (opens from Safari / iMessage)", r.universal_link, esc) : ""}
    ${r.deep_link ? linkRow("Setup Deep Link", r.deep_link, esc) : ""}
    ${r.expires_at ? `<div style="margin-top:6px;"><span class="chip chip--warn">valid until ${esc(fmtExpiry(r.expires_at))}</span></div>` : ""}`;
}

export function bindCopyButtons(root, ui) {
  for (const btn of root.querySelectorAll("[data-copy]")) {
    ui.bindCopyButton(btn, () => btn.dataset.copy);
  }
}
```

- [ ] **Step 6: Run tests**

Run: `node --test tests/js/` → 8 passed.
Run: `python3 -m pytest -q tests` → 144 passed.

- [ ] **Step 7: Commit**

```bash
git add custom_components/casa/__init__.py custom_components/casa/panel/api.js \
  custom_components/casa/panel/views/provision-logic.js custom_components/casa/panel/views/provision-result.js \
  tests/test_reprovision.py tests/js/provision-logic.test.mjs
git commit -m "feat(panel): provision logic + shared setup-result block; summary accounts carry user_id"
```

---

### Task 6: Rewrite the new-device wizard

**Files:**
- Rewrite: `custom_components/casa/panel/views/provision.js` (replace the whole file)
- Delete: `custom_components/casa/panel/views/provision-guided.js`
- Modify: `custom_components/casa/panel/app.js:94-102` (provision routes)
- Modify: `custom_components/casa/panel/views/devices.js:350-355` (`gotoProvision`)

**Interfaces:**
- Consumes: `provision-logic.js` + `provision-result.js` (Task 5); `profile-fields.js` (`DEFAULTS`, `PROFILE_KEYS`, `PROCESS_KEYS`, `LIVE_KEYS`, `renderSectionHtml(sectionId, values, {esc, heading, fields, wgProfiles})`, `bindFieldEvents(el, {values, onSectionRerender, esc})`, `collectFields(values, keySet)`); `username-utils.js` (`USERNAME_RE`, `slugify`, `availabilityHintHtml(a, username, esc)`); `payload-preview.js` (`profileChips(template) -> [{label, cls}]`); `api.createUser`, `api.checkUsername(username, name) -> {available, username_conflict, name_conflict}`, `api.saveProvisionTemplate`, `api.getProvisionTemplates() -> {profiles}`, `api.getWireguardProfiles() -> {profiles}`, `api.provision`; summary `accounts[i].user_id` (Task 5).
- Produces: routes `/provision`, `/provision/account/:username`, `/provision/template/:templateId` (+ legacy `/provision/guided`, `/provision/user/:username`, `/provision/profile/:templateId`), all on `views/provision.js`; mount params `params.username`, `params.templateId`.

- [ ] **Step 1: Update routes**

In `app.js`, replace

```js
    { pattern: ["provision"], view: "views/provision.js" },
    { pattern: ["provision", "guided"], view: "views/provision-guided.js" },
    { pattern: ["provision", "user", ":username"], view: "views/provision.js" },
    { pattern: ["provision", "template", ":templateId"], view: "views/provision.js" },
```

with

```js
    { pattern: ["provision"], view: "views/provision.js" },
    { pattern: ["provision", "account", ":username"], view: "views/provision.js" },
    { pattern: ["provision", "template", ":templateId"], view: "views/provision.js" },
    // Legacy provision paths — same wizard (the guided flow and the
    // username-preset path were folded into it).
    { pattern: ["provision", "guided"], view: "views/provision.js" },
    { pattern: ["provision", "user", ":username"], view: "views/provision.js" },
```

(the existing `["provision", "profile", ":templateId"]` legacy line stays as is).

In `views/devices.js`, replace

```js
  // Provisioning is a full-page flow (views/provision.js); a preset username
  // (re-provision) rides the route as a path segment.
  const gotoProvision = (opts = {}) =>
    app.navigate(
      opts.presetUsername ? "/provision/user/" + encodeURIComponent(opts.presetUsername) : "/provision"
    );
```

with

```js
  // New-device provisioning is a full-page flow (views/provision.js);
  // re-provisioning an existing device is the Re-provision modal.
  const gotoProvision = () => app.navigate("/provision");
```

(The `Re-provision user` menu item that calls `gotoProvision({presetUsername})` is replaced in Task 7; until then it opens the plain wizard.)

- [ ] **Step 2: Delete the guided flow**

```bash
git rm custom_components/casa/panel/views/provision-guided.js
```

- [ ] **Step 3: Write the new `views/provision.js`**

Replace the entire file with:

```js
// Casa admin panel — provision a new device. Full-page wizard at /provision:
// ① Account — create a new account named after the device (username
//   suggested as casa-<slug>, live availability; a taken username that is a
//   Casa account offers "use this account instead"), or pick an existing
//   Casa account; the device name is required either way.
// ② Template — a saved template or "Configure manually". Per-device tweaks
//   live behind "Customize for this device" (with an optional "Also save as
//   new template"); PIN / Wi-Fi join / sign-out-others behind "Advanced".
// ③ Done — QR + setup links.
// Re-provisioning an existing device is a device action
// (views/reprovision.js), not part of this flow.
//
// Generate order is create_user → (save template) → casa.provision, each
// guarded by state so a retry never duplicates the account (createdUser) or
// the template (savedTemplateId). A new account's generated password rides
// casa.provision explicitly so a retry never rotates it; an existing account
// sends none and the server rotates it. Passwords are never shown. Decision
// logic lives in provision-logic.js (node-tested).

const STEPS = [
  { id: "account", label: "Account" },
  { id: "template", label: "Template" },
  { id: "result", label: "Done" },
];
const stepIndex = (id) => STEPS.findIndex((s) => s.id === id);

// Gating fields → the Customize section whose body re-renders when they flip.
const KEY_SECTION = { theme_color_mode: "appui", allow_all_pages: "access", wireguard_profile_id: "pushvpn" };
const CONNECTION_SET = new Set(["host_url"]);
const TIMING_SET = new Set(["expiration_hours", "cache_control_hours"]);
const ADV_CONNECTION_SET = new Set(["pin", "deauthenticate_existing"]);
const ADV_WIFI_SET = new Set(["connect_wifi_ssid", "connect_wifi_password"]);
const SEARCH_THRESHOLD = 8;

export function createView(app) {
  const { api, ui } = app;
  const esc = ui.esc;
  const unwrap = (res) => api.constructor.response(res); // CasaApi.response
  const trim = (v) => String(v ?? "").trim();

  /* ---------- lazily loaded siblings (never static imports) ---------- */
  let fieldsMod = null; // views/profile-fields.js
  let previewMod = null; // payload-preview.js
  let utilsMod = null; // views/username-utils.js
  let logicMod = null; // views/provision-logic.js
  let resultMod = null; // views/provision-result.js

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let state = null;
  let refs = null; // { tabs, body }
  let wgRequested = false;
  let availTimer = 0;
  let mountedWithPresetPath = false;

  function freshState() {
    return {
      step: "account",
      // ① account
      accountMode: "new", // "new" | "existing"
      deviceName: "",
      username: "",
      usernameEdited: false, // admin typed in the username field; stop auto-suggesting
      availability: null, // null | {checking:true} | {available, username_conflict, name_conflict, for}
      existingUsername: "",
      accountError: "",
      createdUser: null, // {name, username, password, user_id} — retry guard
      // ② template
      templates: null, // null = loading
      templatesError: null,
      search: "",
      choice: null, // template id | "manual" | null
      form: null, // full DEFAULTS-shaped values (profile + process keys)
      baseline: null, // collectFields(form, PROFILE_KEYS) at seed time
      customizeOpen: false,
      advancedOpen: false,
      saveAsTemplate: false,
      newTemplateName: "",
      savedTemplateId: null, // retry guard
      wgProfiles: null,
      deployError: "",
      busy: false,
      // ③
      result: null,
    };
  }

  const dirty = () =>
    !!(state && state.step !== "result" && (trim(state.deviceName) || state.choice || state.createdUser));

  const casaAccounts = () =>
    ((app.summary() && app.summary().accounts) || [])
      .slice()
      .sort((a, b) => String(a.name || a.username).localeCompare(String(b.name || b.username)));

  const selectedTemplate = () =>
    state.choice && state.choice !== "manual"
      ? (state.templates || []).find((t) => t && t.id === state.choice) || null
      : null;

  /* ---------- data ---------- */

  async function loadTemplates() {
    state.templates = null;
    state.templatesError = null;
    if (state.step === "template") render();
    const token = mountToken;
    try {
      const res = await api.getProvisionTemplates();
      if (token !== mountToken || !state) return;
      state.templates = (res && res.profiles) || [];
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.templates = [];
      state.templatesError = ui.errMsg(err);
    }
    if (state.step === "template") render();
  }

  function ensureWgProfiles() {
    if (wgRequested) return;
    wgRequested = true;
    const token = mountToken;
    api
      .getWireguardProfiles()
      .then((res) => {
        if (token !== mountToken || !state) return;
        state.wgProfiles = (res && res.profiles) || [];
      })
      .catch(() => {
        if (token !== mountToken || !state) return;
        state.wgProfiles = [];
      })
      .then(() => {
        if (token !== mountToken || !state) return;
        rerenderCustomizeSection("pushvpn");
      });
  }

  /* ---------- username availability (advisory — create_user is authoritative) ---------- */

  function conflictAccount() {
    const a = state.availability;
    const u = trim(state.username);
    if (!a || a.checking || a.for !== u || a.available || !a.username_conflict) return null;
    return casaAccounts().find((x) => x.username === u) || null;
  }

  function availabilityHtml() {
    const acct = conflictAccount();
    if (acct) {
      return `
        <span class="chip chip--error"><ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:14px;"></ha-icon> ${esc(acct.username)} already exists</span>
        <button class="btn btn--text" data-act="use-existing" data-username="${esc(acct.username)}" style="height:24px;">Use this account instead?</button>`;
    }
    return utilsMod ? utilsMod.availabilityHintHtml(state.availability, trim(state.username), esc) : "";
  }

  function renderAvailability() {
    const el = refs && refs.body.querySelector("#pv-availability");
    if (el) el.innerHTML = availabilityHtml();
  }

  function scheduleAvailability() {
    clearTimeout(availTimer);
    const username = trim(state.username);
    if (!username || !utilsMod || !utilsMod.USERNAME_RE.test(username)) {
      state.availability = null;
      renderAvailability();
      return;
    }
    state.availability = { checking: true };
    renderAvailability();
    const token = mountToken;
    const name = trim(state.deviceName);
    availTimer = setTimeout(async () => {
      try {
        const res = await api.checkUsername(username, name);
        if (token !== mountToken || !state || trim(state.username) !== username) return;
        state.availability = { ...res, for: username };
      } catch {
        if (token !== mountToken || !state) return;
        state.availability = null;
      }
      renderAvailability();
    }, 350);
  }

  /* ---------- tabs / shared chrome ---------- */

  function gotoStep(id) {
    state.step = id;
    render();
  }

  function renderTabs() {
    const cur = stepIndex(state.step);
    const done = state.step === "result";
    refs.tabs.innerHTML = STEPS.map((s, i) => `
      <button class="tab ${i === cur ? "tab--active" : ""} ${i < cur && !done ? "tab--done" : ""}"
        data-act="goto-step" data-step="${esc(s.id)}"
        ${i >= cur || done ? "disabled" : ""}>
        <span class="step-dot">${i < cur ? "✓" : i + 1}</span>${esc(s.label)}
      </button>`).join("");
  }

  function footer(primaryLabel, primaryAct, { back = true } = {}) {
    return `
      <div style="display:flex; justify-content:space-between; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        ${back ? `<button class="btn btn--text" data-act="back">Back</button>` : "<span></span>"}
        <button class="btn btn--primary" data-act="${esc(primaryAct)}" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Working…" : esc(primaryLabel)}
        </button>
      </div>`;
  }

  function markFieldError(field, msg) {
    const wrap = refs.body.querySelector(`[data-pv-field="${field}"]`);
    if (!wrap || wrap.classList.contains("field--error")) return;
    wrap.classList.add("field--error");
    wrap.insertAdjacentHTML("beforeend", `<div class="field__error">${esc(msg)}</div>`);
  }

  /* ---------- step 1: account ---------- */

  function modeCard(mode, title, desc) {
    const active = state.accountMode === mode;
    return `
      <button class="option-card" data-act="mode" data-mode="${esc(mode)}" style="flex:1; min-width:240px;
        ${active ? "border-color:var(--casa-primary); background:color-mix(in srgb, var(--casa-primary) 6%, transparent);" : ""}">
        <ha-icon icon="${active ? "mdi:radiobox-marked" : "mdi:radiobox-blank"}" style="color:${active ? "var(--casa-primary)" : "var(--casa-text-2)"};"></ha-icon>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
        </span>
      </button>`;
  }

  function deviceNameField() {
    return `
      <div class="field" data-pv-field="deviceName">
        <label>Device name *</label>
        <input class="input" data-pv="deviceName" value="${esc(state.deviceName)}" maxlength="60"
          placeholder="e.g. Kitchen iPad" autocomplete="off">
        <div class="field__help">${state.accountMode === "new"
          ? "Shown in the device list, and used as the account's name."
          : "Shown in the device list — applied automatically when the device first connects."}</div>
      </div>`;
  }

  function renderAccountStep() {
    if (state.createdUser) {
      const u = state.createdUser;
      return `
        <div style="display:flex; gap:8px; align-items:center; margin:0 0 14px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
          <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
          <span>Account <strong class="mono">${esc(u.username)}</strong> was already created for this run — continue to retry, or leave to keep it (remove it from Accounts if unwanted).</span>
        </div>
        <div class="field"><label>Device name</label><input class="input" value="${esc(state.deviceName)}" disabled></div>
        ${footer("Continue", "to-template", { back: false })}`;
    }
    const accounts = casaAccounts();
    const newFields = `
      ${deviceNameField()}
      <div class="field" data-pv-field="username">
        <label>Username *</label>
        <input class="input mono" data-pv="username" value="${esc(state.username)}"
          placeholder="e.g. casa-kitchen-ipad" autocapitalize="none" autocomplete="off" spellcheck="false">
        <div id="pv-availability" style="min-height:20px; margin-top:6px;">${availabilityHtml()}</div>
        <div class="field__help">The account this device signs in with — suggested from the name, edit if you like.</div>
      </div>`;
    const existingFields = accounts.length
      ? `
        <div class="field" data-pv-field="existingUsername">
          <label>Account *</label>
          <select class="select" data-pv="existingUsername" style="width:100%;">
            <option value="" ${state.existingUsername ? "" : "selected"} disabled>Choose an account…</option>
            ${accounts.map((a) => `<option value="${esc(a.username)}" ${a.username === state.existingUsername ? "selected" : ""}>${esc(a.name || a.username)} (${esc(a.username)}) · ${Number(a.device_count) || 0} device${Number(a.device_count) === 1 ? "" : "s"}</option>`).join("")}
          </select>
          <div class="field__help">Several devices can share an account; other devices on it stay signed in.</div>
        </div>
        ${deviceNameField()}`
      : `<div class="empty-state" style="padding:24px 16px;"><div>No Casa accounts yet</div>
           <button class="btn btn--text" data-act="mode" data-mode="new">Create one instead</button></div>`;
    return `
      ${state.accountError ? `<div class="errbar">${esc(state.accountError)}</div>` : ""}
      <div style="display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px;">
        ${modeCard("new", "Create new account", "A fresh account for this device")}
        ${modeCard("existing", "Use existing account", "Sign this device in as an account you already have")}
      </div>
      ${state.accountMode === "new" ? newFields : existingFields}
      ${footer("Continue", "to-template", { back: false })}`;
  }

  function toTemplateStep() {
    state.accountError = "";
    if (state.createdUser) return gotoStep("template");
    const name = trim(state.deviceName);
    const errs = [];
    if (state.accountMode === "new") {
      const username = trim(state.username);
      if (!name) errs.push(["deviceName", "Required."]);
      if (!username) errs.push(["username", "Required."]);
      else if (!utilsMod.USERNAME_RE.test(username)) errs.push(["username", "Lowercase letters, numbers and dashes only."]);
      const a = state.availability;
      if (username && a && !a.checking && a.for === username && !a.available) {
        errs.push(a.username_conflict ? ["username", "Already in use."] : ["deviceName", `A user named '${name}' already exists.`]);
      }
    } else {
      if (!casaAccounts().some((x) => x.username === state.existingUsername)) errs.push(["existingUsername", "Choose an account."]);
      if (!name) errs.push(["deviceName", "Required."]);
    }
    if (errs.length) {
      state.accountError = "Fix the highlighted fields to continue.";
      render();
      for (const [field, msg] of errs) markFieldError(field, msg);
      return;
    }
    gotoStep("template");
  }

  /* ---------- step 2: template ---------- */

  function seedForm(template) {
    const F = fieldsMod;
    const form = { ...F.DEFAULTS };
    const f = (template && template.fields) || {};
    for (const key of Object.keys(f)) {
      if (F.PROFILE_KEYS.has(key)) form[key] = f[key];
    }
    if (!trim(form.host_url)) form.host_url = window.location.origin;
    // Advanced (process) inputs survive a template switch.
    if (state.form) for (const key of F.PROCESS_KEYS) form[key] = state.form[key];
    return form;
  }

  function applyChoice(choice) {
    state.choice = choice;
    state.form = seedForm(selectedTemplate());
    state.baseline = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
    state.customizeOpen = choice === "manual" || state.customizeOpen;
    state.deployError = "";
    render();
  }

  function isCustomized() {
    if (!state.form || !state.baseline) return false;
    const fields = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
    return logicMod.changedKeys(fields, state.baseline).length > 0;
  }

  function requestChoice(choice) {
    if (choice === state.choice) return;
    if (!isCustomized()) return applyChoice(choice);
    ui.showConfirm({
      title: "Discard customizations?",
      message: "Switching discards the changes you made under 'Customize for this device'.",
      confirmLabel: "Switch",
      confirmDanger: false,
      onConfirm: () => applyChoice(choice),
    });
  }

  function choiceRow({ choice, title, chipsHtml, desc }) {
    const active = state.choice === choice;
    return `
      <button class="option-card" data-act="choose" data-choice="${esc(choice)}" style="width:100%; margin-bottom:8px;
        ${active ? "border-color:var(--casa-primary); background:color-mix(in srgb, var(--casa-primary) 6%, transparent);" : ""}">
        <ha-icon icon="${active ? "mdi:radiobox-marked" : "mdi:radiobox-blank"}" style="color:${active ? "var(--casa-primary)" : "var(--casa-text-2)"};"></ha-icon>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          ${chipsHtml ? `<span style="display:flex; flex-wrap:wrap; gap:4px; margin-top:4px;">${chipsHtml}</span>` : ""}
          ${desc ? `<span class="option-card__desc" style="display:block;">${esc(desc)}</span>` : ""}
        </span>
      </button>`;
  }

  function templateListHtml() {
    if (state.templates === null) {
      return `<div class="empty-state" style="padding:24px 16px;"><span class="muted">Loading templates…</span></div>`;
    }
    const errHtml = state.templatesError ? `
      <div class="errbar" style="display:flex; align-items:center; gap:10px;">
        <span style="flex:1;">Failed to load templates: ${esc(state.templatesError)}</span>
        <button class="btn btn--outlined" data-act="retry-templates" style="height:28px; flex:none;">Retry</button>
      </div>` : "";
    const q = state.search.trim().toLowerCase();
    const rows = state.templates
      .filter((p) => !q || String(p.name || "").toLowerCase().includes(q))
      .map((p) => {
        const chips = (previewMod ? previewMod.profileChips(p) : [])
          .map((c) => `<span class="chip ${esc(c.cls || "chip--neutral")}">${esc(c.label)}</span>`)
          .join("");
        return choiceRow({ choice: p.id, title: p.name || "(unnamed)", chipsHtml: chips });
      })
      .join("");
    const search = state.templates.length > SEARCH_THRESHOLD ? `
      <div class="list-toolbar"><div class="search-field">
        <ha-icon icon="mdi:magnify"></ha-icon>
        <input class="input" id="pv-search" type="search" placeholder="Search templates…" value="${esc(state.search)}">
      </div></div>` : "";
    return `${errHtml}${search}${rows}
      ${choiceRow({ choice: "manual", title: "Configure manually", desc: "Set every option yourself — optionally save it as a new template" })}`;
  }

  function customizeSectionDefs() {
    const F = fieldsMod;
    const opts = (extra) => ({ esc, heading: false, wgProfiles: state.wgProfiles || [], ...extra });
    return [
      { id: "connection", label: "Connection", render: () => F.renderSectionHtml("connection", state.form, opts({ fields: CONNECTION_SET })) },
      { id: "appui", label: "App UI", render: () => F.renderSectionHtml("appui", state.form, opts({ fields: F.LIVE_KEYS })) },
      { id: "access", label: "Access Control", render: () => F.renderSectionHtml("access", state.form, opts({ fields: F.LIVE_KEYS })) },
      { id: "pushvpn", label: "Push & VPN", render: () => F.renderSectionHtml("pushvpn", state.form, opts({ fields: F.LIVE_KEYS })) },
      { id: "timing", label: "Timing & Security", render: () => F.renderSectionHtml("timing", state.form, opts({ fields: TIMING_SET })) },
    ];
  }

  function rerenderCustomizeSection(id) {
    if (!state || state.step !== "template" || !state.form || !fieldsMod) return;
    const def = customizeSectionDefs().find((s) => s.id === id);
    const body = refs.body.querySelector(`[data-cz-section="${id}"]`);
    if (def && body) body.innerHTML = def.render();
  }

  function expander(act, open, label) {
    return `
      <button class="btn btn--text" data-act="${esc(act)}" style="margin:6px 0; padding-left:0;">
        <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${open ? "180deg" : "0deg"});"></ha-icon>
        ${esc(label)}
      </button>`;
  }

  function customizeHtml() {
    const sections = customizeSectionDefs()
      .map((s) => `<h5 style="margin:12px 0 6px;">${esc(s.label)}</h5><div data-cz-section="${esc(s.id)}">${s.render()}</div>`)
      .join("");
    return `
      <div id="pv-customize">${sections}</div>
      <div style="border-top:1px solid var(--casa-divider); margin-top:12px; padding-top:12px;">
        <label class="toggle">
          <input type="checkbox" data-pv="saveAsTemplate" ${state.saveAsTemplate ? "checked" : ""}>
          Also save as new template
        </label>
        ${state.saveAsTemplate ? `
          <div class="field" data-pv-field="newTemplateName" style="margin-top:8px;">
            <label>Template name *</label>
            <input class="input" data-pv="newTemplateName" value="${esc(state.newTemplateName)}" placeholder="e.g. Kitchen tablets">
          </div>` : ""}
      </div>`;
  }

  function advancedHtml() {
    const F = fieldsMod;
    return `
      <div id="pv-advanced">
        ${F.renderSectionHtml("connection", state.form, { esc, heading: false, fields: ADV_CONNECTION_SET })}
        ${F.renderSectionHtml("wifi", state.form, { esc, heading: false, fields: ADV_WIFI_SET })}
      </div>`;
  }

  function accountSummaryChip() {
    const who = state.accountMode === "new" ? (state.createdUser ? state.createdUser.username : trim(state.username)) : state.existingUsername;
    return `
      <div class="card" style="margin:0 0 16px;"><div class="card__body" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
        <ha-icon icon="mdi:cellphone" style="color:var(--casa-text-2); flex:none;"></ha-icon>
        <span style="font-size:14px;"><strong>${esc(trim(state.deviceName))}</strong> <span class="muted">→ ${esc(who)}</span></span>
        <span class="spacer"></span>
        <span class="chip ${state.accountMode === "new" ? "chip--app" : "chip--neutral"}">${state.accountMode === "new" ? "New account" : "Existing account"}</span>
      </div></div>`;
  }

  function renderTemplateStep() {
    const ready = !!(state.form && state.choice);
    return `
      ${accountSummaryChip()}
      ${state.deployError ? `<div class="errbar">${esc(state.deployError)}</div>` : ""}
      <h4 style="margin:0 0 10px; font-size:14px; font-weight:600;">Choose a template</h4>
      <div id="pv-templates">${templateListHtml()}</div>
      ${ready ? `
        <div style="border-top:1px solid var(--casa-divider); margin-top:8px;">
          ${expander("toggle-customize", state.customizeOpen, "Customize for this device")}
          <div ${state.customizeOpen ? "" : "hidden"}>${customizeHtml()}</div>
        </div>
        <div style="border-top:1px solid var(--casa-divider);">
          ${expander("toggle-advanced", state.advancedOpen, "Advanced (PIN, Wi-Fi join, sign out other devices)")}
          <div ${state.advancedOpen ? "" : "hidden"}>${advancedHtml()}</div>
        </div>` : ""}
      ${footer("Generate setup", "generate")}`;
  }

  function rerenderTemplateList() {
    const el = refs && refs.body.querySelector("#pv-templates");
    if (el) el.innerHTML = templateListHtml();
  }

  /* ---------- generate: create user → save template → provision ---------- */

  async function generate() {
    state.deployError = "";
    const F = fieldsMod;
    if (!state.choice || !state.form) {
      state.deployError = "Pick a template, or Configure manually.";
      return render();
    }
    if (!trim(state.form.host_url)) {
      state.deployError = "Host URL is required (under Customize for this device → Connection).";
      state.customizeOpen = true;
      return render();
    }
    if (state.saveAsTemplate && !trim(state.newTemplateName)) {
      state.deployError = "Name the new template, or untick 'Also save as new template'.";
      state.customizeOpen = true;
      render();
      return markFieldError("newTemplateName", "Required.");
    }

    const fields = F.collectFields(state.form, F.PROFILE_KEYS);
    const changed = logicMod.changedKeys(fields, state.baseline);
    const template = selectedTemplate();
    const token = mountToken;
    state.busy = true;
    render();

    // 1. Account.
    let account;
    if (state.accountMode === "new") {
      if (!state.createdUser) {
        const name = trim(state.deviceName);
        const username = trim(state.username);
        let resp;
        try {
          resp = unwrap(await api.createUser({ name, username, localOnly: true }));
        } catch (err) {
          resp = { error: ui.errMsg(err) };
        }
        if (token !== mountToken || !state) return;
        if (resp && resp.error) {
          state.busy = false;
          state.step = "account";
          state.accountError = String(resp.error);
          state.availability = null;
          return render();
        }
        state.createdUser = { name, username, password: resp && resp.password, user_id: resp && resp.user_id };
      }
      account = state.createdUser;
    } else {
      const a = casaAccounts().find((x) => x.username === state.existingUsername);
      if (!a) {
        state.busy = false;
        state.step = "account";
        state.accountError = "That account no longer exists — choose another.";
        return render();
      }
      account = { username: a.username, user_id: a.user_id };
    }

    // 2. Optional new template (skipped on retry via savedTemplateId).
    if (state.saveAsTemplate && !state.savedTemplateId) {
      try {
        const body = {
          name: trim(state.newTemplateName),
          fields: logicMod.templateFieldsToSave({
            fields,
            baseSetKeys: template ? Object.keys(template.fields || {}) : [],
            changed,
          }),
        };
        const res = await api.saveProvisionTemplate(body);
        if (token !== mountToken || !state) return;
        if (res && res.id) state.savedTemplateId = res.id;
        ui.toast(`Template '${trim(state.newTemplateName)}' created.`);
      } catch (err) {
        if (token !== mountToken || !state) return;
        state.busy = false;
        state.deployError = "Failed to save template: " + ui.errMsg(err);
        return render();
      }
    }

    // 3. Provision.
    const data = logicMod.buildProvisionRequest({
      fields,
      form: state.form,
      account,
      deviceName: trim(state.deviceName),
      lineage: logicMod.lineageFor({
        templateId: template && template.id,
        customized: changed.length > 0,
        savedTemplateId: state.savedTemplateId,
      }),
    });
    try {
      const resp = unwrap(await api.provision(data));
      if (token !== mountToken || !state) return;
      state.busy = false;
      if (resp && resp.error) {
        state.deployError = String(resp.error);
        return render();
      }
      state.result = resp;
      gotoStep("result");
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.busy = false;
      state.deployError = ui.errMsg(err);
      render();
    }
  }

  /* ---------- step 3: done ---------- */

  function renderResultStep() {
    const r = state.result || {};
    return `
      <div style="text-align:center; margin-bottom:16px;">
        <ha-icon icon="mdi:check-circle" style="--mdc-icon-size:48px; color:var(--casa-success);"></ha-icon>
        <h3 style="margin:8px 0 0; font-size:16px; font-weight:600;">${esc(trim(state.deviceName))} is ready to set up</h3>
      </div>
      ${resultMod.setupResultHtml(r, { esc, fmtExpiry: ui.fmtExpiry })}
      <div style="display:flex; gap:8px; align-items:center; margin-top:14px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
        <ha-icon icon="mdi:tag-outline" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
        <span>The device will be named <strong>${esc(trim(state.deviceName))}</strong> automatically when it connects (within 30 minutes).</span>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--outlined" data-act="another">Provision another</button>
        <button class="btn btn--primary" data-act="done">Done</button>
      </div>`;
  }

  /* ---------- render + events ---------- */

  function render() {
    if (!refs || !state) return;
    renderTabs();
    if (!fieldsMod || !utilsMod || !logicMod || !resultMod) {
      refs.body.innerHTML = `<div class="empty-state" style="padding:32px 16px;"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (state.step === "account") refs.body.innerHTML = renderAccountStep();
    else if (state.step === "template") refs.body.innerHTML = renderTemplateStep();
    else refs.body.innerHTML = renderResultStep();

    if (state.step === "result") resultMod.bindCopyButtons(refs.body, ui);
    if (state.step === "template" && state.form) {
      const bind = (el) =>
        el && fieldsMod.bindFieldEvents(el, {
          values: state.form,
          onSectionRerender: (key) => rerenderCustomizeSection(KEY_SECTION[key]),
          esc,
        });
      bind(refs.body.querySelector("#pv-customize"));
      bind(refs.body.querySelector("#pv-advanced"));
      if (state.customizeOpen) ensureWgProfiles();
    }
  }

  function onBodyClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled || !(refs.body.contains(el) || refs.tabs.contains(el))) return;
    switch (el.dataset.act) {
      case "goto-step":
        if (stepIndex(el.dataset.step) < stepIndex(state.step) && state.step !== "result") gotoStep(el.dataset.step);
        return;
      case "back":
        if (state.step === "template") gotoStep("account");
        return;
      case "mode":
        state.accountMode = el.dataset.mode;
        state.accountError = "";
        render();
        return;
      case "use-existing":
        state.accountMode = "existing";
        state.existingUsername = el.dataset.username;
        state.accountError = "";
        render();
        return;
      case "to-template":
        toTemplateStep();
        return;
      case "retry-templates":
        loadTemplates();
        return;
      case "choose":
        requestChoice(el.dataset.choice);
        return;
      case "toggle-customize":
        state.customizeOpen = !state.customizeOpen;
        render();
        return;
      case "toggle-advanced":
        state.advancedOpen = !state.advancedOpen;
        render();
        return;
      case "generate":
        generate();
        return;
      case "another":
        state = freshState();
        wgRequested = false;
        loadTemplates();
        render();
        if (mountedWithPresetPath) app.navigate("/provision", { replace: true });
        return;
      case "done":
        app.refresh();
        app.navigate("/");
        return;
    }
  }

  // View-private inputs carry data-pv (shared-renderer fields use data-key and
  // are handled by fieldsMod.bindFieldEvents). Typing never re-renders the
  // whole step, so focus is kept.
  function onBodyInput(e) {
    const t = e.target;
    if (t.id === "pv-search") {
      state.search = t.value;
      rerenderTemplateList();
      return;
    }
    switch (t.dataset && t.dataset.pv) {
      case "deviceName":
        state.deviceName = t.value;
        if (state.accountMode === "new" && !state.usernameEdited) {
          state.username = logicMod.suggestUsername(t.value, utilsMod.slugify);
          const u = refs.body.querySelector('[data-pv="username"]');
          if (u) u.value = state.username;
        }
        if (state.accountMode === "new") scheduleAvailability();
        return;
      case "username": {
        const lower = t.value.toLowerCase();
        if (lower !== t.value) t.value = lower;
        state.username = lower;
        // Clearing the field re-couples it to the device name.
        state.usernameEdited = !!lower;
        scheduleAvailability();
        return;
      }
      case "newTemplateName":
        state.newTemplateName = t.value;
        return;
    }
  }

  function onBodyChange(e) {
    const t = e.target;
    switch (t.dataset && t.dataset.pv) {
      case "existingUsername":
        state.existingUsername = t.value;
        return;
      case "saveAsTemplate":
        state.saveAsTemplate = !!t.checked;
        if (state.saveAsTemplate && !trim(state.newTemplateName)) {
          const base = selectedTemplate();
          state.newTemplateName = base ? `${base.name} (copy)` : "";
        }
        render();
        return;
    }
  }

  function onBeforeUnload(e) {
    if (!dirty()) return;
    e.preventDefault();
    e.returnValue = "";
  }

  /* ---------- view ---------- */

  return {
    id: "provision",
    header: () => ({ title: "Provision device", back: "/" }),
    polling: "paused",

    async mount(el, params) {
      const token = ++mountToken;
      state = freshState();
      wgRequested = false;
      const presetUsername = (params && params.username) || "";
      const presetTemplateId = (params && params.templateId) || "";
      mountedWithPresetPath = !!(presetUsername || presetTemplateId);

      el.innerHTML = `
        <div class="page">
          <div class="tabs tabs--steps" id="pv-tabs"></div>
          <div id="pv-body"></div>
        </div>`;
      refs = { tabs: el.querySelector("#pv-tabs"), body: el.querySelector("#pv-body") };
      refs.tabs.addEventListener("click", onBodyClick); // step tabs live outside the body
      refs.body.addEventListener("click", onBodyClick);
      refs.body.addEventListener("input", onBodyInput);
      refs.body.addEventListener("change", onBodyChange);
      window.addEventListener("beforeunload", onBeforeUnload);
      render();

      try {
        const [fields, preview, utils, logic, result] = await Promise.all([
          fieldsMod || app.loadModule("views/profile-fields.js"),
          previewMod || app.loadModule("payload-preview.js"),
          utilsMod || app.loadModule("views/username-utils.js"),
          logicMod || app.loadModule("views/provision-logic.js"),
          resultMod || app.loadModule("views/provision-result.js"),
        ]);
        if (token !== mountToken) return;
        fieldsMod = fields;
        previewMod = preview;
        utilsMod = utils;
        logicMod = logic;
        resultMod = result;
      } catch (err) {
        if (token !== mountToken) return;
        refs.body.innerHTML = `<div class="errbar">Failed to load: ${esc(ui.errMsg(err))}</div>`;
        return;
      }

      // Deep links can land before the first summary poll.
      if (presetUsername && !app.summary()) await app.refresh();
      if (token !== mountToken || !state) return;
      if (presetUsername) {
        if (casaAccounts().some((a) => a.username === presetUsername)) {
          state.accountMode = "existing";
          state.existingUsername = presetUsername;
        } else {
          ui.toast("That account no longer exists.", { error: true });
        }
      }
      render();

      await loadTemplates();
      if (token !== mountToken || !state) return;
      if (presetTemplateId) {
        if ((state.templates || []).some((t) => t && t.id === presetTemplateId)) {
          state.choice = presetTemplateId;
          state.form = seedForm(selectedTemplate());
          state.baseline = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
        } else {
          ui.toast("That provision template no longer exists.", { error: true });
        }
      }
      render();
    },

    unmount() {
      mountToken++;
      clearTimeout(availTimer);
      window.removeEventListener("beforeunload", onBeforeUnload);
      refs = null;
      state = null;
    },

    // ui.showConfirm has no cancel callback, so build the dialog with openModal
    // and resolve false on any dismissal (X, overlay click, Escape) by watching
    // for the overlay leaving the DOM.
    confirmLeave() {
      if (!dirty()) return true;
      return new Promise((resolve) => {
        let settled = false;
        let observer = null;
        const done = (v) => {
          if (settled) return;
          settled = true;
          observer?.disconnect();
          resolve(v);
        };
        const modal = ui.openModal({
          title: "Leave provisioning?",
          bodyHtml:
            '<p style="margin:0; font-size:14px; line-height:1.5;">This device hasn\'t been provisioned yet. Discard this setup and leave?</p>',
          buttons: [
            { label: "Keep going", variant: "text", onClick: () => done(false) },
            { label: "Discard", variant: "danger", onClick: () => done(true) },
          ],
        });
        observer = new MutationObserver(() => {
          if (!modal.el.isConnected) done(false);
        });
        observer.observe(modal.el.parentNode, { childList: true });
      });
    },
  };
}
```

- [ ] **Step 4: Syntax-check and test**

Run: `node --check custom_components/casa/panel/views/provision.js && node --check custom_components/casa/panel/app.js && node --check custom_components/casa/panel/views/devices.js`
Expected: no output.
Run: `node --test tests/js/ && python3 -m pytest -q tests` → all pass.
Run: `grep -rn "provision-guided" custom_components/casa/panel` → only the comment in `views/username-utils.js` (fixed in Task 7).

- [ ] **Step 5: Commit**

```bash
git add -A custom_components/casa/panel/views/provision.js custom_components/casa/panel/views/provision-guided.js \
  custom_components/casa/panel/app.js custom_components/casa/panel/views/devices.js
git commit -m "feat(panel): one 3-step provision wizard with existing-account support; drop guided flow and scenario popup"
```

---

### Task 7: Re-provision modal; remove Reauthenticate

**Files:**
- Create: `custom_components/casa/panel/views/reprovision.js`
- Modify: `custom_components/casa/panel/views/devices.js:397-412` (row ⋮ menu)
- Modify: `custom_components/casa/panel/views/device-editor.js` — delete `openReauthModal` (from the `// openReauthModal(app, device)` comment ≈line 158 through its closing `}` just before `/* ---------- view ---------- */` ≈line 409); add a Re-provision card to `renderOverview` (≈line 721)
- Modify: `custom_components/casa/panel/api.js` — delete `reauthDevice`
- Modify: `custom_components/casa/panel/views/username-utils.js:1-6,26` (comments)

**Interfaces:**
- Consumes: `api.reprovisionDevice` (Task 5), `provision-result.js` (Task 5), summary device fields `device_id`, `alias`, `username`, `push_registered`, `native`.
- Produces: `openReprovisionModal(app, device) -> Promise<void>` (export of `views/reprovision.js`).

- [ ] **Step 1: Create `views/reprovision.js`**

```js
// Casa admin panel — one-click "Re-provision" for an existing device, always
// on its current account (CasaAdminReprovisionDeviceView). Push-registered
// devices get a fresh login over encrypted push (their old session is
// revoked once they sign back in); others — or "Show QR instead" for a
// wiped/replaced phone — get a new QR/link and are signed out immediately.
// Other devices on the account are never touched; passwords are never shown.
// Opened from the device list's row menu and the device page's Overview.

export async function openReprovisionModal(app, device) {
  const { api, ui } = app;
  const esc = ui.esc;
  const label = device.alias || device.device_id;
  const account = device.username || "this account";
  let forceQr = !device.push_registered;

  const body = document.createElement("div");
  const draw = () => {
    const text = forceQr
      ? `This signs <strong>${esc(label)}</strong> out now and gives you a new QR code / link to set it up again.`
      : `Sends a new login to <strong>${esc(label)}</strong> over push.`;
    body.innerHTML = `
      <p style="margin:0 0 8px; font-size:14px; line-height:1.5;">${text}</p>
      <p class="muted" style="margin:0 0 8px; font-size:13px;">Other devices on <span class="mono">${esc(account)}</span> stay signed in.</p>
      ${device.push_registered && !forceQr
        ? `<button class="btn btn--text" data-act="force-qr" style="padding-left:0;">Phone wiped or replaced? Show QR instead</button>`
        : ""}
      <div class="field__error" data-err hidden></div>`;
  };
  draw();
  body.addEventListener("click", (e) => {
    if (e.target.closest('[data-act="force-qr"]')) {
      forceQr = true;
      draw();
    }
  });

  ui.openModal({
    title: `Re-provision ${label}?`,
    bodyEl: body,
    buttons: [
      { label: "Cancel", variant: "text" },
      {
        label: "Re-provision",
        variant: "primary",
        onClick: async (btn) => {
          btn.disabled = true;
          btn.textContent = "Working…";
          try {
            const res = await api.reprovisionDevice({
              device_id: device.device_id,
              method: forceQr ? "qr" : "auto",
              host_url: window.location.origin,
            });
            await showResult(app, label, res || {});
            app.refresh();
            return undefined; // close the confirm
          } catch (err) {
            const errEl = body.querySelector("[data-err]");
            errEl.hidden = false;
            errEl.textContent = "Failed: " + ((err && err.body && err.body.error) || ui.errMsg(err));
            btn.disabled = false;
            btn.textContent = "Re-provision";
            return false;
          }
        },
      },
    ],
  });
}

async function showResult(app, label, res) {
  const { ui } = app;
  if (res.method === "push") {
    ui.showInfo({
      title: "Re-provision sent",
      message: `${label} will sign back in when it gets it (queued if it's offline). Track it under the device's Pending Updates.`,
    });
    return;
  }
  const resultMod = await app.loadModule("views/provision-result.js");
  const body = document.createElement("div");
  body.innerHTML = resultMod.setupResultHtml(res, { esc: ui.esc, fmtExpiry: ui.fmtExpiry });
  resultMod.bindCopyButtons(body, ui);
  ui.openModal({
    title: `Set up ${label} again`,
    bodyEl: body,
    buttons: [{ label: "Done", variant: "primary" }],
  });
}
```

- [ ] **Step 2: Device list menu**

In `views/devices.js`, replace these two menu items:

```js
        { icon: "mdi:qrcode", label: "Re-provision user", onSelect: () => gotoProvision({ presetUsername: d.username }) },
        {
          icon: "mdi:account-key",
          label: "Reauthenticate…",
          onSelect: async () => {
            const mod = await app.loadModule("views/device-editor.js");
            mod.openReauthModal(app, d);
          },
        },
```

with

```js
        ...(d.native
          ? []
          : [{
              icon: "mdi:qrcode",
              label: "Re-provision",
              onSelect: async () => {
                const mod = await app.loadModule("views/reprovision.js");
                mod.openReprovisionModal(app, d);
              },
            }]),
```

- [ ] **Step 3: Device page Overview card**

In `views/device-editor.js` `renderOverview`, after the closing `</div></div>` of the "Device alias" card and before the closing backtick, add:

```js
      ${d.native ? "" : `
      <div class="card section-card"><div class="card__body">
        <h5>Re-provision</h5>
        <p class="muted" style="margin:0 0 10px; font-size:13px;">Give this device a fresh login on its account — over push when possible, otherwise a new QR code. Other devices on the account stay signed in.</p>
        <button class="btn btn--outlined" id="dev-reprovision"><ha-icon icon="mdi:qrcode"></ha-icon> Re-provision</button>
      </div></div>`}
```

and after `shell.formEl.querySelector("#dev-save-alias").addEventListener("click", saveAlias);` add:

```js
    shell.formEl.querySelector("#dev-reprovision")?.addEventListener("click", async () => {
      const mod = await app.loadModule("views/reprovision.js");
      mod.openReprovisionModal(app, device);
    });
```

- [ ] **Step 4: Remove Reauthenticate**

- In `views/device-editor.js`, delete everything from the line `// openReauthModal(app, device) — reauthenticate a device with a new` through the `}` that closes `export async function openReauthModal`, i.e. up to (not including) the blank line before `/* ---------- view ---------- */`.
- In `api.js`, delete the `// Reauthenticate a device with a new username/password` comment block and the `reauthDevice(body) { ... }` method.
- In `views/username-utils.js`, change the header comment lines 3–5 to:

```js
// /api/casa/admin/check_username. Shared by the provision wizard and the
// Create Account modal so they derive usernames and render availability the
// same way. Loaded lazily via
```

  and change line 26's comment to `// Availability hint markup shared with the provision wizard's availabilityHtml().`
- Also update the Accounts view comment at `views/accounts.js:113-114` from "(same module the guided wizard and Reauthenticate modal use)" to "(same module the provision wizard uses)".

- [ ] **Step 5: Verify**

Run: `grep -rn -e openReauthModal -e reauthDevice -e "Reauthenticate" -e "guided wizard" custom_components/casa/panel` → no matches.
Run: `for f in views/reprovision.js views/devices.js views/device-editor.js api.js views/username-utils.js views/accounts.js; do node --check custom_components/casa/panel/$f || echo FAIL $f; done` → no output.
Run: `node --test tests/js/ && python3 -m pytest -q tests` → all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/casa/panel
git commit -m "feat(panel): one-click Re-provision from the device menu and page; remove Reauthenticate"
```

---

### Task 8: Accounts entry points

**Files:**
- Modify: `custom_components/casa/panel/views/accounts.js` — `showCredentials` (≈line 78–107), its call in `openCreateModal` (≈line 185), `openAccountMenu` (≈line 246)

**Interfaces:**
- Consumes: route `/provision/account/:username` (Task 6).

- [ ] **Step 1: Row menu**

In `openAccountMenu`, make the first item:

```js
        { icon: "mdi:qrcode", label: "Provision device", onSelect: () => app.navigate("/provision/account/" + encodeURIComponent(a.username)) },
```

- [ ] **Step 2: After Create account**

Change the `showCredentials` signature to `function showCredentials({ modalTitle, heading, name, username, password, offerProvision = false })` and replace its `buttons:` line with:

```js
      buttons: [
        ...(offerProvision
          ? [{
              label: "Provision a device now",
              variant: "outlined",
              onClick: () => {
                app.refresh();
                app.navigate("/provision/account/" + encodeURIComponent(username));
              },
            }]
          : []),
        { label: "Done", variant: "primary", onClick: () => { app.refresh(); } },
      ],
```

In `openCreateModal`'s success path, add `offerProvision: true,` to the `showCredentials({ ... })` call (the Reset-password call stays without it).

- [ ] **Step 3: Verify**

Run: `node --check custom_components/casa/panel/views/accounts.js` → no output.

- [ ] **Step 4: Commit**

```bash
git add custom_components/casa/panel/views/accounts.js
git commit -m "feat(panel): provision a device straight from an account"
```

---

### Task 9: Version bump, README, full verification

**Files:**
- Modify: `custom_components/casa/const.py:7`, `custom_components/casa/panel/version.js:6`, `custom_components/casa/manifest.json` (`"version"`)
- Modify: `README.md:28-30`

- [ ] **Step 1: Bump versions to 26.10.02**

```bash
sed -i '' 's/^CASA_VERSION = "26.10.01"/CASA_VERSION = "26.10.02"/' custom_components/casa/const.py
sed -i '' 's/PANEL_VERSION = "26.10.01"/PANEL_VERSION = "26.10.02"/' custom_components/casa/panel/version.js
sed -i '' 's/"version": "26.10.01"/"version": "26.10.02"/' custom_components/casa/manifest.json
python3 -m pytest -q tests/test_versions.py
```

Expected: 2 passed.

- [ ] **Step 2: README**

In `README.md`, in line 28 change `re-provision, delete record, deprovision` to `re-provision (one click: new login over push, or a fresh QR for a wiped/replaced phone), delete record, deprovision` and append to the Accounts sentence: ` Each account's menu can provision a new device straight onto it.`

Replace lines 29–30 (the **Guided provisioning** and **Provision wizard** bullets) with:

```markdown
- **Provision wizard** ("+ Provision device") — three steps. **Account:** create a new account named after the device (username suggested as `casa-<name>`, live availability check; a taken Casa username offers "use this account instead") or pick an existing Casa account — accounts can be shared by several devices. **Template:** pick a saved template or *Configure manually*; per-device tweaks sit under *Customize for this device* (optionally *Also save as new template*), and PIN / Wi-Fi join / sign out other devices under *Advanced*. **Done:** QR code plus universal and `hascasa://` deep links with copy buttons. Passwords are generated server-side and never shown. BLE beacons, window length, password scramble and QR files remain available on the `casa.provision` service (as does `method: manual`).
```

- [ ] **Step 3: Full test run**

Run: `python3 -m pytest -q tests && node --test tests/js/`
Expected: 144 passed; 8 JS tests passed.

- [ ] **Step 4: Commit**

```bash
git add custom_components/casa/const.py custom_components/casa/panel/version.js custom_components/casa/manifest.json README.md
git commit -m "chore: 26.10.02 — provisioning cleanup"
```

- [ ] **Step 5: Manual verification on Fairwater (ask the user before deploying)**

Deploying restarts HA Core on 192.168.1.21 — **ask the user first**, then run `CASA_DEPLOY_PASS=… ./deploy.sh` (the user supplies the password; never write it to a file). After HA is back, at `http://192.168.1.21:8123/casa`:

1. Devices → "+ Provision device" → *Use existing account* → Mobile Bryce → device name "Test Phone" → *Mobile Owners* → Generate → QR + both links shown, no password anywhere.
2. *Create new account* → type "Kitchen iPad" → username shows `casa-kitchen-ipad` + "Available". Type a name whose suggestion collides with an existing Casa username → "Use this account instead?" switches to *Use existing* with it selected.
3. *Configure manually* → Customize auto-opens → tick *Also save as new template* → name it → Generate → Templates tab lists it.
4. Accounts row ⋮ → *Provision device* opens the wizard with that account selected; Create account → "Provision a device now" does the same; Templates row ⋮ → *Provision with this template* preselects it.
5. Device row ⋮ → *Re-provision* on a push-registered device → "Re-provision sent"; the device signs back in; Sessions shows its old token gone and other sessions on the account intact.
6. *Re-provision* → "Show QR instead" → QR modal; the device is signed out immediately. Redeem the QR on a different phone/simulator → after it registers, the old device record is gone and the new one has the old alias.
7. Native devices show no Re-provision in the row menu or Overview.
8. `/casa/provision/guided` and `/casa/provision/user/mobile-bryce` still open the wizard (the latter with Mobile Bryce selected).

---

### Task 10: Name HA devices after their alias (user-requested addition, 2026-10-02)

Shared accounts make every device on an account show up in Home Assistant as "Casa Device (<username>)", with entity ids suffixed `_2`, `_3`. Name the HA device-registry entry after the device's alias instead (fallback unchanged), and rename it when the alias changes. HA's `device_registry.async_get_or_create` overwrites `name` on every call (verified in HA 2025.1 source), and the entity platform calls it with each entity's `device_info` on every entity add — so **every** registry write and **every** entity `device_info` must compute the name the same way. Existing entity ids are not renamed (HA keeps them); new devices get readable ids.

**Files:**
- Modify: `custom_components/casa/__init__.py` — add `_ha_device_name` + `_sync_ha_device_name` right after `_find_device_record`; use `_ha_device_name` at the 3 `name=f"Casa Device ({username})"` registry calls (setup loop for managed users, setup loop for native users, `async_register_device`, `async_heartbeat` — grep `name=f"Casa Device (`); call `_sync_ha_device_name` in `CasaAdminDeviceView.put` after an alias change
- Modify: `custom_components/casa/sensor.py` (`CasaDeviceSensorBase.__init__` device_info name), `custom_components/casa/button.py` (`CasaDeviceReloadButton.__init__` device_info name)
- Test: `tests/test_ha_device_name.py` (create)

**Interfaces:**
- Produces: `_ha_device_name(device_info: dict | None, username: str) -> str`; `async _sync_ha_device_name(hass, device_id: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ha_device_name.py`:

```python
import asyncio
import sys
import types
from types import SimpleNamespace

from custom_components.casa import CasaAdminDeviceView, _ha_device_name, _sync_ha_device_name
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user


class FakeDeviceRegistry:
    def __init__(self):
        self.devices = {}  # identifier tuple -> SimpleNamespace(id, name)
        self.updates = []

    def add(self, device_id, name):
        self.devices[("casa", device_id)] = SimpleNamespace(id="reg-" + device_id, name=name)

    def async_get_device(self, identifiers):
        (ident,) = tuple(identifiers)
        return self.devices.get(ident)

    def async_update_device(self, reg_id, name):
        self.updates.append((reg_id, name))
        for dev in self.devices.values():
            if dev.id == reg_id:
                dev.name = name


def _install_registry(monkeypatch, registry):
    mod = types.ModuleType("homeassistant.helpers.device_registry")
    mod.async_get = lambda hass: registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", mod)
    import homeassistant.helpers as helpers
    monkeypatch.setattr(helpers, "device_registry", mod, raising=False)


def test_name_prefers_alias():
    assert _ha_device_name({"alias": " Kitchen iPad "}, "kiosk") == "Kitchen iPad"


def test_name_falls_back_to_account():
    assert _ha_device_name({"alias": ""}, "kiosk") == "Casa Device (kiosk)"
    assert _ha_device_name(None, "kiosk") == "Casa Device (kiosk)"


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kiosk", "devices": {"D1": {"alias": "Kitchen iPad"}}}
    return hass


def test_sync_renames_registry_entry(monkeypatch):
    hass, reg = _hass(), FakeDeviceRegistry()
    reg.add("D1", "Casa Device (kiosk)")
    _install_registry(monkeypatch, reg)
    asyncio.run(_sync_ha_device_name(hass, "D1"))
    assert reg.updates == [("reg-D1", "Kitchen iPad")]


def test_sync_is_noop_when_name_matches_or_device_missing(monkeypatch):
    hass, reg = _hass(), FakeDeviceRegistry()
    reg.add("D1", "Kitchen iPad")
    _install_registry(monkeypatch, reg)
    asyncio.run(_sync_ha_device_name(hass, "D1"))
    asyncio.run(_sync_ha_device_name(hass, "NOPE"))
    assert reg.updates == []


def test_sync_native_device_uses_ha_user_name(monkeypatch):
    hass, reg = FakeHass(), FakeDeviceRegistry()
    hass.auth.add_user(make_user("n1", login="den", name="Den Tablet User"))
    hass.casa["stored_data"]["native_devices"] = {"n1": {"N1": {"alias": ""}}}
    reg.add("N1", "Old")
    _install_registry(monkeypatch, reg)
    asyncio.run(_sync_ha_device_name(hass, "N1"))
    assert reg.updates == [("reg-N1", "Casa Device (Den Tablet User)")]


def test_admin_alias_change_renames_registry_entry(monkeypatch):
    hass, reg = _hass(), FakeDeviceRegistry()
    reg.add("D1", "Kitchen iPad")
    _install_registry(monkeypatch, reg)
    view = bind_view(CasaAdminDeviceView(hass))
    status, _ = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), {"device_id": "D1", "alias": "Hall iPad"})))
    assert status == 200
    assert reg.updates == [("reg-D1", "Hall iPad")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ha_device_name.py` → FAIL (`ImportError: cannot import name '_ha_device_name'`).

- [ ] **Step 3: Implement the helpers**

After `_find_device_record` in `__init__.py` add:

```python
def _ha_device_name(device_info: dict | None, username: str) -> str:
    """Name for a device's Home Assistant device-registry entry: its alias
    when set — several devices can share one account, so the account name
    alone doesn't tell them apart — else "Casa Device (<username>)".
    HA's async_get_or_create overwrites the name on every call, so every
    registry write and every entity device_info must use this."""
    alias = str((device_info or {}).get("alias") or "").strip()
    return alias or f"Casa Device ({username})"


async def _sync_ha_device_name(hass, device_id: str) -> None:
    """Rename the device's registry entry after its alias changed. No-op when
    registry devices are disabled (no entry) or the name already matches. A
    name the user set in HA (name_by_user) still wins in HA's UI."""
    stored_data = hass.data[DOMAIN]["stored_data"]
    device_info, owner_uid, username = _find_device_record(stored_data, device_id)
    if device_info is None:
        return
    if username is None:  # native device — same label the setup loop uses
        ha_user = await hass.auth.async_get_user(owner_uid)
        username = (ha_user.name if ha_user else None) or f"Native User {owner_uid[:6]}"
    from homeassistant.helpers import device_registry as dr
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
    if device is None:
        return
    name = _ha_device_name(device_info, username)
    if device.name != name:
        dev_reg.async_update_device(device.id, name=name)
```

- [ ] **Step 4: Use the helper everywhere the name is written**

- In `async_setup_entry`'s "Register all existing devices" block: managed loop → `name=_ha_device_name(device_data, username),`; native loop → `name=_ha_device_name(device_data, username),`.
- In `async_register_device`'s registry call → `name=_ha_device_name(devices.get(device_id), username),`.
- In `async_heartbeat`'s registry call → `name=_ha_device_name(device_info, username),`.
- `grep -n 'name=f"Casa Device (' custom_components/casa/__init__.py` → no matches afterwards.
- In `CasaAdminDeviceView.put`, right after `device_info["alias"] = str(body.get("alias") or "").strip()[:DEVICE_ALIAS_MAX_LEN]`, add `await _sync_ha_device_name(self.hass, device_id)`.
- In `sensor.py` `CasaDeviceSensorBase.__init__` and `button.py` `CasaDeviceReloadButton.__init__`, replace `"name": f"Casa Device ({username})",` with `"name": _ha_device_name(_find_device_record(hass.data[DOMAIN]["stored_data"], device_id)[0], username),` and add `from . import _find_device_record, _ha_device_name` to each file's imports.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest -q tests` → 150 passed (144 + 6). `node --test tests/js/*.mjs` → 8 passed.

- [ ] **Step 6: Bump the release**

This lands after Task 9's 26.10.02 bump in the same release — no further bump. Add to README's device list sentence: `Home Assistant devices are named after the device alias (falling back to "Casa Device (<account>)").`

- [ ] **Step 7: Commit**

```bash
git add custom_components/casa/__init__.py custom_components/casa/sensor.py custom_components/casa/button.py tests/test_ha_device_name.py README.md
git commit -m "feat: name Home Assistant devices after their alias so shared-account devices are distinguishable"
```

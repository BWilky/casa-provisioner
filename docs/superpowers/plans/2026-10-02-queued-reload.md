# Queued Clear-Cache-and-Reload Implementation Plan (2026-10-02)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make "clear cache and reload" reliable: an encrypted silent push plus a durable queued update (`app` / `clear_cache_reload`), used by every reload entry point.

**Architecture:** Server helper `_queue_app_reload` (replace pending → enqueue → encrypted push, else check-in nudge) behind `casa.reload_device`, the HA button entity and the panel; the device pull path drops reload entries older than 24 h. iOS applies the new type through its central update applier (push and pull) by triggering the existing cache-clear + reload routine, then acks.

**Tech Stack:** Python HA integration (pytest + tests/fakes.py), vanilla JS panel, Swift (CasaCore + app).

**Spec:** design approved in chat 2026-10-02 (user: upgrade existing reload; offline → apply within 24 h; keep just one pending; **beta — no backward compatibility**: the old plain `clear_cache_and_reload` push is no longer sent).

## Global Constraints

- casa-provisioner `/Users/bryce/Documents/casa/casa-provisioner` branch `queued-reload` (controller creates it). iOS `/Users/bryce/Documents/casa/casa-mobile-app/casa-ios` branch `queued-reload` (Task 3 creates it from main).
- Baselines: pytest 172; node 9; CasaCore 55.
- Queue entry: `type: "app"`, `action: "clear_cache_reload"`, `payload: {}`. Exactly one pending per device. Older than 24 h at pull time → removed, never delivered.
- Delivery order: durable queue first, then encrypted push; nudge only when the encrypted push did not go out. Never hold `_lock_for` across network calls.
- Release: provisioner `26.10.04` (const.py / panel/version.js / manifest.json). iOS `CURRENT_PROJECT_VERSION` 15 → 16 (6 occurrences in `Casa.xcodeproj/project.pbxproj`), MARKETING_VERSION stays 1.8.

## Review Focus

1. Two reloads sent back-to-back leave exactly one queue entry — test in Task 1.
2. A device with no push token still gets the entry queued (applies on next heartbeat), and the call does not raise — test in Task 1.
3. A reload queued 25 h ago is removed on pull while a fresh one and other entry types are kept — test in Task 1.
4. The app acks the entry after applying (or the server re-delivers forever) — verified in Task 3 review.

---

### Task 1: Server — queued reload helper, entry points, 24 h expiry, docs

**Files:** `custom_components/casa/__init__.py`; `custom_components/casa/button.py`; `docs/app-integration.md`; `docs/provisioning-protocol.md`; `README.md` (`casa.reload_device` section only); `custom_components/casa/services.yaml` (`reload_device` description); Test `tests/test_app_reload.py` (create).

**Interfaces — Produces:** `APP_RELOAD_MAX_AGE_SECONDS = 24 * 3600`; `async _queue_app_reload(hass, device_id: str, created_by: str) -> dict` (`{"status": "queued", "update_id", "pushed"}`; raises `HomeAssistantError` for an unknown device); `_drop_expired_app_reloads(qu_data: dict, device_id: str) -> bool` (True if it removed anything).

- [ ] **Step 1: failing tests** — create `tests/test_app_reload.py`:

```python
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import custom_components.casa as casa
from custom_components.casa import _drop_expired_app_reloads, _queue_app_reload
from tests.fakes import FakeHass


def _hass(push_token="a" * 64):
    hass = FakeHass()
    d1 = {"refresh_token_id": "r1"}
    if push_token:
        d1["push_token"] = push_token
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kiosk", "devices": {"D1": d1}}
    hass.casa["stored_data"]["device_key"] = "k" * 44
    return hass


def _patch(monkeypatch, pushed=True):
    calls = {"push": 0, "nudge": 0}

    async def fake_push(*a, **kw):
        calls["push"] += 1
        return pushed

    async def fake_nudge(*a, **kw):
        calls["nudge"] += 1
        return True

    monkeypatch.setattr(casa, "_send_encrypted_update_push", fake_push)
    monkeypatch.setattr(casa, "_nudge_device_checkin", fake_nudge)
    return calls


def _reloads(hass):
    return [e for e in hass.casa["qu_data"]["updates"].get("D1", []) if e["type"] == "app"]


def test_queues_entry_and_pushes(monkeypatch):
    hass = _hass()
    calls = _patch(monkeypatch)
    result = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    (entry,) = _reloads(hass)
    assert entry["action"] == "clear_cache_reload" and entry["payload"] == {}
    assert result == {"status": "queued", "update_id": entry["id"], "pushed": True}
    assert calls == {"push": 1, "nudge": 0}


def test_second_reload_replaces_pending_one(monkeypatch):
    hass = _hass()
    _patch(monkeypatch)
    first = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    hass.casa["qu_data"]["updates"]["D1"].append({"id": "p1", "type": "profile", "action": "update", "payload": {}})
    second = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    reloads = _reloads(hass)
    assert [e["id"] for e in reloads] == [second["update_id"]] != [first["update_id"]]
    assert any(e["id"] == "p1" for e in hass.casa["qu_data"]["updates"]["D1"])


def test_no_push_token_still_queues_and_nudges(monkeypatch):
    hass = _hass(push_token=None)
    calls = _patch(monkeypatch, pushed=False)
    result = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    assert result["pushed"] is False and len(_reloads(hass)) == 1
    assert calls == {"push": 1, "nudge": 1}


def test_unknown_device_raises(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(Exception, match="not found"):
        asyncio.run(_queue_app_reload(_hass(), "NOPE", created_by="test"))


def test_expired_reloads_are_dropped_on_pull():
    now = datetime.now(timezone.utc)
    qu_data = {"updates": {"D1": [
        {"id": "old", "type": "app", "action": "clear_cache_reload", "payload": {}, "created_at": (now - timedelta(hours=25)).isoformat()},
        {"id": "new", "type": "app", "action": "clear_cache_reload", "payload": {}, "created_at": (now - timedelta(hours=1)).isoformat()},
        {"id": "prof", "type": "profile", "action": "update", "payload": {}, "created_at": (now - timedelta(days=3)).isoformat()},
    ]}}
    assert _drop_expired_app_reloads(qu_data, "D1") is True
    assert [e["id"] for e in qu_data["updates"]["D1"]] == ["new", "prof"]
    assert _drop_expired_app_reloads(qu_data, "D1") is False
```

Run `python3 -m pytest -q tests/test_app_reload.py` → ImportError.

- [ ] **Step 2: implement** (module level, right after `_send_encrypted_update_push` / before `_deliver_updates_in_background`):

```python
APP_RELOAD_MAX_AGE_SECONDS = 24 * 3600


def _is_app_reload(entry: dict) -> bool:
    return entry.get("type") == "app" and entry.get("action") == "clear_cache_reload"


def _drop_expired_app_reloads(qu_data: dict, device_id: str) -> bool:
    """Remove clear-cache reloads queued more than 24 h ago: a reload is only
    useful soon after the admin asked for it. Other entry types untouched.
    Returns True when something was removed (caller saves)."""
    entries = (qu_data.get("updates") or {}).get(device_id) or []
    cutoff = dt_util.now() - timedelta(seconds=APP_RELOAD_MAX_AGE_SECONDS)

    def expired(entry):
        if not _is_app_reload(entry):
            return False
        try:
            return datetime.fromisoformat(str(entry.get("created_at"))) < cutoff
        except (TypeError, ValueError):
            return True  # unparseable timestamp: can't prove it's fresh

    stale = [e for e in entries if expired(e)]
    for entry in stale:
        _dequeue_update(qu_data, device_id, entry.get("id"))
    return bool(stale)


async def _queue_app_reload(hass, device_id: str, created_by: str) -> dict:
    """Clear the app's web cache and reload it — reliably: replace any still-
    pending reload (only one is ever queued), queue a durable app/
    clear_cache_reload update, then deliver it over an encrypted silent push
    (or nudge a check-in when that can't go out). The device applies it from
    either path and acks it; unpulled reloads expire after 24 h."""
    data = hass.data[DOMAIN]
    stored_data = data["stored_data"]
    qu_data = data["qu_data"]
    async with _lock_for(hass, "device", device_id):
        device_info, _uid, username = _find_device_record(stored_data, device_id)
        if device_info is None:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")
        for entry in [e for e in (qu_data.get("updates") or {}).get(device_id, []) if _is_app_reload(e)]:
            _dequeue_update(qu_data, device_id, entry.get("id"))
        update_id = _enqueue_update(qu_data, device_id, "app", "clear_cache_reload", {}, created_by)
        data["qu_store"].async_delay_save(lambda: qu_data, 2.0)
    # Delivery accelerators run after the lock: relay calls can take seconds.
    session = async_get_clientsession(hass)
    pushed = await _send_encrypted_update_push(
        hass, stored_data, session, device_id, device_info, update_id, "app", "clear_cache_reload", {},
    )
    if not pushed:
        await _nudge_device_checkin(hass, session, stored_data, device_info)
    _LOGGER.info(
        "CASA: Queued clear-cache reload for device '%s' (%s) by %s (pushed=%s).",
        device_id, username, created_by, pushed,
    )
    return {"status": "queued", "update_id": update_id, "pushed": pushed}
```

(Check `datetime`/`timedelta` are imported at module top — `timedelta` is used elsewhere; add `datetime` if needed. If `_dequeue_update`'s signature differs from `(qu_data, device_id, update_id)`, adapt.)

- [ ] **Step 3: entry points.**
  - `handle_reload_device` (grep `async def handle_reload_device`): replace its body after `_check_authorization(call)` with: read/validate `device_id` (keep the "Missing device_id parameter." error), then `return await _queue_app_reload(hass, device_id, created_by=_caller_label(call))` — if no such label helper exists, use `created_by="service"`. Remove the old relay `/send` code in that handler. Keep the registration (`supports_response=SupportsResponse.OPTIONAL`).
  - `button.py` `CasaDeviceReloadButton.async_press`: replace the body with:

```python
        from . import _queue_app_reload
        await _queue_app_reload(self.hass, self.device_id, created_by="HA button")
```

    and drop now-unused imports in that method.
  - `CasaProfileUpdatesView.get` (the device pull): before reading `updates` for the response, call `if _drop_expired_app_reloads(qu_data, device_id): self.hass.data[DOMAIN]["qu_store"].async_delay_save(lambda: qu_data, 2.0)` and then read `updates` (so expired reloads are never delivered).
  - `grep -n "clear_cache_and_reload" custom_components/casa` → no matches remain in Python.
- [ ] **Step 4: docs.**
  - `docs/app-integration.md` §4 queued-update table: add `| \`app\` | \`clear_cache_reload\` | \`{}\` | clear the web cache and reload the WebView, then ack; the server drops it unpulled after 24 h and keeps at most one pending |`. In the "Command pushes" section, remove `clear_cache_and_reload` from the heading and list and add one line: "(26.10.04) The plain `clear_cache_and_reload` command push is retired — reloads are the queued `app/clear_cache_reload` update."
  - `docs/provisioning-protocol.md` §9: remove the `clear_cache_and_reload` row (or mark it "retired 26.10.04 — see app/clear_cache_reload queued update").
  - `README.md` `casa.reload_device` section and `services.yaml` `reload_device.description`: "Queues a clear-cache-and-reload for the device and delivers it over an encrypted silent push; if the device is offline it applies on its next check-in within 24 h. Only one reload is ever pending."
- [ ] **Step 5:** `python3 -m pytest -q tests` → 177 passed; ast parse check.
- [ ] **Step 6: commit** `feat: reliable clear-cache reload — encrypted push plus durable queue, one pending, 24 h expiry`

---

### Task 2: Panel copy + release 26.10.04

**Files:** `custom_components/casa/panel/views/devices.js`; version files; `README.md`.

- [ ] In `confirmReload(d)`: message → `` `Clear the cache and reload "${deviceName(d)}"? It's sent over push now, or applied when the device next checks in (within 24 h).` ``; success toast → `"Reload queued."`. In the bulk `reload` confirm: message → `` `Clear the cache and reload ${n} ${plural}? Sent over push now, or applied when each device next checks in (within 24 h).` ``; and the bulk result verb for `reload` → `"queued for reload"` (grep `const verb = kind === "reload" ? "reloaded"`).
- [ ] `node --check custom_components/casa/panel/views/devices.js`.
- [ ] Bump `26.10.03` → `26.10.04` in const.py / panel/version.js / manifest.json; `python3 -m pytest -q tests/test_versions.py`.
- [ ] README device-list sentence: change "reload" in the per-device menu description to "reload (clears the cache; queued so an offline device still reloads within 24 h)".
- [ ] Full suites: pytest 177, node 9.
- [ ] Commit `chore: 26.10.04 — queued reload`.

---

### Task 3: iOS — apply `app/clear_cache_reload`

**Files:** `Casa/ContentView.swift` (`applyUpdateEntry` switch ~line 1330); `Casa.xcodeproj/project.pbxproj`.

- [ ] **Step 0:** `cd /Users/bryce/Documents/casa/casa-mobile-app/casa-ios && git checkout main && git pull --ff-only && git checkout -b queued-reload`
- [ ] **Step 1:** In `applyUpdateEntry`'s `switch (type, action)`, add before `default:`:

```swift
        case ("app", "clear_cache_reload"):
            outcome = applyClearCacheReload()
```

  and add near the other `apply*` helpers:

```swift
    /// Queued/pushed `app/clear_cache_reload`: clear WebKit cache and reload
    /// the configured URL via the same routine the retired plain push used.
    private func applyClearCacheReload() -> UpdateApplyOutcome {
        tlog("[Update] Clear cache & reload requested.")
        NotificationCenter.default.post(name: .casaSilentClearCacheAndReload, object: nil)
        return .applied
    }
```

  Verify `applyUpdateEntry` runs on the main thread (or that posting the notification from its thread is fine for the SwiftUI `.onReceive`), and that the `.onReceive(.casaSilentClearCacheAndReload)` handler's `guard appState.isProvisioned` holds for a provisioned device. If CasaCore has a list of known update types (grep `"location"` in `Packages/CasaCore/Sources`), add `app` there and a test.
- [ ] **Step 2:** `CURRENT_PROJECT_VERSION = 15;` → `16;` (6 occurrences; not the `.survived-backup` dir).
- [ ] **Step 3:** CasaCore tests (`cd Packages/CasaCore && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test 2>&1 | grep -E "Executed [0-9]+ tests"` → 0 failures) and the app build (`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild build -project Casa.xcodeproj -scheme Casa -destination 'generic/platform=iOS Simulator' ARCHS=arm64 -quiet` → success, 0 warnings).
- [ ] **Step 4:** commit `feat: apply queued clear-cache reload (app/clear_cache_reload)` and `chore: build 16`.

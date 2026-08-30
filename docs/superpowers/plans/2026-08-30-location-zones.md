# Location Zones Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Devices report a coarse zone label (`House: home` / `away` / `unknown`) to HA, computed on-device from admin-configured anchors+rings; GPS never leaves the phone.

**Architecture:** The provisioner stores a site-wide zone config (anchors, each with ordered labeled radius rings) in a new HA Store, delivers it to devices through the existing queued-update/encrypted-push pipeline, and receives zone reports via a new device_key-encrypted endpoint plus heartbeat piggyback. A per-device HA sensor exposes the state. The iOS app registers native geofences (≤18 regions, server-enforced), evaluates the innermost containing ring mathematically on each crossing, and reports change-only.

**Tech Stack:** Python (HA custom component, `cryptography` HKDF/AES-GCM), vanilla-JS panel + vendored Leaflet, Swift/SwiftUI + CoreLocation + CryptoKit.

**Spec:** `docs/superpowers/specs/2026-08-30-location-zones-design.md` (read it first — it is the authority on behavior).

## Global Constraints

- Provisioner repo: `/Users/bryce/Documents/casa/casa-provisioner` (git; commit per task). iOS: `/Users/bryce/Documents/casa/casa-mobile-app/casa-ios` (NOT a git repo — no commit steps; the verification gate is `xcodebuild`).
- The Xcode project uses filesystem-synchronized groups (objectVersion 77): new `.swift` files placed under `Casa/` are picked up automatically — do NOT edit `project.pbxproj` to add files.
- No coordinates, distances, or GPS-derived values may ever appear in any report schema, device record, sensor state/attribute, or log line on the server side.
- Reserved state labels: `away`, `unknown` — rejected as ring labels.
- Region budget: total rings across all anchors ≤ 18 (`MAX_TOTAL_RINGS = 18`).
- Crypto: HKDF-SHA256(input=UTF-8 of device_key hex string, salt=UTF-8 device_id, info=`casa-report-v1`) → AES-256-GCM, base64(nonce(12) || ct || tag(16)). Same primitive family as the existing `casa-update-v1` push path, distinct info string for domain separation (spec's "AAD binding" is realized via the HKDF salt, matching the existing primitive).
- Version bump: `CASA_VERSION` in `custom_components/casa/const.py` and the version constant in `custom_components/casa/panel/version.js` change together, once, in Task 7 (value `26.08.30`).
- iOS deployment target is 17.6; match existing code style (`tlog(...)` logging, singletons, dispatcher patterns).
- Python tests: plain `pytest` from repo root, no Home Assistant test harness — all tested logic lives in `custom_components/casa/location.py`, which must import only stdlib + `cryptography`.

---

### Task 1: Server pure helpers — `location.py` (validation, versioning, report crypto)

**Files:**
- Create: `custom_components/casa/location.py`
- Create: `tests/test_location.py`
- Create: `tests/__init__.py` (empty)

**Interfaces:**
- Produces: `validate_zone_config(config: dict) -> list[str]` (empty list = valid), `compute_config_version(anchors: list) -> str` (8 hex chars), `encrypt_report_payload(plaintext: str, device_key: str, device_id: str) -> str`, `decrypt_report_payload(payload_b64: str, device_key: str, device_id: str) -> dict` (raises `ValueError` on any failure), constants `MAX_TOTAL_RINGS = 18`, `RESERVED_LABELS = frozenset({"away", "unknown"})`, `ALLOWED_REASONS = frozenset({"permission_denied", "no_fix", "stale"})`, `ALLOWED_REPORT_KEYS = frozenset({"state", "reason", "config_version", "ts"})`.
- Consumes: nothing from this codebase (stdlib + `cryptography` only — that keeps it plain-pytest testable).

- [ ] **Step 1: Write the failing tests**

`tests/test_location.py`:

```python
import json
import time

import pytest

from custom_components.casa.location import (
    ALLOWED_REPORT_KEYS,
    MAX_TOTAL_RINGS,
    RESERVED_LABELS,
    compute_config_version,
    decrypt_report_payload,
    encrypt_report_payload,
    validate_zone_config,
)


def _valid_config():
    return {
        "stale_after_minutes": 30,
        "anchors": [
            {
                "id": "a1",
                "name": "House",
                "latitude": 49.1,
                "longitude": -123.2,
                "rings": [
                    {"label": "home", "radius_m": 150},
                    {"label": "nearby", "radius_m": 1000},
                ],
            }
        ],
    }


def test_valid_config_passes():
    assert validate_zone_config(_valid_config()) == []


def test_empty_anchors_is_valid():
    assert validate_zone_config({"stale_after_minutes": 0, "anchors": []}) == []


def test_radii_must_strictly_ascend():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"][1]["radius_m"] = 150
    assert any("ascending" in e for e in validate_zone_config(cfg))


def test_reserved_labels_rejected():
    for label in RESERVED_LABELS:
        cfg = _valid_config()
        cfg["anchors"][0]["rings"][0]["label"] = label
        assert any("reserved" in e for e in validate_zone_config(cfg))


def test_duplicate_ring_labels_within_anchor_rejected():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"][1]["label"] = "home"
    assert any("unique" in e for e in validate_zone_config(cfg))


def test_duplicate_anchor_names_rejected():
    cfg = _valid_config()
    cfg["anchors"].append(dict(cfg["anchors"][0], id="a2"))
    assert any("unique" in e for e in validate_zone_config(cfg))


def test_ring_budget_enforced():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"] = [
        {"label": f"r{i}", "radius_m": 100 * (i + 1)} for i in range(MAX_TOTAL_RINGS + 1)
    ]
    assert any("18" in e for e in validate_zone_config(cfg))


def test_lat_long_range_checked():
    cfg = _valid_config()
    cfg["anchors"][0]["latitude"] = 91
    assert any("latitude" in e for e in validate_zone_config(cfg))


def test_negative_stale_minutes_rejected():
    cfg = _valid_config()
    cfg["stale_after_minutes"] = -5
    assert any("stale_after_minutes" in e for e in validate_zone_config(cfg))


def test_config_version_stable_and_content_sensitive():
    a = _valid_config()["anchors"]
    v1 = compute_config_version(a)
    v2 = compute_config_version(json.loads(json.dumps(a)))
    assert v1 == v2 and len(v1) == 8
    a[0]["rings"][0]["radius_m"] = 151
    assert compute_config_version(a) != v1


def test_report_roundtrip():
    inner = {"state": "House: home", "reason": None, "config_version": "abcd1234", "ts": int(time.time())}
    blob = encrypt_report_payload(json.dumps(inner), "deadbeef" * 8, "DEV-1")
    assert decrypt_report_payload(blob, "deadbeef" * 8, "DEV-1") == inner


def test_report_wrong_device_rejected():
    blob = encrypt_report_payload("{}", "deadbeef" * 8, "DEV-1")
    with pytest.raises(ValueError):
        decrypt_report_payload(blob, "deadbeef" * 8, "DEV-2")


def test_report_not_replayable_from_push_domain():
    # A payload encrypted with the push info string must not decrypt as a report.
    import base64, secrets as pysecrets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"DEV-1", info=b"casa-update-v1").derive(b"k" * 64)
    nonce = pysecrets.token_bytes(12)
    blob = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, b"{}", None)).decode()
    with pytest.raises(ValueError):
        decrypt_report_payload(blob, "k" * 64, "DEV-1")


def test_report_garbage_rejected():
    with pytest.raises(ValueError):
        decrypt_report_payload("not base64!!!", "k" * 64, "DEV-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `/Users/bryce/Documents/casa/casa-provisioner`: `python3 -m pytest tests/test_location.py -q`
Expected: collection error / ImportError (`location.py` doesn't exist). If `pytest` is missing, `python3 -m pip install pytest cryptography` first.

- [ ] **Step 3: Implement `custom_components/casa/location.py`**

```python
"""Location-zones helpers: config validation, versioning, report crypto.

Pure functions only (stdlib + cryptography) so they are testable with plain
pytest, without the Home Assistant test harness. The report crypto mirrors
_encrypt_push_payload in __init__.py — same HKDF/AES-GCM construction, with
a distinct info string so pushes and reports live in separate key domains.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_TOTAL_RINGS = 18
RESERVED_LABELS = frozenset({"away", "unknown"})
ALLOWED_REASONS = frozenset({"permission_denied", "no_fix", "stale"})
ALLOWED_REPORT_KEYS = frozenset({"state", "reason", "config_version", "ts"})
_REPORT_INFO = b"casa-report-v1"


def validate_zone_config(config: dict) -> list[str]:
    """Return a list of human-readable errors; empty list means valid."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["config must be an object"]

    stale = config.get("stale_after_minutes", 0)
    if not isinstance(stale, int) or isinstance(stale, bool) or stale < 0:
        errors.append("stale_after_minutes must be a non-negative integer")

    anchors = config.get("anchors")
    if not isinstance(anchors, list):
        return errors + ["anchors must be a list"]

    names = set()
    total_rings = 0
    for i, anchor in enumerate(anchors):
        where = f"anchor {i + 1}"
        if not isinstance(anchor, dict):
            errors.append(f"{where}: must be an object")
            continue
        name = str(anchor.get("name", "")).strip()
        if not name:
            errors.append(f"{where}: name is required")
        elif name.casefold() in names:
            errors.append(f"{where}: anchor names must be unique ('{name}')")
        else:
            names.add(name.casefold())

        lat, lon = anchor.get("latitude"), anchor.get("longitude")
        if not isinstance(lat, (int, float)) or isinstance(lat, bool) or not -90 <= lat <= 90:
            errors.append(f"{where}: latitude must be a number in [-90, 90]")
        if not isinstance(lon, (int, float)) or isinstance(lon, bool) or not -180 <= lon <= 180:
            errors.append(f"{where}: longitude must be a number in [-180, 180]")

        rings = anchor.get("rings")
        if not isinstance(rings, list) or not rings:
            errors.append(f"{where}: at least one ring is required")
            continue
        total_rings += len(rings)
        labels = set()
        prev_radius = 0.0
        for j, ring in enumerate(rings):
            rw = f"{where} ring {j + 1}"
            if not isinstance(ring, dict):
                errors.append(f"{rw}: must be an object")
                continue
            label = str(ring.get("label", "")).strip()
            if not label:
                errors.append(f"{rw}: label is required")
            elif label.casefold() in RESERVED_LABELS:
                errors.append(f"{rw}: '{label}' is a reserved label")
            elif label.casefold() in labels:
                errors.append(f"{rw}: labels must be unique within an anchor")
            else:
                labels.add(label.casefold())
            radius = ring.get("radius_m")
            if not isinstance(radius, (int, float)) or isinstance(radius, bool) or radius <= 0:
                errors.append(f"{rw}: radius_m must be a positive number")
            elif radius <= prev_radius:
                errors.append(f"{rw}: radii must be strictly ascending")
            else:
                prev_radius = radius

    if total_rings > MAX_TOTAL_RINGS:
        errors.append(
            f"total rings across anchors is {total_rings}; iOS geofencing caps this at {MAX_TOTAL_RINGS}"
        )
    return errors


def compute_config_version(anchors: list) -> str:
    """Short stable content hash of the anchors list (canonical JSON)."""
    canonical = json.dumps(anchors, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _derive_report_key(device_key: str, device_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=device_id.encode("utf-8"),
        info=_REPORT_INFO,
    ).derive(device_key.encode("utf-8"))


def encrypt_report_payload(plaintext: str, device_key: str, device_id: str) -> str:
    """Encrypt a report the way the iOS app does (used by tests; the app has
    the Swift mirror). base64(nonce || ciphertext || tag)."""
    key = _derive_report_key(device_key, device_id)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def decrypt_report_payload(payload_b64: str, device_key: str, device_id: str) -> dict:
    """Decrypt and JSON-parse a device report. Raises ValueError on any
    failure (bad base64, wrong key/device, tampered, non-JSON, non-object)."""
    try:
        raw = base64.b64decode(payload_b64, validate=True)
        if len(raw) < 12 + 16:
            raise ValueError("payload too short")
        key = _derive_report_key(device_key, device_id)
        plaintext = AESGCM(key).decrypt(raw[:12], raw[12:], None)
        inner = json.loads(plaintext.decode("utf-8"))
    except ValueError:
        raise
    except Exception as err:  # binascii.Error, InvalidTag, JSONDecodeError, ...
        raise ValueError(f"report decrypt failed: {type(err).__name__}") from err
    if not isinstance(inner, dict):
        raise ValueError("report payload is not an object")
    return inner
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_location.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/location.py tests/
git commit -m "feat(location): zone-config validation, versioning, report crypto helpers"
```

---

### Task 2: Store, admin CRUD view, enqueue-on-save

**Files:**
- Modify: `custom_components/casa/__init__.py`

**Interfaces:**
- Consumes: Task 1's `validate_zone_config`, `compute_config_version`; existing `_enqueue_update`, `_deliver_updates_in_background`, `Store`, `HomeAssistantView`.
- Produces: `hass.data[DOMAIN]["lz_store"]` / `["lz_data"]` (shape: `{"config_version": str, "stale_after_minutes": int, "anchors": []}`); helper `_enqueue_location_update_for_device(hass, qu_data, device_id, lz_data, created_by) -> str` (drops older queued `location` entries for that device, enqueues `type="location", action="update", payload={"anchors": [...], "config_version": ...}`, returns update_id); helper `_iter_all_devices(stored_data)` yielding `(device_id, device_info)` for every device under non-deleted users plus native devices; view `CasaLocationZonesView` at `/api/casa/admin/location_zones`.

- [ ] **Step 1: Add the import and store setup**

At the top of `__init__.py`, next to the other local imports (`from .const import ...`), add:

```python
from .location import (
    ALLOWED_REASONS,
    ALLOWED_REPORT_KEYS,
    compute_config_version,
    decrypt_report_payload,
    validate_zone_config,
)
```

In `async_setup_entry`, directly after the `qu_store` block (near line 2435, pattern-match the `wg_store` block at 2414):

```python
    lz_store = Store(hass, 1, "casa_location_zones")
    lz_data = await lz_store.async_load()
    if lz_data is None:
        lz_data = {"config_version": "", "stale_after_minutes": 30, "anchors": []}
    hass.data[DOMAIN]["lz_store"] = lz_store
    hass.data[DOMAIN]["lz_data"] = lz_data
```

- [ ] **Step 2: Add the helpers**

Place next to `_enqueue_update` (module level, ~line 300):

```python
def _iter_all_devices(stored_data: dict):
    """Yield (device_id, device_info) for every device under a non-deleted
    owner, plus native devices."""
    for _uid, udata in stored_data.get("users", {}).items():
        if udata.get("deleted", False):
            continue
        for did, dinfo in (udata.get("devices", {}) or {}).items():
            yield did, dinfo
    for _uid, devices in stored_data.get("native_devices", {}).items():
        for did, dinfo in (devices or {}).items():
            yield did, dinfo


def _enqueue_location_update_for_device(qu_data: dict, device_id: str, lz_data: dict, created_by: str) -> str:
    """Enqueue the current zone config for a device, superseding any older
    queued location entries (two configs applied in sequence is pointless)."""
    entries = qu_data.get("updates", {}).get(device_id, [])
    kept = [e for e in entries if e.get("type") != "location"]
    if kept:
        qu_data["updates"][device_id] = kept
    elif entries:
        qu_data.get("updates", {}).pop(device_id, None)
    payload = {"anchors": lz_data.get("anchors", []), "config_version": lz_data.get("config_version", "")}
    return _enqueue_update(qu_data, device_id, "location", "update", payload, created_by)
```

- [ ] **Step 3: Add the view**

Place after `CasaWireGuardProfilesView` (mirror its auth guard/JSON handling exactly):

```python
class CasaLocationZonesView(HomeAssistantView):
    """Admin-only zone config CRUD. PUT replaces wholesale, then queues a
    'location' update (encrypted silent push + durable queue) to every device."""

    url = "/api/casa/admin/location_zones"
    name = "api:casa:admin:location_zones"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)
        return self.json(self.hass.data.get(DOMAIN, {}).get("lz_data", {"config_version": "", "stale_after_minutes": 30, "anchors": []}))

    async def put(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        errors = validate_zone_config(body)
        if errors:
            return self.json({"error": "; ".join(errors)}, status_code=400)

        lz_data = self.hass.data[DOMAIN]["lz_data"]
        old_version = lz_data.get("config_version", "")
        lz_data["anchors"] = body.get("anchors", [])
        lz_data["stale_after_minutes"] = int(body.get("stale_after_minutes", 30))
        lz_data["config_version"] = compute_config_version(lz_data["anchors"])
        self.hass.data[DOMAIN]["lz_store"].async_delay_save(lambda: lz_data, 2.0)

        queued = 0
        jobs = []
        if lz_data["config_version"] != old_version:
            stored_data = self.hass.data[DOMAIN]["stored_data"]
            qu_data = self.hass.data[DOMAIN]["qu_data"]
            created_by = user.name or user.id
            payload = {"anchors": lz_data["anchors"], "config_version": lz_data["config_version"]}
            for did, dinfo in _iter_all_devices(stored_data):
                update_id = _enqueue_location_update_for_device(qu_data, did, lz_data, created_by)
                jobs.append((did, dinfo, update_id))
                queued += 1
            self.hass.data[DOMAIN]["qu_store"].async_delay_save(lambda: qu_data, 2.0)
            if jobs:
                self.hass.async_create_task(_deliver_updates_in_background(
                    self.hass, stored_data, jobs, "location", "update", payload,
                    send_update_push=True, notify_push=False, title="", message="",
                    created_by=created_by,
                ))
        _LOGGER.info("CASA: Location zones saved (version=%s); queued for %d device(s).", lz_data["config_version"], queued)
        return self.json({"status": "ok", "config_version": lz_data["config_version"], "queued": queued})
```

- [ ] **Step 4: Register the view**

In the `register_view` block (~line 2807), add:

```python
    hass.http.register_view(CasaLocationZonesView(hass))
```

- [ ] **Step 5: Verify**

Run: `python3 -m pytest tests/ -q` (still green) and `python3 -c "import ast; ast.parse(open('custom_components/casa/__init__.py').read())"` — Expected: no output/errors.

- [ ] **Step 6: Commit**

```bash
git add custom_components/casa/__init__.py
git commit -m "feat(location): zone store, admin CRUD view, enqueue-to-all on save"
```

---

### Task 3: Report endpoint, shared applier, heartbeat piggyback + reconcile

**Files:**
- Modify: `custom_components/casa/__init__.py`

**Interfaces:**
- Consumes: Task 1 crypto/validation constants; Task 2 `lz_data` and `_enqueue_location_update_for_device`; existing `_find_device_record`, dispatcher pattern (`async_dispatcher_send(hass, f"casa_device_updated_{device_id}")`, see line ~591).
- Produces: `_apply_location_report(hass, device_id, device_info, state, reason, config_version) -> bool` (validates + stamps the four device-record fields + signals + delay-saves; returns False on invalid input); view `CasaLocationReportView` at `POST /api/casa/location_report`; heartbeat request fields `location_state`/`location_reason`/`location_config_version`; heartbeat response field `location_config_version`.

- [ ] **Step 1: Add the applier** (module level, near `_enqueue_location_update_for_device`):

```python
def _apply_location_report(hass, device_id: str, device_info: dict, state, reason, config_version) -> bool:
    """Validate and stamp a device's zone report onto its record. The state
    string is opaque ('<anchor>: <label>' | 'away' | 'unknown') but bounded;
    anything containing digits-with-dots (coordinate-ish) is rejected outright
    per the no-location-data rule."""
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    if not isinstance(state, str) or not state.strip() or len(state) > 120:
        return False
    state = state.strip()
    if reason is not None:
        reason = str(reason)
        if reason not in ALLOWED_REASONS:
            return False
    if state == "unknown" and reason is None:
        reason = "no_fix"
    if state != "unknown":
        reason = None

    device_info["location_state"] = state
    device_info["location_reason"] = reason
    device_info["location_reported_at"] = dt_util.now().isoformat()
    if isinstance(config_version, str) and config_version:
        device_info["location_config_version"] = config_version
    async_dispatcher_send(hass, f"casa_device_updated_{device_id}")
    hass.data[DOMAIN]["store"].async_delay_save(lambda: hass.data[DOMAIN]["stored_data"], 2.0)
    return True
```

- [ ] **Step 2: Add the report view** (after `CasaLocationZonesView`):

```python
class CasaLocationReportView(HomeAssistantView):
    """Device-facing zone report endpoint. No HA session required — the
    device_key encryption IS the authentication (same trust model as the
    encrypted push path; per-device HKDF salt prevents cross-device replay).
    The report schema deliberately has no location fields, and unknown keys
    are rejected so none can be smuggled in later."""

    url = "/api/casa/location_report"
    name = "api:casa:location_report"
    requires_auth = False

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def post(self, request):
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = str(body.get("device_id", "")).strip()
        payload_b64 = body.get("payload")
        if not device_id or not isinstance(payload_b64, str):
            return self.json({"error": "device_id and payload are required"}, status_code=400)

        data = self.hass.data.get(DOMAIN, {})
        stored_data = data.get("stored_data", {})
        device_key = stored_data.get("device_key")
        device_info, _uid, _uname = _find_device_record(stored_data, device_id)
        if device_info is None or not device_key:
            # Deliberately vague: this endpoint is unauthenticated.
            return self.json({"error": "Rejected"}, status_code=403)

        try:
            inner = decrypt_report_payload(payload_b64, device_key, device_id)
        except ValueError:
            _LOGGER.warning("CASA: Rejected location report for device '%s' (decrypt failed).", device_id)
            return self.json({"error": "Rejected"}, status_code=403)

        if set(inner.keys()) - ALLOWED_REPORT_KEYS:
            return self.json({"error": "Rejected"}, status_code=403)
        ts = inner.get("ts")
        if not isinstance(ts, (int, float)) or abs(time.time() - ts) > 300:
            return self.json({"error": "Rejected"}, status_code=403)

        if not _apply_location_report(self.hass, device_id, device_info,
                                      inner.get("state"), inner.get("reason"),
                                      inner.get("config_version")):
            return self.json({"error": "Rejected"}, status_code=403)
        return self.json({"status": "ok"})
```

- [ ] **Step 3: Register it** alongside the Task 2 registration:

```python
    hass.http.register_view(CasaLocationReportView(hass))
```

- [ ] **Step 4: Heartbeat piggyback + reconcile**

In `CasaHeartbeatView.post` (line ~746): after the `alias` extraction block, read the three optional fields:

```python
        location_state = data.get("location_state")
        location_reason = data.get("location_reason")
        location_config_version = data.get("location_config_version")
```

After the `heartbeat_func` call succeeds and `stored_data` is in scope (~line 826), apply and reconcile:

```python
        lz_data = self.hass.data[DOMAIN].get("lz_data", {})
        server_lz_version = lz_data.get("config_version", "")
        device_info, _uid, _uname = _find_device_record(stored_data, device_id)
        if device_info is not None and isinstance(location_state, str):
            _apply_location_report(self.hass, device_id, device_info,
                                   location_state, location_reason, location_config_version)
        # Reconciler: device confirmed a stale config version → re-enqueue.
        if (device_info is not None and server_lz_version
                and isinstance(location_config_version, str)
                and location_config_version != server_lz_version):
            qu_data = self.hass.data[DOMAIN]["qu_data"]
            _enqueue_location_update_for_device(qu_data, device_id, lz_data, "system:lz-reconcile")
            self.hass.data[DOMAIN]["qu_store"].async_delay_save(lambda: qu_data, 2.0)
            result["updates"] = True
```

And in the response dict (~line 828) add:

```python
            "location_config_version": server_lz_version or None,
```

- [ ] **Step 5: Verify** — `python3 -m pytest tests/ -q` and the `ast.parse` one-liner from Task 2. Also confirm `import time` already exists at the top of `__init__.py` (it does — `time.time()` is used by the push path; if not, add it).

- [ ] **Step 6: Commit**

```bash
git add custom_components/casa/__init__.py
git commit -m "feat(location): report endpoint, heartbeat piggyback and version reconcile"
```

---

### Task 4: Location sensor + staleness sweep

**Files:**
- Modify: `custom_components/casa/sensor.py`
- Modify: `custom_components/casa/__init__.py`

**Interfaces:**
- Consumes: device-record fields from Task 3; existing sensor base pattern (`CasaDeviceLastSeenSensor` and siblings — copy their `__init__`/dispatcher/`device_info` wiring exactly); `async_track_time_interval` (already imported in `__init__.py` line 30).
- Produces: `CasaDeviceLocationSensor` (state = `location_state` or `unknown`; attributes `reason`, `reported_at`, `config_version`); a 60 s staleness sweep registered in `async_setup_entry`, unsubscribed in `async_unload_entry` via `hass.data[DOMAIN]["lz_stale_unsub"]`.

- [ ] **Step 1: Add the sensor class**

In `sensor.py`, read one existing sensor class fully (e.g. `CasaDeviceLastSeenSensor`) and clone its structure — same base class, same `unique_id` scheme with suffix `location`, same dispatcher subscription to `casa_device_updated_{device_id}`, same device_info registry linkage. State/attribute logic:

```python
class CasaDeviceLocationSensor(/* same base as siblings */):
    """Zone state reported by the device (label only — never coordinates)."""
    # name suffix: "Location"; unique_id suffix: "location"; icon "mdi:map-marker-radius"

    @property
    def native_value(self):
        dinfo = self._device_info_dict()  # however siblings fetch their record
        return (dinfo or {}).get("location_state") or "unknown"

    @property
    def extra_state_attributes(self):
        dinfo = self._device_info_dict() or {}
        return {
            "reason": dinfo.get("location_reason"),
            "reported_at": dinfo.get("location_reported_at"),
            "config_version": dinfo.get("location_config_version"),
        }
```

(The exact record-fetch helper differs per this file's idiom — mirror how `CasaDeviceLastSeenSensor` reads `stored_data`; do not invent a new access path.) Add it to the list in `create_sensors_for_device`.

- [ ] **Step 2: Add the staleness sweep**

In `__init__.py`'s `async_setup_entry`, next to the existing `reconcile_unsub` interval registration (~line 4705):

```python
    async def _location_staleness_sweep(_now):
        data = hass.data.get(DOMAIN, {})
        lz = data.get("lz_data", {})
        minutes = lz.get("stale_after_minutes", 0)
        if not minutes:
            return
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        cutoff = dt_util.now() - timedelta(minutes=minutes)
        changed = False
        for did, dinfo in _iter_all_devices(data.get("stored_data", {})):
            reported_at = dinfo.get("location_reported_at")
            if not reported_at or dinfo.get("location_state") in (None, "unknown"):
                continue
            try:
                reported_dt = dt_util.parse_datetime(reported_at)
            except (ValueError, TypeError):
                reported_dt = None
            if reported_dt is None or reported_dt < cutoff:
                dinfo["location_state"] = "unknown"
                dinfo["location_reason"] = "stale"
                async_dispatcher_send(hass, f"casa_device_updated_{did}")
                changed = True
        if changed:
            data["store"].async_delay_save(lambda: data["stored_data"], 2.0)

    hass.data[DOMAIN]["lz_stale_unsub"] = async_track_time_interval(
        hass, _location_staleness_sweep, timedelta(seconds=60)
    )
```

Confirm `timedelta` is imported (`from datetime import timedelta` — check the imports; add if missing). In `async_unload_entry`, mirror how `reconcile_unsub` is unsubscribed and do the same for `lz_stale_unsub`.

- [ ] **Step 3: Verify** — `ast.parse` both files; `python3 -m pytest tests/ -q`.

- [ ] **Step 4: Commit**

```bash
git add custom_components/casa/sensor.py custom_components/casa/__init__.py
git commit -m "feat(location): per-device zone sensor and staleness sweep"
```

---

### Task 5: Provisioning payload carries the zone config

**Files:**
- Modify: `custom_components/casa/__init__.py`

**Interfaces:**
- Consumes: `lz_data`; the v2 provisioning payload dict built near line 3303 (the JSON object containing the `"wireguard": {...}` key).
- Produces: a sibling key in that payload:

```python
                "location_zones": {
                    "anchors": lz_anchors,
                    "config_version": lz_version,
                } if lz_anchors else None,
```

- [ ] **Step 1: Implement**

In the payload-builder function containing the `"wireguard": {...}` block (line ~3303), before the payload dict is assembled, resolve:

```python
        lz_data = hass.data.get(DOMAIN, {}).get("lz_data", {})
        lz_anchors = lz_data.get("anchors", [])
        lz_version = lz_data.get("config_version", "")
```

then add the `"location_zones"` key next to `"wireguard"` as above. If the builder strips `None` values elsewhere, follow that idiom; otherwise emit the key only when `lz_anchors` is non-empty (wrap in a conditional `payload["location_zones"] = {...}` after dict construction — match surrounding style).

- [ ] **Step 2: Verify** — `ast.parse`; pytest still green.

- [ ] **Step 3: Commit**

```bash
git add custom_components/casa/__init__.py
git commit -m "feat(location): include zone config in v2 provisioning payload"
```

---

### Task 6: Panel — API methods, Location Zones pane with Leaflet map

**Files:**
- Create: `custom_components/casa/panel/vendor/leaflet.js` and `custom_components/casa/panel/vendor/leaflet.css` (vendored Leaflet 1.9.x, downloaded from unpkg: `https://unpkg.com/leaflet@1.9.4/dist/leaflet.js` and `leaflet.css`)
- Create: `custom_components/casa/panel/views/location-zones.js`
- Modify: `custom_components/casa/panel/api.js` (add `getLocationZones()` / `saveLocationZones(config)` next to the WireGuard methods, with the same version-skew guard used by `saveProvisionTemplate`)
- Modify: `custom_components/casa/panel/views/settings.js` (render the new card and lazy-load the module via the panel's existing `app.loadModule` pattern — see how `device-editor.js` loads `views/username-utils.js`)

**Interfaces:**
- Consumes: `GET/PUT /api/casa/admin/location_zones` (Task 2 shapes; PUT response `{status, config_version, queued}`); panel idioms: `createView(app)` contract (documented at the top of `panel/app.js`), `ui.toast`, `ui.errMsg`, `esc`.
- Produces: a "Location Zones" card inside Settings: Leaflet map (OSM tiles `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, attribution "© OpenStreetMap contributors"), draggable marker per anchor, one `L.circle` per ring redrawn live from a radius number-input, anchor add/remove + name field, ring add/remove/reorder + label field, "Use HA home location" button seeding from `app.hass.config.latitude/longitude`, `stale_after_minutes` number field, Save button.

- [ ] **Step 1: Vendor Leaflet** — download the two dist files into `panel/vendor/`; do not modify them. Confirm the panel serves static files from its directory (check how `app.js`/`version.js` are served — the panel registers a static path; mirror whatever URL prefix the existing panel assets use, and load leaflet.css by injecting a `<link>` into the shadow root, leaflet.js via dynamic `import()` or a script-tag fallback the module pattern supports; Leaflet is a classic script exposing global `L`, so fetch+eval is NOT acceptable — append a `<script src>` to the shadow host document and await its `load` event).
- [ ] **Step 2: Client-side validation mirrors** — ascending radii per anchor, non-empty unique labels, reserved `away`/`unknown` rejected, total rings ≤ 18, lat/long ranges; render inline errors and disable Save while invalid.
- [ ] **Step 3: Save flow** — `saveLocationZones` PUT; on success toast `Saved — pushing to ${res.queued} device${res.queued===1?"":"s"}`; on failure `ui.errMsg`. Version-skew: same refusal message pattern as `saveProvisionTemplate`.
- [ ] **Step 4: Manual verification** — deploy to the dev HA (user runs `deploy.sh` or copies the component; note in the task report if not run), open Settings, confirm: map renders, pin drag updates lat/long fields, ring circle tracks its input, invalid states block Save, Save round-trips (re-open shows saved values).
- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/panel/
git commit -m "feat(location): panel zone editor with Leaflet map picker"
```

---

### Task 7: Panel device surfaces + admin API fields + version bump

**Files:**
- Modify: `custom_components/casa/__init__.py` (admin devices API serialization — the dicts at lines ~974 and ~1014 that already carry `wireguard_configured`: add the four `location_*` fields)
- Modify: `custom_components/casa/panel/views/devices.js` (zone chip beside the WireGuard chips: green when state contains `: `, neutral `away`, amber `unknown` with `location_reason` in the `title` tooltip)
- Modify: `custom_components/casa/panel/views/device-editor.js` (add the four fields to `RECORD_KEYS` (line ~10) and to the info grid beside the wireguard chips (~line 674/1099); render `location_config_version` amber when it differs from the summary's current zone version if available, else plain)
- Modify: `custom_components/casa/const.py` (`CASA_VERSION = "26.08.30"`) and `custom_components/casa/panel/version.js` (same value)

**Interfaces:**
- Consumes: serialized device fields from this task's API change; `triChip`/chip helpers already in those views (mirror the wireguard chip rendering).
- Produces: visible zone state everywhere devices are listed.

- [ ] **Step 1: Implement all four file changes** (each is a small, pattern-mirroring edit — read the surrounding code first, match it exactly).
- [ ] **Step 2: Verify** — `ast.parse` on `__init__.py`; `node --check panel/views/devices.js panel/views/device-editor.js panel/version.js` (or `npx --yes esbuild --bundle=false` if node lacks module support for `--check`; any syntax validation is acceptable).
- [ ] **Step 3: Commit**

```bash
git add custom_components/casa
git commit -m "feat(location): device zone chips, editor fields, version bump 26.08.30"
```

---

### Task 8: iOS — `LocationZoneConfig` model + `ZoneEvaluator` (pure logic + CLI tests)

**Files:**
- Create: `casa-mobile-app/casa-ios/Casa/LocationZoneConfig.swift`
- Create: `casa-mobile-app/casa-ios/Casa/ZoneEvaluator.swift`
- Create: `casa-mobile-app/casa-ios/zone-evaluator-tests.swift` (standalone assertion script, NOT part of the app target — lives outside `Casa/`)

**Interfaces:**
- Produces:

```swift
struct LocationZoneRing: Codable, Equatable {
    let label: String
    let radiusM: Double
    enum CodingKeys: String, CodingKey { case label; case radiusM = "radius_m" }
}

struct LocationZoneAnchor: Codable, Equatable {
    let id: String
    let name: String
    let latitude: Double
    let longitude: Double
    let rings: [LocationZoneRing]
}

struct LocationZoneConfig: Codable, Equatable {
    let anchors: [LocationZoneAnchor]
    let configVersion: String
    enum CodingKeys: String, CodingKey { case anchors; case configVersion = "config_version" }
}

enum ZoneEvaluator {
    /// Innermost containing ring across all anchors → "<anchor>: <label>";
    /// outside everything → "away". Ties (equal radius): first anchor in
    /// config order wins. Pure math — no CoreLocation import.
    static func evaluate(config: LocationZoneConfig, latitude: Double, longitude: Double) -> String
    /// Haversine distance in meters.
    static func distanceMeters(lat1: Double, lon1: Double, lat2: Double, lon2: Double) -> Double
}
```

- [ ] **Step 1: Write the test script** (`zone-evaluator-tests.swift`) — plain `assert`s covering: inside inner ring → `"House: home"`; between rings → `"House: nearby"`; outside all → `"away"`; two anchors, point inside both → smaller containing radius wins; equal radii tie → first anchor; empty anchors → `"away"`; haversine sanity (Vancouver→Seattle ≈ 192–195 km). End with `print("ALL PASS")`.
- [ ] **Step 2: Run to verify it fails**: `cd casa-mobile-app/casa-ios && swift zone-evaluator-tests.swift Casa/ZoneEvaluator.swift Casa/LocationZoneConfig.swift` — expected: compile error (files missing). (If `swift` multi-file invocation needs it, use `swiftc ... -o /tmp/zt && /tmp/zt`.)
- [ ] **Step 3: Implement both source files.** Haversine with Earth radius 6_371_000 m. `evaluate` collects `(radius, anchorIndex, "\(anchor.name): \(ring.label)")` for every ring whose radius ≥ distance to its anchor, sorts by `(radius, anchorIndex)`, returns first, else `"away"`.
- [ ] **Step 4: Run tests** — expected `ALL PASS`.
- [ ] **Step 5: Verify app still builds**: `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project Casa.xcodeproj -scheme Casa -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build -quiet` — expected exit 0. (No commit — not a git repo.)

---

### Task 9: iOS — report encryption + `LocationZoneManager`

**Files:**
- Create: `casa-mobile-app/casa-ios/Casa/CasaReportCrypto.swift`
- Create: `casa-mobile-app/casa-ios/Casa/LocationZoneManager.swift`

**Interfaces:**
- Consumes: Task 8 types; `CasaDecryptor.decryptUpdatePush` as the crypto reference (`Casa/CasaDecryptor.swift:280-303` — same HKDF/AES-GCM, mirrored direction); `AppState.shared`-style access pattern (check how other singletons get `deviceKey`/`deviceId`/`serverURL` — `AppState` is an `ObservableObject` injected via environment; the manager needs the values, so give it stored copies set from `configure(appState:)` called at app init, or read the same UserDefaults keys `casa_device_key` / persisted `deviceId` — follow whatever `ConnectionMonitor` does to reach app state).
- Produces:

```swift
enum CasaReportCrypto {
    /// HKDF-SHA256(ikm: utf8(deviceKey), salt: utf8(deviceId), info: "casa-report-v1")
    /// → AES-256-GCM combined (nonce||ct||tag), base64. Mirror of the server's
    /// decrypt_report_payload; distinct info string from the push path.
    static func encryptReport(_ plaintext: Data, deviceKey: String, deviceId: String) -> String?
}

final class LocationZoneManager: NSObject, CLLocationManagerDelegate, ObservableObject {
    static let shared = LocationZoneManager()
    @Published private(set) var currentState: String   // last computed state string
    @Published private(set) var permissionOK: Bool
    var pendingPermissionExplainer: Bool               // set when config arrives with .notDetermined

    /// SINGLE write path (per the 2026-08-30 stale-config lesson): persists to
    /// the App Group (key "casa_location_zones_config", suite "group.com.kelby.casa"),
    /// updates memory, re-registers regions, then requestLocation() → evaluate → report.
    /// Pass nil / empty anchors to tear everything down.
    func applyLocationConfig(_ config: LocationZoneConfig?)
    /// Two-step permission request (WhenInUse → Always).
    func requestPermissions()
    /// Called from heartbeat build: returns (state, reason, configVersion).
    func heartbeatFields() -> (state: String?, reason: String?, configVersion: String?)
    func wipe()   // clear persisted config, unregister regions, reset state
}
```

- [ ] **Step 1: Implement `CasaReportCrypto`** using CryptoKit exactly like `decryptUpdatePush` but `AES.GCM.seal(_:using:nonce:)` with a fresh 12-byte nonce and `sealed.combined` base64-encoded, info `"casa-report-v1"`.
- [ ] **Step 2: Implement `LocationZoneManager`:**
  - Own `CLLocationManager` (`allowsBackgroundLocationUpdates` NOT needed for region monitoring; `desiredAccuracy = kCLLocationAccuracyHundredMeters`).
  - Region registration: remove all monitored regions whose identifier has prefix `casa-zone:`, then for each anchor/ring register `CLCircularRegion(center:radius:identifier: "casa-zone:\(anchor.id):\(ringIndex)")` with `notifyOnEntry = true, notifyOnExit = true`. Clamp radius to `manager.maximumRegionMonitoringDistance`.
  - `didEnterRegion`/`didExitRegion`/`didDetermineState` (identifier prefix-filtered) → `requestLocation()`.
  - `didUpdateLocations` → `ZoneEvaluator.evaluate` → if result ≠ last **sent** state, build `{"state": result, "reason": null-or-reason, "config_version": cfg.configVersion, "ts": Int(Date().timeIntervalSince1970)}`, encrypt, POST `{"device_id":…, "payload":…}` to `serverURL + "/api/casa/location_report"` with a 8 s `URLSession` timeout; on HTTP 200 record it as sent. Failures: log via `tlog`, keep last-sent unchanged so the heartbeat path reconciles.
  - `didFailWithError` → state `unknown` reason `no_fix` (report via same change-only path).
  - `locationManagerDidChangeAuthorization`: `.authorizedAlways` → re-register + `requestLocation()`; anything else → state `unknown` reason `permission_denied` (change-only report), `permissionOK` updated.
  - `requestPermissions()` → `requestWhenInUseAuthorization()` then, in the authorization callback when `.authorizedWhenInUse`, `requestAlwaysAuthorization()`.
  - Cold start: `init` loads config from the App Group and, if non-empty and authorized, re-registers regions (registration is idempotent).
  - No config / empty anchors: unregister all `casa-zone:` regions, `heartbeatFields()` returns `(nil, nil, nil)`.
- [ ] **Step 3: Build gate**: same `xcodebuild` command as Task 8, exit 0.

---

### Task 10: iOS — wiring: update apply, provisioning, wipe, heartbeat JS, app init

**Files:**
- Modify: `casa-mobile-app/casa-ios/Casa/ContentView.swift` (new `("location", "update")` case in `applyUpdateEntry` (~line 1295); provisioning-payload handling where `wireguardConfigEncoded` is applied (~line 1710/1749) gains parsing of the payload's `location_zones` object)
- Modify: `casa-mobile-app/casa-ios/Casa/URLParser.swift` (v2 JSON payload parse: carry `location_zones` through — find where the v2 JSON fields (`p.wireguard.config` etc., ~line 131) are mapped and add `locationZonesJSON: String?` (the raw sub-object re-serialized) to `ProvisioningData`)
- Modify: `casa-mobile-app/casa-ios/Casa/AppState.swift` (`wipeAll` calls `LocationZoneManager.shared.wipe()`)
- Modify: `casa-mobile-app/casa-ios/Casa/FullScreenWebView.swift` (heartbeat JS body (~line 1815) gains `location_state`, `location_reason`, `location_config_version` injected from `LocationZoneManager.shared.heartbeatFields()`; omit keys when nil — build the three lines into the interpolated JS the same way `wireguard_configured` is)
- Modify: `casa-mobile-app/casa-ios/Casa/CasaApp.swift` (touch `LocationZoneManager.shared` in init so the delegate is live before iOS delivers a relaunch region event)

**Interfaces:**
- Consumes: Task 9's `applyLocationConfig`, `heartbeatFields`, `wipe`, `pendingPermissionExplainer`; the update payload shape `{"anchors": [...], "config_version": "..."}` (decode into `LocationZoneConfig` — note the payload carries `anchors` + `config_version` at the top level, matching `LocationZoneConfig`'s coding keys).
- Produces: end-to-end apply path: push/pull update → `applyLocationConfig`; provisioning → same; heartbeat carries the three fields.

- [ ] **Step 1: `applyUpdateEntry` case** — decode the `JSONValue` payload to `Data` (there is an existing pattern for re-serializing `JSONValue` in `applyProfileUpdate`'s handling; if none exists for whole-payload, serialize via `JSONSerialization` from the `JSONValue`'s object form the same way `applyWireguardUpdate` extracts fields) → `JSONDecoder().decode(LocationZoneConfig.self, ...)` → on success: `LocationZoneManager.shared.applyLocationConfig(config)`; if `CLLocationManager.authorizationStatus == .notDetermined`, set `pendingPermissionExplainer = true`; return `true`. Decode failure → `tlog` + return `false`.
- [ ] **Step 2: Provisioning path** — where the wireguard config from provisioning is applied, decode `locationZonesJSON` if present and call the same `applyLocationConfig`. Empty/absent → no-op (don't tear down an existing config during re-provisioning unless the payload explicitly carries an empty `anchors` list).
- [ ] **Step 3: Heartbeat JS + wipe + app init** — as listed in Files. For the heartbeat, nil fields must be omitted from the JSON body (send nothing rather than nulls).
- [ ] **Step 4: Build gate** — `xcodebuild`, exit 0.

---

### Task 11: iOS — permission UX + settings row + Info.plist

**Files:**
- Modify: `casa-mobile-app/casa-ios/Casa/OnboardingView.swift` (location step shown only when provisioning data carries zones: explainer copy — "Casa reports only a zone name like 'home' or 'away' to your Home Assistant. Your GPS location never leaves this device." — with Allow → `LocationZoneManager.shared.requestPermissions()` and Skip)
- Modify: `casa-mobile-app/casa-ios/Casa/SettingsSheet.swift` (new "Location" row group: current zone state from `LocationZoneManager.shared.currentState`, permission status, and when permission is the blocker a button deep-linking `UIApplication.openSettingsURLString`; plus the explainer sheet for existing devices, presented when `pendingPermissionExplainer` is set — mirror the pending-WireGuard sheet pattern at `SettingsSheet.swift:763-807`)
- Modify: `casa-mobile-app/casa-ios/Casa/ContentView.swift` or wherever foreground transitions are observed (present the explainer once on foreground when `pendingPermissionExplainer` — follow how other pending prompts are surfaced; SettingsSheet's pending pattern is acceptable if a global sheet is intrusive)
- Modify: Xcode build settings (`Casa.xcodeproj/project.pbxproj`): add `INFOPLIST_KEY_NSLocationAlwaysAndWhenInUseUsageDescription = "Casa reports only a zone name (like home or away) to your Home Assistant. Your GPS location never leaves this device.";` to the Casa app target's Debug and Release configs (lines ~502-590, beside the existing `INFOPLIST_KEY_NSLocationWhenInUseUsageDescription`), and update that existing WhenInUse description to mention zones too.
- Modify: `casa-mobile-app/casa-ios/Casa/Info.plist` if it carries its own `NSLocationWhenInUseUsageDescription` (line ~41) — keep plist and build-setting copies consistent.

**Interfaces:**
- Consumes: Task 9/10 manager API.
- Produces: complete permission UX per spec.

- [ ] **Step 1: Implement the four surfaces.** Match each file's existing structure (OnboardingView steps are an enum/sequence — read it before adding a step; the location step is skippable and only appears when zones were provisioned).
- [ ] **Step 2: Build gate** — `xcodebuild` exit 0.
- [ ] **Step 3: Manual test plan note** — emit (in the task report) the on-device checklist: grant/deny/revoke permission → sensor shows `unknown`/`permission_denied`; cross rings → state changes; kill app and cross → relaunch reports; airplane-mode cross → next heartbeat reconciles.

---

### Task 12: Final review pass

- [ ] **Step 1:** Re-read the spec end to end; verify every requirement maps to landed code (use the spec's decision table as the checklist).
- [ ] **Step 2:** `python3 -m pytest tests/ -q` green; `xcodebuild` (device destination) exit 0; `git -C casa-provisioner log --oneline` shows one commit per server task.
- [ ] **Step 3:** Grep server code for accidental location leakage: `grep -rn "latitude\|longitude" custom_components/casa/__init__.py` — hits must only be in zone-config storage/validation/panel-serving paths, never in device-record writes or report handling.

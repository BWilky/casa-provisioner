# Provisioning Parity (provisioner + card) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the casa Home Assistant integration a written, tested provisioning protocol; add the device self-deprovision endpoint and single-use links the app needs; deliver the QR inline; align versions; and bring the card back into parity.

**Architecture:** All server work is in `custom_components/casa/__init__.py` (one large file; new logic is added as small module-level helpers so plain pytest can import and test them through the existing `tests/conftest.py` stubs), `const.py`, `config_flow.py`, and the panel JS. The card is a separate repo (`casa-card`) with one file. Documentation lives in `docs/`.

**Tech Stack:** Python 3.14 locally, Home Assistant 2026.9 on the Fairwater test host, `cryptography` (RSA-OAEP, AES-GCM), `qrcode`+Pillow, pytest with the repo's `conftest.py` that stubs `homeassistant`, `aiohttp`, `qrcode` when absent. Panel and card are ES modules with no build step.

**Spec:** `docs/superpowers/specs/2026-09-30-provisioning-parity-design.md`

## Global Constraints

- Provisioner commands run from `/Users/bryce/Documents/casa/casa-provisioner`; tests: `python3 -m pytest -q tests` (baseline 16 passed). Card commands run from `/Users/bryce/Documents/casa/casa-card`.
- Versions: `CASA_VERSION = "26.09.30"` in `custom_components/casa/const.py`, `PANEL_VERSION = "26.09.30"` in `custom_components/casa/panel/version.js`, `"version": "26.09.30"` in `custom_components/casa/manifest.json`, `CARD_VERSION = "26.09.30"` in `casa-card/index.js`.
- v2 payload `v` stays `2`; `server_version` is additive and placed immediately after `v`.
- Link grammar (v2): `hascasa://setup?data=<P>` and `https://bonjour.casa/setup?d=<P>`, `P` = padding-stripped base64url, never percent-encoded. v1 grammar frozen: `hascasa://setup?data=` + `urllib.parse.quote(P)` and `https://bonjour.casa/setup?d=` + `urllib.parse.quote(P, safe='')`.
- New endpoint: `POST /api/casa/deprovision`, body `{"device_id"}`, 400 missing id, 404 for anything not owned by the caller, 200 `{"status":"success","access_revoked":bool}`.
- Self-deprovision never deletes the HA user.
- The production key `casa_public.pem` is never used to generate committed vectors; vectors use a committed throwaway pair.
- Commit: `git add <files> && git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "<msg>"`. No attribution lines.
- Never print passwords, site keys, or device keys in logs or tests beyond obvious test placeholders.
- Deploys go only to Fairwater (192.168.1.21) via `deploy.sh`; never Barnabas.

## Review Focus

1. A device id that exists under a *different* user must get 404 from `/api/casa/deprovision`, never 200 and never a purge. Pinned in Task 4.
2. A deleted managed user (`deleted: true`) must not be able to deprovision through its stale record. Pinned in Task 4.
3. `password_scramble=false` must leave a redeemed link usable (no scramble on redemption). Pinned in Task 5.
4. `casa_code_redeemed` must fire before the scramble so the card's success pane sees it even if the scramble raises. Pinned in Task 5.
5. `relay_url()` with an option value carrying a trailing slash must not produce `//send`. Pinned in Task 7.

---

### Task 1: Version alignment with an enforcing test

**Files:**
- Modify: `custom_components/casa/const.py` (`CASA_VERSION`)
- Modify: `custom_components/casa/panel/version.js` (`PANEL_VERSION`)
- Modify: `custom_components/casa/manifest.json` (`version`)
- Create: `tests/test_versions.py`

**Interfaces:**
- Produces: the three version strings equal `"26.09.30"`. Task 8 (card) uses the same string.

- [ ] **Step 1: Write the failing test**

`tests/test_versions.py`:

```python
"""The three version strings must move together (see memory: casa version handshake)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "casa"


def _const_version():
    text = (ROOT / "const.py").read_text()
    return re.search(r'^CASA_VERSION\s*=\s*"([^"]+)"', text, re.M).group(1)


def _panel_version():
    text = (ROOT / "panel" / "version.js").read_text()
    return re.search(r'PANEL_VERSION\s*=\s*"([^"]+)"', text).group(1)


def _manifest_version():
    return json.loads((ROOT / "manifest.json").read_text())["version"]


def test_versions_agree():
    assert _const_version() == _panel_version() == _manifest_version()


def test_version_is_dated_release_format():
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", _const_version())
```

- [ ] **Step 2: Run it, expect failure**

Run: `python3 -m pytest -q tests/test_versions.py`
Expected: `test_versions_agree` FAILS (`"26.08.30" == "26.08.30" == "1.0.0"` is False).

- [ ] **Step 3: Bump all three**

`const.py`: `CASA_VERSION = "26.09.30"`. `panel/version.js`: `export const PANEL_VERSION = "26.09.30";`. `manifest.json`: `"version": "26.09.30",`.

- [ ] **Step 4: Run, expect pass**

Run: `python3 -m pytest -q tests`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/const.py custom_components/casa/panel/version.js custom_components/casa/manifest.json tests/test_versions.py
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "chore: version 26.09.30 across const, panel and manifest, enforced by test"
```

---

### Task 2: Extract `build_links`, add `server_version`, mirror in the panel preview

**Files:**
- Modify: `custom_components/casa/__init__.py` — new module-level `build_links` near `_encrypt_payload_hybrid` (search for `def _encrypt_payload_hybrid`); replace the two inline link constructions inside `_provision_internal` (search for `deep_link = f"hascasa://setup?data={urllib.parse.quote(final_payload)}"` and `deep_link = f"hascasa://setup?data={final_payload}"`); add `server_version` to the v2 `profile` dict (search for `"v": 2,`).
- Modify: `custom_components/casa/panel/payload-preview.js` (`buildV2PayloadPreview`, search for `v: 2,`).
- Create: `tests/test_links.py`

**Interfaces:**
- Produces: `build_links(payload: str, version: int) -> tuple[str, str]` returning `(deep_link, universal_link)`. Task 3's vectors call it.

- [ ] **Step 1: Write the failing tests**

`tests/test_links.py`:

```python
from custom_components.casa import build_links


def test_v2_links_are_raw_base64url():
    payload = "Aj-gk_Zj"  # base64url chars incl. '-' and '_'
    deep, universal = build_links(payload, 2)
    assert deep == "hascasa://setup?data=Aj-gk_Zj"
    assert universal == "https://bonjour.casa/setup?d=Aj-gk_Zj"


def test_v1_links_keep_legacy_quoting():
    payload = "ab+/cd=="  # standard base64 alphabet
    deep, universal = build_links(payload, 1)
    assert deep == "hascasa://setup?data=ab%2B/cd%3D%3D"
    assert universal == "https://bonjour.casa/setup?d=ab%2B%2Fcd%3D%3D"


def test_unknown_version_rejected():
    import pytest
    with pytest.raises(ValueError):
        build_links("x", 3)
```

- [ ] **Step 2: Run, expect ImportError**

Run: `python3 -m pytest -q tests/test_links.py`
Expected: FAIL, `cannot import name 'build_links'`.

- [ ] **Step 3: Add the helper**

In `__init__.py`, directly after the `_encrypt_payload_hybrid` function ends, add:

```python
def build_links(payload: str, version: int) -> tuple[str, str]:
    """Return (deep_link, universal_link) for an encoded provisioning payload.

    v2 payloads are padding-stripped base64url and go into the URL untouched.
    v1 payloads are standard base64 and keep their historical quoting: the
    hascasa:// link leaves '/' raw, the https link percent-encodes it too.
    Documented in docs/provisioning-protocol.md; the v1 rules are frozen.
    """
    if version == 2:
        return (
            f"hascasa://setup?data={payload}",
            f"{UNIVERSAL_LINK_SETUP_URL}?d={payload}",
        )
    if version == 1:
        return (
            f"hascasa://setup?data={urllib.parse.quote(payload)}",
            f"{UNIVERSAL_LINK_SETUP_URL}?d={urllib.parse.quote(payload, safe='')}",
        )
    raise ValueError(f"Unsupported payload version: {version}")
```

Then in `_provision_internal` replace the v1 pair of lines

```python
            deep_link = f"hascasa://setup?data={urllib.parse.quote(final_payload)}"
            # v1 payloads are standard base64 ('+', '/', '=') so must be percent-encoded.
            universal_link = f"{UNIVERSAL_LINK_SETUP_URL}?d={urllib.parse.quote(final_payload, safe='')}"
```

with `deep_link, universal_link = build_links(final_payload, 1)`, and the v2 pair

```python
            deep_link = f"hascasa://setup?data={final_payload}"
            # v2 payloads are padding-stripped base64url — already URL-safe.
            universal_link = f"{UNIVERSAL_LINK_SETUP_URL}?d={final_payload}"
```

with `deep_link, universal_link = build_links(final_payload, 2)`.

- [ ] **Step 4: Add `server_version`**

In the v2 `profile` dict change the first two entries to:

```python
                "v": 2,
                "server_version": CASA_VERSION,
```

`CASA_VERSION` is imported locally elsewhere (`from .const import CASA_VERSION` inside the summary view); add it to the module-level `from .const import (...)` list at the top of `__init__.py` instead, and leave the local import alone.

In `panel/payload-preview.js` change the returned object's first line `v: 2,` to:

```js
    v: 2,
    server_version: ctx.serverVersion || "(this server's version)",
```

and add after `push_notifications: normalizePush(f.push_notifications),`:

```js
    require_alias: toBool(f.require_alias),
```

and, after the `connect_wifi` block (inside the return object, last entry):

```js
    ...(ctx.locationZones ? { location_zones: ctx.locationZones } : {}),
```

Update the doc comment above `buildV2PayloadPreview` to list `serverVersion?` and `locationZones?` in `ctx`. Find the panel call sites with `grep -rn "buildV2PayloadPreview(" custom_components/casa/panel` and pass `serverVersion: <the summary's version field the view already holds>` where a summary object is in scope; where it is not, pass nothing (the placeholder string renders).

- [ ] **Step 5: Run, expect pass**

Run: `python3 -m pytest -q tests`
Expected: 21 passed.

- [ ] **Step 6: Commit**

```bash
git add custom_components/casa/__init__.py custom_components/casa/panel/payload-preview.js custom_components/casa/panel/views tests/test_links.py
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "feat: build_links helper, server_version in v2 payload, preview parity"
```

---

### Task 3: Protocol vectors and the contract document

**Files:**
- Create: `tests/vectors/provisioning-v2.json`
- Create: `tests/vectors/generate_vectors.py` (dev tool, not a test)
- Create: `tests/test_protocol_vectors.py`
- Create: `docs/provisioning-protocol.md`

**Interfaces:**
- Consumes: `build_links` (Task 2), `_encrypt_payload_hybrid` (existing).
- Produces: the vectors file shape `{plaintext, test_public_key_pem, test_private_key_pem, envelope_b64url, deep_link, universal_link}`. Sub-project 3 (iOS tests) consumes this file.

- [ ] **Step 1: Write the generator**

`tests/vectors/generate_vectors.py`:

```python
"""Regenerate provisioning-v2.json. Run once; commit the output.

Uses a throwaway RSA-2048 pair generated here, NEVER the production
casa_public.pem, so the private half can be committed for the iOS tests.
"""
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from custom_components.casa import _encrypt_payload_hybrid, build_links  # noqa: E402

OUT = Path(__file__).with_name("provisioning-v2.json")

PLAINTEXT = {
    "v": 2,
    "server_version": "26.09.30",
    "server_url": "http://192.0.2.10:8123",
    "username": "vector-user",
    "password": "vector-pass",
    "site_id": "0123456789abcdef0123456789abcdef",
    "pin": "1234",
    "default_dashboard": "/lovelace/0",
    "welcome_url": "",
    "immersive_level": "1",
    "theme_color_mode": "inherit",
    "custom_color": "#000000",
    "session_expiration": 0,
    "expiration": 0,
    "cache_control_hours": "48",
    "allowed_pages": "/*",
    "allowed_wifi": "",
    "require_alias": False,
    "push_notifications": "false",
    "wireguard": {"allowed": False, "config": "", "excluded_wifi": ""},
    "connect_wifi": {"ssid": "", "password": ""},
}


def main():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    plaintext = json.dumps(PLAINTEXT, separators=(",", ":"))
    envelope = _encrypt_payload_hybrid(plaintext, pub_pem.encode())
    deep, universal = build_links(envelope, 2)
    OUT.write_text(json.dumps({
        "note": "Throwaway test key pair. Regenerate with generate_vectors.py.",
        "plaintext": PLAINTEXT,
        "test_public_key_pem": pub_pem,
        "test_private_key_pem": priv_pem,
        "envelope_b64url": envelope,
        "deep_link": deep,
        "universal_link": universal,
    }, indent=2) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
```

Run: `python3 tests/vectors/generate_vectors.py` and confirm `tests/vectors/provisioning-v2.json` exists. (The conftest stubs are not loaded outside pytest; if the import of `custom_components.casa` fails because `homeassistant` is absent, run it as `python3 -m pytest -q -p no:cacheprovider tests/vectors/generate_vectors.py` is NOT valid — instead prepend `import tests.conftest` before the casa import in the script, which installs the stubs, and keep that line.)

- [ ] **Step 2: Write the failing test**

`tests/test_protocol_vectors.py`:

```python
import base64
import json
import zlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.casa import build_links

VEC = json.loads((Path(__file__).parent / "vectors" / "provisioning-v2.json").read_text())


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_envelope_shape():
    env = VEC["envelope_b64url"]
    assert "=" not in env and "+" not in env and "/" not in env
    raw = _b64url_decode(env)
    assert raw[0] == 0x02
    assert len(raw) > 1 + 256 + 12 + 16


def test_envelope_round_trips_with_test_key():
    raw = _b64url_decode(VEC["envelope_b64url"])
    priv = serialization.load_pem_private_key(VEC["test_private_key_pem"].encode(), password=None)
    aes_key = priv.decrypt(
        raw[1:257],
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    nonce, ct = raw[257:269], raw[269:]
    compressed = AESGCM(aes_key).decrypt(nonce, ct, None)
    plaintext = zlib.decompress(compressed, -15).decode()
    assert json.loads(plaintext) == VEC["plaintext"]


def test_links_match_vectors():
    deep, universal = build_links(VEC["envelope_b64url"], 2)
    assert deep == VEC["deep_link"]
    assert universal == VEC["universal_link"]


def test_plaintext_key_order_matches_server():
    keys = list(VEC["plaintext"].keys())
    assert keys[:2] == ["v", "server_version"]
    assert keys[-2:] == ["wireguard", "connect_wifi"]
```

- [ ] **Step 3: Run, expect pass** (the vectors were generated by the real helper, so this passes first time; the RED here is the pre-generation `FileNotFoundError` if you run before Step 1)

Run: `python3 -m pytest -q tests`
Expected: 25 passed.

- [ ] **Step 4: Write the contract document**

`docs/provisioning-protocol.md` with these sections, filled from the code (cite `__init__.py` symbols, not line numbers):

1. **Overview** — who produces links (provisioner), who consumes (iOS app), where the card and panel fit.
2. **Links** — the grammar table for v2 and frozen v1 (copy from `build_links`), and the rule "the app accepts `data` and `d` on either scheme".
3. **v2 envelope** — the byte layout from `_encrypt_payload_hybrid`'s docstring; key `casa_public.pem`; no AAD; raw DEFLATE.
4. **v2 payload fields** — one table row per key in the exact server order, with type and meaning: `v`, `server_version`, `server_url`, `username`, `password`, `site_id`, `pin`, `default_dashboard`, `welcome_url`, `immersive_level`, `theme_color_mode`, `custom_color`, `session_expiration`, `expiration`, `cache_control_hours`, `allowed_pages`, `allowed_wifi`, `require_alias`, `push_notifications`, `wireguard{allowed,config,excluded_wifi}`, `connect_wifi{ssid,password}`, optional `location_zones{anchors,config_version}`.
5. **Version policy** — verbatim from the spec section 2.
6. **Single use** — scramble on first redemption (Task 5), timer fallback.
7. **QR delivery** — `qr_data_uri`; `url_path` deprecated this release.
8. **Device endpoints** — table: `register_device` POST/GET/DELETE, `heartbeat`, `profile_updates` GET/POST, `profile_report`, `location_report` (no bearer; device-key encrypted body), `deprovision` (Task 4). Link to `docs/app-integration.md` for queue mechanics.
9. **Push commands** — the list from the spec section 1.
10. **Relay** — pointer to the relay README "Protocol 1"; how the provisioner probes `/health` (Task 7).
11. **Test vectors** — path and field meanings; "never generated with the production key".

- [ ] **Step 5: Commit**

```bash
git add tests/vectors tests/test_protocol_vectors.py docs/provisioning-protocol.md
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "docs: provisioning protocol contract with committed test vectors"
```

---

### Task 4: Device self-deprovision endpoint

**Files:**
- Modify: `custom_components/casa/__init__.py` — new module-level `_device_owned_by` next to `_find_device_record` (search `def _find_device_record`); new `CasaDeprovisionView` class next to `CasaRegisterDeviceView` (search `class CasaRegisterDeviceView`); register it in the view list (search `hass.http.register_view(CasaRegisterDeviceView`).
- Modify: `docs/app-integration.md` (new section) and `docs/provisioning-protocol.md` (endpoint row).
- Create: `tests/test_deprovision_auth.py`

**Interfaces:**
- Consumes: existing `_purge_device(hass, device_id) -> dict` and `_remove_registry_device(hass, device_id)`.
- Produces: `_device_owned_by(stored_data: dict, user_id: str, device_id: str) -> bool`; `POST /api/casa/deprovision`.

- [ ] **Step 1: Write the failing test**

`tests/test_deprovision_auth.py`:

```python
from custom_components.casa import _device_owned_by


def _data():
    return {
        "users": {
            "u1": {"username": "alice", "devices": {"d1": {}}},
            "u2": {"username": "bob", "devices": {"d2": {}}},
            "u3": {"username": "gone", "deleted": True, "devices": {"d3": {}}},
        },
        "native_devices": {"n1": {"d4": {}}},
    }


def test_managed_user_owns_own_device():
    assert _device_owned_by(_data(), "u1", "d1") is True


def test_other_users_device_is_not_owned():
    assert _device_owned_by(_data(), "u1", "d2") is False


def test_deleted_user_cannot_own():
    assert _device_owned_by(_data(), "u3", "d3") is False


def test_native_user_owns_native_device():
    assert _device_owned_by(_data(), "n1", "d4") is True


def test_unknown_ids_are_false():
    assert _device_owned_by(_data(), "u1", "nope") is False
    assert _device_owned_by(_data(), "nobody", "d1") is False
    assert _device_owned_by({}, "u1", "d1") is False
```

- [ ] **Step 2: Run, expect ImportError**

Run: `python3 -m pytest -q tests/test_deprovision_auth.py`
Expected: FAIL, `cannot import name '_device_owned_by'`.

- [ ] **Step 3: Implement the helper**

After `_find_device_record` in `__init__.py`:

```python
def _device_owned_by(stored_data: dict, user_id: str, device_id: str) -> bool:
    """True when device_id belongs to user_id (managed, not deleted; or native)."""
    users = stored_data.get("users", {}) if stored_data else {}
    entry = users.get(user_id)
    if entry and not entry.get("deleted", False) and device_id in entry.get("devices", {}):
        return True
    native = stored_data.get("native_devices", {}) if stored_data else {}
    return device_id in native.get(user_id, {})
```

- [ ] **Step 4: Implement the view**

After `CasaRegisterDeviceView` class ends, add:

```python
class CasaDeprovisionView(HomeAssistantView):
    """Device-initiated removal of its own record: POST /api/casa/deprovision.

    Auth is the device's own HA session. The caller must own device_id;
    anything else is 404 so other users' device ids are never confirmed.
    Purges the record, unregisters the relay proxy token, revokes this
    device's refresh token and queued updates. The HA user account stays.
    """

    url = "/api/casa/deprovision"
    name = "api:casa:deprovision"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def post(self, request):
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        device_id = str((body or {}).get("device_id", "")).strip()
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        if not _device_owned_by(stored_data, user.id, device_id):
            return self.json({"error": "Device not found"}, status_code=404)

        result = await _purge_device(self.hass, device_id)
        _remove_registry_device(self.hass, device_id)
        store = self.hass.data[DOMAIN]["store"]
        store.async_delay_save(lambda: stored_data, 2.0)
        _LOGGER.info(
            "CASA: Device '%s' self-deprovisioned by user '%s'.",
            device_id, result.get("username") or user.name or user.id,
        )
        return self.json({"status": "success", "access_revoked": bool(result.get("access_revoked"))})
```

Check whether `_purge_device` already schedules the store save (read its body after the token-revoke block); if it does, drop the `store.async_delay_save` lines here to avoid a double save. Register the view: add `hass.http.register_view(CasaDeprovisionView(hass))` right after the `CasaRegisterDeviceView` registration line.

- [ ] **Step 5: Document**

`docs/app-integration.md`: add a section "Self-deprovision" after the `profile_updates` sections with the request/response and the rule about 404. `docs/provisioning-protocol.md`: add the row to the endpoints table.

- [ ] **Step 6: Run, expect pass**

Run: `python3 -m pytest -q tests`
Expected: 30 passed.

- [ ] **Step 7: Commit**

```bash
git add custom_components/casa/__init__.py docs/app-integration.md docs/provisioning-protocol.md tests/test_deprovision_auth.py
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "feat: POST /api/casa/deprovision for device-initiated removal"
```

---

### Task 5: Scramble on first redemption

**Files:**
- Modify: `custom_components/casa/__init__.py` — move `_login_listener` from inside `async_setup_entry` to module level with a `hass` parameter and an `on_redeemed` callback (search `async def _login_listener`); extract `_scramble_and_close` from `_cleanup_sequence` (search `elif event["action"] == "scramble":`); pass the callback at the launch site (search `_login_listener(login_username, target_user.id, known_token_ids, listener_ttl, method)`).
- Create: `tests/test_scramble_on_redeem.py`

**Interfaces:**
- Produces: `async def _login_listener(hass, username, user_id, known_tokens, ttl_seconds, method, on_redeemed=None)`; `async def _scramble_and_close(hass, username, auth_provider) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_scramble_on_redeem.py`:

```python
import asyncio
from types import SimpleNamespace

from custom_components.casa import _login_listener


class _Token:
    def __init__(self, tid):
        self.id = tid
        self.client_name = "Casa"
        self.client_id = "http://x/"
        self.last_used_ip = "192.0.2.1"


class _FakeHass:
    def __init__(self, user):
        self._user = user
        self.fired = []
        self.auth = SimpleNamespace(async_get_users=self._users)
        self.bus = SimpleNamespace(async_fire=lambda name, data: self.fired.append((name, data)))
        self.data = {"casa": {"listeners": {}, "timers": {}}}

    async def _users(self):
        return [self._user]


def _run(listener_coro):
    return asyncio.run(listener_coro)


def test_on_redeemed_called_once_after_event(monkeypatch):
    import custom_components.casa as casa
    monkeypatch.setattr(casa.asyncio, "sleep", _fast_sleep)
    user = SimpleNamespace(id="u1", refresh_tokens={"t1": _Token("t1")})
    hass = _FakeHass(user)
    calls = []

    async def on_redeemed():
        calls.append(len(hass.fired))  # how many events had fired when called

    async def scenario():
        task = asyncio.ensure_future(
            _login_listener(hass, "alice", "u1", {"t1"}, 10, "deep_link", on_redeemed=on_redeemed)
        )
        await asyncio.sleep(0)  # let it start
        user.refresh_tokens["t2"] = _Token("t2")  # the phone logged in
        await task

    _run(scenario())
    assert [n for n, _ in hass.fired] == ["casa_code_redeemed"]
    assert calls == [1]  # called exactly once, and after the event fired


def test_without_callback_only_fires_event(monkeypatch):
    import custom_components.casa as casa
    monkeypatch.setattr(casa.asyncio, "sleep", _fast_sleep)
    user = SimpleNamespace(id="u1", refresh_tokens={"t1": _Token("t1")})
    hass = _FakeHass(user)

    async def scenario():
        task = asyncio.ensure_future(_login_listener(hass, "alice", "u1", {"t1"}, 4, "qr"))
        await asyncio.sleep(0)
        user.refresh_tokens["t2"] = _Token("t2")
        await task

    _run(scenario())
    assert len(hass.fired) == 1


async def _fast_sleep(_seconds):
    await asyncio.sleep(0)
```

Note the listener must return after calling `on_redeemed` (a redeemed single-use link has nothing left to watch), which is what lets `await task` finish in the first test; the second test finishes via the TTL loop with the fast sleep.

- [ ] **Step 2: Run, expect ImportError**

Run: `python3 -m pytest -q tests/test_scramble_on_redeem.py`
Expected: FAIL, `cannot import name '_login_listener'` (it is currently nested).

- [ ] **Step 3: Move the listener to module level and add the callback**

Cut the whole nested `_login_listener` out of `async_setup_entry` and paste it at module level (a good spot is just before `def _find_device_record`), with this signature and body:

```python
async def _login_listener(hass, username, user_id, known_tokens, ttl_seconds, method, on_redeemed=None):
    """Poll for new refresh tokens; fire casa_code_redeemed when one appears.

    on_redeemed: optional coroutine function run once after the first
    redemption event (used to scramble the password so the link is
    single-use). When it is set the listener returns after the first
    redemption; otherwise it keeps reporting until the TTL ends.
    """
    if ttl_seconds <= 0:
        _LOGGER.warning("CASA: Listener for '%s' skipped — TTL is %s.", username, ttl_seconds)
        return
    try:
        elapsed = 0
        poll_interval = 2
        while elapsed < ttl_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            users = await hass.auth.async_get_users()
            user = next((u for u in users if u.id == user_id), None)
            if not user:
                return

            current_tokens = set(user.refresh_tokens.keys())
            new_tokens = current_tokens - known_tokens
            if not new_tokens:
                continue

            for tid in new_tokens:
                token = user.refresh_tokens.get(tid)
                if token:
                    hass.bus.async_fire("casa_code_redeemed", {
                        "username": username,
                        "client_name": token.client_name,
                        "client_id": token.client_id,
                        "token_id": token.id,
                        "ip_address": token.last_used_ip,
                        "redeemed_at": dt_util.now().isoformat(),
                        "method": method,
                    })
                    _LOGGER.info(
                        "CASA EVENT: Code redeemed by '%s' via %s (client: %s, IP: %s).",
                        username, method, token.client_name, token.last_used_ip
                    )
            known_tokens.update(new_tokens)

            if on_redeemed is not None:
                try:
                    await on_redeemed()
                except Exception as err:  # never let a scramble failure kill the listener silently
                    _LOGGER.error("CASA: on_redeemed for '%s' failed: %s", username, err)
                return
    except asyncio.CancelledError:
        pass
```

`dt_util` is already imported at module level (the summary view uses `dt_util.now()`); confirm with `grep -n "dt_util" custom_components/casa/__init__.py | head -3`.

- [ ] **Step 4: Extract the scramble and wire the callback**

Add at module level, near the listener:

```python
async def _scramble_and_close(hass, username: str, auth_provider) -> None:
    """Rotate the account password so the provisioning link is dead, and stop the listener."""
    scrambled_password = generate_random_password()
    auth_provider.data.change_password(username, scrambled_password)
    await auth_provider.data.async_save()
    _LOGGER.info("CASA: Password for %s scrambled.", username)
    listener_task = hass.data[DOMAIN]["listeners"].get(username)
    if listener_task:
        listener_task.cancel()
```

`generate_random_password` is an existing module-level helper (confirm with grep). In `_cleanup_sequence`, replace the body of the `elif event["action"] == "scramble":` branch with `await _scramble_and_close(hass, username, auth_provider)` (note that branch currently cancels the listener keyed by `target_username`; the listener dict is keyed by `target_username` at the launch site — keep that key consistent: use the same variable the launch site uses when you look up the task in `_scramble_and_close`; if the two names differ (`login_username` vs `target_username`), pass the key explicitly as a fourth parameter `listener_key` and use it in both places).

At the launch site replace

```python
        listener_task = hass.async_create_task(
            _login_listener(login_username, target_user.id, known_token_ids, listener_ttl, method)
        )
```

with

```python
        async def _on_redeemed():
            # Single-use link: kill the password the moment it is used, and
            # drop the fallback timer that would have done it later.
            timer = hass.data[DOMAIN]["timers"].pop(target_username, None)
            if timer:
                timer.cancel()
            await _scramble_and_close(hass, login_username, provider)

        listener_task = hass.async_create_task(
            _login_listener(
                hass, login_username, target_user.id, known_token_ids, listener_ttl, method,
                on_redeemed=_on_redeemed if password_scramble else None,
            )
        )
```

`provider` and `password_scramble` are the local names already used when the cleanup task is created (`_cleanup_sequence(login_username, provider, ...)`); confirm with grep and reuse them.

- [ ] **Step 5: Run, expect pass**

Run: `python3 -m pytest -q tests`
Expected: 32 passed.

- [ ] **Step 6: Commit**

```bash
git add custom_components/casa/__init__.py tests/test_scramble_on_redeem.py
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "feat: scramble the password on first redemption so links are single-use"
```

---

### Task 6: QR as a data URI (server + panel)

**Files:**
- Modify: `custom_components/casa/__init__.py` — `_provision_internal`, the `if method == "qr":` block (search `await hass.async_add_executor_job(create_qr_images, deep_link)`) and the `qr` result dict (search `"url_path": f"/local/{final_filename}"`).
- Modify: `custom_components/casa/panel/views/provision.js` (`renderQrResult`) and `custom_components/casa/panel/views/provision-guided.js` (the `<img src="${esc(r.url_path)}"` block).
- Modify: `docs/provisioning-protocol.md` (QR delivery section already says this; confirm wording).

**Interfaces:**
- Produces: `qr` responses gain `qr_data_uri: "data:image/png;base64,..."` and `url_path_deprecated: true`.

- [ ] **Step 1: Server**

Add a module-level helper next to `build_links`:

```python
def _qr_png_data_uri(text: str) -> str:
    """Render text as a PNG QR and return it as a data: URI (no file on disk)."""
    import io
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
```

In the `if method == "qr":` block, after the existing `create_qr_images` executor call, add:

```python
            qr_data_uri = await hass.async_add_executor_job(_qr_png_data_uri, deep_link)
```

and initialise `qr_data_uri = None` before the `if method == "qr":` line. In the `qr` result dict add, after `"url_path": ...`:

```python
                "url_path_deprecated": True,
                "qr_data_uri": qr_data_uri,
```

- [ ] **Step 2: Panel**

`provision.js` `renderQrResult`: change the `<img src="${esc(r.url_path)}"` to `<img src="${esc(r.qr_data_uri || r.url_path)}"`. Same edit in `provision-guided.js`, and change its guard `state.qrChecked && r.url_path` to `state.qrChecked && (r.qr_data_uri || r.url_path)`.

- [ ] **Step 3: Verify**

There is no unit test for the executor path (qrcode is stubbed under pytest); verification is the Fairwater deploy in Task 10. Run the suite anyway: `python3 -m pytest -q tests` → 32 passed. Also `node --check` is not available for ES modules with imports; instead confirm the two JS files still parse by running `python3 - <<'EOF'\nimport re,sys\nfor p in ["custom_components/casa/panel/views/provision.js","custom_components/casa/panel/views/provision-guided.js"]:\n    s=open(p).read(); assert s.count("`")%2==0, p\nprint("ok")\nEOF` (a balanced-backtick sanity check).

- [ ] **Step 4: Commit**

```bash
git add custom_components/casa/__init__.py custom_components/casa/panel/views/provision.js custom_components/casa/panel/views/provision-guided.js
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "feat: return the provisioning QR inline as a data URI; url_path deprecated"
```

---

### Task 7: Relay base URL option and relay protocol probe

**Files:**
- Modify: `custom_components/casa/const.py` — add `CONF_RELAY_BASE_URL = "relay_base_url"`; keep `RELAY_BASE_URL` as the default; delete the derived `RELAY_*_URL` constants and `RELAY_URLS`.
- Modify: `custom_components/casa/config_flow.py` — add the option to both the user step and the options flow (`vol.Optional(CONF_RELAY_BASE_URL, default=RELAY_BASE_URL): str`).
- Modify: `custom_components/casa/__init__.py` — new module-level `relay_url(hass, path) -> str`; replace every use of `RELAY_REGISTER_SITE_URL`, `RELAY_VERIFY_SITE_URL`, `RELAY_UNREGISTER_URL`, `RELAY_RECONCILE_URL`, `RELAY_REMOVE_SITE_URL` with `relay_url(hass, "/register_site")` etc., and each `for url in RELAY_URLS:` loop with `url = relay_url(hass, "/send")` (there are three: `_send_push_to_relay` and two service handlers; `grep -n "RELAY_" custom_components/casa/__init__.py` lists them all, plus the import line); new `_probe_relay(hass) -> None` called from `async_setup_entry` right after `_ensure_site_registration`; admin summary adds `relay_version` and `relay_protocol`.
- Modify: `custom_components/casa/translations/en.json` (label for the new option; copy the pattern of the existing three).
- Modify: `README.md` (one paragraph under configuration).
- Create: `tests/test_relay_url.py`

**Interfaces:**
- Produces: `relay_url(hass, path: str) -> str`; `hass.data[DOMAIN]["relay_version"]`, `hass.data[DOMAIN]["relay_protocol"]`.

- [ ] **Step 1: Write the failing test**

`tests/test_relay_url.py`:

```python
from types import SimpleNamespace

from custom_components.casa import relay_url


def _hass(option=None):
    entry = SimpleNamespace(options={} if option is None else {"relay_base_url": option})
    return SimpleNamespace(data={"casa": {"config_entry": entry}})


def test_default_when_option_absent():
    assert relay_url(_hass(), "/send") == "https://push.bonjour.casa/send"


def test_option_overrides():
    assert relay_url(_hass("http://192.168.1.50:8000"), "/register_site") == "http://192.168.1.50:8000/register_site"


def test_trailing_slash_and_blank_are_normalised():
    assert relay_url(_hass("http://relay.local/"), "/send") == "http://relay.local/send"
    assert relay_url(_hass("   "), "/send") == "https://push.bonjour.casa/send"
```

- [ ] **Step 2: Run, expect ImportError**

Run: `python3 -m pytest -q tests/test_relay_url.py`
Expected: FAIL, `cannot import name 'relay_url'`.

- [ ] **Step 3: Implement**

`const.py`: add `CONF_RELAY_BASE_URL = "relay_base_url"` beside the other `CONF_` names; keep `RELAY_BASE_URL = "https://push.bonjour.casa"`; remove `RELAY_REGISTER_SITE_URL`, `RELAY_VERIFY_SITE_URL`, `RELAY_UNREGISTER_URL`, `RELAY_RECONCILE_URL`, `RELAY_REMOVE_SITE_URL`, `RELAY_URLS`.

`__init__.py`, module level near `_register_site`:

```python
def relay_url(hass, path: str) -> str:
    """Absolute relay URL for path, honouring the per-site relay_base_url option."""
    base = RELAY_BASE_URL
    entry = (hass.data.get(DOMAIN) or {}).get("config_entry")
    if entry is not None:
        configured = str((entry.options or {}).get(CONF_RELAY_BASE_URL, "") or "").strip()
        if configured:
            base = configured
    return base.rstrip("/") + "/" + path.lstrip("/")
```

Confirm `hass.data[DOMAIN]["config_entry"]` is set in `async_setup_entry` (grep `"config_entry"`); if it is stored under another key, use that key in both the helper and the test fixture. Update the import line to import `RELAY_BASE_URL, CONF_RELAY_BASE_URL` and drop the removed names. Replace each call site as listed in Files.

Probe, module level:

```python
async def _probe_relay(hass) -> None:
    """Record the relay's version/protocol from GET /health; never blocks setup."""
    data = hass.data[DOMAIN]
    data["relay_version"] = None
    data["relay_protocol"] = None
    try:
        session = async_get_clientsession(hass)
        async with session.get(relay_url(hass, "/health"), timeout=aiohttp.ClientTimeout(total=5)) as resp:
            body = await resp.json(content_type=None)
    except Exception as err:
        _LOGGER.debug("CASA: relay /health probe failed: %s", err)
        return
    data["relay_version"] = body.get("version")
    data["relay_protocol"] = body.get("protocol")
    if data["relay_protocol"] not in (None, 1):
        _LOGGER.warning(
            "CASA: relay reports protocol %s; this integration knows protocol 1.",
            data["relay_protocol"],
        )
```

`aiohttp` and `async_get_clientsession` are already imported at module level (confirm with grep). Call `await _probe_relay(hass)` immediately after `await _ensure_site_registration(hass, stored_data, store)` in `async_setup_entry`. In `CasaAdminSummaryView.get`, where the response dict has `"version": CASA_VERSION,` add `"relay_version": self.hass.data[DOMAIN].get("relay_version"), "relay_protocol": self.hass.data[DOMAIN].get("relay_protocol"),`.

`config_flow.py`: add `vol.Optional(CONF_RELAY_BASE_URL, default=RELAY_BASE_URL): str` to the user-step schema and, in the options flow, `vol.Optional(CONF_RELAY_BASE_URL, default=self.config_entry.options.get(CONF_RELAY_BASE_URL, RELAY_BASE_URL)): str`. Import the two names from `.const`. Add the label in `translations/en.json` for both `config.step.user.data` and `options.step.init.data` following the existing keys.

README: under configuration options add: "`relay_base_url` — the push relay this site talks to. Default `https://push.bonjour.casa`. Point it at a local or staging relay to test relay changes without touching production; reload the integration after changing it."

- [ ] **Step 4: Run, expect pass**

Run: `python3 -m pytest -q tests`
Expected: 35 passed. Also `grep -n "RELAY_URLS\|RELAY_.*_URL" custom_components/casa/__init__.py` must return nothing.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/const.py custom_components/casa/config_flow.py custom_components/casa/__init__.py custom_components/casa/translations/en.json README.md tests/test_relay_url.py
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "feat: configurable relay base URL and relay protocol probe in the admin summary"
```

---

### Task 8: Card parity (separate repo)

**Files (all in `/Users/bryce/Documents/casa/casa-card`, new branch `card-parity`):**
- Modify: `index.js`
- Modify: `README.md`

**Interfaces:**
- Consumes: `qr_data_uri`, `expires_at` from the provisioner; `casa.provision` service names.

- [ ] **Step 1: Branch**

`cd /Users/bryce/Documents/casa/casa-card && git checkout -b card-parity`

- [ ] **Step 2: Version banner**

At the top of `index.js`, after the three `const LitElement/html/css` lines, add:

```js
const CARD_VERSION = "26.09.30";
console.info(`%c CASA-PROVISION-CARD %c ${CARD_VERSION} `, "color: white; background: #1976d2; font-weight: bold;", "color: #1976d2; background: white; font-weight: bold;");
```

- [ ] **Step 3: Expiry keys and data URI**

In `provisionQR` (search `this.qrData = result.response || result;`):

```js
      if (!this.qrData.qr_data_uri && !this.qrData.url_path) throw new Error(this.qrData.message || "Invalid response: no QR image returned.");

      let expiresAt = this.qrData.expires_at ?? this.qrData.qr_expires_at;
```

In `provisionBLE` (search `let expiresAt = responseData.ble_expires_at;`):

```js
      let expiresAt = responseData.expires_at ?? responseData.ble_expires_at;
```

In the QR pane template (search `<img src="${this.qrData.url_path}"`):

```js
          <img src="${this.qrData.qr_data_uri || this.qrData.url_path}" alt="Provisioning QR Code" />
```

- [ ] **Step 4: Remove the external QR service**

In the `app_links` pane (search `api.qrserver.com`), replace the `${this.appQrUrl ? html\`...\` : html\`...\`}` conditional so that tapping a store icon opens the store link directly and no QR is rendered:

```js
                <p class="casa-subtitle" style="margin-bottom: 24px;">Select your platform to get the app.</p>
                <div class="store-icons">
                  <a class="store-icon ios" href="${this.config.ios_url}" target="_blank" rel="noopener">
                    <ha-icon icon="mdi:apple"></ha-icon>
                    <span>iOS</span>
                  </a>
                  <a class="store-icon android" href="${this.config.android_url}" target="_blank" rel="noopener">
                    <ha-icon icon="mdi:android"></ha-icon>
                    <span>Android</span>
                  </a>
                </div>
                <div class="casa-btn-row" style="justify-content: center; margin-top: 24px;">
                  <button class="casa-btn blue" @click=${this.nextPane}>Skip to Provisioning</button>
                </div>
```

Delete the now-unused `appQrUrl` property and its declaration in `static get properties()` (grep `appQrUrl`). Add `text-decoration: none; color: inherit;` to the `.store-icon` CSS rule so the anchors look like before.

- [ ] **Step 5: Editor duplicate key**

Search `{ entity: { domain: 'sensor', domain: 'text' } }` and change to `{ entity: { domain: ['sensor', 'text'] } }`.

- [ ] **Step 6: README**

Replace every `casa.generate_qr` with `casa.provision` and add `method: qr` under its `data:`; replace every `casa.start_ble` with `casa.provision` with `method: ble`; replace every `duration: 300` with `timeout_minutes: 5`; add `intro` (boolean, default `false`, "show the welcome pane") to the options table; add a "Version" line stating the card logs its version to the console; update the sentence about `casa.generate_qr`/`casa.start_ble` being admin-restricted to name `casa.provision`.

- [ ] **Step 7: Sanity and commit**

`node -e "new Function(require('fs').readFileSync('index.js','utf8').replace(/^const LitElement.*$/m,'const LitElement={prototype:{html(){},css(){}}};'))" && echo parses` (a syntax-only check; if `node` is missing, skip and rely on the balanced-backtick check from Task 6 Step 3).

```bash
git add index.js README.md
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "fix: read expires_at, render qr_data_uri, drop external QR service, version banner, README parity"
git tag v26.09.30
```

---

### Task 9: Documentation drift and deploy secret

**Files:**
- Modify: `docs/app-integration.md`
- Modify: `deploy.sh`
- Modify: `README.md` (provisioner)

- [ ] **Step 1: app-integration.md**

Apply each correction from the spec section 11 in place: line 12's "every endpoint uses the bearer token" gains "except `/api/casa/location_report`, which is unauthenticated at the HTTP layer and relies on the device-key-encrypted body"; §1 heartbeat response adds `profile_report_interval_seconds`, `location_config_version` (always present, `null` when no zones), `expires_at` (only while an override is pending); §2 `type` becomes `wireguard | profile | auth | location` with one line each for the two new ones (`auth/reauthenticate` is never acked by the device; `location/update` replaces older location entries); a new `POST /api/casa/profile_report` subsection with body `{device_id, fields}`; §5 adds `deprovision` and `clear_cache_and_reload` commands; the legacy WireGuard push note says `title`/`message` are always present, `""` when silent.

- [ ] **Step 2: deploy.sh**

Replace the three literals with:

```bash
HOST="${CASA_DEPLOY_HOST:-192.168.1.21}"
USER="${CASA_DEPLOY_USER:-root}"
PASS="${CASA_DEPLOY_PASS:?set CASA_DEPLOY_PASS in the environment (never in this file)}"
```

`deploy.sh` is git-ignored; edit it in place anyway so the plaintext is gone from disk. Do not commit it (it stays ignored). Check `git check-ignore -q deploy.sh && echo ignored`.

- [ ] **Step 3: README (provisioner)**

Under the endpoints list (search `/api/casa/heartbeat` in README.md) add the missing device and admin endpoints: `profile_report`, `location_report`, `deprovision`, `admin/location_zones`, `admin/settings`, `admin/sessions`, `admin/check_username`, `admin/reauth_device`. Add a "Deploying to a test HA" paragraph: `CASA_DEPLOY_PASS=... ./deploy.sh`. Add a "Protocol" line pointing at `docs/provisioning-protocol.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/app-integration.md README.md
git -c user.name="Bryce" -c user.email="tech@barnabaslanding.com" commit -m "docs: app-integration drift fixes, endpoint list, deploy notes"
```

---

### Task 10: Deploy to Fairwater and verify (controller-run)

**Files:** none.

- [ ] **Step 1:** `python3 -m pytest -q tests` → 35 passed; `git status --short` empty.
- [ ] **Step 2:** `CASA_DEPLOY_PASS=<from the user's environment> ./deploy.sh`; wait for `curl -s -o /dev/null -w '%{http_code}' http://192.168.1.21:8123/` to return 200.
- [ ] **Step 3:** HA log check: `ha_get_logs(source="error_log", search="casa")` shows no tracebacks; the probe debug line or a `relay reports protocol` warning is acceptable.
- [ ] **Step 4:** `casa.provision` with `method: deep_link` for `casa-test-1` returns a payload; decode nothing (encrypted) but confirm `expires_at` present; with `method: qr` confirm `qr_data_uri` starts with `data:image/png;base64,` and `url_path_deprecated` is true.
- [ ] **Step 5:** Single-use: provision `casa-test-1` with `password_scramble: true`, fire the link at the simulator (user tap), confirm the dashboard loads, then `casa.list_tokens` shows a new token and the HA log shows `Password for casa-test-1 scrambled.` within a few seconds of the login; firing the same link again must fail the auto-login.
- [ ] **Step 6:** Self-deprovision: from the simulator app's session there is no UI yet (sub-project 3); verify the endpoint with the HA MCP: not possible via service. Instead `curl -X POST http://192.168.1.21:8123/api/casa/deprovision -H "Authorization: Bearer <a long-lived token for casa-test-1>" -d '{"device_id":"<sim device id from the summary>"}'` → 200; the device disappears from the admin summary. Creating that long-lived token needs the user; if unavailable, record the step as pending in the ledger.
- [ ] **Step 7:** Admin summary shows `version: 26.09.30`, `relay_version`, `relay_protocol` (null until the relay deploy).
- [ ] **Step 8:** Ledger the results; tag `git tag v26.09.30` on the provisioner.

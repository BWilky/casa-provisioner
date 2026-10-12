# Actionable Notifications (HA integration + panel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `casa.notify_user` accepts `data.actions`; HA stores what it sent, pushes a device-safe copy with `category: CASA_DYNAMIC`, accepts an encrypted press callback on `/api/casa/notification_action`, fires `casa_notification_action`, and runs the action's stored service call.

**Architecture:** Pure logic lives in a new `custom_components/casa/actions.py` (validation, device-safe copy, pending-record lifecycle) so it is testable with plain pytest. `location.py`'s HKDF/AES-GCM helpers gain an `info` parameter and are reused for the `casa-action-v1` channel. `__init__.py` gets a thin wiring helper used by `handle_notify_user`, a new store, an hourly prune timer, and one new `HomeAssistantView`. The panel's two test-push forms get an optional JSON data box with an example.

**Tech Stack:** Python HA custom integration tested with plain pytest + fakes (`tests/fakes.py`, stubs in `tests/conftest.py`); vanilla ES-module panel with `node:test` for pure logic.

**Spec:** `docs/superpowers/specs/2026-10-12-actionable-notifications-design.md`

## Global Constraints

- Run all commands from `/Users/bryce/Documents/casa/casa-provisioner`.
- Tests: `python3 -m pytest -q` (whole suite stays green after every task); panel logic: `node --test tests/js/`.
- Validation limits (spec §1.1): 1–10 actions; `action` ≤ 64 chars, unique; `title` ≤ 64; `service` matches `^[a-z0-9_]+\.[a-z0-9_]+$`; `nav_path` ≤ 512; `tag` ≤ 128; `reply_text` ≤ 1,000; `ts` within ±300 s.
- Category string is exactly `CASA_DYNAMIC`. HKDF info for the callback is exactly `b"casa-action-v1"`.
- Pending record TTL 24 h; consumed records kept 1 h; `notification_id = secrets.token_urlsafe(24)`.
- Event name `casa_notification_action` with the §1.5 field set; no event for plain taps.
- Device-safe action copy strips exactly `service`, `target`, `data`.
- Without `data.actions`, `notify_user` output is unchanged.
- Every callback failure is `403 {"error": "Rejected"}`; malformed JSON/body is 400.
- `CASA_VERSION`, `PANEL_VERSION`, `manifest.json` bump together to `26.10.07` (Task 7 only).
- Commits: `git add <files>` then `git commit -m "<msg>"`. No attribution lines.
- Relay plan (`push-notifications-relay/docs/superpowers/plans/2026-10-12-notification-category.md`) ships first; this plan sends `category` regardless, and an old relay simply ignores the unknown field (pydantic default ignores extras — verified in Task 4 note).

## Review Focus

1. `data` given as a JSON **string** containing `actions` must validate and work exactly like a dict (the service already accepts both). Pinned in Task 4.
2. Two devices under one `username` must get two different `notification_id`s and two pending records; a press from device B with device A's id must be rejected. Pinned in Tasks 4 and 5.
3. A press on an action with no `service` must fire the event and make no service call; a press whose stored service raises must still have fired the event and must return 200. Pinned in Task 5.
4. A record older than 24 h must be gone after the prune, and a press for it rejected; a record consumed 2 h ago must be pruned but one consumed 10 min ago must still return `duplicate: true`. Pinned in Tasks 3 and 5.
5. `reply_text` on an action that did not ask for `text_input` must be ignored (not echoed) rather than rejected, so a mismatched client cannot lock the user out of the press. Pinned in Task 5.

---

### Task 1: `info` parameter on the report crypto helpers

**Files:**
- Modify: `custom_components/casa/location.py:24` (`_REPORT_INFO`), `:118-153` (`_derive_report_key`, `encrypt_report_payload`, `decrypt_report_payload`)
- Test: `tests/test_location.py`

**Interfaces:**
- Produces: `encrypt_report_payload(plaintext: str, device_key: str, device_id: str, info: bytes = _REPORT_INFO) -> str`; `decrypt_report_payload(payload_b64: str, device_key: str, device_id: str, info: bytes = _REPORT_INFO) -> dict`; new constant `ACTION_INFO = b"casa-action-v1"` exported from `location.py`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_location.py`)

```python
def test_report_info_strings_are_separate_channels():
    from custom_components.casa.location import ACTION_INFO
    blob = encrypt_report_payload(json.dumps({"a": 1}), "deadbeef" * 8, "DEV-1", info=ACTION_INFO)
    assert decrypt_report_payload(blob, "deadbeef" * 8, "DEV-1", info=ACTION_INFO) == {"a": 1}
    with pytest.raises(ValueError):
        decrypt_report_payload(blob, "deadbeef" * 8, "DEV-1")  # default casa-report-v1 must not open it


def test_action_info_constant_value():
    from custom_components.casa.location import ACTION_INFO
    assert ACTION_INFO == b"casa-action-v1"
```

(`pytest` and `json` are already imported at the top of that file; add them if not.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_location.py -q -k "info"`
Expected: FAIL — `ImportError: cannot import name 'ACTION_INFO'`.

- [ ] **Step 3: Implement**

In `location.py`, next to `_REPORT_INFO`:

```python
_REPORT_INFO = b"casa-report-v1"
ACTION_INFO = b"casa-action-v1"  # notification-action presses; distinct channel from reports
```

Change the three helpers:

```python
def _derive_report_key(device_key: str, device_id: str, info: bytes = _REPORT_INFO) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=device_id.encode("utf-8"),
        info=info,
    ).derive(device_key.encode("utf-8"))


def encrypt_report_payload(plaintext: str, device_key: str, device_id: str, info: bytes = _REPORT_INFO) -> str:
    key = _derive_report_key(device_key, device_id, info)
    ...  # body unchanged


def decrypt_report_payload(payload_b64: str, device_key: str, device_id: str, info: bytes = _REPORT_INFO) -> dict:
    ...
        key = _derive_report_key(device_key, device_id, info)
    ...  # rest unchanged
```

- [ ] **Step 4: Run the suite**

Run: `python3 -m pytest -q`
Expected: all pass (existing location tests use the default and are unaffected).

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/location.py tests/test_location.py
git commit -m "refactor(location): report crypto helpers take an HKDF info string; add ACTION_INFO"
```

---

### Task 2: `actions.py` — validation and device-safe copy

**Files:**
- Create: `custom_components/casa/actions.py`
- Test: `tests/test_actions.py` (new)

**Interfaces:**
- Produces:
  - `CATEGORY = "CASA_DYNAMIC"`, `MAX_ACTIONS = 10`, `SERVER_ONLY_KEYS = frozenset({"service", "target", "data"})`
  - `validate_actions(data: dict) -> list[dict]` — returns the normalised action list; raises `ValueError(msg)` with a human-readable message on any violation. Also validates `data["tag"]` and `data["action_data"]` when present.
  - `device_actions(actions: list[dict]) -> list[dict]` — deep copy with `SERVER_ONLY_KEYS` removed.

- [ ] **Step 1: Write the failing tests**

```python
"""Pure logic for actionable notifications: validation, device-safe copy,
pending-record lifecycle."""
import pytest

from custom_components.casa.actions import (
    CATEGORY,
    MAX_ACTIONS,
    device_actions,
    validate_actions,
)


def _ok(**extra):
    return {"actions": [{"action": "UNLOCK", "title": "Unlock", **extra}]}


def test_category_constant():
    assert CATEGORY == "CASA_DYNAMIC"


def test_minimal_action_passes_and_is_normalised():
    out = validate_actions(_ok())
    assert out == [{"action": "UNLOCK", "title": "Unlock"}]


def test_full_action_passes():
    out = validate_actions(_ok(service="lock.unlock", target={"entity_id": "lock.a"}, data={"x": 1},
                                nav_path="/lovelace/0", text_input=True, destructive=True, auth_required=True))
    assert out[0]["service"] == "lock.unlock" and out[0]["auth_required"] is True


@pytest.mark.parametrize("bad", [
    {"actions": []},
    {"actions": "nope"},
    {"actions": [{"title": "x"}]},                       # missing action
    {"actions": [{"action": "A"}]},                      # missing title
    {"actions": [{"action": "", "title": "x"}]},
    {"actions": [{"action": "a" * 65, "title": "x"}]},
    {"actions": [{"action": "A", "title": "t" * 65}]},
    {"actions": [{"action": "A", "title": "x"}, {"action": "A", "title": "y"}]},   # duplicate
    {"actions": [{"action": "A", "title": "x", "service": "lock"}]},                # no dot
    {"actions": [{"action": "A", "title": "x", "service": "Lock.Unlock"}]},         # uppercase
    {"actions": [{"action": "A", "title": "x", "target": "lock.a"}]},               # not a dict
    {"actions": [{"action": "A", "title": "x", "data": []}]},
    {"actions": [{"action": "A", "title": "x", "nav_path": "/" * 513}]},
    {"actions": [{"action": "A", "title": "x", "text_input": "yes"}]},
    {"actions": [{"action": "A", "title": "x", "bogus": 1}]},                       # unknown key
    {"actions": [{"action": str(i), "title": "x"} for i in range(MAX_ACTIONS + 1)]},
    {"actions": [{"action": "A", "title": "x"}], "tag": "t" * 129},
    {"actions": [{"action": "A", "title": "x"}], "tag": 5},
    {"actions": [{"action": "A", "title": "x"}], "action_data": "str"},
])
def test_invalid_actions_rejected(bad):
    with pytest.raises(ValueError):
        validate_actions(bad)


def test_ten_actions_allowed():
    out = validate_actions({"actions": [{"action": str(i), "title": "x"} for i in range(MAX_ACTIONS)]})
    assert len(out) == MAX_ACTIONS


def test_device_actions_strips_server_only_keys_without_mutating():
    actions = validate_actions(_ok(service="lock.unlock", target={"entity_id": "lock.a"}, data={"x": 1}, nav_path="/p"))
    safe = device_actions(actions)
    assert safe == [{"action": "UNLOCK", "title": "Unlock", "nav_path": "/p"}]
    assert "service" in actions[0]  # original untouched
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_actions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.casa.actions'`.

- [ ] **Step 3: Implement `custom_components/casa/actions.py`**

```python
"""Actionable notifications: validation of `data.actions`, the device-safe
copy pushed to the phone, and the pending-record lifecycle that lets HA
verify a press against what it actually sent. Pure functions; no HA imports
so plain pytest can exercise everything."""
from __future__ import annotations

import copy
import re
import secrets
import time

CATEGORY = "CASA_DYNAMIC"
MAX_ACTIONS = 10
SERVER_ONLY_KEYS = frozenset({"service", "target", "data"})
ALLOWED_ACTION_KEYS = frozenset({
    "action", "title", "service", "target", "data",
    "nav_path", "text_input", "destructive", "auth_required",
})
BOOL_KEYS = ("text_input", "destructive", "auth_required")
ALLOWED_PRESS_KEYS = frozenset({"notification_id", "action", "reply_text", "ts"})

PENDING_TTL_SECONDS = 24 * 3600
CONSUMED_TTL_SECONDS = 3600
MAX_REPLY_TEXT = 1000
MAX_ACTION_ID = 64
MAX_TITLE = 64
MAX_NAV_PATH = 512
MAX_TAG = 128

_SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _str(value, name: str, max_len: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{name} is longer than {max_len} characters")
    return value


def validate_actions(data: dict) -> list[dict]:
    """Validate `data.actions` (+ `tag`, `action_data`). Returns a normalised
    copy of the action list. Raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    raw = data.get("actions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("actions must be a non-empty list")
    if len(raw) > MAX_ACTIONS:
        raise ValueError(f"at most {MAX_ACTIONS} actions are allowed")

    tag = data.get("tag")
    if tag is not None and (not isinstance(tag, str) or len(tag) > MAX_TAG):
        raise ValueError(f"tag must be a string of at most {MAX_TAG} characters")
    action_data = data.get("action_data")
    if action_data is not None and not isinstance(action_data, dict):
        raise ValueError("action_data must be an object")

    out: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"actions[{i}] must be an object")
        unknown = set(entry) - ALLOWED_ACTION_KEYS
        if unknown:
            raise ValueError(f"actions[{i}] has unknown keys: {', '.join(sorted(unknown))}")
        norm: dict = {
            "action": _str(entry.get("action"), f"actions[{i}].action", MAX_ACTION_ID, required=True),
            "title": _str(entry.get("title"), f"actions[{i}].title", MAX_TITLE, required=True),
        }
        if norm["action"] in seen:
            raise ValueError(f"duplicate action id '{norm['action']}'")
        seen.add(norm["action"])

        service = _str(entry.get("service"), f"actions[{i}].service", 128, required=False)
        if service is not None:
            if not _SERVICE_RE.match(service):
                raise ValueError(f"actions[{i}].service must look like domain.service")
            norm["service"] = service
        for key in ("target", "data"):
            if key in entry:
                if not isinstance(entry[key], dict):
                    raise ValueError(f"actions[{i}].{key} must be an object")
                norm[key] = copy.deepcopy(entry[key])
        nav_path = _str(entry.get("nav_path"), f"actions[{i}].nav_path", MAX_NAV_PATH, required=False)
        if nav_path is not None:
            norm["nav_path"] = nav_path
        for key in BOOL_KEYS:
            if key in entry:
                if not isinstance(entry[key], bool):
                    raise ValueError(f"actions[{i}].{key} must be true or false")
                norm[key] = entry[key]
        out.append(norm)
    return out


def device_actions(actions: list[dict]) -> list[dict]:
    """The copy that leaves HA: everything except the server-side keys."""
    return [{k: copy.deepcopy(v) for k, v in a.items() if k not in SERVER_ONLY_KEYS} for a in actions]
```

- [ ] **Step 4: Run**

Run: `python3 -m pytest tests/test_actions.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/actions.py tests/test_actions.py
git commit -m "feat(actions): validate notify_user data.actions; device-safe copy"
```

---

### Task 3: `actions.py` — pending-record lifecycle

**Files:**
- Modify: `custom_components/casa/actions.py`
- Modify: `tests/fakes.py:184-200` (`FakeHass.data["casa"]`: add `"na_data": {"pending": {}}`, `"na_store": FakeStore()`)
- Test: `tests/test_actions.py`

**Interfaces:**
- Produces (all pure, operate on the `na_data` dict `{"pending": {id: record}}`):
  - `create_pending(na_data, *, device_id, user_id, username, actions, tag=None, action_data=None, now=None) -> str` — returns the new `notification_id`.
  - `prune_pending(na_data, now=None) -> int` — removes expired/old-consumed records; returns count removed.
  - `consume_pending(na_data, *, notification_id, device_id, action, now=None) -> tuple[dict, dict | None]` — returns `(record, entry)`; `entry is None` means already consumed (duplicate). Raises `LookupError` for missing/expired/wrong device/unknown action.
  - `validate_press(inner: dict, now=None) -> dict` — whitelist keys, check types, `ts` window, `reply_text` cap. Raises `ValueError`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_actions.py`)

```python
from custom_components.casa.actions import (
    CONSUMED_TTL_SECONDS, PENDING_TTL_SECONDS,
    consume_pending, create_pending, prune_pending, validate_press,
)

ACTS = [{"action": "UNLOCK", "title": "Unlock", "service": "lock.unlock"}, {"action": "IGNORE", "title": "Ignore"}]


def _na(now=1_000_000.0):
    na = {"pending": {}}
    nid = create_pending(na, device_id="D1", user_id="u1", username="alice", actions=ACTS, tag="t", action_data={"k": 1}, now=now)
    return na, nid


def test_create_pending_records_everything_and_unique_ids():
    na, nid = _na()
    rec = na["pending"][nid]
    assert rec["device_id"] == "D1" and rec["user_id"] == "u1" and rec["username"] == "alice"
    assert rec["actions"] == ACTS and rec["tag"] == "t" and rec["action_data"] == {"k": 1}
    assert rec["created_at"] == 1_000_000.0 and "consumed_at" not in rec
    nid2 = create_pending(na, device_id="D2", user_id="u1", username="alice", actions=ACTS, now=1_000_000.0)
    assert nid2 != nid and len(nid) >= 32


def test_consume_returns_entry_then_duplicate():
    na, nid = _na()
    rec, entry = consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_010.0)
    assert entry["service"] == "lock.unlock" and rec["consumed_at"] == 1_000_010.0
    rec2, entry2 = consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_020.0)
    assert rec2 is rec and entry2 is None


@pytest.mark.parametrize("kw", [
    dict(notification_id="nope", device_id="D1", action="UNLOCK"),
    dict(device_id="D2", action="UNLOCK"),            # wrong device
    dict(device_id="D1", action="EXPLODE"),           # unknown action
])
def test_consume_rejects(kw):
    na, nid = _na()
    kw.setdefault("notification_id", nid)
    with pytest.raises(LookupError):
        consume_pending(na, now=1_000_010.0, **kw)


def test_consume_rejects_expired():
    na, nid = _na()
    with pytest.raises(LookupError):
        consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_000.0 + PENDING_TTL_SECONDS + 1)


def test_prune_drops_expired_and_old_consumed_only():
    na, old = _na(now=0.0)
    fresh = create_pending(na, device_id="D1", user_id="u1", username="a", actions=ACTS, now=1_000_000.0)
    recent_consumed = create_pending(na, device_id="D1", user_id="u1", username="a", actions=ACTS, now=1_000_000.0)
    consume_pending(na, notification_id=recent_consumed, device_id="D1", action="IGNORE", now=1_000_000.0)
    old_consumed = create_pending(na, device_id="D1", user_id="u1", username="a", actions=ACTS, now=990_000.0)
    consume_pending(na, notification_id=old_consumed, device_id="D1", action="IGNORE", now=990_000.0)
    removed = prune_pending(na, now=1_000_000.0 + 600)
    assert removed == 2
    assert set(na["pending"]) == {fresh, recent_consumed}
    assert prune_pending(na, now=1_000_000.0 + CONSUMED_TTL_SECONDS + 1) == 1
    assert set(na["pending"]) == {fresh}


def test_validate_press_ok_and_optional_reply():
    inner = validate_press({"notification_id": "n", "action": "A", "ts": 1_000_000}, now=1_000_100.0)
    assert inner == {"notification_id": "n", "action": "A", "ts": 1_000_000}
    inner = validate_press({"notification_id": "n", "action": "A", "reply_text": "hi", "ts": 1_000_000}, now=1_000_000.0)
    assert inner["reply_text"] == "hi"


@pytest.mark.parametrize("inner", [
    {"notification_id": "n", "action": "A"},                                  # no ts
    {"notification_id": "n", "action": "A", "ts": 1_000_000 - 301},
    {"notification_id": "n", "action": "A", "ts": "1000000"},
    {"notification_id": "n", "action": "A", "ts": 1_000_000, "extra": 1},
    {"notification_id": "", "action": "A", "ts": 1_000_000},
    {"notification_id": "n", "action": 5, "ts": 1_000_000},
    {"notification_id": "n", "action": "A", "ts": 1_000_000, "reply_text": "x" * 1001},
    {"notification_id": "n", "action": "A", "ts": 1_000_000, "reply_text": 7},
])
def test_validate_press_rejects(inner):
    with pytest.raises(ValueError):
        validate_press(inner, now=1_000_000.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_actions.py -q`
Expected: FAIL — `ImportError` for the new names.

- [ ] **Step 3: Implement** (append to `actions.py`)

```python
def _now(now):
    return time.time() if now is None else now


def create_pending(na_data: dict, *, device_id: str, user_id: str, username: str | None,
                   actions: list[dict], tag: str | None = None, action_data: dict | None = None,
                   now: float | None = None) -> str:
    pending = na_data.setdefault("pending", {})
    nid = secrets.token_urlsafe(24)
    while nid in pending:
        nid = secrets.token_urlsafe(24)
    record = {
        "device_id": device_id,
        "user_id": user_id,
        "username": username,
        "actions": copy.deepcopy(actions),
        "created_at": _now(now),
    }
    if tag is not None:
        record["tag"] = tag
    if action_data is not None:
        record["action_data"] = copy.deepcopy(action_data)
    pending[nid] = record
    return nid


def prune_pending(na_data: dict, now: float | None = None) -> int:
    t = _now(now)
    pending = na_data.setdefault("pending", {})
    doomed = [
        nid for nid, rec in pending.items()
        if t - rec.get("created_at", 0) > PENDING_TTL_SECONDS
        or ("consumed_at" in rec and t - rec["consumed_at"] > CONSUMED_TTL_SECONDS)
    ]
    for nid in doomed:
        pending.pop(nid, None)
    return len(doomed)


def consume_pending(na_data: dict, *, notification_id: str, device_id: str, action: str,
                    now: float | None = None) -> tuple[dict, dict | None]:
    """Returns (record, entry). entry is None when the record was already
    consumed (duplicate press). Raises LookupError when the press cannot be
    matched to something HA sent to this device."""
    t = _now(now)
    rec = na_data.setdefault("pending", {}).get(notification_id)
    if rec is None:
        raise LookupError("unknown notification")
    if t - rec.get("created_at", 0) > PENDING_TTL_SECONDS:
        raise LookupError("expired")
    if rec.get("device_id") != device_id:
        raise LookupError("device mismatch")
    entry = next((a for a in rec.get("actions", []) if a.get("action") == action), None)
    if entry is None:
        raise LookupError("unknown action")
    if "consumed_at" in rec:
        return rec, None
    rec["consumed_at"] = t
    return rec, entry


def validate_press(inner: dict, now: float | None = None) -> dict:
    if not isinstance(inner, dict):
        raise ValueError("press is not an object")
    if set(inner) - ALLOWED_PRESS_KEYS:
        raise ValueError("unexpected keys")
    nid = inner.get("notification_id")
    action = inner.get("action")
    ts = inner.get("ts")
    if not isinstance(nid, str) or not nid:
        raise ValueError("notification_id")
    if not isinstance(action, str) or not action:
        raise ValueError("action")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or abs(_now(now) - ts) > 300:
        raise ValueError("ts")
    out = {"notification_id": nid, "action": action, "ts": ts}
    if "reply_text" in inner:
        rt = inner["reply_text"]
        if not isinstance(rt, str) or len(rt) > MAX_REPLY_TEXT:
            raise ValueError("reply_text")
        out["reply_text"] = rt
    return out
```

In `tests/fakes.py` `FakeHass.__init__`, add two entries to `self.data["casa"]`:

```python
                "na_data": {"pending": {}},
                "na_store": FakeStore(),
```

- [ ] **Step 4: Run**

Run: `python3 -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/actions.py tests/test_actions.py tests/fakes.py
git commit -m "feat(actions): pending-record lifecycle and press validation"
```

---

### Task 4: Wire actions into `notify_user`; store and prune timer

**Files:**
- Modify: `custom_components/casa/__init__.py`
  - imports (`:29-36`): add `from .actions import CATEGORY, create_pending, device_actions, prune_pending, validate_actions, validate_press, consume_pending` and `ACTION_INFO` to the `.location` import.
  - new module-level helper `_action_push_fields` (place after `_find_device_record`, `:1118`).
  - `handle_notify_user` (`:5846-5983`).
  - store setup after the `lz_store` block (`:4225-4240` region), hourly prune near `:6521`, unload near `:6994`.
- Test: `tests/test_notify_actions.py` (new)

**Interfaces:**
- Consumes: Task 2/3 functions.
- Produces: `_action_push_fields(hass, *, device_id, user_id, username, parsed_data, actions) -> dict` — returns the `data` dict to put on the relay body (caller data + `notification_id` + stripped `actions`), having created the pending record and scheduled a save. `_save_notification_actions(hass)` — `na_store.async_delay_save(lambda: na_data, 2.0)`.

- [ ] **Step 1: Write the failing tests**

```python
"""notify_user with data.actions: one pending record per device, server-only
keys stripped from the pushed copy, category set."""
from custom_components.casa import _action_push_fields
from custom_components.casa.actions import CATEGORY, validate_actions
from tests.fakes import FakeHass

DATA = {"tag": "door", "action_data": {"c": 1}, "nav_path": "/x",
        "actions": [{"action": "UNLOCK", "title": "Unlock", "service": "lock.unlock", "target": {"entity_id": "lock.a"}},
                    {"action": "IGNORE", "title": "Ignore", "destructive": True}]}


def test_push_fields_strip_server_keys_and_record_pending():
    hass = FakeHass()
    actions = validate_actions(DATA)
    out = _action_push_fields(hass, device_id="D1", user_id="u1", username="alice", parsed_data=DATA, actions=actions)
    nid = out["notification_id"]
    assert out["nav_path"] == "/x" and out["tag"] == "door"
    assert out["actions"] == [{"action": "UNLOCK", "title": "Unlock"}, {"action": "IGNORE", "title": "Ignore", "destructive": True}]
    rec = hass.casa["na_data"]["pending"][nid]
    assert rec["device_id"] == "D1" and rec["actions"][0]["service"] == "lock.unlock"
    assert hass.casa["na_store"].delayed == 1


def test_two_devices_two_records():
    hass = FakeHass()
    actions = validate_actions(DATA)
    a = _action_push_fields(hass, device_id="D1", user_id="u1", username="alice", parsed_data=DATA, actions=actions)
    b = _action_push_fields(hass, device_id="D2", user_id="u1", username="alice", parsed_data=DATA, actions=actions)
    assert a["notification_id"] != b["notification_id"]
    assert {r["device_id"] for r in hass.casa["na_data"]["pending"].values()} == {"D1", "D2"}


def test_category_constant_is_what_the_app_registers():
    assert CATEGORY == "CASA_DYNAMIC"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_notify_actions.py -q`
Expected: FAIL — `ImportError: cannot import name '_action_push_fields'`.

- [ ] **Step 3: Implement**

Module-level helpers in `__init__.py` (after `_find_device_record`):

```python
def _save_notification_actions(hass) -> None:
    data = hass.data[DOMAIN]
    data["na_store"].async_delay_save(lambda: data["na_data"], 2.0)


def _action_push_fields(hass, *, device_id: str, user_id: str, username, parsed_data: dict, actions: list) -> dict:
    """Create the pending record for one device and return the `data` dict
    that goes to the relay: caller keys + notification_id + device-safe actions."""
    nid = create_pending(
        hass.data[DOMAIN]["na_data"],
        device_id=device_id, user_id=user_id, username=username, actions=actions,
        tag=parsed_data.get("tag"), action_data=parsed_data.get("action_data"),
    )
    _save_notification_actions(hass)
    out = {k: v for k, v in parsed_data.items() if k != "actions"}
    out["notification_id"] = nid
    out["actions"] = device_actions(actions)
    return out
```

In `handle_notify_user`, after `parsed_data` is computed and before the username/device check:

```python
        actions = None
        if isinstance(parsed_data, dict) and "actions" in parsed_data:
            try:
                actions = validate_actions(parsed_data)
            except ValueError as err:
                raise HomeAssistantError(f"Invalid actions: {err}") from err
```

Track the owning user id in both branches: in the `device_id` branch, after `uid, device_data, username = found`, add `target_uid = uid`; in the username branch, after `user_id = target_user.id`, add `target_uid = user_id`.

In the per-device loop, replace

```python
            if parsed_data is not None:
                payload["data"] = parsed_data
```

with

```python
            if actions is not None:
                payload["category"] = CATEGORY
                payload["data"] = _action_push_fields(
                    hass, device_id=device_id, user_id=target_uid, username=username,
                    parsed_data=parsed_data, actions=actions,
                )
            elif parsed_data is not None:
                payload["data"] = parsed_data
```

Note: a relay older than 1.2.0 ignores the unknown `category` key (pydantic v2 default `extra="ignore"`), so the push still arrives, just without buttons.

Store setup, after the `lz_store` block in `async_setup_entry`:

```python
    # Pending actionable-notification records (separate .storage file).
    # Shape: {"pending": {notification_id: record}}; see actions.py.
    na_store = Store(hass, 1, "casa_notification_actions")
    na_data = await na_store.async_load()
    if na_data is None:
        na_data = {"pending": {}}
    if prune_pending(na_data):
        await na_store.async_save(na_data)
    hass.data[DOMAIN]["na_store"] = na_store
    hass.data[DOMAIN]["na_data"] = na_data
```

Hourly prune, next to `reconcile_unsub` (`:6521`):

```python
    async def _scheduled_action_prune(now):
        if prune_pending(hass.data[DOMAIN]["na_data"]):
            _save_notification_actions(hass)

    hass.data[DOMAIN]["na_prune_unsub"] = async_track_time_interval(
        hass, _scheduled_action_prune, timedelta(hours=1)
    )
```

Unload, next to the `lz_stale_unsub` block (`:6998`):

```python
    na_prune_unsub = hass.data[DOMAIN].get("na_prune_unsub")
    if na_prune_unsub:
        na_prune_unsub()
```

- [ ] **Step 4: Run**

Run: `python3 -m pytest -q && python3 -m py_compile custom_components/casa/__init__.py`
Expected: all pass; compile OK.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/__init__.py tests/test_notify_actions.py
git commit -m "feat(notify_user): data.actions → pending record, category, device-safe push"
```

---

### Task 5: `/api/casa/notification_action` endpoint

**Files:**
- Modify: `custom_components/casa/__init__.py` — new `CasaNotificationActionView` class after `CasaLocationReportView` (`:2489`); register it in the view block (`:4693-4711`); add `Context` to the `homeassistant.core` import (`:16`).
- Modify: `tests/conftest.py:~60` — add `Context=_Stub` to the `homeassistant.core` stub.
- Modify: `tests/fakes.py` — `FakeHass`: add `self.service_calls = []` and `self.services = SimpleNamespace(async_call=self._async_call)` with

```python
    async def _async_call(self, domain, service, data=None, blocking=False, context=None, target=None):
        self.service_calls.append((domain, service, data, target))
        if getattr(self, "service_raises", None):
            raise self.service_raises
```

- Test: `tests/test_notification_action_view.py` (new)

**Interfaces:**
- Consumes: `decrypt_report_payload(..., info=ACTION_INFO)`, `validate_press`, `consume_pending`, `_find_device_record`, `_save_notification_actions`.
- Produces: `CasaNotificationActionView(hass)`, `url = "/api/casa/notification_action"`, `requires_auth = False`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
import json
import time

from custom_components.casa import CasaNotificationActionView
from custom_components.casa.actions import create_pending
from custom_components.casa.location import ACTION_INFO, encrypt_report_payload
from tests.fakes import FakeHass, FakeRequest, bind_view

KEY = "deadbeef" * 8
ACTS = [{"action": "UNLOCK", "title": "Unlock", "service": "lock.unlock", "target": {"entity_id": "lock.a"}, "data": {"code": "1"}},
        {"action": "REPLY", "title": "Reply", "text_input": True},
        {"action": "IGNORE", "title": "Ignore"}]


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["device_key"] = KEY
    hass.casa["stored_data"]["users"]["u1"] = {"username": "alice", "devices": {"D1": {}, "D2": {}}}
    nid = create_pending(hass.casa["na_data"], device_id="D1", user_id="u1", username="alice",
                         actions=ACTS, tag="door", action_data={"c": 1})
    return hass, nid


def _press(hass, device_id, inner, info=ACTION_INFO):
    inner = {"ts": int(time.time()), **inner}
    blob = encrypt_report_payload(json.dumps(inner), KEY, device_id, info=info)
    view = bind_view(CasaNotificationActionView(hass))
    return asyncio.run(view.post(FakeRequest(None, {"device_id": device_id, "payload": blob})))


def test_valid_press_fires_event_then_calls_service():
    hass, nid = _hass()
    status, body = _press(hass, "D1", {"notification_id": nid, "action": "UNLOCK"})
    assert status == 200 and body == {"status": "ok"}
    assert hass.fired == [("casa_notification_action", {
        "action": "UNLOCK", "notification_id": nid, "tag": "door", "action_data": {"c": 1},
        "device_id": "D1", "username": "alice", "user_id": "u1"})]
    assert hass.service_calls == [("lock", "unlock", {"code": "1"}, {"entity_id": "lock.a"})]
    assert hass.casa["na_store"].delayed == 1


def test_press_without_service_fires_event_only_and_reply_text_echoed_for_text_input():
    hass, nid = _hass()
    status, _ = _press(hass, "D1", {"notification_id": nid, "action": "REPLY", "reply_text": "on my way"})
    assert status == 200
    assert hass.fired[0][1]["reply_text"] == "on my way"
    assert hass.service_calls == []


def test_reply_text_ignored_for_non_text_action():
    hass, nid = _hass()
    _press(hass, "D1", {"notification_id": nid, "action": "IGNORE", "reply_text": "junk"})
    assert "reply_text" not in hass.fired[0][1]


def test_duplicate_press_is_200_without_second_event():
    hass, nid = _hass()
    _press(hass, "D1", {"notification_id": nid, "action": "IGNORE"})
    status, body = _press(hass, "D1", {"notification_id": nid, "action": "IGNORE"})
    assert status == 200 and body == {"status": "ok", "duplicate": True}
    assert len(hass.fired) == 1


def test_service_error_still_200_and_event_fired():
    hass, nid = _hass()
    hass.service_raises = RuntimeError("boom")
    status, _ = _press(hass, "D1", {"notification_id": nid, "action": "UNLOCK"})
    assert status == 200 and len(hass.fired) == 1


def _rejected(hass, device_id, inner, **kw):
    status, body = _press(hass, device_id, inner, **kw)
    assert status == 403 and body == {"error": "Rejected"}
    assert hass.fired == [] and hass.service_calls == []


def test_wrong_device_rejected():
    hass, nid = _hass()
    _rejected(hass, "D2", {"notification_id": nid, "action": "UNLOCK"})


def test_unknown_notification_rejected():
    hass, _ = _hass()
    _rejected(hass, "D1", {"notification_id": "nope", "action": "UNLOCK"})


def test_unknown_action_rejected():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "EXPLODE"})


def test_expired_rejected():
    hass, nid = _hass()
    hass.casa["na_data"]["pending"][nid]["created_at"] -= 24 * 3600 + 1
    _rejected(hass, "D1", {"notification_id": nid, "action": "UNLOCK"})


def test_stale_ts_rejected():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "UNLOCK", "ts": int(time.time()) - 301})


def test_report_channel_key_cannot_press():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "UNLOCK"}, info=b"casa-report-v1")


def test_oversize_reply_rejected():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "REPLY", "reply_text": "x" * 1001})


def test_unknown_device_rejected():
    hass, nid = _hass()
    _rejected(hass, "ZZ", {"notification_id": nid, "action": "UNLOCK"})


def test_bad_body_is_400():
    hass, _ = _hass()
    view = bind_view(CasaNotificationActionView(hass))
    status, _ = asyncio.run(view.post(FakeRequest(None, {"device_id": "D1"})))
    assert status == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_notification_action_view.py -q`
Expected: FAIL — `ImportError: cannot import name 'CasaNotificationActionView'`.

- [ ] **Step 3: Implement**

`from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback, Context` at `:16`.

Add the view after `CasaLocationReportView`:

```python
class CasaNotificationActionView(HomeAssistantView):
    """Device-facing 'notification action pressed' endpoint. No HA session:
    the device_key encryption (HKDF info casa-action-v1) is the
    authentication, as for location_report. The press must name a
    notification HA sent to this device_id and an action from that list;
    HA runs the stored service call, never one the device names."""

    url = "/api/casa/notification_action"
    name = "api:casa:notification_action"
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
            return self.json({"error": "Rejected"}, status_code=403)

        try:
            inner = validate_press(decrypt_report_payload(payload_b64, device_key, device_id, info=ACTION_INFO))
        except ValueError as err:
            _LOGGER.info("CASA: Rejected notification action from device '%s' (%s).", device_id, err)
            return self.json({"error": "Rejected"}, status_code=403)

        try:
            record, entry = consume_pending(
                data["na_data"], notification_id=inner["notification_id"],
                device_id=device_id, action=inner["action"],
            )
        except LookupError as err:
            _LOGGER.info("CASA: Rejected notification action from device '%s' (%s).", device_id, err)
            return self.json({"error": "Rejected"}, status_code=403)

        _save_notification_actions(self.hass)
        if entry is None:
            return self.json({"status": "ok", "duplicate": True})

        event = {
            "action": inner["action"],
            "notification_id": inner["notification_id"],
            "device_id": device_id,
            "username": record.get("username"),
            "user_id": record.get("user_id"),
        }
        if "tag" in record:
            event["tag"] = record["tag"]
        if "action_data" in record:
            event["action_data"] = record["action_data"]
        if entry.get("text_input") and "reply_text" in inner:
            event["reply_text"] = inner["reply_text"]
        self.hass.bus.async_fire("casa_notification_action", event)

        service = entry.get("service")
        if service:
            domain, name = service.split(".", 1)
            try:
                await self.hass.services.async_call(
                    domain, name, entry.get("data") or {},
                    blocking=False, context=Context(), target=entry.get("target"),
                )
            except Exception as err:  # the event already fired; never fail the press
                _LOGGER.warning("CASA: Notification action '%s' service %s failed: %s", inner["action"], service, err)
        return self.json({"status": "ok"})
```

Register: `hass.http.register_view(CasaNotificationActionView(hass))` right after the `CasaLocationReportView` line.

`tests/conftest.py`: in the `homeassistant.core` `_ensure_module(...)` call add `Context=_Stub`.

`tests/fakes.py`: the `FakeHass` additions listed under **Files**.

- [ ] **Step 4: Run**

Run: `python3 -m pytest -q && python3 -m py_compile custom_components/casa/__init__.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/__init__.py tests/conftest.py tests/fakes.py tests/test_notification_action_view.py
git commit -m "feat(api): /api/casa/notification_action — encrypted press callback, event, stored service call"
```

---

### Task 6: Panel — Data (JSON) box with actions example on both test-push forms

**Files:**
- Create: `custom_components/casa/panel/views/push-data.js`
- Modify: `custom_components/casa/panel/views/devices.js:505-540` (`openTestPushModal`)
- Modify: `custom_components/casa/panel/views/device-editor.js:890-946` (`renderPush`, `sendTestPush`)
- Test: `tests/js/push-data.test.mjs` (new)

**Interfaces:**
- Produces (`push-data.js`): `ACTIONS_EXAMPLE` (object), `actionsExampleText()` → pretty JSON string, `parsePushData(text) -> {data} | {error}` (empty/whitespace → `{data: undefined}`; non-object JSON → error).

- [ ] **Step 1: Write the failing test**

```js
import test from "node:test";
import assert from "node:assert/strict";
import { ACTIONS_EXAMPLE, actionsExampleText, parsePushData } from "../../custom_components/casa/panel/views/push-data.js";

test("example has three actions covering nav_path, service and destructive", () => {
  const kinds = ACTIONS_EXAMPLE.actions.map((a) => (a.nav_path ? "nav" : a.service ? "service" : a.destructive ? "destructive" : "?"));
  assert.deepEqual(kinds, ["nav", "service", "destructive"]);
  assert.deepEqual(parsePushData(actionsExampleText()), { data: ACTIONS_EXAMPLE });
});

test("blank text means no data", () => {
  assert.deepEqual(parsePushData(""), { data: undefined });
  assert.deepEqual(parsePushData("   \n"), { data: undefined });
});

test("bad JSON and non-objects report an error", () => {
  assert.match(parsePushData("{nope").error, /JSON/);
  assert.match(parsePushData("[1]").error, /object/);
  assert.match(parsePushData('"s"').error, /object/);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test tests/js/`
Expected: FAIL — cannot find module `push-data.js`.

- [ ] **Step 3: Implement `push-data.js`**

```js
// Optional `data` for a test push, plus the actionable-notification example
// the admin can insert. Pure; shared by the devices-list modal and the
// device editor's Push section.
export const ACTIONS_EXAMPLE = {
  tag: "test",
  actions: [
    { action: "VIEW", title: "Open dashboard", nav_path: "/lovelace/0" },
    { action: "RUN", title: "Run script", service: "script.turn_on", target: { entity_id: "script.example" } },
    { action: "IGNORE", title: "Ignore", destructive: true },
  ],
};

export function actionsExampleText() {
  return JSON.stringify(ACTIONS_EXAMPLE, null, 2);
}

export function parsePushData(text) {
  const trimmed = String(text ?? "").trim();
  if (!trimmed) return { data: undefined };
  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    return { error: "Data must be valid JSON: " + e.message };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { error: "Data must be a JSON object." };
  }
  return { data: parsed };
}

export const PUSH_DATA_HELP =
  "Optional. Button presses arrive in Home Assistant as the casa_notification_action event (Developer Tools → Events).";
```

`devices.js` `openTestPushModal`: import `{ actionsExampleText, parsePushData, PUSH_DATA_HELP }` from `./push-data.js`; extend the body HTML after the message field:

```html
      <div class="field"><label>Data (JSON) <a href="#" id="tp-example">Insert actions example</a></label>
        <textarea class="input" id="tp-data" rows="8" spellcheck="false"></textarea>
        <p class="field__help">${PUSH_DATA_HELP}</p></div>
```

Wire the link: `body.querySelector("#tp-example").addEventListener("click", (e) => { e.preventDefault(); body.querySelector("#tp-data").value = actionsExampleText(); });`. In `onClick`, after the title/message check:

```js
            const parsed = parsePushData(body.querySelector("#tp-data").value);
            if (parsed.error) { err.hidden = false; err.textContent = parsed.error; return false; }
            ...
              await api.notifyUser({ deviceId: d.device_id, title, message, data: parsed.data });
```

`device-editor.js` `renderPush`: same textarea block with ids `dev-push-data` / `dev-push-example` (respecting `${dis}`), the same click handler, and in `sendTestPush`:

```js
    const parsed = parsePushData(shell.formEl.querySelector("#dev-push-data")?.value);
    if (parsed.error) { msgs.push = { error: parsed.error }; renderSection(); return; }
    ...
      await api.notifyUser({ deviceId: device.device_id, title, message, data: parsed.data });
```

Keep the textarea's current text across `renderSection()` re-renders by reading it into a local `pushDataText` before re-render and writing it back (the editor already re-renders the section on success/error, which would otherwise clear the box).

- [ ] **Step 4: Run**

Run: `node --test tests/js/ && node --check custom_components/casa/panel/views/devices.js && node --check custom_components/casa/panel/views/device-editor.js`
Expected: tests pass; both files parse. (`node --check` on ES modules: if it complains about `import`, use `node --input-type=module --check < file` or skip; the test import of `push-data.js` is the real check.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/casa/panel/views/push-data.js custom_components/casa/panel/views/devices.js custom_components/casa/panel/views/device-editor.js tests/js/push-data.test.mjs
git commit -m "feat(panel): optional Data (JSON) with actions example on test-push forms"
```

---

### Task 7: Docs, service description, version 26.10.07

**Files:**
- Modify: `custom_components/casa/services.yaml:785-820` (`notify_user.data` description)
- Modify: `README.md:134-144` (`casa.notify_user` section)
- Modify: `docs/app-integration.md:324-337` (`casa.notify_user` pushes) + new subsection
- Modify: `docs/provisioning-protocol.md:93-119` (§8 table, §9 note)
- Modify: `custom_components/casa/const.py:7`, `custom_components/casa/panel/version.js`, `custom_components/casa/manifest.json` → `26.10.07`
- Test: `tests/test_versions.py` (existing)

- [ ] **Step 1: services.yaml** — replace the `data` field description with:

```yaml
    data:
      name: "Data"
      description: >-
        Optional JSON object passed to the app. Add `actions` (1–10) for buttons on the
        notification; each needs `action` (id) and `title`, and may set `service`
        (e.g. script.turn_on, with `target`/`data`) to run when pressed, `nav_path` to
        open the app at a path, `text_input: true` to ask for text, `destructive` or
        `auth_required`. Presses fire the `casa_notification_action` event with the
        action id, device_id, username, and any `tag` / `action_data` you include.
        Example: {"tag": "door", "actions": [{"action": "UNLOCK", "title": "Unlock",
        "service": "lock.unlock", "target": {"entity_id": "lock.front"}},
        {"action": "IGNORE", "title": "Ignore", "destructive": true}]}
      required: false
      selector:
        object: {}
```

- [ ] **Step 2: README.md** — after the `casa.notify_user` field table add:

````markdown
#### Action buttons

Add `data.actions` (1–10 entries) to show buttons on the notification. Each entry has `action` (id, unique) and `title`, plus optionally `service` + `target` + `data` (run by HA when pressed), `nav_path` (opens the app there), `text_input: true` (typed text returns as `reply_text`), `destructive`, `auth_required`. `data.tag` and `data.action_data` are echoed back.

A press fires `casa_notification_action` with `action`, `notification_id`, `device_id`, `username`, `user_id`, and `tag` / `action_data` / `reply_text` when present. Only buttons fire the event; a plain tap does not. Presses are accepted for 24 hours.

```yaml
- action: casa.notify_user
  data:
    username: bryce
    title: Garage
    message: Closing in 60 s
    data: {tag: garage_close, actions: [{action: CANCEL, title: Keep open}]}
- wait_for_trigger:
    - trigger: event
      event_type: casa_notification_action
      event_data: {tag: garage_close, action: CANCEL}
  timeout: "00:01:00"
  continue_on_timeout: true
- if: "{{ wait.trigger is none }}"
  then: {action: cover.close_cover, target: {entity_id: cover.garage}}
```
````

- [ ] **Step 3: docs/app-integration.md** — in the `casa.notify_user` pushes section add to the key table:

```
| `notification_id` | string | Present when the push carries actions; echo it in the press callback. |
| `actions` | array | Buttons: `{action, title, nav_path?, text_input?, destructive?, auth_required?}`. Server-side keys are never sent. |
```

and state that such pushes carry `aps.category = "CASA_DYNAMIC"`. Add a new subsection:

````markdown
### Notification action presses (`/api/casa/notification_action`)

`POST` with `{"device_id": "<id>", "payload": "<b64>"}`; no bearer token. `payload` is AES-256-GCM (`nonce12 || ciphertext || tag16`, base64) under `HKDF-SHA256(ikm=utf8(device_key), salt=utf8(device_id), info="casa-action-v1")` — the location-report scheme with a different info string. Inner JSON: `{"notification_id", "action", "reply_text"?, "ts"}` (`ts` unix seconds, ±300 s). Any failure is `403 {"error": "Rejected"}`; a repeat of an accepted press is `200 {"status": "ok", "duplicate": true}`. Presses older than 24 h are rejected. Treat 403 as final; retry only transport errors and 5xx.
````

- [ ] **Step 4: docs/provisioning-protocol.md** — §8 table row:

```
| `/api/casa/notification_action` (26.10.07) | POST | No bearer token. Encrypted press callback (`casa-action-v1`); see app-integration.md. |
```

§9: append "Pushes with `category: CASA_DYNAMIC` are `notify_user` visibles with `data.actions`; still no `data.command`, still never command dispatch."

- [ ] **Step 5: Version bump** — `CASA_VERSION = "26.10.07"` in `const.py`, `PANEL_VERSION = "26.10.07"` in `panel/version.js`, `"version": "26.10.07"` in `manifest.json`.

- [ ] **Step 6: Run**

Run: `python3 -m pytest -q && python3 -c "import yaml; yaml.safe_load(open('custom_components/casa/services.yaml')); print('OK yaml')"`
Expected: all pass, `OK yaml`.

- [ ] **Step 7: Commit**

```bash
git add custom_components/casa/services.yaml README.md docs/app-integration.md docs/provisioning-protocol.md custom_components/casa/const.py custom_components/casa/panel/version.js custom_components/casa/manifest.json
git commit -m "chore: 26.10.07 — actionable notifications docs and version"
```

---

## Done when

- `python3 -m pytest -q` and `node --test tests/js/` are green.
- Sending `casa.notify_user` with the README example from Developer Tools returns `sent_count ≥ 1` and a record appears in `.storage/casa_notification_actions`.
- Deploy to Fairwater (`deploy.sh`) and the lock-screen checklist happen after the iOS plan ships; both are user-driven.

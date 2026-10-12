"""Actionable notifications: validation of `data.actions`, the device-safe
copy pushed to the phone, and the pending-record lifecycle that lets HA
verify a press against what it actually sent. Pure functions; no HA imports
so plain pytest can exercise everything."""
from __future__ import annotations

import copy
import math
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
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) \
            or abs(_now(now) - ts) > 300:
        raise ValueError("ts")
    out = {"notification_id": nid, "action": action, "ts": ts}
    if "reply_text" in inner:
        rt = inner["reply_text"]
        if not isinstance(rt, str) or len(rt) > MAX_REPLY_TEXT:
            raise ValueError("reply_text")
        out["reply_text"] = rt
    return out

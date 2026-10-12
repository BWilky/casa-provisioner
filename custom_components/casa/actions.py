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

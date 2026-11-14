"""Per-device encryption keys (security Phase 4, G1).

Each device record holds its own random `device_key`, handed only to the
device's own pinned session on heartbeat. It replaces the site-wide key,
which every app used to receive and which therefore let any one phone read
or forge every other phone's traffic.

Record fields:
  device_key               the device's own key (never logged or listed)
  device_key_delivered_at  set once a heartbeat has handed it over; from
                           then on pushes use it, even after a rotation
  device_key_confirmed_at  set once a report decrypts under it; from then
                           on the site key is refused for this device
  legacy_key_uses          reports that only decrypted under the site key
  device_key_prev          the key before a rotation; still opens reports
                           and presses (never encrypts) until the heartbeat
                           hands the new key over, so a backgrounded phone
                           doesn't lose them meanwhile

The site key stays as the fallback for records that haven't switched yet
(every app version switches on its next check-in). Phase 5 removes it.
"""
from __future__ import annotations

import hashlib
import logging
import secrets

from homeassistant.util import dt as dt_util

from .location import _REPORT_INFO, decrypt_report_payload

_LOGGER = logging.getLogger(__name__)

REPORT_INFO = _REPORT_INFO


def key_id(device_key: str) -> str:
    """Short, non-secret fingerprint of a key, sent alongside every
    encrypted push so the app can tell whether the key it holds is current."""
    return hashlib.sha256(device_key.encode("utf-8")).hexdigest()[:8]


def ensure_device_key(device_info: dict) -> str:
    key = device_info.get("device_key")
    if not key:
        key = device_info["device_key"] = secrets.token_hex(32)
    return key


def encryption_key(stored_data: dict, device_info: dict) -> str | None:
    """The key a push to this device is encrypted with: its own once it has
    been handed over, the site key before that."""
    own = device_info.get("device_key")
    if own and device_info.get("device_key_delivered_at"):
        return own
    return stored_data.get("device_key")


def decrypt_from_device(stored_data: dict, device_info: dict, device_id: str, payload_b64: str, *, info: bytes = REPORT_INFO) -> dict:
    """Decrypt a report or button press from device_id. Raises ValueError
    when no acceptable key opens it."""
    prev = device_info.get("device_key_prev")
    if prev:
        try:
            return decrypt_report_payload(payload_b64, prev, device_id, info=info)
        except ValueError:
            pass
    own = device_info.get("device_key")
    if own:
        try:
            inner = decrypt_report_payload(payload_b64, own, device_id, info=info)
        except ValueError:
            pass
        else:
            if not device_info.get("device_key_confirmed_at"):
                device_info["device_key_confirmed_at"] = dt_util.now().isoformat()
            return inner
    site = stored_data.get("device_key")
    if not site or device_info.get("device_key_confirmed_at"):
        raise ValueError("no acceptable key")
    inner = decrypt_report_payload(payload_b64, site, device_id, info=info)
    uses = int(device_info.get("legacy_key_uses") or 0) + 1
    device_info["legacy_key_uses"] = uses
    if uses == 1:
        _LOGGER.info("CASA: Device '%s' sent a report under the shared site key; it switches to its own key on its next check-in.", device_id)
    return inner


def rotate_device_key(device_info: dict) -> str:
    """Give the device a new key. Pushes switch to it at once (the device
    sees a new key id, checks in for it and pulls its queue meanwhile).
    device_key_confirmed_at is kept: a device that has proven it holds its
    own key never goes back to the site key."""
    old = device_info.get("device_key")
    if old and not device_info.get("device_key_prev"):
        device_info["device_key_prev"] = old
    device_info["device_key"] = secrets.token_hex(32)
    return key_id(device_info["device_key"])


def mark_delivered(device_info: dict) -> bool:
    """The heartbeat handed the current key to the device's own session.
    Returns True when the record changed (caller saves)."""
    changed = False
    if not device_info.get("device_key_delivered_at"):
        device_info["device_key_delivered_at"] = dt_util.now().isoformat()
        changed = True
    if device_info.pop("device_key_prev", None):
        changed = True
    return changed


def public_key_state(device_info: dict) -> dict:
    own = device_info.get("device_key")
    return {
        "device_key_id": key_id(own) if own else None,
        "delivered": bool(device_info.get("device_key_delivered_at")),
        "legacy_key_uses": int(device_info.get("legacy_key_uses") or 0),
    }

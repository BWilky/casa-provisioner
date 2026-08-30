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
        elif len(name) > 48:
            errors.append(f"{where}: name must be 48 characters or fewer")
        elif name.casefold() in names:
            errors.append(f"{where}: anchor names must be unique ('{name}')")
        else:
            names.add(name.casefold())

        anchor_id = anchor.get("id")
        if not isinstance(anchor_id, str) or not anchor_id.strip():
            errors.append(f"{where}: id is required")

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
            elif len(label) > 48:
                errors.append(f"{rw}: label must be 48 characters or fewer")
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

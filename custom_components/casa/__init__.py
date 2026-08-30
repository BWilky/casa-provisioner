import asyncio
import logging
import os
import string
import secrets
import base64
import hashlib
import time
import json
import uuid
import zlib
import urllib.parse
import re
from datetime import timedelta

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import qrcode

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from .const import DOMAIN, CONF_ADMIN_SYSTEM_ONLY, RELAY_URLS, RELAY_REGISTER_SITE_URL, RELAY_VERIFY_SITE_URL, RELAY_UNREGISTER_URL, RELAY_RECONCILE_URL, RELAY_REMOVE_SITE_URL, CONF_CREATE_DEVICES, CONF_SHOW_PANEL, UNIVERSAL_LINK_SETUP_URL, DEVICE_ALIAS_MAX_LEN, DEFAULT_HEARTBEAT_INTERVAL_SECONDS, MIN_HEARTBEAT_INTERVAL_SECONDS, MAX_HEARTBEAT_INTERVAL_SECONDS, DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS, MIN_PROFILE_REPORT_INTERVAL_SECONDS, MAX_PROFILE_REPORT_INTERVAL_SECONDS, LIVE_PROVISIONING_FIELDS, PROFILE_PROVISIONING_FIELDS
from .location import (
    ALLOWED_REASONS,
    ALLOWED_REPORT_KEYS,
    compute_config_version,
    decrypt_report_payload,
    validate_zone_config,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.components import frontend
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from aiohttp import ClientTimeout

_LOGGER = logging.getLogger(__name__)

# The panel static path is registered on the http app and survives entry reloads,
# so only register it once per HA process.
_PANEL_STATIC_REGISTERED = False

def generate_random_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def _generate_update_id() -> str:
    """Random 32-char [A-Za-z0-9] id for a queued update entry."""
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(32))

def _encrypt_payload(payload_str: str, key_bytes: bytes) -> str:
    """Helper to perform RSA OAEP encryption in the executor thread."""
    public_key = serialization.load_pem_public_key(key_bytes)
    ciphertext = public_key.encrypt(
        payload_str.encode('utf-8'),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return base64.b64encode(ciphertext).decode('utf-8')


def _device_key_id(device_key: str) -> str:
    """Short, non-secret fingerprint of the site device_key.

    Sent in heartbeats and on every encrypted push so the app can tell whether the
    key it holds is current. On mismatch the app falls back to pulling the plaintext
    update from /api/casa/profile_updates instead of trying to decrypt.
    """
    return hashlib.sha256(device_key.encode("utf-8")).hexdigest()[:8]


def _encrypt_push_payload(plaintext: str, device_key: str, device_id: str) -> str:
    """End-to-end encrypt an update push payload for a specific device.

    The AES-256 key is HKDF-derived from the site-wide device_key (the shared secret,
    delivered to devices only over the authenticated heartbeat) salted with the
    device's own device_id. The relay never receives device_key, so it cannot read or
    tamper with the payload; per-device salting means a payload encrypted for one
    device can't be decrypted by another. The iOS app derives the same key from its
    copy of device_key + its device_id. Output is base64(nonce || ciphertext || tag).
    """
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=device_id.encode("utf-8"),
        info=b"casa-update-v1",
    ).derive(device_key.encode("utf-8"))
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def _encrypt_payload_hybrid(plaintext: str, public_key_bytes: bytes) -> str:
    """Hybrid-encrypt a v2 provisioning profile, returning a base64url envelope.

    Layout before base64url: 0x02 || RSA-OAEP-SHA256(aes_key)[256] || nonce[12] || AES-256-GCM(deflate(json)).
    RSA only wraps the 32-byte AES key, so the JSON body has no 190-byte size limit;
    GCM authenticates it, and base64url keeps the deep link/QR free of percent-encoding.
    """
    public_key = serialization.load_pem_public_key(public_key_bytes)
    # Raw DEFLATE (wbits=-15): no zlib header/Adler-32 trailer, so iOS's Compression
    # framework (COMPRESSION_ZLIB == raw DEFLATE) inflates it directly. GCM already
    # authenticates the payload, so the zlib checksum would be redundant anyway.
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = deflate.compress(plaintext.encode("utf-8")) + deflate.flush()
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(aes_key).encrypt(nonce, compressed, None)
    wrapped_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    envelope = bytes([2]) + wrapped_key + nonce + ciphertext
    return base64.urlsafe_b64encode(envelope).decode("utf-8").rstrip("=")


def _get_refresh_token_id_from_jwt(jwt_str: str) -> str:
    """Extract the refresh token id from a Home Assistant access token JWT.

    HA signs access tokens with the refresh token's key and stores the refresh
    token id in the 'iss' claim (not 'jti'); 'jti' is kept only as a fallback.
    """
    import base64
    import json
    try:
        parts = jwt_str.split('.')
        if len(parts) == 3:
            payload_b64 = parts[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            payload = json.loads(payload_bytes.decode('utf-8'))
            return payload.get("iss") or payload.get("jti")
    except Exception:
        pass
    return None


async def _register_site(hass: HomeAssistant, stored_data: dict, store) -> bool:
    """Register this HA instance's site with the relay and persist the issued site_key.

    site_id is a 32-char [A-Za-z0-9] value (secrets.token_hex(16)). The relay issues
    the site_key exactly once (HTTP 201) and never returns it again, so it must be
    persisted. A 409 means the site_id exists but we hold no key (unrecoverable lockout)
    — recovery is to register a brand-new site_id, not retry the same one.
    """
    session = async_get_clientsession(hass)

    for _attempt in range(3):
        site_id = stored_data.get("site_id")
        if not site_id:
            site_id = secrets.token_hex(16)  # 32 hex chars
            stored_data["site_id"] = site_id

        try:
            async with session.post(
                RELAY_REGISTER_SITE_URL,
                json={"site_id": site_id},
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    stored_data["site_key"] = data["site_key"]
                    await store.async_save(stored_data)
                    _LOGGER.info("CASA: Registered site with relay; site_key persisted.")
                    return True

                if resp.status == 409:
                    # site_id taken but we have no key -> lockout; rotate to a fresh site_id.
                    _LOGGER.warning("CASA: site_id already registered with no local key; rotating site_id and retrying.")
                    stored_data["site_id"] = secrets.token_hex(16)
                    continue

                if resp.status == 422:
                    _LOGGER.warning("CASA: Relay rejected site_id as malformed (422); regenerating.")
                    stored_data["site_id"] = secrets.token_hex(16)
                    continue

                if resp.status == 400:
                    _LOGGER.error("CASA: Relay reports no database configured (400); cannot register site.")
                    return False

                text = await resp.text()
                _LOGGER.error("CASA: Unexpected /register_site status %s: %s", resp.status, text)
                return False
        except Exception as err:
            _LOGGER.error("CASA: Failed to reach relay /register_site: %s", err)
            return False

    _LOGGER.error("CASA: Could not register site after multiple attempts.")
    return False


async def _ensure_site_registration(hass: HomeAssistant, stored_data: dict, store) -> None:
    """Verify the stored site credentials against the relay; self-heal if stale.

    A stored site_key can outlive the relay's record of the site (relay DB reset,
    site removed out-of-band). /verify_site is silent-mode-exempt: 403 means the
    relay does not accept our site_id + site_key. Dropping the stale key and
    re-registering the same site_id heals the unknown-site case with a 201; if the
    site exists under a different key, _register_site's 409 handling rotates to a
    fresh site_id.
    """
    if not stored_data.get("site_key"):
        await _register_site(hass, stored_data, store)
        return

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            RELAY_VERIFY_SITE_URL,
            json={"site_id": stored_data.get("site_id"), "site_key": stored_data.get("site_key")},
            timeout=ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return
            if resp.status == 403:
                _LOGGER.warning(
                    "CASA: Relay does not recognize our site credentials (403); re-registering site."
                )
                stored_data.pop("site_key", None)
                await store.async_save(stored_data)
                await _register_site(hass, stored_data, store)
                return
            text = await resp.text()
            _LOGGER.warning("CASA: /verify_site returned %s: %s — keeping existing credentials.", resp.status, text)
    except Exception as err:
        # Transient network failure: keep credentials, do not churn the site.
        _LOGGER.warning("CASA: Could not reach relay /verify_site (%s); keeping existing credentials.", err)


def _user_matches_username(user, target_username: str) -> bool:
    """Match a user by display name (historic behavior) or, failing that, by the
    homeassistant-provider credential username. Accounts whose display name
    differs from the login username (the guided flow names the account after
    the device) are only reachable by their login username."""
    target = target_username.casefold()
    if user.name and user.name.casefold() == target:
        return True
    return any(
        cred.auth_provider_type == "homeassistant"
        and str(cred.data.get("username", "")).casefold() == target
        for cred in user.credentials
    )


def _find_device_record(stored_data: dict, device_id: str):
    """Locate a device across integration-managed and native users.

    Returns (device_info, owning_user_id, username) or (None, None, None).
    """
    for uid, udata in stored_data.get("users", {}).items():
        if udata.get("deleted", False):
            continue
        devices = udata.get("devices", {})
        if device_id in devices:
            return devices[device_id], uid, udata.get("username", "Unknown")
    for uid, devices in stored_data.get("native_devices", {}).items():
        if device_id in devices:
            return devices[device_id], uid, None
    return None, None, None


def _set_expiry_override(stored_data: dict, device_id: str, value):
    """Set or cancel a device's pending expiration override.

    value: int epoch seconds (0 = make permanent) to set, None to cancel.
    Returns the device_info dict, or None if the device was not found.
    """
    device_info, _, _ = _find_device_record(stored_data, device_id)
    if device_info is None:
        return None
    if value is None:
        device_info.pop("expires_at_override", None)
        device_info.pop("expires_at_override_set_at", None)
        device_info.pop("expires_at_override_sent", None)
    else:
        device_info["expires_at_override"] = int(value)
        device_info["expires_at_override_set_at"] = dt_util.now().isoformat()
        device_info.pop("expires_at_override_sent", None)
    return device_info


def _enqueue_update(qu_data: dict, device_id: str, update_type: str, action: str, payload: dict, created_by: str) -> str:
    """Append a queued update entry for a device and return its generated id."""
    entry = {
        "id": _generate_update_id(),
        "type": update_type,
        "action": action,
        "payload": payload,
        "created_at": dt_util.now().isoformat(),
        "created_by": created_by,
    }
    qu_data.setdefault("updates", {}).setdefault(device_id, []).append(entry)
    return entry["id"]


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


def _apply_location_report(hass, device_id: str, device_info: dict, state, reason, config_version) -> bool:
    """Validate and stamp a device's zone report onto its record. The state
    string is opaque ('<anchor>: <label>' | 'away' | 'unknown') but bounded;
    no-location-data is guaranteed structurally, not by content-sniffing here —
    the report schema has no location fields and the endpoint rejects any
    unknown keys before this function is ever called."""
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


def _dequeue_update(qu_data: dict, device_id: str, update_id: str) -> dict | None:
    """Remove a queued update entry by id. Returns the removed entry or None."""
    entries = qu_data.get("updates", {}).get(device_id, [])
    removed = next((e for e in entries if e.get("id") == update_id), None)
    if removed is None:
        return None
    remaining = [e for e in entries if e.get("id") != update_id]
    if remaining:
        qu_data["updates"][device_id] = remaining
    else:
        qu_data.get("updates", {}).pop(device_id, None)
    return removed


async def _prune_stale_queued_updates(hass) -> int:
    """Drop queued updates that can never be consumed, and repair or drop
    reauth_pending markers that can never complete.

    Queue entries only leave the store via a device ack or a reauth
    completion, so anything that severs those paths strands the entry
    forever — and a stranded auth entry is a poison pill: it re-delivers on
    every heartbeat pull, the device logs out to apply it, the login fails
    (or succeeds without ever dequeuing it), and the device ends up in a
    reauthenticate/self-wipe loop.

    Specifically:
      - queues for a device with no record under any non-deleted owner are
        dropped (the pull path 404s them; nothing can ever ack);
      - auth/reauthenticate entries whose target user no longer exists in HA
        are dropped (the pushed credentials can never log in);
      - reauth_pending markers stranded on device-record copies under
        deleted owners (unreachable via _find_device_record, so completion
        no-ops forever) are moved onto the reachable record when one exists;
      - reauth_pending markers whose target user is gone are dropped along
        with the queue entry they reference;
      - device-record copies under deleted owners are removed when the same
        device_id also has a record under a live owner (duplicates left
        behind by register/heartbeat racing an incomplete reauth).

    Returns the number of queue entries removed.
    """
    data = hass.data.get(DOMAIN)
    if not data:
        return 0
    stored_data = data.get("stored_data", {})
    qu_data = data.get("qu_data", {"updates": {}})

    ha_users = await hass.auth.async_get_users()
    ha_user_ids = {u.id for u in ha_users}
    ha_usernames = set()
    for u in ha_users:
        for cred in u.credentials:
            if cred.auth_provider_type == "homeassistant":
                username = str(cred.data.get("username", "") or "")
                if username:
                    ha_usernames.add(username.casefold())

    removed = 0
    stored_changed = False

    def _iter_records():
        for uid, udata in stored_data.get("users", {}).items():
            for did, dinfo in (udata.get("devices", {}) or {}).items():
                yield did, dinfo, udata.get("deleted", False)
        for uid, devices in stored_data.get("native_devices", {}).items():
            for did, dinfo in (devices or {}).items():
                yield did, dinfo, False

    # 1. Repair or drop reauth_pending markers.
    for did, dinfo, owner_deleted in list(_iter_records()):
        pending = dinfo.get("reauth_pending")
        if not pending:
            continue
        if pending.get("target_user_id") not in ha_user_ids:
            if _dequeue_update(qu_data, did, pending.get("update_id")):
                removed += 1
            dinfo.pop("reauth_pending", None)
            stored_changed = True
            _LOGGER.info(
                "CASA: Dropped pending reauthentication of device '%s' — target user '%s' no longer exists.",
                did, pending.get("target_username"),
            )
        elif owner_deleted:
            canonical, _uid, _username = _find_device_record(stored_data, did)
            if canonical is not None and canonical is not dinfo and not canonical.get("reauth_pending"):
                canonical["reauth_pending"] = pending
                dinfo.pop("reauth_pending", None)
                stored_changed = True
                _LOGGER.info(
                    "CASA: Moved stranded reauth_pending for device '%s' onto its record under a live owner.",
                    did,
                )

    # 2. Remove duplicate device records under deleted owners.
    for uid, udata in stored_data.get("users", {}).items():
        if not udata.get("deleted", False):
            continue
        for did in list((udata.get("devices", {}) or {}).keys()):
            canonical, _cuid, _cname = _find_device_record(stored_data, did)
            if canonical is not None and canonical is not udata["devices"][did]:
                udata["devices"].pop(did, None)
                stored_changed = True
                _LOGGER.info(
                    "CASA: Removed duplicate record of device '%s' under deleted user '%s'.",
                    did, udata.get("username", uid),
                )

    # 3. Drop undeliverable queue entries.
    for device_id in list(qu_data.get("updates", {}).keys()):
        entries = qu_data["updates"].get(device_id, [])
        device_info, _uid, _username = _find_device_record(stored_data, device_id)
        if device_info is None:
            removed += len(entries)
            qu_data["updates"].pop(device_id, None)
            _LOGGER.info(
                "CASA: Purged %d queued update(s) for device '%s' — no record under any active user.",
                len(entries), device_id,
            )
            continue
        kept = []
        for e in entries:
            if e.get("type") == "auth":
                target = str((e.get("payload") or {}).get("username", "") or "")
                if target and target.casefold() not in ha_usernames:
                    removed += 1
                    _LOGGER.info(
                        "CASA: Purged queued reauthenticate for device '%s' — target user '%s' no longer exists.",
                        device_id, target,
                    )
                    continue
            kept.append(e)
        if len(kept) != len(entries):
            if kept:
                qu_data["updates"][device_id] = kept
            else:
                qu_data["updates"].pop(device_id, None)

    if removed or stored_changed:
        qu_store = data.get("qu_store")
        if qu_store:
            qu_store.async_delay_save(lambda: qu_data, 2.0)
        if stored_changed:
            store = data.get("store")
            if store:
                store.async_delay_save(lambda: stored_data, 2.0)
    return removed


async def _create_casa_user(hass, name: str, username: str, password: str | None, created_by: str, local_only: bool = True, users=None):
    """Create a local HA user with homeassistant-provider credentials and track it
    in the casa store. Shared by the casa.create_user service and the admin
    reauthenticate endpoint. Returns (result_dict, error_str); exactly one is set.
    """
    name = str(name or "").strip()
    username = str(username or "").strip().casefold()
    password = str(password or "").strip()

    if not name or not username:
        return None, "Missing mandatory name or username"

    if users is None:
        users = await hass.auth.async_get_users()
    if any(u.name and u.name.casefold() == username for u in users) or any(u.name and u.name.casefold() == name.casefold() for u in users):
        return None, "User with this name or username already exists"

    provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
    if not provider:
        return None, "Home Assistant core auth provider not found"

    if not password:
        password = generate_random_password()

    new_user = await hass.auth.async_create_user(
        name=name,
        group_ids=["system-users"],
        local_only=local_only
    )

    provider.data.add_auth(username, password)
    await provider.data.async_save()

    credentials = await provider.async_get_or_create_credentials({"username": username})
    await hass.auth.async_link_user(new_user, credentials)

    _LOGGER.info("CASA: New local user '%s' created (Local Only: %s).", username, local_only)

    stored_data = hass.data[DOMAIN]["stored_data"]
    stored_data["users"][new_user.id] = {
        "user_id": new_user.id,
        "username": username,
        "name": name,
        "created_at": dt_util.now().isoformat(),
        "created_by": created_by,
        "deleted": False,
        "deleted_at": None,
        "deleted_by": None,
    }
    await hass.data[DOMAIN]["store"].async_save(stored_data)

    return {
        "name": name,
        "username": username,
        "password": password,
        "user_id": new_user.id,
        "is_local_only": local_only
    }, None


async def _complete_pending_reauth(hass, device_id: str, user_id: str, refresh_token_id: str) -> bool:
    """Finish an admin-initiated device reauthentication on the device's first
    authenticated contact under its new identity (see CasaAdminReauthDeviceView).

    The admin action only queues/pushes the new credentials and stamps a
    reauth_pending marker — the old session token and the record's placement
    are left intact so the bearer-authenticated pull/ack path keeps working
    until the device actually re-logs-in. This helper does the deferred half:
    move the record to the new owner, rebind refresh_token_id, revoke the old
    session, and drop the queued entry so the device never re-pulls it.

    Called from async_register_device/async_heartbeat before their user-keyed
    lookups (and before the heartbeat computes has_updates). No-ops unless the
    contact comes from the pending target user with a session token different
    from the one the reauth is replacing.
    """
    if not refresh_token_id:
        return False

    data = hass.data[DOMAIN]
    stored_data = data["stored_data"]
    device_info, owner_uid, _username = _find_device_record(stored_data, device_id)
    if not device_info:
        return False
    pending = device_info.get("reauth_pending")
    if not pending or pending.get("target_user_id") != user_id:
        return False
    old_rtid = pending.get("old_refresh_token_id")
    if old_rtid and refresh_token_id == old_rtid:
        # Same-user reauth: the pre-reauth session is still heartbeating.
        # Completion requires a fresh login (a new refresh token).
        return False

    # Move the record to the new owner (cross-user reauth only).
    if owner_uid != user_id:
        users_map = stored_data.get("users", {})
        native_map = stored_data.setdefault("native_devices", {})
        if user_id in users_map and not users_map[user_id].get("deleted", False):
            dest = users_map[user_id].setdefault("devices", {})
        else:
            dest = native_map.setdefault(user_id, {})
        if len(dest) >= 100:
            _LOGGER.warning(
                "CASA: Cannot complete reauthentication of device '%s' — target user already has the maximum of 100 devices.",
                device_id,
            )
            return False
        if owner_uid in users_map and device_id in users_map[owner_uid].get("devices", {}):
            users_map[owner_uid]["devices"].pop(device_id, None)
        elif owner_uid in native_map:
            native_map[owner_uid].pop(device_id, None)
            if not native_map[owner_uid]:
                native_map.pop(owner_uid, None)
        dest[device_id] = device_info

    device_info["refresh_token_id"] = refresh_token_id

    # Revoke the session the device held before the reauth.
    old_user_id = pending.get("old_user_id")
    if old_rtid and old_rtid != refresh_token_id and old_user_id:
        old_user = await hass.auth.async_get_user(old_user_id)
        token = old_user.refresh_tokens.get(old_rtid) if old_user else None
        if token:
            hass.auth.async_remove_refresh_token(token)

    # Drop the queued credentials entry — the device consumed it (or no longer
    # needs it), and it must not be re-delivered on the next pull.
    qu_data = data.get("qu_data", {"updates": {}})
    if _dequeue_update(qu_data, device_id, pending.get("update_id")):
        data["qu_store"].async_delay_save(lambda: qu_data, 2.0)

    device_info.pop("reauth_pending", None)
    data["store"].async_delay_save(lambda: stored_data, 2.0)
    _LOGGER.info(
        "CASA: Device '%s' completed reauthentication to user '%s'.",
        device_id, pending.get("target_username"),
    )

    from homeassistant.helpers.dispatcher import async_dispatcher_send
    async_dispatcher_send(hass, f"casa_device_updated_{device_id}")
    return True


class CasaRegisterDeviceView(HomeAssistantView):
    """View to register devices for push notifications."""

    url = "/api/casa/register_device"
    name = "api:casa:register_device"

    def __init__(self, hass: HomeAssistant, register_device_func):
        self.hass = hass
        self.register_device_func = register_device_func

    async def post(self, request):
        """Handle device registration."""
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        try:
            data = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = data.get("device_id")
        push_token = data.get("push_token")

        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        # Extract bearer token details from request headers
        auth_header = request.headers.get("Authorization")
        last_12_token = None
        refresh_token_id = None
        if auth_header and auth_header.startswith("Bearer "):
            bearer_token = auth_header[7:].strip()
            last_12_token = bearer_token[-12:]
            refresh_token_id = _get_refresh_token_id_from_jwt(bearer_token)

        # Determine the client's IP address from request headers or remote peer
        client_ip = request.headers.get("X-Forwarded-For")
        if client_ip:
            client_ip = client_ip.split(",")[0].strip()
        else:
            client_ip = request.headers.get("X-Real-IP") or request.remote

        try:
            await self.register_device_func(user.id, device_id, push_token, last_12_token, refresh_token_id, client_ip)
        except HomeAssistantError as err:
            return self.json({"error": str(err)}, status_code=400)
        except Exception as err:
            _LOGGER.exception("CASA: Unexpected error during device registration: %s", err)
            return self.json({"error": "Internal server error"}, status_code=500)

        return self.json({"status": "success"})

    async def get(self, request):
        """Check if a device is registered."""
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        device_id = request.query.get("device_id")
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        
        # Check if the user is an active integration user
        if user.id in stored_data["users"] and not stored_data["users"][user.id].get("deleted", False):
            devices = stored_data["users"][user.id].get("devices", {})
        else:
            native_devices = stored_data.get("native_devices", {})
            devices = native_devices.get(user.id, {})
        
        if device_id in devices:
            devices[device_id]["last_seen_at"] = dt_util.now().isoformat()
            
            # Extract and update active token details if available
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                bearer_token = auth_header[7:].strip()
                devices[device_id]["last_12_token"] = bearer_token[-12:]
                refresh_token_id = _get_refresh_token_id_from_jwt(bearer_token)
                if refresh_token_id:
                    devices[device_id]["refresh_token_id"] = refresh_token_id

            store = self.hass.data[DOMAIN]["store"]
            store.async_delay_save(lambda: stored_data, 2.0)
            
            device_info = devices[device_id]
            # "registered" means push-registered: after a push-only unregister the
            # record persists without a push_token and must not read as registered.
            return self.json({
                "registered": bool(device_info.get("push_token")),
                "push_token": device_info.get("push_token"),
                "registered_at": device_info.get("registered_at"),
                "last_seen_at": device_info.get("last_seen_at")
            })
        
        return self.json({"registered": False, "reason": "Device not registered for this user"}, status_code=200)

    async def delete(self, request):
        """Unregister/delete a device."""
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        device_id = request.query.get("device_id")
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        
        if user.id in stored_data["users"] and not stored_data["users"][user.id].get("deleted", False):
            user_entry = stored_data["users"][user.id]
            devices = user_entry.get("devices", {})
            username = user_entry.get("username")
        else:
            native_devices = stored_data.setdefault("native_devices", {})
            if user.id in native_devices:
                devices = native_devices[user.id]
                username = user.name or user.id
            else:
                return self.json({"error": "User not found or deleted"}, status_code=404)
        
        if device_id in devices:
            # Push-only unregister: the device record (and its HA registry entry)
            # stays visible and manageable — only the push registration is cleared.
            # Full removal is an admin action (casa.delete_device / deprovision).
            device_info = devices[device_id]
            proxy_token = device_info.pop("push_token", None)
            device_info.pop("needs_reregister", None)
            await _unregister_relay_token(self.hass, proxy_token, device_id)

            store = self.hass.data[DOMAIN]["store"]
            store.async_delay_save(lambda: stored_data, 2.0)

            _LOGGER.info("CASA: Cleared push registration for device '%s' (user '%s'); record retained.", device_id, username)
            return self.json({"status": "success"})
            
        return self.json({"error": "Device not found"}, status_code=404)


class CasaHeartbeatView(HomeAssistantView):
    """View to handle heartbeats from devices."""

    url = "/api/casa/heartbeat"
    name = "api:casa:heartbeat"

    def __init__(self, hass: HomeAssistant, heartbeat_func):
        self.hass = hass
        self.heartbeat_func = heartbeat_func

    async def post(self, request):
        """Handle heartbeat ping."""
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        try:
            data = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = data.get("device_id")
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        # Extract bearer token details from request headers
        auth_header = request.headers.get("Authorization")
        last_12_token = None
        refresh_token_id = None
        if auth_header and auth_header.startswith("Bearer "):
            bearer_token = auth_header[7:].strip()
            last_12_token = bearer_token[-12:]
            refresh_token_id = _get_refresh_token_id_from_jwt(bearer_token)

        # Determine the client's IP address from request headers or remote peer
        client_ip = request.headers.get("X-Forwarded-For")
        if client_ip:
            client_ip = client_ip.split(",")[0].strip()
        else:
            client_ip = request.headers.get("X-Real-IP") or request.remote

        last_12_token = data.get("last_12_token") or last_12_token
        ip_address = data.get("ip_address") or client_ip
        provisioned_at = data.get("provisioned_at")
        expires_at = data.get("expires_at")
        current_url = data.get("current_url")
        app_version = data.get("app_version")
        wireguard_configured = data.get("wireguard_configured")
        wireguard_connected = data.get("wireguard_connected")
        alias = data.get("alias")
        if not isinstance(alias, str):
            alias = None

        location_state = data.get("location_state")
        location_reason = data.get("location_reason")
        location_config_version = data.get("location_config_version")

        if expires_at is not None:
            try:
                expires_at = int(expires_at)
            except (ValueError, TypeError):
                expires_at = None

        if wireguard_configured is not None:
            wireguard_configured = bool(wireguard_configured)
        if wireguard_connected is not None:
            wireguard_connected = bool(wireguard_connected)

        try:
            result = await self.heartbeat_func(
                user.id,
                device_id,
                last_12_token=last_12_token,
                refresh_token_id=refresh_token_id,
                ip_address=ip_address,
                provisioned_at=provisioned_at,
                expires_at=expires_at,
                current_url=current_url,
                app_version=app_version,
                wireguard_configured=wireguard_configured,
                wireguard_connected=wireguard_connected,
                alias=alias
            )
        except HomeAssistantError as err:
            return self.json({"error": str(err)}, status_code=400)
        except Exception as err:
            _LOGGER.exception("CASA: Unexpected error during heartbeat: %s", err)
            return self.json({"error": "Internal server error"}, status_code=500)

        # reregister=true tells the device its relay registration was lost (detected
        # by /reconcile) and it should re-register and report a fresh proxy token.
        # updates=true tells the device to pull queued updates from /api/casa/profile_updates.
        # device_key is the shared secret used to decrypt encrypted pushes; device_key_id
        # lets the device detect when its stored key is stale after a rotation.
        stored_data = self.hass.data[DOMAIN]["stored_data"]

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

        device_key = stored_data.get("device_key")
        response = {
            "status": "success",
            "reregister": bool(result.get("reregister")),
            "updates": bool(result.get("updates")),
            "device_key": device_key,
            "device_key_id": _device_key_id(device_key) if device_key else None,
            # require_alias is the site-wide flag only; the per-profile flag is
            # carried by the provisioning payload / profile updates and ORed
            # with this on the device. has_alias reflects the stored alias.
            "require_alias": bool(result.get("require_alias")),
            "has_alias": bool(result.get("has_alias")),
            "heartbeat_interval_seconds": result.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            "profile_report_interval_seconds": result.get("profile_report_interval_seconds", DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS),
            "location_config_version": server_lz_version or None,
        }
        # Only emitted while an admin-set override is pending; the stored value is
        # never echoed back, so a freshly re-provisioned device keeps its own expiry.
        if result.get("expires_at") is not None:
            response["expires_at"] = result["expires_at"]
        return self.json(response)


class CasaDeviceProfileReportView(HomeAssistantView):
    """Device-authenticated endpoint for a device to self-report its current
    provisioning/profile state, so the admin panel can show what a specific
    device is actually running (there is no other durable source of this —
    provisioning never learns a device's id, see _provision_internal)."""

    url = "/api/casa/profile_report"
    name = "api:casa:profile_report"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def post(self, request):
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        try:
            data = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = str(data.get("device_id", "")).strip()
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        fields = data.get("fields")
        if not isinstance(fields, dict):
            return self.json({"error": "Missing or invalid 'fields' object"}, status_code=400)

        # Devices only ever report live settings; filtering here makes device
        # records self-cleaning by construction and keeps a buggy client from
        # injecting arbitrary keys into admin-panel-rendered state.
        fields = {k: v for k, v in fields.items() if k in LIVE_PROVISIONING_FIELDS}

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        device_info, _uid, _username = _find_device_record(stored_data, device_id)
        if device_info is None:
            return self.json({"error": "Device not found"}, status_code=404)

        device_info["provisioning_fields"] = fields
        device_info["provisioning_reported_at"] = dt_util.now().isoformat()
        device_info["provisioning_pending_push"] = False

        store = self.hass.data[DOMAIN]["store"]
        store.async_delay_save(lambda: stored_data, 2.0)

        return self.json({"status": "success"})


class CasaAdminSummaryView(HomeAssistantView):
    """Admin-only JSON summary backing the Casa sidebar panel."""

    url = "/api/casa/admin/summary"
    name = "api:casa:admin:summary"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        from datetime import datetime
        from .const import STALE_DAYS

        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        stored_data = self.hass.data.get(DOMAIN, {}).get("stored_data", {})
        qu_updates = self.hass.data.get(DOMAIN, {}).get("qu_data", {}).get("updates", {})
        now = dt_util.now()

        def _pending(did):
            entries = qu_updates.get(did, [])
            return [
                {
                    "id": e.get("id"),
                    "type": e.get("type"),
                    "action": e.get("action"),
                    "created_at": e.get("created_at"),
                }
                for e in entries
            ]

        def _stale(last_seen) -> bool:
            if not last_seen:
                return True
            try:
                return (now - datetime.fromisoformat(last_seen)).days >= STALE_DAYS
            except Exception:
                return False

        def _reauth(dinfo):
            rp = dinfo.get("reauth_pending")
            if not rp:
                return None
            return {
                "target_username": rp.get("target_username"),
                "requested_at": rp.get("requested_at"),
                "requested_by": rp.get("requested_by"),
                "update_id": rp.get("update_id"),
            }

        devices = []

        # Integration-managed users + their devices.
        for udata in stored_data.get("users", {}).values():
            if udata.get("deleted", False):
                continue
            username = udata.get("username", "Unknown")
            for did, dinfo in udata.get("devices", {}).items():
                devices.append({
                    "username": username,
                    "device_id": did,
                    "ip": dinfo.get("ip_address"),
                    "last_seen": dinfo.get("last_seen_at"),
                    "push_registered": bool(dinfo.get("push_token")),
                    "orphaned": bool(dinfo.get("needs_reregister", False)),
                    "stale": _stale(dinfo.get("last_seen_at")),
                    "native": False,
                    "alias": dinfo.get("alias", ""),
                    "registered_at": dinfo.get("registered_at"),
                    "push_token": dinfo.get("push_token"),
                    "last_12_token": dinfo.get("last_12_token"),
                    "refresh_token_id": dinfo.get("refresh_token_id"),
                    "app_version": dinfo.get("app_version"),
                    "wireguard_configured": dinfo.get("wireguard_configured"),
                    "wireguard_connected": dinfo.get("wireguard_connected"),
                    "current_url": dinfo.get("current_url"),
                    "provisioned_at": dinfo.get("provisioned_at"),
                    "expires_at": dinfo.get("expires_at"),
                    "expires_at_override": dinfo.get("expires_at_override"),
                    "expires_at_override_set_at": dinfo.get("expires_at_override_set_at"),
                    "pending_updates": len(qu_updates.get(did, [])),
                    "pending_update_list": _pending(did),
                    "provisioning_fields": dinfo.get("provisioning_fields", {}),
                    "provisioning_reported_at": dinfo.get("provisioning_reported_at"),
                    "provisioning_pending_push": bool(dinfo.get("provisioning_pending_push", False)),
                    "provisioning_profile_id": dinfo.get("provisioning_profile_id"),
                    "provisioning_profile_name": dinfo.get("provisioning_profile_name"),
                    "reauth_pending": _reauth(dinfo),
                })

        # Native devices (HA users not managed by the integration).
        native = stored_data.get("native_devices", {})
        if native:
            ha_users = await self.hass.auth.async_get_users()
            user_map = {u.id: (u.name or u.id) for u in ha_users}
            for uid, devs in native.items():
                username = user_map.get(uid) or f"Native {uid[:6]}"
                for did, dinfo in devs.items():
                    devices.append({
                        "username": username,
                        "device_id": did,
                        "ip": dinfo.get("ip_address"),
                        "last_seen": dinfo.get("last_seen_at"),
                        "push_registered": bool(dinfo.get("push_token")),
                        "orphaned": bool(dinfo.get("needs_reregister", False)),
                        "stale": _stale(dinfo.get("last_seen_at")),
                        "native": True,
                        "alias": dinfo.get("alias", ""),
                        "registered_at": dinfo.get("registered_at"),
                        "push_token": dinfo.get("push_token"),
                        "last_12_token": dinfo.get("last_12_token"),
                        "refresh_token_id": dinfo.get("refresh_token_id"),
                        "app_version": dinfo.get("app_version"),
                        "wireguard_configured": dinfo.get("wireguard_configured"),
                        "wireguard_connected": dinfo.get("wireguard_connected"),
                        "current_url": dinfo.get("current_url"),
                        "provisioned_at": dinfo.get("provisioned_at"),
                        "expires_at": dinfo.get("expires_at"),
                        "expires_at_override": dinfo.get("expires_at_override"),
                        "expires_at_override_set_at": dinfo.get("expires_at_override_set_at"),
                        "pending_updates": len(qu_updates.get(did, [])),
                        "pending_update_list": _pending(did),
                        "provisioning_fields": dinfo.get("provisioning_fields", {}),
                        "provisioning_reported_at": dinfo.get("provisioning_reported_at"),
                        "provisioning_pending_push": bool(dinfo.get("provisioning_pending_push", False)),
                        "provisioning_profile_id": dinfo.get("provisioning_profile_id"),
                        "provisioning_profile_name": dinfo.get("provisioning_profile_name"),
                        "reauth_pending": _reauth(dinfo),
                    })

        accounts = []
        for uid, udata in stored_data.get("users", {}).items():
            if udata.get("deleted", False):
                continue
            accounts.append({
                "name": udata.get("name"),
                "username": udata.get("username"),
                "created_at": udata.get("created_at"),
                "created_by": udata.get("created_by"),
                "device_count": len(udata.get("devices", {})),
            })

        device_key = stored_data.get("device_key")
        from .const import CASA_VERSION
        return self.json({
            "version": CASA_VERSION,
            "site_id": stored_data.get("site_id"),
            "device_key_id": _device_key_id(device_key) if device_key else None,
            "require_device_alias": bool(stored_data.get("require_device_alias", False)),
            "heartbeat_interval_seconds": stored_data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            "profile_report_interval_seconds": stored_data.get("profile_report_interval_seconds", DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS),
            "stats": {
                "devices": len(devices),
                "managed_users": len(accounts),
                "orphaned": sum(1 for d in devices if d["orphaned"]),
                "stale": sum(1 for d in devices if d["stale"]),
                "pending_updates": sum(d.get("pending_updates", 0) for d in devices),
            },
            "devices": devices,
            "accounts": accounts,
        })


class CasaWireGuardProfilesView(HomeAssistantView):
    """Admin-only CRUD for WireGuard profiles stored in Casa_WireGuardProfiles."""

    url = "/api/casa/admin/wireguard_profiles"
    name = "api:casa:admin:wireguard_profiles"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        wg_data = self.hass.data.get(DOMAIN, {}).get("wg_data", {"profiles": []})
        return self.json({"profiles": wg_data.get("profiles", [])})

    async def post(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        config = body.get("config", "").strip()
        if not config:
            return self.json({"error": "config is required"}, status_code=400)

        alias = body.get("alias", "").strip()
        if not alias:
            suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
            alias = f"WireGuard {suffix}"

        excluded_wifi = body.get("excluded_wifi", "").strip()

        profile = {
            "id": str(uuid.uuid4()),
            "alias": alias,
            "config": config,
            "excluded_wifi": excluded_wifi,
            "created_at": dt_util.now().isoformat(),
        }

        wg_data = self.hass.data[DOMAIN]["wg_data"]
        wg_data.setdefault("profiles", []).append(profile)
        wg_store = self.hass.data[DOMAIN]["wg_store"]
        wg_store.async_delay_save(lambda: wg_data, 2.0)

        _LOGGER.info("CASA: Created WireGuard profile '%s' (id=%s).", alias, profile["id"])
        return self.json(profile, status_code=201)

    async def put(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        profile_id = body.get("id", "").strip()
        if not profile_id:
            return self.json({"error": "Missing id"}, status_code=400)

        wg_data = self.hass.data[DOMAIN]["wg_data"]
        target = None
        for p in wg_data.get("profiles", []):
            if p.get("id") == profile_id:
                target = p
                break
        if not target:
            return self.json({"error": "Profile not found"}, status_code=404)

        config = body.get("config", "").strip()
        if config:
            target["config"] = config

        alias = body.get("alias", "").strip()
        if alias:
            target["alias"] = alias

        if "excluded_wifi" in body:
            target["excluded_wifi"] = body.get("excluded_wifi", "").strip()

        wg_store = self.hass.data[DOMAIN]["wg_store"]
        wg_store.async_delay_save(lambda: wg_data, 2.0)

        _LOGGER.info("CASA: Updated WireGuard profile '%s' (id=%s).", target["alias"], profile_id)
        return self.json(target)

    async def delete(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        profile_id = request.query.get("id", "").strip()
        if not profile_id:
            return self.json({"error": "Missing id query parameter"}, status_code=400)

        wg_data = self.hass.data[DOMAIN]["wg_data"]
        profiles = wg_data.get("profiles", [])
        before_len = len(profiles)
        wg_data["profiles"] = [p for p in profiles if p.get("id") != profile_id]

        if len(wg_data["profiles"]) == before_len:
            return self.json({"error": "Profile not found"}, status_code=404)

        wg_store = self.hass.data[DOMAIN]["wg_store"]
        wg_store.async_delay_save(lambda: wg_data, 2.0)

        _LOGGER.info("CASA: Deleted WireGuard profile id=%s.", profile_id)
        return self.json({"status": "ok"})


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


class CasaAdminDeviceView(HomeAssistantView):
    """Admin-only API to inspect/update registered devices."""

    url = "/api/casa/admin/device"
    name = "api:casa:admin:device"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def put(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = body.get("device_id", "").strip()
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        stored_data = self.hass.data[DOMAIN]["stored_data"]

        device_info, _, _ = _find_device_record(stored_data, device_id)
        if device_info is None:
            return self.json({"error": "Device not found"}, status_code=404)

        # Fields are updated only when present in the body, so callers can set
        # one without clobbering the others.
        if "alias" in body:
            device_info["alias"] = str(body.get("alias") or "").strip()[:DEVICE_ALIAS_MAX_LEN]

        if "expires_at_override" in body:
            value = body.get("expires_at_override")
            if value is not None:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    return self.json({"error": "expires_at_override must be an integer or null"}, status_code=400)
                if value < 0:
                    return self.json({"error": "expires_at_override must be >= 0"}, status_code=400)
            _set_expiry_override(stored_data, device_id, value)

        # "Force Device Changes": a direct per-device stamp of live settings.
        # Pushed through the same encrypted profile-update pipeline template
        # applies use (update_type "profile", profile_id None) — the app
        # already knows how to apply this exact envelope, so no app-side
        # changes are needed for the edit to take effect.
        pushed = False
        if "provisioning_fields" in body:
            fields = body.get("provisioning_fields")
            if not isinstance(fields, dict):
                return self.json({"error": "provisioning_fields must be an object"}, status_code=400)

            # Only live device settings may be stored or pushed post-provision;
            # process-scope keys (password, pin, timeout_minutes, ...) are
            # dropped even if an older panel or manual API client sends them.
            fields = {k: v for k, v in fields.items() if k in LIVE_PROVISIONING_FIELDS}

            device_info["provisioning_fields"] = fields
            device_info["provisioning_pending_push"] = True

            qu_data = self.hass.data[DOMAIN]["qu_data"]
            session = async_get_clientsession(self.hass)
            created_by = user.name or user.id
            payload = {"profile_id": None, "name": "Custom device settings", "fields": fields}
            _update_id, pushed, _skipped = await _enqueue_and_push_update(
                stored_data, qu_data, session, device_id, device_info,
                "profile", "update", payload, created_by, send_push=True,
            )
            qu_store = self.hass.data[DOMAIN]["qu_store"]
            qu_store.async_delay_save(lambda: qu_data, 2.0)
            # Nudge only when the encrypted push did NOT go out (see the same
            # pattern in CasaAdminQueueUpdateView — spares the relay's small
            # per-device burst budget).
            if not pushed:
                await _nudge_device_checkin(session, stored_data, device_info)

        # Save store
        store = self.hass.data[DOMAIN]["store"]
        store.async_delay_save(lambda: stored_data, 2.0)

        return self.json({
            "status": "ok",
            "device_id": device_id,
            "alias": device_info.get("alias", ""),
            "expires_at": device_info.get("expires_at"),
            "expires_at_override": device_info.get("expires_at_override"),
            "expires_at_override_set_at": device_info.get("expires_at_override_set_at"),
            "provisioning_fields": device_info.get("provisioning_fields", {}),
            "provisioning_pending_push": bool(device_info.get("provisioning_pending_push", False)),
            "pushed": pushed,
        })


class CasaAdminSettingsView(HomeAssistantView):
    """Admin-only GET/PUT for site-wide settings stored on the main casa store."""

    url = "/api/casa/admin/settings"
    name = "api:casa:admin:settings"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        return self.json({
            "require_device_alias": bool(stored_data.get("require_device_alias", False)),
            "heartbeat_interval_seconds": stored_data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            "profile_report_interval_seconds": stored_data.get("profile_report_interval_seconds", DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS),
        })

    async def put(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        stored_data = self.hass.data[DOMAIN]["stored_data"]

        if "require_device_alias" in body:
            stored_data["require_device_alias"] = bool(body.get("require_device_alias"))

        if "heartbeat_interval_seconds" in body:
            try:
                interval = int(body.get("heartbeat_interval_seconds"))
            except (TypeError, ValueError):
                return self.json({"error": "heartbeat_interval_seconds must be an integer"}, status_code=400)
            if not (MIN_HEARTBEAT_INTERVAL_SECONDS <= interval <= MAX_HEARTBEAT_INTERVAL_SECONDS):
                return self.json(
                    {"error": f"heartbeat_interval_seconds must be between {MIN_HEARTBEAT_INTERVAL_SECONDS} and {MAX_HEARTBEAT_INTERVAL_SECONDS}"},
                    status_code=400,
                )
            stored_data["heartbeat_interval_seconds"] = interval

        if "profile_report_interval_seconds" in body:
            try:
                report_interval = int(body.get("profile_report_interval_seconds"))
            except (TypeError, ValueError):
                return self.json({"error": "profile_report_interval_seconds must be an integer"}, status_code=400)
            if not (MIN_PROFILE_REPORT_INTERVAL_SECONDS <= report_interval <= MAX_PROFILE_REPORT_INTERVAL_SECONDS):
                return self.json(
                    {"error": f"profile_report_interval_seconds must be between {MIN_PROFILE_REPORT_INTERVAL_SECONDS} and {MAX_PROFILE_REPORT_INTERVAL_SECONDS}"},
                    status_code=400,
                )
            stored_data["profile_report_interval_seconds"] = report_interval

        store = self.hass.data[DOMAIN]["store"]
        store.async_delay_save(lambda: stored_data, 2.0)

        return self.json({
            "status": "ok",
            "require_device_alias": bool(stored_data.get("require_device_alias", False)),
            "heartbeat_interval_seconds": stored_data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            "profile_report_interval_seconds": stored_data.get("profile_report_interval_seconds", DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS),
        })


class CasaAdminSessionsView(HomeAssistantView):
    """Admin-only listing/revocation of HA refresh tokens (login sessions)."""

    url = "/api/casa/admin/sessions"
    name = "api:casa:admin:sessions"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        current_token_id = request.get("hass_refresh_token_id")
        stored_data = self.hass.data.get(DOMAIN, {}).get("stored_data", {})

        # Correlate refresh tokens to casa device records so the panel can
        # show which session belongs to which device.
        token_devices = {}
        casa_users = {}
        for uid, udata in stored_data.get("users", {}).items():
            if udata.get("deleted", False):
                continue
            casa_users[uid] = udata
            for did, dinfo in udata.get("devices", {}).items():
                rtid = dinfo.get("refresh_token_id")
                if rtid:
                    token_devices[rtid] = {"device_id": did, "alias": dinfo.get("alias", "")}
        for uid, devs in stored_data.get("native_devices", {}).items():
            for did, dinfo in devs.items():
                rtid = dinfo.get("refresh_token_id")
                if rtid:
                    token_devices[rtid] = {"device_id": did, "alias": dinfo.get("alias", "")}

        users_out = []
        for ha_user in await self.hass.auth.async_get_users():
            if ha_user.system_generated:
                continue

            sessions = []
            for token in ha_user.refresh_tokens.values():
                if getattr(token, "token_type", "normal") == "system":
                    continue
                created_at = getattr(token, "created_at", None)
                last_used_at = getattr(token, "last_used_at", None)
                device = token_devices.get(token.id, {})
                sessions.append({
                    "token_id": token.id,
                    "token_suffix": token.id[-12:],
                    "client_name": getattr(token, "client_name", None),
                    "client_id": getattr(token, "client_id", None),
                    "token_type": getattr(token, "token_type", None),
                    "created_at": created_at.isoformat() if created_at else None,
                    "last_used_at": last_used_at.isoformat() if last_used_at else None,
                    "last_used_ip": getattr(token, "last_used_ip", None),
                    "expire_at": getattr(token, "expire_at", None),
                    "is_current": token.id == current_token_id,
                    "device_id": device.get("device_id"),
                    "device_alias": device.get("alias"),
                })
            sessions.sort(key=lambda s: s.get("last_used_at") or "", reverse=True)

            casa_record = casa_users.get(ha_user.id)
            users_out.append({
                "user_id": ha_user.id,
                "name": ha_user.name or ha_user.id,
                "username": casa_record.get("username") if casa_record else None,
                "casa_managed": casa_record is not None,
                "is_owner": bool(getattr(ha_user, "is_owner", False)),
                "is_admin": bool(getattr(ha_user, "is_admin", False)),
                "is_active": bool(getattr(ha_user, "is_active", True)),
                "session_count": len(sessions),
                "sessions": sessions,
            })

        users_out.sort(key=lambda u: (not u["casa_managed"], (u["name"] or "").casefold()))

        return self.json({
            "current_token_id": current_token_id,
            "users": users_out,
        })

    async def delete(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        user_id = request.query.get("user_id", "").strip()
        token_id = request.query.get("token_id", "").strip()
        if not user_id or not token_id:
            return self.json({"error": "user_id and token_id are required"}, status_code=400)

        target = await self.hass.auth.async_get_user(user_id)
        if not target:
            return self.json({"error": "User not found"}, status_code=404)

        token = target.refresh_tokens.get(token_id)
        if not token:
            return self.json({"error": "Session not found"}, status_code=404)

        self.hass.auth.async_remove_refresh_token(token)
        _LOGGER.info(
            "CASA: Session '%s' revoked for %s by %s.",
            token_id[-12:], target.name, user.name,
        )

        return self.json({
            "status": "ok",
            "revoked": token_id,
            "was_current": token_id == request.get("hass_refresh_token_id"),
        })


class CasaAdminCheckUsernameView(HomeAssistantView):
    """Admin-only availability check for a prospective guest username/name.

    Checks both HA display names (what create_user rejects on) and
    homeassistant-provider credential usernames (what add_auth would
    crash on) so the panel can warn before attempting account creation.
    """

    url = "/api/casa/admin/check_username"
    name = "api:casa:admin:check_username"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        username = request.query.get("username", "").strip().casefold()
        display_name = request.query.get("name", "").strip().casefold()
        if not username and not display_name:
            return self.json({"error": "username or name is required"}, status_code=400)

        users = await self.hass.auth.async_get_users()

        def _cred_usernames(u):
            for cred in u.credentials:
                if cred.auth_provider_type == "homeassistant":
                    yield str(cred.data.get("username", "")).casefold()

        username_conflict = bool(username) and any(
            (u.name and u.name.casefold() == username) or username in _cred_usernames(u)
            for u in users
        )
        name_conflict = bool(display_name) and any(
            u.name and u.name.casefold() == display_name for u in users
        )

        return self.json({
            "available": not (username_conflict or name_conflict),
            "username_conflict": username_conflict,
            "name_conflict": name_conflict,
        })


def _coerce_template_fields(src: dict) -> dict:
    """Sparse-coerce provision-template fields.

    Keeps only known PROFILE_PROVISIONING_FIELDS keys, coerced by the type of
    their schema default. Keys whose coerced value equals the default are
    dropped — a template stores a field iff it sets a non-default value, and
    an absent key means "unset" (surfaced as a fill-in-the-blank at provision
    time). Unparseable ints are dropped rather than defaulted.
    """
    fields = {}
    for key, default in PROFILE_PROVISIONING_FIELDS.items():
        if key not in src:
            continue
        val = src[key]
        if isinstance(default, bool):
            if isinstance(val, str):
                val = val.strip().lower() == "true"
            else:
                val = bool(val)
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        else:
            val = "" if val is None else str(val)
        if val == default:
            continue
        fields[key] = val
    return fields


def _migrate_provision_templates(pp_data: dict) -> bool:
    """Sparse-normalize stored provision templates; returns True if changed.

    Legacy saves coerced every field to a concrete value (and could carry
    one-time process keys), so key presence carried no authorial intent.
    Idempotent — safe to run on every startup.
    """
    changed = False
    for template in pp_data.get("profiles", []):
        old = template.get("fields", {})
        new = _coerce_template_fields(old)
        if new != old:
            template["fields"] = new
            changed = True
    return changed


class CasaProvisionProfilesView(HomeAssistantView):
    """Admin-only CRUD for provision templates stored in casa_provision_profiles.

    Templates persist only template-appropriate fields (live device settings
    + expiration_hours), sparsely: an absent key means the template does not
    set that field. One-time process inputs (username/password/pin,
    deauthenticate_existing, timeout_minutes, password_scramble*,
    connect_wifi_*) belong in casa.provision service_data, never on a saved
    template. The URL keeps its historical "provision_profiles" name for
    compatibility with version-skewed panels.
    """

    url = "/api/casa/admin/provision_profiles"
    name = "api:casa:admin:provision_profiles"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        pp_data = self.hass.data.get(DOMAIN, {}).get("pp_data", {"profiles": []})
        return self.json({"profiles": pp_data.get("profiles", [])})

    async def post(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        name = body.get("name", "").strip()
        if not name:
            suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
            name = f"Template {suffix}"

        if isinstance(body.get("fields"), dict):
            fields = _coerce_template_fields(body["fields"])
        else:
            # Legacy flat body from a version-skewed panel; sparse-normalizing
            # converges it to the same shape. TODO(remove after 2 releases)
            fields = _coerce_template_fields(body)

        now_iso = dt_util.now().isoformat()
        profile = {
            "id": str(uuid.uuid4()),
            "name": name,
            "created_at": now_iso,
            "updated_at": now_iso,
            "fields": fields,
        }

        pp_data = self.hass.data[DOMAIN]["pp_data"]
        pp_data.setdefault("profiles", []).append(profile)
        pp_store = self.hass.data[DOMAIN]["pp_store"]
        pp_store.async_delay_save(lambda: pp_data, 2.0)

        _LOGGER.info("CASA: Created provision template '%s' (id=%s).", name, profile["id"])
        return self.json(profile, status_code=201)

    async def put(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        profile_id = body.get("id", "").strip()
        if not profile_id:
            return self.json({"error": "Missing id"}, status_code=400)

        pp_data = self.hass.data[DOMAIN]["pp_data"]
        target = None
        for p in pp_data.get("profiles", []):
            if p.get("id") == profile_id:
                target = p
                break
        if not target:
            return self.json({"error": "Template not found"}, status_code=404)

        name = body.get("name", "").strip()
        if name:
            target["name"] = name

        if isinstance(body.get("fields"), dict):
            # Replace wholesale: the editor always sends its full sparse dict,
            # and replace-not-merge is what makes un-setting a field possible.
            target["fields"] = _coerce_template_fields(body["fields"])
        else:
            # Legacy flat body from a version-skewed panel: merge present keys
            # as before, then sparse-normalize. TODO(remove after 2 releases)
            merged = dict(target.get("fields", {}))
            for key in PROFILE_PROVISIONING_FIELDS:
                if key in body:
                    merged[key] = body[key]
            target["fields"] = _coerce_template_fields(merged)
        target["updated_at"] = dt_util.now().isoformat()

        pp_store = self.hass.data[DOMAIN]["pp_store"]
        pp_store.async_delay_save(lambda: pp_data, 2.0)

        _LOGGER.info("CASA: Updated provision template '%s' (id=%s).", target["name"], profile_id)
        return self.json(target)

    async def delete(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        profile_id = request.query.get("id", "").strip()
        if not profile_id:
            return self.json({"error": "Missing id query parameter"}, status_code=400)

        pp_data = self.hass.data[DOMAIN]["pp_data"]
        profiles = pp_data.get("profiles", [])
        before_len = len(profiles)
        pp_data["profiles"] = [p for p in profiles if p.get("id") != profile_id]

        if len(pp_data["profiles"]) == before_len:
            return self.json({"error": "Template not found"}, status_code=404)

        pp_store = self.hass.data[DOMAIN]["pp_store"]
        pp_store.async_delay_save(lambda: pp_data, 2.0)

        _LOGGER.info("CASA: Deleted provision template id=%s.", profile_id)
        return self.json({"status": "ok"})


class CasaProfileUpdatesView(HomeAssistantView):
    """Device-facing endpoint to pull and acknowledge queued updates.

    A device learns it has work via the heartbeat ("updates": true), then GETs its
    queued entries here and POSTs back each consumed id to dequeue it. Entries are
    returned in plaintext over the authenticated HA TLS connection — only the
    optional push-notification copy is encrypted.
    """

    url = "/api/casa/profile_updates"
    name = "api:casa:profile_updates"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    def _authorize(self, request, device_id):
        """Return (device_info, error_response). Verifies the caller owns the device
        by matching the bearer JWT's stable refresh_token_id to the device record."""
        stored_data = self.hass.data[DOMAIN]["stored_data"]
        device_info, _uid, _username = _find_device_record(stored_data, device_id)
        if not device_info:
            return None, self.json({"error": "Device not found"}, status_code=404)

        stored_refresh_id = device_info.get("refresh_token_id")
        auth_header = request.headers.get("Authorization")
        bearer_refresh_id = None
        if auth_header and auth_header.startswith("Bearer "):
            bearer_refresh_id = _get_refresh_token_id_from_jwt(auth_header[7:].strip())

        if not stored_refresh_id or not bearer_refresh_id or bearer_refresh_id != stored_refresh_id:
            _LOGGER.warning(
                "CASA: Rejected profile_updates access for device '%s' (token mismatch).",
                device_id,
            )
            return None, self.json({"error": "Forbidden"}, status_code=403)

        return device_info, None

    async def get(self, request):
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        device_id = request.query.get("device_id")
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        _device_info, err = self._authorize(request, device_id)
        if err:
            return err

        qu_data = self.hass.data[DOMAIN]["qu_data"]
        updates = qu_data.get("updates", {}).get(device_id, [])
        if any(e.get("type") == "auth" for e in updates):
            # Never hand a device credentials for a user that no longer
            # exists: it would log out to apply them, fail the login, and
            # treat the failure as a revoked session (self-wipe). Prune,
            # then serve whatever legitimately remains.
            await _prune_stale_queued_updates(self.hass)
            updates = qu_data.get("updates", {}).get(device_id, [])
        return self.json({"updates": updates})

    async def post(self, request):
        """Acknowledge consumed updates by id, removing them from the queue."""
        user = request.get("hass_user")
        if not user:
            return self.json({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = body.get("device_id")
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        ack_ids = body.get("ids")
        if ack_ids is None and body.get("id"):
            ack_ids = [body.get("id")]
        if not ack_ids or not isinstance(ack_ids, list):
            return self.json({"error": "Missing id or ids"}, status_code=400)

        _device_info, err = self._authorize(request, device_id)
        if err:
            return err

        qu_data = self.hass.data[DOMAIN]["qu_data"]
        ack_set = set(ack_ids)
        entries = qu_data.get("updates", {}).get(device_id, [])
        remaining = [e for e in entries if e.get("id") not in ack_set]

        if remaining:
            qu_data["updates"][device_id] = remaining
        else:
            qu_data.get("updates", {}).pop(device_id, None)

        qu_store = self.hass.data[DOMAIN]["qu_store"]
        qu_store.async_delay_save(lambda: qu_data, 2.0)

        return self.json({"status": "ok", "remaining": len(remaining)})


async def _send_push_to_relay(session, payload) -> bool:
    for url in RELAY_URLS:
        try:
            async with session.post(url, json=payload, timeout=ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                text = await resp.text()
                _LOGGER.warning("CASA: Relay %s returned %s for push: %s", url, resp.status, text)
                if resp.status < 500:
                    return False
        except Exception as err:
            _LOGGER.warning("CASA: Failed to reach relay %s for push: %s", url, err)
    return False


async def _nudge_device_checkin(session, stored_data, device_info, command="request_heartbeat"):
    """Best-effort, content-free silent push asking a device to check in
    (heartbeat) right away instead of waiting for its next scheduled tick.
    The actual state change already lives durably (qu_data queue /
    stored_data) — this only accelerates the device noticing it. Nesting
    the command under "data" is required: the relay's request schema only
    declares a `data` field for caller-supplied extras, so anything sent as
    a top-level key is silently dropped before it ever reaches APNs (see
    the now-fixed casa_update/deprovision/wireguard_update payloads below).
    No-ops if the device has no push_token; never raises."""
    push_token = device_info.get("push_token")
    if not push_token:
        return False
    payload = {
        "title": "",
        "message": "",
        "target": push_token,
        "site_id": stored_data.get("site_id"),
        "site_key": stored_data.get("site_key"),
        "push_type": "background",
        "priority": 5,
        "data": {"command": command},
    }
    return await _send_push_to_relay(session, payload)


async def _send_encrypted_update_push(stored_data, session, device_id, device_info, update_id, update_type, action, payload) -> bool:
    """Deliver an already-queued update over an encrypted silent push.
    Returns False (without raising) when the device has no push_token /
    device_key or encryption fails — the durable queue still delivers."""
    device_key = stored_data.get("device_key")
    push_token = device_info.get("push_token")
    if not push_token or not device_key:
        return False
    inner = {"id": update_id, "type": update_type, "action": action, "payload": payload, "ts": int(time.time())}
    try:
        enc = _encrypt_push_payload(json.dumps(inner), device_key, device_id)
    except Exception as e:
        _LOGGER.error("CASA ERROR: Failed to encrypt queued update for device '%s': %s", device_id, e)
        return False
    return await _send_push_to_relay(session, {
        "target": push_token,
        "site_id": stored_data.get("site_id"),
        "site_key": stored_data.get("site_key"),
        "title": "",
        "message": "",
        "push_type": "background",
        "priority": 5,
        "data": {
            "command": "casa_update",
            "encrypted": True,
            "update_payload": enc,
            "update_id": update_id,
            "device_key_id": _device_key_id(device_key),
        },
    })


async def _enqueue_and_push_update(stored_data, qu_data, session, device_id, device_info, update_type, action, payload, created_by, send_push=True):
    """Enqueue a durable update for a device and optionally deliver it via an
    encrypted silent push. Used by CasaAdminDeviceView's "Force Device
    Changes" (single-device off-profile pushes); bulk template applies go
    through CasaAdminQueueUpdateView, which enqueues synchronously and
    delivers in the background instead.

    Returns (update_id, pushed, skipped). `skipped` means the push was
    requested but couldn't be attempted (no push_token / device_key yet).
    """
    update_id = _enqueue_update(qu_data, device_id, update_type, action, payload, created_by)
    pushed = False
    skipped = False
    if send_push:
        if not device_info.get("push_token") or not stored_data.get("device_key"):
            skipped = True
        else:
            pushed = await _send_encrypted_update_push(stored_data, session, device_id, device_info, update_id, update_type, action, payload)
    return update_id, pushed, skipped


async def _deliver_updates_in_background(hass, stored_data, jobs, update_type, action, payload, send_update_push, notify_push, title, message, created_by):
    """Best-effort delivery accelerators for already-queued updates, run off
    the request path so CasaAdminQueueUpdateView can respond immediately
    (each relay POST can block up to 10s and jobs run sequentially). Every
    update in `jobs` is already in the durable queue — devices pick it up on
    their next heartbeat even if every push here fails."""
    session = async_get_clientsession(hass)
    pushed = notified = 0
    for device_id, device_info, update_id in jobs:
        if send_update_push:
            ok = await _send_encrypted_update_push(stored_data, session, device_id, device_info, update_id, update_type, action, payload)
            if ok:
                pushed += 1
            else:
                # Nudge only when the encrypted push did NOT go out — it's a
                # delivery accelerator, and the relay's per-device rate limit
                # has a small burst budget; a redundant nudge here can starve
                # the visible notify push below.
                await _nudge_device_checkin(session, stored_data, device_info)
        if notify_push and title and message:
            push_token = device_info.get("push_token")
            if push_token:
                ok = await _send_push_to_relay(session, {
                    "target": push_token,
                    "site_id": stored_data.get("site_id"),
                    "site_key": stored_data.get("site_key"),
                    "title": title,
                    "message": message,
                    "data": {"update_id": update_id, "type": update_type, "action": action},
                })
                if ok:
                    notified += 1
    _LOGGER.info(
        "CASA: Background delivery finished for %s '%s' update(s) by %s: pushed=%s notified=%s.",
        len(jobs), update_type, created_by, pushed, notified,
    )


class CasaAdminQueueUpdateView(HomeAssistantView):
    """Admin-only endpoint to queue a profile/WireGuard update for a device.

    The update is always written to the durable queue (consumed by the device on its
    next heartbeat via /api/casa/profile_updates). Two independent flags optionally
    accelerate delivery: send_update_push delivers the full payload over an encrypted
    silent push; notify_push sends a visible notification. Both carry the update id so
    the device can dequeue whichever way it consumes the update.
    """

    url = "/api/casa/admin/queue_update"
    name = "api:casa:admin:queue_update"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def _resolve_targets(self, device_id, username, device_ids=None):
        """Return (targets, not_found) for the requested target(s).

        targets is a list of (device_id, device_info), or None when a single
        requested device/user does not exist. With device_ids (bulk template
        apply), unknown ids are skipped and counted in not_found instead of
        failing the whole batch.
        """
        stored_data = self.hass.data[DOMAIN]["stored_data"]
        if device_ids:
            targets = []
            not_found = 0
            for did in device_ids:
                device_info, _uid, _name = _find_device_record(stored_data, did)
                if device_info:
                    targets.append((did, device_info))
                else:
                    not_found += 1
            return targets, not_found
        if device_id:
            device_info, _uid, _name = _find_device_record(stored_data, device_id)
            if not device_info:
                return None, 0
            return [(device_id, device_info)], 0

        users = await self.hass.auth.async_get_users()
        target_user = next((u for u in users if u.name and u.name.casefold() == username.casefold()), None)
        if not target_user:
            for u in users:
                for cred in u.credentials:
                    if cred.auth_provider_type == "homeassistant" and cred.data.get("username", "").casefold() == username.casefold():
                        target_user = u
                        break
                if target_user:
                    break
        if not target_user:
            return None, 0

        uid = target_user.id
        if uid in stored_data["users"] and not stored_data["users"][uid].get("deleted", False):
            devices = stored_data["users"][uid].get("devices", {})
        else:
            devices = stored_data.get("native_devices", {}).get(uid, {})
        return list(devices.items()), 0

    async def post(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON"}, status_code=400)

        device_id = str(body.get("device_id", "")).strip()
        username = str(body.get("username", "")).strip()
        device_ids_raw = body.get("device_ids")
        device_ids = None
        if device_ids_raw is not None:
            if not isinstance(device_ids_raw, list) or not all(isinstance(d, str) for d in device_ids_raw):
                return self.json({"error": "device_ids must be a list of strings"}, status_code=400)
            device_ids = [d.strip() for d in device_ids_raw if d.strip()]
        update_type = str(body.get("update_type", "")).strip().lower()
        action = str(body.get("action", "update")).strip().lower()
        notify_push = bool(body.get("notify_push", False))
        send_update_push = bool(body.get("send_update_push", False))
        title = str(body.get("title", "")).strip()
        message = str(body.get("message", "")).strip()

        if update_type not in ("wireguard", "profile"):
            return self.json({"error": "update_type must be 'wireguard' or 'profile'"}, status_code=400)
        if action not in ("update", "revoke"):
            return self.json({"error": "action must be 'update' or 'revoke'"}, status_code=400)
        if not device_id and not username and not device_ids:
            return self.json({"error": "Must provide device_id, device_ids, or username"}, status_code=400)

        # Build the type-specific payload.
        if update_type == "wireguard":
            if action == "update":
                config = str(body.get("wireguard_config", "")).strip()
                if not config:
                    return self.json({"error": "wireguard_config is required for action 'update'"}, status_code=400)
                payload = {"config": config, "excluded_wifi": str(body.get("wireguard_excluded_wifi", "")).strip()}
            else:
                payload = {}
        else:  # profile
            if action != "update":
                return self.json({"error": "profile updates only support action 'update'"}, status_code=400)
            profile_id = str(body.get("profile_id", "")).strip()
            if not profile_id:
                return self.json({"error": "profile_id is required for profile updates"}, status_code=400)
            pp_data = self.hass.data[DOMAIN].get("pp_data", {"profiles": []})
            matched = next((p for p in pp_data.get("profiles", []) if p.get("id") == profile_id), None)
            if not matched:
                return self.json({"error": "Provision template not found"}, status_code=404)
            # A template apply is a one-time stamp of the fields the template
            # actually sets, and only live device settings can change
            # post-provision (expiration_hours goes through
            # expires_at_override instead).
            fields = {k: v for k, v in matched.get("fields", {}).items() if k in LIVE_PROVISIONING_FIELDS}
            if not fields:
                return self.json({"error": "This template sets no device-live fields"}, status_code=400)
            payload = {"profile_id": profile_id, "name": matched.get("name"), "fields": fields}

        targets, not_found = await self._resolve_targets(device_id, username, device_ids)
        if targets is None:
            return self.json({"error": "Target device or user not found"}, status_code=404)
        if not targets:
            return self.json({"queued": 0, "skipped": 0, "not_found": not_found})

        qu_data = self.hass.data[DOMAIN]["qu_data"]
        stored_data = self.hass.data[DOMAIN]["stored_data"]
        created_by = user.name or user.id

        # Enqueue durably (in-memory + delayed save) and respond right away;
        # the push/nudge/notify relay calls run as a background task so a
        # slow or unreachable relay (10s timeout per POST, per device) can't
        # stall the panel's Apply button.
        queued = skipped = 0
        pending_flagged = False
        jobs = []
        device_key = stored_data.get("device_key")

        for did, dinfo in targets:
            update_id = _enqueue_update(qu_data, did, update_type, action, payload, created_by)
            queued += 1
            push_token = dinfo.get("push_token")
            if send_update_push and (not push_token or not device_key):
                skipped += 1
            if notify_push:
                if not push_token:
                    skipped += 1
                elif not title or not message:
                    _LOGGER.warning("CASA: notify_push requested without title/message; skipping notification for '%s'.", did)

            # A template apply is one-time: the device does not become
            # attached to the template. Flag it pending until its next
            # profile self-report confirms the fields landed.
            if update_type == "profile":
                dinfo["provisioning_pending_push"] = True
                pending_flagged = True

            jobs.append((did, dinfo, update_id))

        qu_store = self.hass.data[DOMAIN]["qu_store"]
        qu_store.async_delay_save(lambda: qu_data, 2.0)
        if pending_flagged:
            store = self.hass.data[DOMAIN]["store"]
            store.async_delay_save(lambda: stored_data, 2.0)

        if send_update_push or notify_push:
            self.hass.async_create_task(_deliver_updates_in_background(
                self.hass, stored_data, jobs, update_type, action, payload,
                send_update_push, notify_push, title, message, created_by,
            ))

        _LOGGER.info(
            "CASA: Queued %s '%s' update(s) (push=%s notify=%s) by %s; delivery in background.",
            queued, update_type, send_update_push, notify_push, created_by,
        )
        return self.json({"queued": queued, "skipped": skipped, "not_found": not_found})

    async def delete(self, request):
        """Cancel a single queued update by device_id + id.

        Only removes it from the server queue; a copy already delivered by push cannot
        be recalled (the device would simply ack an id that's no longer queued).
        """
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        device_id = request.query.get("device_id", "").strip()
        update_id = request.query.get("id", "").strip()
        if not device_id or not update_id:
            return self.json({"error": "Missing device_id or id query parameter"}, status_code=400)

        qu_data = self.hass.data[DOMAIN]["qu_data"]
        removed = _dequeue_update(qu_data, device_id, update_id)
        if removed is None:
            return self.json({"error": "Queued update not found"}, status_code=404)

        # Cancelling a queued reauthentication also clears the pending marker
        # so the device record stops advertising it (a push copy already
        # delivered still can't be recalled).
        if removed.get("type") == "auth":
            stored_data = self.hass.data[DOMAIN]["stored_data"]
            device_info, _uid, _name = _find_device_record(stored_data, device_id)
            if device_info and device_info.get("reauth_pending", {}).get("update_id") == update_id:
                device_info.pop("reauth_pending", None)
                self.hass.data[DOMAIN]["store"].async_delay_save(lambda: stored_data, 2.0)

        qu_store = self.hass.data[DOMAIN]["qu_store"]
        qu_store.async_delay_save(lambda: qu_data, 2.0)
        _LOGGER.info("CASA: Admin %s cancelled queued update %s for device '%s'.", user.name or user.id, update_id, device_id)
        remaining = len(qu_data.get("updates", {}).get(device_id, []))
        return self.json({"status": "ok", "remaining": remaining})


class CasaAdminReauthDeviceView(HomeAssistantView):
    """Admin-only endpoint to reauthenticate a provisioned device with new credentials.

    Queues an encrypted `auth`/`reauthenticate` update carrying a username and
    password (an existing HA user, or one created inline) and optionally
    accelerates delivery over an encrypted silent push — the same two channels
    profile/WireGuard updates use, so this works off-network. Nothing about the
    device's identity changes here: the record move, refresh-token rebind, and
    old-session revocation are deferred to _complete_pending_reauth on the
    device's first authenticated contact as the new user, because the queue
    pull/ack path authorizes against the *current* refresh_token_id.
    """

    url = "/api/casa/admin/reauth_device"
    name = "api:casa:admin:reauth_device"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

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

        hass = self.hass
        data = hass.data[DOMAIN]
        stored_data = data["stored_data"]
        qu_data = data["qu_data"]

        device_info, old_uid, old_username = _find_device_record(stored_data, device_id)
        if not device_info:
            return self.json({"error": "Device not found"}, status_code=404)

        password = str(body.get("password", "") or "").strip()
        send_update_push = bool(body.get("send_update_push", True))
        scramble_old = bool(body.get("scramble_old", False))
        create_user = body.get("create_user")
        created_by = user.name or user.id

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        if not provider:
            return self.json({"error": "Home Assistant core auth provider not found"}, status_code=500)

        created_user = False
        revealed_password = None

        if isinstance(create_user, dict):
            result, err = await _create_casa_user(
                hass,
                create_user.get("name"),
                create_user.get("username"),
                password or None,
                created_by=created_by,
            )
            if err:
                return self.json({"error": err}, status_code=400)
            target_user = await hass.auth.async_get_user(result["user_id"])
            login_username = result["username"]
            login_password = result["password"]
            revealed_password = login_password
            created_user = True
        else:
            users = await hass.auth.async_get_users()
            target_user_id = str(body.get("user_id", "") or "").strip()
            target_username = str(body.get("username", "") or "").strip()
            target_user = None
            if target_user_id:
                target_user = next((u for u in users if u.id == target_user_id), None)
            elif target_username:
                target_user = next((u for u in users if _user_matches_username(u, target_username)), None)
            else:
                return self.json({"error": "Must provide user_id, username, or create_user"}, status_code=400)
            if not target_user:
                return self.json({"error": "Target user not found"}, status_code=404)
            if getattr(target_user, "is_admin", False):
                _LOGGER.error("CASA ERROR: Attempted to reauthenticate a device to an admin user. Blocked.")
                return self.json({"error": "Cannot reauthenticate a device to an admin user"}, status_code=400)
            if not getattr(target_user, "is_active", True):
                return self.json({"error": "Target user is inactive"}, status_code=400)

            login_username = None
            for cred in target_user.credentials:
                if cred.auth_provider_type == "homeassistant":
                    login_username = cred.data.get("username")
                    break
            if not login_username:
                return self.json({"error": "No local Home Assistant credentials found for this user"}, status_code=400)

            if password:
                # Provisioning semantics: a supplied password is assumed to
                # already be the account's password and is not changed.
                login_password = password
            else:
                login_password = generate_random_password()
                provider.data.change_password(login_username, login_password)
                await provider.data.async_save()
                revealed_password = login_password

        # Never leave two sequential reauth entries: retrying replaces any
        # still-pending one.
        prev = device_info.get("reauth_pending")
        if prev and prev.get("update_id"):
            _dequeue_update(qu_data, device_id, prev["update_id"])

        switching = old_uid is not None and target_user.id != old_uid
        scrambled_old = False
        if scramble_old and switching:
            # Lock the old account down now: scramble its password and revoke
            # every session except the device's own, which must survive until
            # the device logs in as the new user (it authorizes the queue
            # pull/ack path). That last token dies at completion.
            old_user = await hass.auth.async_get_user(old_uid)
            if old_user and not getattr(old_user, "is_admin", False):
                old_login = next(
                    (c.data.get("username") for c in old_user.credentials if c.auth_provider_type == "homeassistant"),
                    None,
                )
                if old_login:
                    provider.data.change_password(old_login, generate_random_password())
                    await provider.data.async_save()
                spare = device_info.get("refresh_token_id")
                for token in list(old_user.refresh_tokens.values()):
                    if token.id != spare:
                        hass.auth.async_remove_refresh_token(token)
                scrambled_old = True

        # Enqueue durably and stamp the marker BEFORE any push goes out, so a
        # fast device can't complete the reauth against a half-written state.
        update_id = _enqueue_update(
            qu_data, device_id, "auth", "reauthenticate",
            {"username": login_username, "password": login_password}, created_by,
        )
        device_info["reauth_pending"] = {
            "target_user_id": target_user.id,
            "target_username": login_username,
            "old_user_id": old_uid,
            "old_refresh_token_id": device_info.get("refresh_token_id"),
            "update_id": update_id,
            "scramble_old": scrambled_old,
            "requested_at": dt_util.now().isoformat(),
            "requested_by": created_by,
        }
        data["qu_store"].async_delay_save(lambda: qu_data, 2.0)
        data["store"].async_delay_save(lambda: stored_data, 2.0)

        pushed = False
        push_skipped = False
        if send_update_push:
            if not device_info.get("push_token") or not stored_data.get("device_key"):
                push_skipped = True
            else:
                session = async_get_clientsession(hass)
                pushed = await _send_encrypted_update_push(
                    stored_data, session, device_id, device_info,
                    update_id, "auth", "reauthenticate",
                    {"username": login_username, "password": login_password},
                )
                if not pushed:
                    await _nudge_device_checkin(session, stored_data, device_info)

        _LOGGER.info(
            "CASA: Queued reauthentication of device '%s' from user '%s' to '%s' by %s (pushed=%s skipped=%s scrambled_old=%s).",
            device_id, old_username or old_uid, login_username, created_by, pushed, push_skipped, scrambled_old,
        )

        resp = {
            "status": "ok",
            "update_id": update_id,
            "pushed": pushed,
            "push_skipped": push_skipped,
            "username": login_username,
            "created_user": created_user,
            "scrambled_old": scrambled_old,
        }
        if revealed_password:
            resp["password"] = revealed_password
        return self.json(resp)


class CasaAdminRegenerateKeyView(HomeAssistantView):
    """Admin-only, non-destructive rotation of the site device_key.

    Unlike regenerate_site (which nukes the relay site and invalidates every
    provisioned profile), this only rotates the push-encryption secret.
    Every registered device is nudged to heartbeat immediately (see
    _nudge_device_checkin below) so they pick up the new key as soon as
    possible; any push encrypted with the new key that still reaches a
    not-yet-updated device simply fails to decrypt and the device falls back
    to pulling the plaintext update from /api/casa/profile_updates.
    """

    url = "/api/casa/admin/regenerate_device_key"
    name = "api:casa:admin:regenerate_device_key"

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def post(self, request):
        user = request.get("hass_user")
        if not user or not getattr(user, "is_admin", False):
            return self.json_message("Admin access required", status_code=403)

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        stored_data["device_key"] = secrets.token_hex(32)
        store = self.hass.data[DOMAIN]["store"]
        await store.async_save(stored_data)

        key_id = _device_key_id(stored_data["device_key"])
        _LOGGER.info("CASA: Rotated site device_key (new id=%s) by %s.", key_id, user.name or user.id)

        # Nudge every registered device to heartbeat now — the rotation
        # affects all of them, not just one, and an earlier check-in means
        # less time spent falling back to plaintext pulls with the old key.
        session = async_get_clientsession(self.hass)
        for udata in stored_data.get("users", {}).values():
            for dinfo in udata.get("devices", {}).values():
                await _nudge_device_checkin(session, stored_data, dinfo)

        return self.json({"status": "ok", "device_key_id": key_id})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True

async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle migration of config entries."""
    _LOGGER.debug("CASA: Migrating config entry from version %s", config_entry.version)
    # No data transformation needed — options schema is backwards compatible
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("timers", {})
    hass.data[DOMAIN].setdefault("listeners", {})

    # Initialize user tracking store
    STORAGE_KEY = "casa_users"
    STORAGE_VERSION = 1
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    hass.data[DOMAIN]["store"] = store
    
    stored_data = await store.async_load()
    if stored_data is None:
        stored_data = {"users": {}}

    # Register the site with the relay once, verifying stored credentials against
    # the relay so a stale site_key (relay lost the site) self-heals at startup.
    await _ensure_site_registration(hass, stored_data, store)

    # Site-wide device_key: a 256-bit secret (64 hex chars) shared with provisioned
    # devices over the authenticated heartbeat and used to encrypt push payloads. It
    # is NOT the relay's site_key (that credential is never sent to devices).
    if not stored_data.get("device_key"):
        stored_data["device_key"] = secrets.token_hex(32)
        await store.async_save(stored_data)

    hass.data[DOMAIN]["stored_data"] = stored_data

    # Initialize WireGuard profiles store (separate .storage file)
    wg_store = Store(hass, 1, "Casa_WireGuardProfiles")
    wg_data = await wg_store.async_load()
    if wg_data is None:
        wg_data = {"profiles": []}
    hass.data[DOMAIN]["wg_store"] = wg_store
    hass.data[DOMAIN]["wg_data"] = wg_data

    # Initialize provision templates store (separate .storage file; keeps its
    # historical "casa_provision_profiles" name for compatibility).
    pp_store = Store(hass, 1, "casa_provision_profiles")
    pp_data = await pp_store.async_load()
    if pp_data is None:
        pp_data = {"profiles": []}
    # Sparse-normalize legacy templates (total field dicts -> only-set keys).
    if _migrate_provision_templates(pp_data):
        await pp_store.async_save(pp_data)
    hass.data[DOMAIN]["pp_store"] = pp_store
    hass.data[DOMAIN]["pp_data"] = pp_data

    # Initialize queued-updates store (separate .storage file).
    # Shape: {"updates": {device_id: [entry, ...]}}
    qu_store = Store(hass, 1, "casa_queued_updates")
    qu_data = await qu_store.async_load()
    if qu_data is None:
        qu_data = {"updates": {}}
    hass.data[DOMAIN]["qu_store"] = qu_store
    hass.data[DOMAIN]["qu_data"] = qu_data

    # Initialize location-zones store (separate .storage file).
    lz_store = Store(hass, 1, "casa_location_zones")
    lz_data = await lz_store.async_load()
    if lz_data is None:
        lz_data = {"config_version": "", "stale_after_minutes": 30, "anchors": []}
    hass.data[DOMAIN]["lz_store"] = lz_store
    hass.data[DOMAIN]["lz_data"] = lz_data

    # Self-heal stranded queue state (entries for deprovisioned devices,
    # reauthentications targeting since-deleted users, markers stuck on
    # records under deleted owners) before any device pulls it.
    pruned = await _prune_stale_queued_updates(hass)
    if pruned:
        _LOGGER.warning("CASA: Startup pruned %d stale queued update(s).", pruned)

    create_devices = entry.options.get(CONF_CREATE_DEVICES, True)
    
    if create_devices:
        # Register all existing devices in the Device Registry
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        
        # 1. Integration users
        for user_id, user_entry in stored_data.get("users", {}).items():
            if not user_entry.get("deleted", False):
                username = user_entry.get("username", "Unknown")
                for device_id, device_data in user_entry.get("devices", {}).items():
                    dev_reg.async_get_or_create(
                        config_entry_id=entry.entry_id,
                        identifiers={(DOMAIN, device_id)},
                        name=f"Casa Device ({username})",
                        model="Casa Push Client",
                        manufacturer="Casa Integration",
                        sw_version="1.0",
                    )
                    
        # 2. Native users
        native_devices = stored_data.get("native_devices", {})
        if native_devices:
            users = await hass.auth.async_get_users()
            user_map = {u.id: (u.name or u.id) for u in users}
            for user_id, devices in native_devices.items():
                username = user_map.get(user_id) or f"Native User {user_id[:6]}"
                for device_id, device_data in devices.items():
                    dev_reg.async_get_or_create(
                        config_entry_id=entry.entry_id,
                        identifiers={(DOMAIN, device_id)},
                        name=f"Casa Device ({username})",
                        model="Casa Push Client",
                        manufacturer="Casa Integration",
                        sw_version="1.0",
                    )
    else:
        # Purge all Casa devices from the Device Registry if disabled
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        
        # 1. Integration users
        for user_id, user_entry in stored_data.get("users", {}).items():
            for device_id in user_entry.get("devices", {}).keys():
                device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
                if device_entry:
                    dev_reg.async_remove_device(device_entry.id)
                    
        # 2. Native users
        native_devices = stored_data.get("native_devices", {})
        for user_id, devices in native_devices.items():
            for device_id in devices.keys():
                device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
                if device_entry:
                    dev_reg.async_remove_device(device_entry.id)

    async def async_register_device(
        user_id: str,
        device_id: str,
        push_token: str = None,
        last_12_token: str = None,
        refresh_token_id: str = None,
        ip_address: str = None
    ) -> None:
        """Register or update a device for a user."""
        if DOMAIN not in hass.data:
            raise HomeAssistantError("Casa integration is not loaded.")

        if push_token:
            if not re.match(r"^[0-9a-fA-F]{64}$", push_token):
                raise HomeAssistantError("Invalid push token format. Must be a 64-character hex string.")

        stored_data = hass.data[DOMAIN]["stored_data"]

        # Finish any pending admin-initiated reauthentication first, so the
        # user-keyed lookup below finds the record under its new owner instead
        # of duplicating it under the old one.
        await _complete_pending_reauth(hass, device_id, user_id, refresh_token_id)

        # Check if user is an integration-managed user
        if user_id in stored_data["users"] and not stored_data["users"][user_id].get("deleted", False):
            user_entry = stored_data["users"][user_id]
            if "devices" not in user_entry:
                user_entry["devices"] = {}
            devices = user_entry["devices"]
            username = user_entry.get("username")
        else:
            # Check if they are a valid Home Assistant user
            users = await hass.auth.async_get_users()
            ha_user = next((u for u in users if u.id == user_id), None)
            if not ha_user or not ha_user.is_active:
                raise HomeAssistantError("User not found or inactive in Home Assistant.")

            native_devices = stored_data.setdefault("native_devices", {})
            if user_id not in native_devices:
                native_devices[user_id] = {}
            devices = native_devices[user_id]
            username = ha_user.name or user_id

        if device_id not in devices and len(devices) >= 100:
            raise HomeAssistantError("Maximum of 100 registered devices reached for this user.")
            
        now_iso = dt_util.now().isoformat()
        
        # Keep existing push token if not provided in the update
        existing_token = devices.get(device_id, {}).get("push_token")
        final_token = push_token if push_token is not None else existing_token

        # Keep existing bearer token details if not provided in the update
        existing_last_12 = devices.get(device_id, {}).get("last_12_token")
        final_last_12 = last_12_token if last_12_token is not None else existing_last_12

        existing_refresh_id = devices.get(device_id, {}).get("refresh_token_id")
        final_refresh_id = refresh_token_id if refresh_token_id is not None else existing_refresh_id

        existing_ip = devices.get(device_id, {}).get("ip_address")
        final_ip = ip_address if ip_address is not None else existing_ip

        # Reporting a fresh proxy token clears any pending re-register flag;
        # a token-less update preserves it.
        existing_reregister = devices.get(device_id, {}).get("needs_reregister", False)
        final_reregister = False if push_token is not None else existing_reregister

        # Merge onto the existing record — this function is called on every
        # re-registration (periodic push-registration check, relaunch,
        # etc.), not just the first one. A wholesale replacement here would
        # silently wipe alias, expires_at, provisioning_fields, and
        # everything else not listed below.
        existing_info = devices.get(device_id, {})
        devices[device_id] = {
            **existing_info,
            "push_token": final_token,
            "registered_at": existing_info.get("registered_at", now_iso),
            "last_seen_at": now_iso,
            "last_12_token": final_last_12,
            "refresh_token_id": final_refresh_id,
            "ip_address": final_ip,
            "needs_reregister": final_reregister
        }

        # "Originally provisioned from template" lineage — consume the pending
        # record _provision_internal stashed by user_id (device_id wasn't
        # known then). Purely informational under stamp-only semantics; it
        # never re-syncs the device. Ignore stale entries (abandoned code,
        # unrelated later registration) past the same 30-min TTL
        # _login_listener caps at.
        pending_by_user = hass.data[DOMAIN].get("pending_profile_by_user", {})
        pending = pending_by_user.pop(user_id, None)
        if pending and (time.time() - pending.get("set_at", 0)) <= 1800:
            devices[device_id]["provisioning_profile_id"] = pending.get("profile_id")
            devices[device_id]["provisioning_profile_name"] = pending.get("profile_name")
            # Alias typed at provision time; never overwrite one already set
            # (same precedence as the heartbeat's device-submitted alias).
            pending_alias = str(pending.get("device_alias") or "").strip()
            if pending_alias and not str(devices[device_id].get("alias") or "").strip():
                devices[device_id]["alias"] = pending_alias[:DEVICE_ALIAS_MAX_LEN]

        store.async_delay_save(lambda: stored_data, 2.0)
        
        # Register in Home Assistant Device Registry if enabled
        create_devices = entry.options.get(CONF_CREATE_DEVICES, True)
        if create_devices:
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(hass)
            dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, device_id)},
                name=f"Casa Device ({username})",
                model="Casa Push Client",
                manufacturer="Casa Integration",
                sw_version="1.0",
            )
            
            # Dispatch dynamic added/updated signals
            from homeassistant.helpers.dispatcher import async_dispatcher_send
            is_native = user_id not in stored_data["users"]
            # To be safe, check if it was new
            if devices.get(device_id, {}).get("registered_at") == now_iso:
                async_dispatcher_send(hass, "casa_device_added", device_id, username, is_native)
            else:
                async_dispatcher_send(hass, f"casa_device_updated_{device_id}")
        
        _LOGGER.info("CASA: Registered device '%s' for user '%s'.", device_id, username)

    async def async_heartbeat(
        user_id: str,
        device_id: str,
        last_12_token: str = None,
        refresh_token_id: str = None,
        ip_address: str = None,
        provisioned_at: str = None,
        expires_at: int = None,
        current_url: str = None,
        app_version: str = None,
        wireguard_configured: bool = None,
        wireguard_connected: bool = None,
        alias: str = None
    ) -> None:
        """Process heartbeat from a device."""
        if DOMAIN not in hass.data:
            raise HomeAssistantError("Casa integration is not loaded.")

        stored_data = hass.data[DOMAIN]["stored_data"]

        # Finish any pending admin-initiated reauthentication first: it moves
        # the record under the new owner (so the lookup below can't duplicate
        # it) and dequeues the reauth entry before has_updates is computed at
        # the bottom, so the device never re-pulls its own reauth.
        await _complete_pending_reauth(hass, device_id, user_id, refresh_token_id)

        # Check if user is an integration-managed user
        if user_id in stored_data["users"] and not stored_data["users"][user_id].get("deleted", False):
            user_entry = stored_data["users"][user_id]
            if "devices" not in user_entry:
                user_entry["devices"] = {}
            devices = user_entry["devices"]
            username = user_entry.get("username")
        else:
            # Check if they are a valid Home Assistant user
            users = await hass.auth.async_get_users()
            ha_user = next((u for u in users if u.id == user_id), None)
            if not ha_user or not ha_user.is_active:
                raise HomeAssistantError("User not found or inactive in Home Assistant.")

            native_devices = stored_data.setdefault("native_devices", {})
            if user_id not in native_devices:
                native_devices[user_id] = {}
            devices = native_devices[user_id]
            username = ha_user.name or user_id

        if device_id not in devices and len(devices) >= 100:
            raise HomeAssistantError("Maximum of 100 registered devices reached for this user.")

        now_iso = dt_util.now().isoformat()

        # Get or initialize existing device info
        device_info = devices.setdefault(device_id, {
            "registered_at": now_iso
        })

        if last_12_token is not None:
            device_info["last_12_token"] = last_12_token
        if refresh_token_id is not None:
            device_info["refresh_token_id"] = refresh_token_id
        if ip_address is not None:
            device_info["ip_address"] = ip_address
        if provisioned_at is not None:
            device_info["provisioned_at"] = provisioned_at
        if expires_at is not None:
            device_info["expires_at"] = expires_at
        if current_url is not None:
            device_info["current_url"] = current_url
        if app_version is not None:
            device_info["app_version"] = app_version
        if wireguard_configured is not None:
            device_info["wireguard_configured"] = wireguard_configured
        if wireguard_connected is not None:
            device_info["wireguard_connected"] = wireguard_connected

        # A user-submitted alias is accepted only while the stored alias is
        # empty — an admin-set alias always wins and is never overwritten.
        if alias is not None:
            cleaned = alias.strip()[:DEVICE_ALIAS_MAX_LEN]
            if cleaned and not (device_info.get("alias") or "").strip():
                device_info["alias"] = cleaned
                _LOGGER.info("CASA: Device '%s' set its alias via heartbeat.", device_id)

        device_info["last_seen_at"] = now_iso

        # Reconcile any admin-set expiration override. The app applies a returned
        # expires_at only when it differs from its current value, so re-sending a
        # pending override every heartbeat is a no-op on the device. A device with
        # no expiry omits expires_at from its heartbeat, so override=0 ("permanent")
        # is confirmed by absence of the reported value — but only after the
        # override was sent at least once, to avoid mistaking a never-expiring
        # device's normal heartbeat for confirmation.
        pending_expiry = None
        override = device_info.get("expires_at_override")
        if override is not None and provisioned_at is not None:
            # A re-provision supersedes any override set before it. The override
            # targeted the previous session (and likely already expired it); the
            # device can never confirm a past-dated override because it wipes
            # itself immediately on applying one, so without this guard the
            # pending override re-expires every fresh session in a loop.
            set_at = dt_util.parse_datetime(device_info.get("expires_at_override_set_at") or "")
            reported = dt_util.parse_datetime(str(provisioned_at))
            try:
                superseded = set_at is not None and reported is not None and reported > set_at
            except TypeError:  # naive/aware mismatch — don't guess, keep the override
                superseded = False
            if superseded:
                device_info.pop("expires_at_override", None)
                device_info.pop("expires_at_override_set_at", None)
                device_info.pop("expires_at_override_sent", None)
                override = None
                _LOGGER.info(
                    "CASA: Device '%s' was re-provisioned after its expiration override was set — dropping stale override.",
                    device_id,
                )
        if override is not None:
            if override == 0 and expires_at is None and device_info.get("expires_at_override_sent"):
                device_info["expires_at"] = 0
                device_info.pop("expires_at_override", None)
                device_info.pop("expires_at_override_set_at", None)
                device_info.pop("expires_at_override_sent", None)
                _LOGGER.info("CASA: Device '%s' confirmed permanent session (override applied).", device_id)
            elif override > 0 and expires_at == override:
                device_info.pop("expires_at_override", None)
                device_info.pop("expires_at_override_set_at", None)
                device_info.pop("expires_at_override_sent", None)
                _LOGGER.info("CASA: Device '%s' confirmed expiration override %s.", device_id, override)
            else:
                device_info["expires_at_override_sent"] = True
                pending_expiry = override

        store.async_delay_save(lambda: stored_data, 2.0)

        # Ensure registered in Home Assistant Device Registry if enabled
        create_devices = entry.options.get(CONF_CREATE_DEVICES, True)
        if create_devices:
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(hass)
            dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, device_id)},
                name=f"Casa Device ({username})",
                model="Casa Push Client",
                manufacturer="Casa Integration",
                sw_version="1.0",
            )
            
            # Dispatch dynamic added/updated signals
            from homeassistant.helpers.dispatcher import async_dispatcher_send
            is_native = user_id not in stored_data["users"]
            if device_info["registered_at"] == now_iso:
                async_dispatcher_send(hass, "casa_device_added", device_id, username, is_native)
            else:
                async_dispatcher_send(hass, f"casa_device_updated_{device_id}")

        _LOGGER.debug("CASA: Processed heartbeat for device '%s' for user '%s'.", device_id, username)

        qu_data = hass.data[DOMAIN].get("qu_data", {"updates": {}})
        has_updates = bool(qu_data.get("updates", {}).get(device_id))

        result = {
            "reregister": bool(device_info.get("needs_reregister", False)),
            "updates": has_updates,
            "require_alias": bool(stored_data.get("require_device_alias", False)),
            "has_alias": bool((device_info.get("alias") or "").strip()),
            "heartbeat_interval_seconds": stored_data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            "profile_report_interval_seconds": stored_data.get("profile_report_interval_seconds", DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS),
        }
        if pending_expiry is not None:
            result["expires_at"] = pending_expiry
        return result

    # Register the HTTP views
    hass.http.register_view(CasaRegisterDeviceView(hass, async_register_device))
    hass.http.register_view(CasaHeartbeatView(hass, async_heartbeat))
    hass.http.register_view(CasaDeviceProfileReportView(hass))
    hass.http.register_view(CasaAdminSummaryView(hass))
    hass.http.register_view(CasaWireGuardProfilesView(hass))
    hass.http.register_view(CasaLocationZonesView(hass))
    hass.http.register_view(CasaLocationReportView(hass))
    hass.http.register_view(CasaProvisionProfilesView(hass))
    hass.http.register_view(CasaAdminDeviceView(hass))
    hass.http.register_view(CasaAdminSettingsView(hass))
    hass.http.register_view(CasaAdminSessionsView(hass))
    hass.http.register_view(CasaAdminCheckUsernameView(hass))
    hass.http.register_view(CasaProfileUpdatesView(hass))
    hass.http.register_view(CasaAdminQueueUpdateView(hass))
    hass.http.register_view(CasaAdminReauthDeviceView(hass))
    hass.http.register_view(CasaAdminRegenerateKeyView(hass))

    # Serve the admin panel assets once per process; the route survives reloads.
    global _PANEL_STATIC_REGISTERED
    if not _PANEL_STATIC_REGISTERED:
        panel_dir = os.path.join(os.path.dirname(__file__), "panel")
        await hass.http.async_register_static_paths(
            [StaticPathConfig("/casa_static", panel_dir, False)]
        )
        _PANEL_STATIC_REGISTERED = True

    # Optionally add the Casa admin dashboard to the sidebar.
    if entry.options.get(CONF_SHOW_PANEL, False):
        try:
            frontend.async_remove_panel(hass, "casa")
        except Exception:
            pass
        # Cache-bust the module URL with the newest mtime across every panel JS
        # file (the entry propagates ?v= to its sibling module imports), so any
        # updated panel file is picked up after an HA restart without a hard refresh.
        panel_dir_path = os.path.join(os.path.dirname(__file__), "panel")
        panel_version = 0
        try:
            for dirpath, _dirs, filenames in os.walk(panel_dir_path):
                for fname in filenames:
                    if fname.endswith(".js"):
                        mtime = int(os.path.getmtime(os.path.join(dirpath, fname)))
                        panel_version = max(panel_version, mtime)
        except OSError:
            pass
        if not panel_version:
            panel_version = int(time.time())
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="Casa",
            sidebar_icon="mdi:shield-home",
            frontend_url_path="casa",
            require_admin=True,
            config={
                "_panel_custom": {
                    "name": "casa-admin-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    "module_url": f"/casa_static/casa-panel.js?v={panel_version}",
                }
            },
        )

    # Reload the entry when options change so toggles (panel, devices) apply at once.
    async def _options_update_listener(hass_, updated_entry):
        await hass_.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_update_listener))

    async def _check_authorization(call: ServiceCall):
        """Check if the service call is authorized."""
        users = await hass.auth.async_get_users()
        if not entry.options.get(CONF_ADMIN_SYSTEM_ONLY, True):
            return users

        # System/Script contexts are allowed:
        # - call.context.parent_id is set when called from script/automation
        # - call.context.user_id is None when triggered by the system/time triggers
        if call.context.parent_id is not None or call.context.user_id is None:
            return users

        # Directly called by a user. Verify that they are an admin.
        calling_user = next((u for u in users if u.id == call.context.user_id), None)
        if not calling_user or not getattr(calling_user, "is_admin", False):
            _LOGGER.warning(
                "CASA SECURITY: Blocked unauthorized service call to '%s' by user '%s' (ID: %s).",
                call.service,
                getattr(calling_user, "name", "Unknown") if calling_user else "Unknown",
                call.context.user_id,
            )
            raise HomeAssistantError("Admin or system context is required to execute this service.")
        return users

    async def _get_context_creator(context) -> str:
        """Analyze the context to find who/what triggered the action."""
        if context.user_id:
            users = await hass.auth.async_get_users()
            calling_user = next((u for u in users if u.id == context.user_id), None)
            user_name = calling_user.name if calling_user else "Unknown User"
            if context.parent_id:
                return f"user: {user_name} ({context.user_id}) via automation/script"
            return f"user: {user_name} ({context.user_id})"
        elif context.parent_id:
            return "automation or script"
        else:
            return "system"

    # ==========================================
    # SHARED: LOGIN LISTENER
    # ==========================================
    async def _login_listener(username, user_id, known_tokens, ttl_seconds, method):
        """Poll for new refresh tokens and fire casa_code_redeemed when detected."""
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

                if new_tokens:
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
        except asyncio.CancelledError:
            pass

    # ==========================================
    # UNIFIED SERVICE: PROVISION (QR & BLE)
    # ==========================================
    async def _provision_internal(service_data: dict, users: list = None) -> dict:
        method = str(service_data.get("method", "qr")).strip().lower()
        if method not in ("qr", "ble", "deep_link", "manual"):
            return {"error": f"Invalid method: {method}"}

        _LOGGER.debug("CASA: Internal provision function triggered (method: %s).", method)

        current_dir = os.path.dirname(__file__)
        public_key_path = os.path.join(current_dir, "casa_public.pem")

        def read_public_key():
            with open(public_key_path, "rb") as key_file:
                return key_file.read()

        try:
            public_key_data = await hass.async_add_executor_job(read_public_key)
        except Exception as e:
            _LOGGER.error("CASA CRITICAL CRASH: Failed to load public key. Error: %s", str(e))
            return {"error": "Missing Public Key"}

        # 1. Resolve Profile data if present
        # "template" is the preferred service key; "profile" is the historical
        # alias kept for existing automations.
        profile_key = str(service_data.get("template") or service_data.get("profile") or "").strip()
        profile_fields = {}
        matched_profile = None
        if profile_key:
            pp_data = hass.data.get(DOMAIN, {}).get("pp_data", {"profiles": []})
            for p in pp_data.get("profiles", []):
                if p.get("id") == profile_key or p.get("name") == profile_key:
                    matched_profile = p
                    break
            if not matched_profile:
                _LOGGER.error("CASA ERROR: Provision template '%s' not found.", profile_key)
                return {"error": f"Provision template '{profile_key}' not found"}
            profile_fields = matched_profile.get("fields", {})

        # Resolve fields: service_data overrides the template's (sparse) set
        # fields, which override the schema default — an unset template key
        # simply falls through to the default.
        def get_field(key, default=None):
            val = service_data.get(key)
            if val is not None and val != "":
                if isinstance(default, bool):
                    if isinstance(val, str):
                        return val.lower() == "true"
                    return bool(val)
                if isinstance(default, int) and not isinstance(default, bool):
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        return default
                return val
            
            p_val = profile_fields.get(key)
            if p_val is not None:
                if isinstance(default, bool):
                    if isinstance(p_val, str):
                        return p_val.lower() == "true"
                    return bool(p_val)
                if isinstance(default, int) and not isinstance(default, bool):
                    try:
                        return int(p_val)
                    except (TypeError, ValueError):
                        return default
                return p_val
            return default

        final_server_url = str(get_field("host_url", "")).strip()
        target_username = str(get_field("username", "")).strip()

        if not final_server_url or not target_username:
            _LOGGER.error("CASA ERROR: Missing mandatory host_url or username.")
            return {"error": "Missing mandatory fields"}

        # BLE targets are accepted with any method — a QR/deep-link provision
        # may broadcast a beacon alongside. Only method="ble" requires them.
        esphome_services_input = service_data.get("esphome_service", [])
        if isinstance(esphome_services_input, list):
            esphome_targets = [str(s).strip() for s in esphome_services_input if str(s).strip()]
        else:
            esphome_targets = [str(esphome_services_input).strip()] if str(esphome_services_input).strip() else []
        if method == "ble" and not esphome_targets:
            _LOGGER.error("CASA ERROR: Missing mandatory ESPHome services for BLE method.")
            return {"error": "Missing ESPHome Services"}

        target_pin = str(get_field("pin", "")).strip()[:6]
        connect_wifi_ssid = str(get_field("connect_wifi_ssid", "")).strip()
        connect_wifi_password = str(get_field("connect_wifi_password", "")).strip()

        deauthenticate_existing = get_field("deauthenticate_existing", False)

        allow_all_pages = get_field("allow_all_pages", False)
        if allow_all_pages:
            allowed_paths_str = "/*"
        else:
            allowed_pages_input = get_field("allowed_pages", [])
            if isinstance(allowed_pages_input, list):
                clean_paths = [str(p).strip() for p in allowed_pages_input if str(p).strip()]
                allowed_paths_str = ",".join(clean_paths)
            else:
                allowed_paths_str = str(allowed_pages_input).strip()

        require_alias = bool(get_field("require_alias", False))

        allowed_wifi_input = get_field("allowed_wifi", [])
        if isinstance(allowed_wifi_input, list):
            clean_wifi = [str(w).strip() for w in allowed_wifi_input if str(w).strip()]
            allowed_wifi = ",".join(clean_wifi)
        else:
            allowed_wifi = str(allowed_wifi_input).strip()

        default_dashboard = str(get_field("default_dashboard", ""))
        welcome_url = str(get_field("welcome_url", "")).strip()

        immersive_level = str(get_field("immersive_level", "1"))
        theme_color_mode = str(get_field("theme_color_mode", "inherit"))
        custom_color = str(get_field("custom_color", "#000000")).strip().replace("|", "")

        val_hours = get_field("expiration_hours", 336)
        try:
            expiration_hours = int(val_hours)
        except (TypeError, ValueError):
            expiration_hours = 336

        if expiration_hours == 0:
            session_expiration_unix = 0
        else:
            future_dt = dt_util.now() + timedelta(hours=expiration_hours)
            session_expiration_unix = int(future_dt.timestamp())

        # Extract Time Windows
        val_timeout = get_field("timeout_minutes", 5)
        try:
            timeout_mins = int(val_timeout)
        except (TypeError, ValueError):
            timeout_mins = 5

        password_scramble = get_field("password_scramble", True)
        val_scramble = get_field("password_scramble_in", 0)
        try:
            password_scramble_in = int(val_scramble)
        except (TypeError, ValueError):
            password_scramble_in = 0

        # Inheritance & Validation Logic
        if password_scramble:
            if password_scramble_in > 0:
                scramble_timeout_secs = password_scramble_in * 60
            elif timeout_mins > 0:
                scramble_timeout_secs = timeout_mins * 60
            else:
                scramble_timeout_secs = 120 # Fallback on 2 minutes
        else:
            scramble_timeout_secs = 0

        if timeout_mins > 0:
            timeout_secs = timeout_mins * 60
            dead_dt = dt_util.now() + timedelta(seconds=timeout_secs)
            expiration_unix = int(dead_dt.timestamp())
        else:
            expiration_unix = 0
            timeout_secs = 0

        # Extract Cache Control Hours
        val_cache_control = get_field("cache_control_hours", "")
        cache_control_hours_str = str(val_cache_control) if val_cache_control is not None else ""

        if users is None:
            users = await hass.auth.async_get_users()
        # Exact user_id (sent by the guided flow right after create_user) wins;
        # otherwise display-name with credential-username fallback.
        target_user_id = str(get_field("user_id", "") or "").strip()
        target_user = None
        if target_user_id:
            target_user = next((u for u in users if u.id == target_user_id), None)
        if target_user is None:
            target_user = next((u for u in users if _user_matches_username(u, target_username)), None)
        if not target_user:
            return {"error": "User not found"}

        if getattr(target_user, "is_admin", False):
            _LOGGER.error("CASA ERROR: Attempted to provision an admin user '%s'. Blocked.", target_username)
            return {"error": "Cannot provision an admin user"}

        # Bridge for the device-editor's "Originally provisioned from" display.
        # The device_id doesn't exist yet at provision time (see
        # async_register_device below) — stash which profile (if any) was
        # used, keyed by the one thing both moments share: the HA user_id.
        # In-memory only (like timers/listeners below); self-heals on the
        # next provision if lost to a reload.
        hass.data[DOMAIN].setdefault("pending_profile_by_user", {})[target_user.id] = {
            "profile_id": profile_key or None,
            "profile_name": matched_profile.get("name") if matched_profile else None,
            "device_alias": str(service_data.get("device_alias", "")).strip()[:DEVICE_ALIAS_MAX_LEN] or None,
            "set_at": time.time(),
        }

        login_username = None
        for cred in target_user.credentials:
            if cred.auth_provider_type == "homeassistant":
                login_username = cred.data.get("username")
                break
        if not login_username: 
            return {"error": "No credentials"}

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        if not provider:
            return {"error": "Home Assistant core auth provider not found"}

        target_password = str(get_field("password", "")).strip()

        if target_password:
            login_password = target_password
        else:
            login_password = generate_random_password()
            provider.data.change_password(login_username, login_password)
            await provider.data.async_save()

        if deauthenticate_existing:
            for token in list(target_user.refresh_tokens.values()):
                hass.auth.async_remove_refresh_token(token)
            _LOGGER.debug("CASA: All existing sessions for '%s' terminated.", target_username)

        stored_data = hass.data[DOMAIN]["stored_data"]

        # Construct payload field values (shared by v1 and v2)
        site_id = stored_data.get("site_id", "")
        push_val = get_field("push_notifications", "false")
        if push_val is True or (isinstance(push_val, str) and push_val.lower() == "true"):
            normalized_push = "true"
        elif isinstance(push_val, str) and push_val.lower() == "mandatory":
            normalized_push = "mandatory"
        else:
            normalized_push = "false"

        allow_wireguard = get_field("allow_wireguard", False)
        normalized_wireguard = "true" if allow_wireguard else "false"

        wireguard_config_raw = ""
        wireguard_excluded_wifi_raw = ""

        # Fetch from linked WireGuard profile if specified
        wg_profile_key = get_field("wireguard_profile_id", "") or get_field("wireguard_profile", "")
        if wg_profile_key:
            wg_data = hass.data.get(DOMAIN, {}).get("wg_data", {"profiles": []})
            wg_profile = None
            for wp in wg_data.get("profiles", []):
                if wp.get("id") == wg_profile_key or wp.get("alias") == wg_profile_key:
                    wg_profile = wp
                    break
            if wg_profile:
                wireguard_config_raw = wg_profile.get("config", "")
                wireguard_excluded_wifi_raw = wg_profile.get("excluded_wifi", "")
                _LOGGER.info("CASA: Linked WireGuard profile '%s' resolved.", wg_profile.get("alias"))

        # Fallback to direct field values if not linked/not found
        if not wireguard_config_raw:
            wireguard_config_raw = get_field("wireguard_config", "")
        if not wireguard_excluded_wifi_raw:
            wireguard_excluded_wifi_raw = get_field("wireguard_excluded_wifi", "")

        if wireguard_config_raw:
            wireguard_config_encoded = base64.b64encode(str(wireguard_config_raw).encode("utf-8")).decode("utf-8")
        else:
            wireguard_config_encoded = ""

        wireguard_excluded_wifi = str(wireguard_excluded_wifi_raw).strip().replace("|", "")

        try:
            payload_version = int(service_data.get("payload_version", 2))
        except (TypeError, ValueError):
            payload_version = 2

        payload_decrypted = service_data.get("payload_decrypted", False)

        lz_data = hass.data.get(DOMAIN, {}).get("lz_data", {})
        lz_anchors = lz_data.get("anchors", [])
        lz_version = lz_data.get("config_version", "")

        if method == "manual":
            # Manual entry: no payload is built. The resolved plaintext values are
            # returned below for an admin to read into the app's manual sheet.
            final_payload = None
            deep_link = None
            universal_link = None
        elif payload_version == 1:
            # Legacy v1: 21-field, '|'-joined, RSA-OAEP (plaintext capped at 190 bytes).
            raw_payload_array = [
                str(final_server_url),
                str(login_username),
                str(login_password),
                str(site_id),
                target_pin,
                default_dashboard,
                welcome_url,
                immersive_level,
                theme_color_mode,
                custom_color,
                str(session_expiration_unix),
                str(expiration_unix),
                cache_control_hours_str,
                allowed_paths_str,
                allowed_wifi,
                normalized_push,
                normalized_wireguard,
                wireguard_config_encoded,
                wireguard_excluded_wifi,
                connect_wifi_ssid,
                connect_wifi_password
            ]
            payload_string = "|".join(raw_payload_array)
            if payload_decrypted:
                final_payload = base64.b64encode(payload_string.encode('utf-8')).decode('utf-8')
            else:
                try:
                    final_payload = await hass.async_add_executor_job(
                        _encrypt_payload, payload_string, public_key_data
                    )
                except Exception as e:
                    _LOGGER.error("CASA ERROR: Failed to encrypt v1 payload. Error: %s", str(e))
                    return {"error": "Encryption failed"}
            deep_link = f"hascasa://setup?data={urllib.parse.quote(final_payload)}"
            # v1 payloads are standard base64 ('+', '/', '=') so must be percent-encoded.
            universal_link = f"{UNIVERSAL_LINK_SETUP_URL}?d={urllib.parse.quote(final_payload, safe='')}"
        else:
            # v2: JSON profile, hybrid encryption (AES-256-GCM body + RSA-wrapped key), base64url.
            # No size cap, '|' is no longer a delimiter, and fields are named instead of positional.
            profile = {
                "v": 2,
                "server_url": str(final_server_url),
                "username": str(login_username),
                "password": str(login_password),
                "site_id": str(site_id),
                "pin": target_pin,
                "default_dashboard": default_dashboard,
                "welcome_url": welcome_url,
                "immersive_level": immersive_level,
                "theme_color_mode": theme_color_mode,
                "custom_color": custom_color,
                "session_expiration": session_expiration_unix,
                "expiration": expiration_unix,
                "cache_control_hours": cache_control_hours_str,
                "allowed_pages": allowed_paths_str,
                "allowed_wifi": allowed_wifi,
                "require_alias": require_alias,
                "push_notifications": normalized_push,
                "wireguard": {
                    "allowed": normalized_wireguard == "true",
                    "config": str(wireguard_config_raw),
                    "excluded_wifi": wireguard_excluded_wifi,
                },
                "connect_wifi": {
                    "ssid": connect_wifi_ssid,
                    "password": connect_wifi_password,
                },
            }
            if lz_anchors:
                profile["location_zones"] = {
                    "anchors": lz_anchors,
                    "config_version": lz_version,
                }
            payload_string = json.dumps(profile, separators=(",", ":"))
            if payload_decrypted:
                final_payload = base64.urlsafe_b64encode(payload_string.encode("utf-8")).decode("utf-8").rstrip("=")
            else:
                try:
                    final_payload = await hass.async_add_executor_job(
                        _encrypt_payload_hybrid, payload_string, public_key_data
                    )
                except Exception as e:
                    _LOGGER.error("CASA ERROR: Failed to encrypt v2 payload. Error: %s", str(e))
                    return {"error": "Encryption failed"}
            deep_link = f"hascasa://setup?data={final_payload}"
            # v2 payloads are padding-stripped base64url — already URL-safe.
            universal_link = f"{UNIVERSAL_LINK_SETUP_URL}?d={final_payload}"

        # Setup method-specific fields
        delete_qr = False
        final_filename = None
        successful_targets = []

        # Filename & QR creation helper
        def create_qr_images(text):
            www_dir = hass.config.path("www")
            os.makedirs(www_dir, exist_ok=True)
            custom_path = os.path.join(www_dir, final_filename) if final_filename else None
            dashboard_path = os.path.join(www_dir, "casa_qr.png")

            img = qrcode.make(text)
            if custom_path:
                img.save(custom_path)
            img.save(dashboard_path)
            return final_filename

        if method == "qr":
            delete_qr = service_data.get("delete_qr_after_window", True) if timeout_mins > 0 else False
            qr_filename_input = str(service_data.get("qr_filename", "")).strip()
            if qr_filename_input:
                final_filename = qr_filename_input if qr_filename_input.endswith(".png") else f"{qr_filename_input}.png"
            else:
                final_filename = f"qr_{login_username}_{int(time.time())}.png"

            await hass.async_add_executor_job(create_qr_images, deep_link)
            _LOGGER.info("CASA: QR Code saved as %s.", final_filename)

        if esphome_targets:
            for target in esphome_targets:
                try:
                    domain, service = target.split(".")
                    await hass.services.async_call(
                        domain,
                        service,
                        {
                            "payload": final_payload,
                            "expires_at": expiration_unix,
                            "pin": target_pin
                        },
                        blocking=False
                    )
                    successful_targets.append(target)
                    _LOGGER.info("CASA: Pushed payload and PIN to %s.", target)
                except Exception as e:
                    _LOGGER.error("CASA ERROR: Failed to call ESPHome service %s. Error: %s", target, str(e))

        # Detach Cleanup/Auto-Destruct Timer
        async def _cleanup_sequence(username, auth_provider, trans_time, scramble_time, do_scramble, do_delete, filename):
            try:
                current_time = 0
                events = []

                # Only add QR actions if a timeout exists and method is qr
                if method == "qr" and trans_time > 0:
                    events.append({"time": trans_time, "action": "qr"})
                if do_scramble:
                    events.append({"time": scramble_time, "action": "scramble"})

                events.sort(key=lambda x: x["time"])

                for event in events:
                    wait_time = event["time"] - current_time
                    if wait_time > 0:
                        await asyncio.sleep(wait_time)
                        current_time += wait_time

                    if event["action"] == "qr":
                        if do_delete:
                            def delete_and_overwrite():
                                www_dir = hass.config.path("www")
                                custom_path = os.path.join(www_dir, filename)
                                dashboard_path = os.path.join(www_dir, "casa_qr.png")

                                if os.path.exists(custom_path):
                                    os.remove(custom_path)

                                img = qrcode.make("EXPIRED - Request a new Casa code.")
                                img.save(dashboard_path)

                            await hass.async_add_executor_job(delete_and_overwrite)
                            _LOGGER.info("CASA: QR Code file %s physically deleted.", filename)
                        else:
                            await hass.async_add_executor_job(create_qr_images, "EXPIRED - Request a new Casa code.")
                            _LOGGER.info("CASA: QR Code %s wiped from dashboard.", filename)

                    elif event["action"] == "scramble":
                        scrambled_password = generate_random_password()
                        auth_provider.data.change_password(username, scrambled_password)
                        await auth_provider.data.async_save()
                        _LOGGER.info("CASA: Password for %s scrambled.", username)
                        # Cancel active listener since code can no longer be redeemed
                        listener_task = hass.data[DOMAIN]["listeners"].get(target_username)
                        if listener_task:
                            listener_task.cancel()
            except asyncio.CancelledError:
                pass

        if target_username in hass.data[DOMAIN]["timers"]:
            hass.data[DOMAIN]["timers"][target_username].cancel()

        if (method in ("qr", "deep_link", "manual") and timeout_mins > 0) or password_scramble:
            countdown_task = hass.async_create_task(
                _cleanup_sequence(login_username, provider, timeout_secs, scramble_timeout_secs, password_scramble, delete_qr, final_filename)
            )
            hass.data[DOMAIN]["timers"][target_username] = countdown_task
        else:
            _LOGGER.warning("CASA: No timeout or password scramble configured. Code is permanent.")

        # Start login listener to detect code redemption
        known_token_ids = set(target_user.refresh_tokens.keys())
        if password_scramble and scramble_timeout_secs > 0:
            listener_ttl = scramble_timeout_secs + 30
        elif expiration_hours > 0:
            listener_ttl = min(expiration_hours * 3600, 86400)
        else:
            listener_ttl = 300

        # E4: Hard cap listener TTL to 30 minutes (1800 seconds)
        listener_ttl = min(listener_ttl, 1800)

        if target_username in hass.data[DOMAIN]["listeners"]:
            hass.data[DOMAIN]["listeners"][target_username].cancel()

        listener_task = hass.async_create_task(
            _login_listener(login_username, target_user.id, known_token_ids, listener_ttl, method)
        )
        hass.data[DOMAIN]["listeners"][target_username] = listener_task

        if method == "manual":
            # Plaintext values for the iOS app's manual provisioning sheet,
            # field-for-field. The password/window expiry still applies.
            return {
                "method": "manual",
                "expires_at": expiration_unix,
                "fields": {
                    "server_url": final_server_url,
                    "username": login_username,
                    "password": login_password,
                    "allowed_paths": allowed_paths_str,
                    "allowed_wifi": allowed_wifi,
                    "default_dashboard": default_dashboard,
                    "immersive_level": immersive_level,
                    "theme_color_mode": theme_color_mode,
                    "custom_color": custom_color,
                    "session_expiration": session_expiration_unix,
                    "cache_control_hours": cache_control_hours_str,
                    "welcome_url": welcome_url,
                    "connect_wifi_ssid": connect_wifi_ssid,
                    "connect_wifi_password": connect_wifi_password,
                },
                # Resolved but impossible to enter in the app's manual sheet.
                "unsupported": {
                    "pin": bool(target_pin),
                    "site_id": bool(site_id),
                    "push_notifications": normalized_push,
                    "wireguard": bool(wireguard_config_raw) or normalized_wireguard == "true",
                    "require_alias": require_alias,
                },
            }
        elif method == "qr":
            result = {
                "method": "qr",
                "filename": final_filename,
                "url_path": f"/local/{final_filename}",
                "expires_at": expiration_unix,
                "deep_link": deep_link,
                "universal_link": universal_link
            }
            if esphome_targets:
                result["successful_targets"] = successful_targets
                result["pin_required"] = bool(target_pin)
            return result
        elif method == "deep_link":
            result = {
                "method": "deep_link",
                "deep_link": deep_link,
                "universal_link": universal_link,
                "expires_at": expiration_unix
            }
            if esphome_targets:
                result["successful_targets"] = successful_targets
                result["pin_required"] = bool(target_pin)
            return result
        else:
            return {
                "method": "ble",
                "status": "success",
                "successful_targets": successful_targets,
                "expires_at": expiration_unix,
                "pin_required": bool(target_pin)
            }

    async def handle_provision(call: ServiceCall):
        users = await _check_authorization(call)
        return await _provision_internal(call.data, users)

    async def handle_generate_qr_legacy(call: ServiceCall):
        users = await _check_authorization(call)
        _LOGGER.warning("CASA: generate_qr service is deprecated. Please use the provision service with method='qr' instead.")
        data = dict(call.data)
        data["method"] = "qr"
        if "qr_timeout_minutes" in data:
            data["timeout_minutes"] = data.pop("qr_timeout_minutes")
        return await _provision_internal(data, users)

    async def handle_provision_ble_beacon_legacy(call: ServiceCall):
        users = await _check_authorization(call)
        _LOGGER.warning("CASA: provision_ble_beacon service is deprecated. Please use the provision service with method='ble' instead.")
        data = dict(call.data)
        data["method"] = "ble"
        if "ble_timeout_minutes" in data:
            data["timeout_minutes"] = data.pop("ble_timeout_minutes")
        return await _provision_internal(data, users)

    hass.services.async_register(
        DOMAIN, "provision", handle_provision,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "generate_qr", handle_generate_qr_legacy,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "provision_ble_beacon", handle_provision_ble_beacon_legacy,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 2: REMOVE TOKEN
    # ==========================================
    async def handle_remove_token(call: ServiceCall):
        users = await _check_authorization(call)
        token_id = str(call.data.get("token_id", "")).strip()
        target_username = str(call.data.get("username", "")).strip()
        
        if not token_id or not target_username:
            return
            
        target_user = next((u for u in users if _user_matches_username(u, target_username)), None)
        if not target_user:
            return

        if token_id == "*":
            for token in list(target_user.refresh_tokens.values()):
                hass.auth.async_remove_refresh_token(token)
            _LOGGER.info("CASA: All active sessions terminated for %s.", target_username)
        else:
            # Let's find the actual refresh token ID to remove
            real_token_id = None
            
            # 1. Check if token_id is the exact refresh token ID
            if token_id in target_user.refresh_tokens:
                real_token_id = token_id
            # 2. Check if token_id is the last 12 characters of any refresh token ID
            else:
                for r_token_id in target_user.refresh_tokens.keys():
                    if r_token_id[-12:] == token_id:
                        real_token_id = r_token_id
                        break
            
            # 3. Check if it matches the last_12_token of any registered devices for this user
            if not real_token_id:
                stored_data = hass.data[DOMAIN]["stored_data"]
                # Search integration users
                for uid, udata in stored_data.get("users", {}).items():
                    if uid == target_user.id:
                        for dev_id, dev_info in udata.get("devices", {}).items():
                            l12 = dev_info.get("last_12_token")
                            if l12 == token_id or (l12 and l12[-12:] == token_id):
                                real_token_id = dev_info.get("refresh_token_id")
                                break
                        if real_token_id:
                            break
                
                # Search native users
                if not real_token_id:
                    native_devices = stored_data.get("native_devices", {})
                    if target_user.id in native_devices:
                        for dev_id, dev_info in native_devices[target_user.id].items():
                            l12 = dev_info.get("last_12_token")
                            if l12 == token_id or (l12 and l12[-12:] == token_id):
                                real_token_id = dev_info.get("refresh_token_id")
                                break

            if real_token_id:
                token_to_remove = target_user.refresh_tokens.get(real_token_id)
                if token_to_remove:
                    hass.auth.async_remove_refresh_token(token_to_remove)
                    _LOGGER.info("CASA: Session '%s' (last 12 matched) revoked for %s.", real_token_id[-12:], target_username)

    hass.services.async_register(DOMAIN, "remove_token", handle_remove_token)

    # ==========================================
    # SERVICE 3: CREATE USER
    # ==========================================
    async def handle_create_user(call: ServiceCall):
        users = await _check_authorization(call)
        creator = await _get_context_creator(call.context)
        result, err = await _create_casa_user(
            hass,
            call.data.get("name"),
            call.data.get("username"),
            str(call.data.get("password", "")).strip() or None,
            created_by=creator,
            local_only=call.data.get("local_only", True),
            users=users,
        )
        if err:
            return {"error": err}
        return result

    hass.services.async_register(
        DOMAIN, "create_user", handle_create_user,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 4: LIST TOKENS
    # ==========================================
    async def handle_list_tokens(call: ServiceCall):
        users = await _check_authorization(call)
        target_username = str(call.data.get("username", "")).strip()
        
        if not target_username:
            return {"error": "Missing mandatory username"}

        target_user = next((u for u in users if _user_matches_username(u, target_username)), None)

        if not target_user:
            return {"error": "User not found"}

        active_tokens = []
        for token in target_user.refresh_tokens.values():
            active_tokens.append({
                "id": token.id,
                "client_id": token.client_id,
                "client_name": token.client_name,
                "created_at": token.created_at.isoformat() if token.created_at else None,
                "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
                "last_used_ip": token.last_used_ip
            })

        return {"tokens": active_tokens}

    hass.services.async_register(
        DOMAIN, "list_tokens", handle_list_tokens,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 5: HOUSEKEEPING
    # ==========================================
    async def handle_housekeeping(call: ServiceCall):
        await _check_authorization(call)
        val_hours = call.data.get("hours_old")
        hours_old = float(val_hours) if val_hours is not None else 24.0
        prefix = str(call.data.get("prefix", "qr_")).strip()

        if not prefix:
            return {"error": "Prefix cannot be empty"}

        def cleanup_files():
            deleted_count = 0
            www_dir = hass.config.path("www")
            
            if not os.path.exists(www_dir):
                return 0

            current_time = time.time()
            cutoff_time = current_time - (hours_old * 3600)

            for filename in os.listdir(www_dir):
                if filename.startswith(prefix) and filename.endswith(".png"):
                    filepath = os.path.join(www_dir, filename)
                    if os.path.isfile(filepath):
                        file_mtime = os.path.getmtime(filepath)
                        if file_mtime < cutoff_time:
                            try:
                                os.remove(filepath)
                                deleted_count += 1
                            except Exception as e:
                                _LOGGER.error("CASA ERROR: Failed to delete %s: %s", filename, e)
            return deleted_count

        deleted_count = await hass.async_add_executor_job(cleanup_files)
        _LOGGER.info("CASA: Housekeeping deleted %s old files matching prefix '%s'.", deleted_count, prefix)

        return {"deleted_count": deleted_count}

    hass.services.async_register(
        DOMAIN, "housekeeping", handle_housekeeping,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 6: SCRAMBLE USER PASSWORD
    # ==========================================
    async def handle_scramble_guest_password(call: ServiceCall):
        users = await _check_authorization(call)
        target_username = str(call.data.get("username", "")).strip()
        deauthenticate = call.data.get("deauthenticate", True)

        if not target_username:
            return {"error": "Missing mandatory username"}

        target_user = next((u for u in users if _user_matches_username(u, target_username)), None)

        if not target_user:
            return {"error": "User not found"}

        if getattr(target_user, "is_admin", False):
            _LOGGER.error("CASA ERROR: Attempted to scramble an admin user's password. Blocked.")
            return {"error": "Cannot scramble password for an admin user"}

        login_username = None
        for cred in target_user.credentials:
            if cred.auth_provider_type == "homeassistant":
                login_username = cred.data.get("username")
                break
                
        if not login_username: 
            return {"error": "No local Home Assistant credentials found for this user"}

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        if not provider:
            return {"error": "Home Assistant core auth provider not found"}

        new_password = generate_random_password()

        provider.data.change_password(login_username, new_password)
        await provider.data.async_save()
        
        _LOGGER.info("CASA: Password for user '%s' manually scrambled.", target_username)

        if deauthenticate:
            for token in list(target_user.refresh_tokens.values()):
                hass.auth.async_remove_refresh_token(token)
            _LOGGER.info("CASA: All active sessions for '%s' terminated.", target_username)

        return {
            "username": target_username,
            "password": new_password,
            "scrambled": True,
            "deauthenticated": deauthenticate
        }

    hass.services.async_register(
        DOMAIN, "scramble_guest_password", handle_scramble_guest_password,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 8: CLEAR BLE BEACON
    # ==========================================
    async def handle_clear_ble_beacon(call: ServiceCall):
        await _check_authorization(call)
        esphome_services_input = call.data.get("esphome_service", [])
        if isinstance(esphome_services_input, list):
            esphome_targets = [str(s).strip() for s in esphome_services_input if str(s).strip()]
        else:
            esphome_targets = [str(esphome_services_input).strip()] if str(esphome_services_input).strip() else []
        
        if not esphome_targets:
            return {"error": "Missing ESPHome target services"}

        successful_targets = []
        for target in esphome_targets:
            try:
                domain, service = target.split(".")
                await hass.services.async_call(
                    domain, 
                    service, 
                    {
                        "payload": "EXPIRED",
                        "expires_at": 0,
                        "pin": ""
                    }, 
                    blocking=False
                )
                successful_targets.append(target)
                _LOGGER.info("CASA: Manually cleared BLE beacon at %s.", target)
            except Exception as e:
                _LOGGER.error("CASA ERROR: Failed to clear %s: %s", target, str(e))
                
        return {"status": "cleared", "successful_targets": successful_targets}

    # ==========================================
    # SERVICE: REMOVE USER
    # ==========================================
    async def handle_remove_user(call: ServiceCall):
        users = await _check_authorization(call)
        target_username = str(call.data.get("username", "")).strip()
        if not target_username:
            raise HomeAssistantError("Missing mandatory username.")

        target_user = next((u for u in users if _user_matches_username(u, target_username)), None)
        if not target_user:
            raise HomeAssistantError(f"User '{target_username}' not found.")

        if getattr(target_user, "is_admin", False) or target_user.is_owner:
            raise HomeAssistantError("Cannot delete an admin or owner user account.")

        user_id = target_user.id
        user_name = target_user.name

        stored_data = hass.data[DOMAIN]["stored_data"]
        if user_id not in stored_data["users"] or stored_data["users"][user_id].get("deleted", False):
            raise HomeAssistantError(f"User '{target_username}' was not created via this integration and cannot be removed.")

        # Perform deletion
        await hass.auth.async_remove_user(target_user)
        _LOGGER.info("CASA: Local user '%s' (ID: %s) removed.", target_username, user_id)

        # Remove from Home Assistant Device Registry
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        user_entry = stored_data["users"][user_id]
        for device_id in list(user_entry.get("devices", {}).keys()):
            device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
            if device_entry:
                dev_reg.async_remove_device(device_entry.id)

        # Track the deletion in the store
        deleter = await _get_context_creator(call.context)
        
        stored_data["users"][user_id].update({
            "deleted": True,
            "deleted_at": dt_util.now().isoformat(),
            "deleted_by": deleter,
        })
        await hass.data[DOMAIN]["store"].async_save(stored_data)

        # The user's device records are now unreachable (_find_device_record
        # skips deleted owners), so their queued updates — and any queued
        # reauthentication *targeting* this user from another device — can
        # never be consumed. Left behind, an auth entry re-delivers forever
        # and loops the device through failed logins until it wipes itself.
        await _prune_stale_queued_updates(hass)

        return {
            "status": "removed",
            "username": target_username,
            "user_id": user_id
        }

    # ==========================================
    # SERVICE: VIEW CASA USERS
    # ==========================================
    async def handle_view_casa_users(call: ServiceCall):
        users_in_ha = await _check_authorization(call)
        include_deleted = call.data.get("include_deleted", False)

        ha_user_ids = {u.id for u in users_in_ha}

        stored_data = hass.data[DOMAIN]["stored_data"]
        store = hass.data[DOMAIN]["store"]

        # Sync with actual Home Assistant state to detect out-of-band deletions
        changed = False
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        for uid, udata in list(stored_data["users"].items()):
            if uid not in ha_user_ids and not udata.get("deleted", False):
                stored_data["users"][uid].update({
                    "deleted": True,
                    "deleted_at": dt_util.now().isoformat(),
                    "deleted_by": "deleted outside integration (UI or other means)",
                })
                # Clean up their devices from Device Registry
                for device_id in list(udata.get("devices", {}).keys()):
                    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
                    if device_entry:
                        dev_reg.async_remove_device(device_entry.id)
                changed = True

        # Sync with actual Home Assistant state to detect out-of-band deletions for native_devices
        native_devices = stored_data.setdefault("native_devices", {})
        for uid in list(native_devices.keys()):
            if uid not in ha_user_ids:
                # Clean up their devices from Device Registry
                for device_id in list(native_devices[uid].keys()):
                    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
                    if device_entry:
                        dev_reg.async_remove_device(device_entry.id)
                native_devices.pop(uid)
                changed = True

        if changed:
            await store.async_save(stored_data)
            # Users deleted outside the integration strand their devices'
            # queued updates the same way handle_remove_user would.
            await _prune_stale_queued_updates(hass)

        result_users = []
        for uid, udata in stored_data["users"].items():
            is_deleted = udata.get("deleted", False)
            if is_deleted and not include_deleted:
                continue

            user_info = {
                "user_id": uid,
                "name": udata.get("name"),
                "username": udata.get("username"),
                "created_at": udata.get("created_at"),
                "created_by": udata.get("created_by"),
                "deleted": is_deleted,
                "deleted_at": udata.get("deleted_at"),
                "deleted_by": udata.get("deleted_by"),
            }

            if not is_deleted:
                ha_user = next((u for u in users_in_ha if u.id == uid), None)
                if ha_user:
                    user_info.update({
                        "is_owner": ha_user.is_owner,
                        "is_active": ha_user.is_active,
                        "is_admin": getattr(ha_user, "is_admin", False),
                        "local_only": getattr(ha_user, "local_only", False),
                        "group_ids": ha_user.groups,
                    })

            result_users.append(user_info)

        return {"users": result_users}

    async def handle_register_device(call: ServiceCall):
        user_id = call.context.user_id
        if not user_id:
            raise HomeAssistantError("User context required to register device.")

        device_id = str(call.data.get("device_id", "")).strip()
        push_token = str(call.data.get("push_token", "")).strip()

        if not device_id or not push_token:
            raise HomeAssistantError("Missing device_id or push_token.")

        await async_register_device(user_id, device_id, push_token)
        return {"status": "success"}

    async def handle_notify_user(call: ServiceCall):
        users = await _check_authorization(call)
        device_id = str(call.data.get("device_id", "")).strip()
        username = str(call.data.get("username", "")).strip()
        title = str(call.data.get("title", "")).strip()
        message = str(call.data.get("message", "")).strip()
        custom_data = call.data.get("data")

        parsed_data = None
        if custom_data is not None:
            if isinstance(custom_data, str):
                import json
                try:
                    parsed_data = json.loads(custom_data)
                except ValueError:
                    parsed_data = custom_data
            else:
                parsed_data = custom_data

        if not (username or device_id) or not title or not message:
            raise HomeAssistantError("Missing username or device_id, title, or message.")

        stored_data = hass.data[DOMAIN]["stored_data"]

        devices = {}
        if device_id:
            found = None
            for uid, udata in stored_data.get("users", {}).items():
                if device_id in udata.get("devices", {}):
                    found = (uid, udata["devices"][device_id], udata.get("username", "Unknown"))
                    break
            if not found:
                for uid, devs in stored_data.get("native_devices", {}).items():
                    if device_id in devs:
                        users_list = await hass.auth.async_get_users()
                        ha_user = next((u for u in users_list if u.id == uid), None)
                        uname = ha_user.name or uid if ha_user else f"Native {uid[:6]}"
                        found = (uid, devs[device_id], uname)
                        break
            if not found:
                raise HomeAssistantError(f"Device '{device_id}' not found.")
            uid, device_data, username = found
            devices = {device_id: device_data}
        else:
            target_user = next((u for u in users if u.name and u.name.casefold() == username.casefold()), None)
            if not target_user:
                for u in users:
                    for cred in u.credentials:
                        if cred.auth_provider_type == "homeassistant" and cred.data.get("username", "").casefold() == username.casefold():
                            target_user = u
                            break
                    if target_user:
                        break

            if not target_user:
                raise HomeAssistantError(f"User '{username}' not found.")

            user_id = target_user.id
            if user_id in stored_data["users"] and not stored_data["users"][user_id].get("deleted", False):
                devices = stored_data["users"][user_id].get("devices", {})
            else:
                native_devices = stored_data.get("native_devices", {})
                devices = native_devices.get(user_id, {})

        if not devices:
            _LOGGER.warning("CASA: No registered devices found for user '%s'.", username)
            return {"success": True, "sent_count": 0, "failed_count": 0}

        session = async_get_clientsession(hass)
        sem = asyncio.Semaphore(10)

        tasks = []
        for device_id, device_data in devices.items():
            push_token = device_data.get("push_token")
            if not push_token:
                _LOGGER.warning("CASA: Device '%s' for user '%s' has no push token registered.", device_id, username)
                continue

            payload = {
                "title": title,
                "message": message,
                "target": push_token,
                "site_id": stored_data.get("site_id"),
                "site_key": stored_data.get("site_key")
            }
            if parsed_data is not None:
                payload["data"] = parsed_data

            _LOGGER.info(
                "CASA: Attempting to send push notification to user '%s' device '%s'. Target (obfuscated): %s, Site ID: %s",
                username,
                device_id,
                push_token[:10] + "..." if isinstance(push_token, str) and len(push_token) > 10 else "invalid",
                stored_data.get("site_id")
            )
            _LOGGER.debug(
                "CASA DEBUG PAYLOAD: Target=%s, SiteID=%s, SiteKey=%s, Data=%s",
                push_token,
                stored_data.get("site_id"),
                stored_data.get("site_key"),
                parsed_data
            )

            async def send_post(tok=push_token, data_payload=dict(payload)):
                async with sem:
                    success = False
                    for url in RELAY_URLS:
                        try:
                            _LOGGER.info("CASA: Posting payload to relay %s", url)
                            async with session.post(url, json=data_payload, timeout=ClientTimeout(total=10)) as response:
                                if response.status == 200:
                                    _LOGGER.info("CASA: Notification successfully sent to token %s... via %s", tok[:10], url)
                                    success = True
                                    break
                                
                                text = await response.text()
                                _LOGGER.warning("CASA: Relay %s returned status %s for token %s...: %s", url, response.status, tok[:10], text)
                                if response.status < 500:
                                    # Client-side error (4xx): don't attempt failover since it's a validation error
                                    break
                        except Exception as err:
                            _LOGGER.warning("CASA: Failed to connect to relay %s for token %s...: %s", url, tok[:10], err)
                    
                    if not success:
                        _LOGGER.error("CASA: Failed to send notification to token %s... after trying all relays", tok[:10])
                    return success

            tasks.append(send_post())

        success_count = 0
        failed_count = 0
        if tasks:
            results = await asyncio.gather(*tasks)
            success_count = sum(1 for r in results if r)
            failed_count = len(results) - success_count

        return {
            "success": failed_count == 0,
            "sent_count": success_count,
            "failed_count": failed_count,
        }

    async def handle_reload_device(call: ServiceCall):
        users = await _check_authorization(call)
        device_id = str(call.data.get("device_id", "")).strip()

        if not device_id:
            raise HomeAssistantError("Missing device_id parameter.")

        # Find the device in stored_data
        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info = {}
        username = "Unknown"
        
        # 1. Search in integration users
        for uid, udata in stored_data.get("users", {}).items():
            if device_id in udata.get("devices", {}):
                device_info = udata["devices"][device_id]
                username = udata.get("username", "Unknown")
                break
                
        # 2. Search in native users if not found
        if not device_info:
            for uid, devices in stored_data.get("native_devices", {}).items():
                if device_id in devices:
                    device_info = devices[device_id]
                    ha_user = next((u for u in users if u.id == uid), None)
                    username = ha_user.name if ha_user else uid
                    break

        if not device_info:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")

        push_token = device_info.get("push_token")
        if not push_token:
            raise HomeAssistantError(f"No push notification token registered for device '{device_id}'.")

        # Send silent push
        session = async_get_clientsession(hass)
        payload = {
            "title": "",
            "message": "",
            "target": push_token,
            "site_id": stored_data.get("site_id"),
            "site_key": stored_data.get("site_key"),
            "push_type": "background",
            "priority": 5,
            "data": {"command": "clear_cache_and_reload"}
        }

        _LOGGER.info(
            "CASA: Service called to send silent reload push to device '%s' of user '%s'. Target: %s",
            device_id, username, push_token[:10] + "..."
        )

        success = False
        for url in RELAY_URLS:
            try:
                _LOGGER.info("CASA: Posting reload payload to relay %s", url)
                async with session.post(url, json=payload, timeout=ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        _LOGGER.info("CASA: Reload command successfully sent to token %s... via %s", push_token[:10], url)
                        success = True
                        break
                    
                    text = await response.text()
                    _LOGGER.warning("CASA: Relay %s returned status %s: %s", url, response.status, text)
            except Exception as err:
                _LOGGER.warning("CASA: Failed to connect to relay %s: %s", url, err)

        if not success:
            raise HomeAssistantError("Failed to deliver reload command to any Casa push relay.")

        return {"status": "success"}

    async def handle_request_device_report(call: ServiceCall):
        """Silently ask a device to report its provisioning state right now,
        instead of waiting for its next periodic self-report. Purely a nudge —
        no cache-clear/reload side effects, unlike reload_device."""
        device_id = str(call.data.get("device_id", "")).strip()
        if not device_id:
            raise HomeAssistantError("Missing device_id parameter.")

        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info, _uid, username = _find_device_record(stored_data, device_id)
        if device_info is None:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")

        if not device_info.get("push_token"):
            raise HomeAssistantError(f"No push notification token registered for device '{device_id}'.")

        _LOGGER.info(
            "CASA: Service called to request an on-demand profile report from device '%s' of user '%s'.",
            device_id, username or "Unknown",
        )

        session = async_get_clientsession(hass)
        success = await _nudge_device_checkin(session, stored_data, device_info, command="request_profile_report")
        if not success:
            raise HomeAssistantError("Failed to deliver profile report request to any Casa push relay.")

        return {"status": "success"}

    async def handle_request_heartbeat(call: ServiceCall):
        """Silently ask a device to heartbeat right now, instead of waiting
        for its next scheduled tick. Purely a nudge — the device's own
        sendHeartbeat already pulls any durably-queued profile_updates when
        the response says they're pending, so this is sufficient to make any
        queued admin change land immediately."""
        device_id = str(call.data.get("device_id", "")).strip()
        if not device_id:
            raise HomeAssistantError("Missing device_id parameter.")

        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info, _uid, username = _find_device_record(stored_data, device_id)
        if device_info is None:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")

        if not device_info.get("push_token"):
            raise HomeAssistantError(f"No push notification token registered for device '{device_id}'.")

        _LOGGER.info(
            "CASA: Service called to request an on-demand heartbeat from device '%s' of user '%s'.",
            device_id, username or "Unknown",
        )

        session = async_get_clientsession(hass)
        success = await _nudge_device_checkin(session, stored_data, device_info, command="request_heartbeat")
        if not success:
            raise HomeAssistantError("Failed to deliver heartbeat request to any Casa push relay.")

        return {"status": "success"}

    async def handle_set_device_expiration(call: ServiceCall):
        await _check_authorization(call)
        device_id = str(call.data.get("device_id", "")).strip()
        if not device_id:
            raise HomeAssistantError("Missing device_id parameter.")

        permanent = bool(call.data.get("permanent", False))
        expires_at = call.data.get("expires_at")
        expires_in_hours = call.data.get("expires_in_hours")

        if permanent:
            value = 0
        elif expires_at is not None:
            try:
                value = int(expires_at)
            except (TypeError, ValueError):
                raise HomeAssistantError("expires_at must be an integer unix timestamp.")
            if value < 0:
                raise HomeAssistantError("expires_at must be >= 0.")
        elif expires_in_hours is not None:
            try:
                hours = float(expires_in_hours)
            except (TypeError, ValueError):
                raise HomeAssistantError("expires_in_hours must be a number.")
            if hours <= 0:
                raise HomeAssistantError("expires_in_hours must be greater than 0.")
            value = int(time.time() + hours * 3600)
        else:
            raise HomeAssistantError("Provide one of: expires_at, expires_in_hours, or permanent.")

        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info = _set_expiry_override(stored_data, device_id, value)
        if device_info is None:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")

        store.async_delay_save(lambda: stored_data, 2.0)
        _LOGGER.info(
            "CASA: Expiration override for device '%s' set to %s.",
            device_id, "permanent" if value == 0 else value
        )
        session = async_get_clientsession(hass)
        await _nudge_device_checkin(session, stored_data, device_info)
        return {"status": "success", "device_id": device_id, "expires_at_override": value}

    async def handle_deprovision_device(call: ServiceCall):
        await _check_authorization(call)
        device_id = str(call.data.get("device_id", "")).strip()

        if not device_id:
            raise HomeAssistantError("Missing device_id parameter.")

        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info, _, username = _find_device_record(stored_data, device_id)
        if device_info is None:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")

        # Send the silent wipe push first, while the proxy token is still
        # registered on the relay. Best-effort: an offline or push-less device is
        # still cut off below (revoked refresh token -> next heartbeat/refresh
        # 401s and the app wipes itself on auth failure).
        push_sent = False
        push_token = device_info.get("push_token")
        if push_token:
            session = async_get_clientsession(hass)
            payload = {
                "title": "",
                "message": "",
                "target": push_token,
                "site_id": stored_data.get("site_id"),
                "site_key": stored_data.get("site_key"),
                "push_type": "background",
                "priority": 5,
                "data": {"command": "deprovision"},
            }
            _LOGGER.info(
                "CASA: Sending silent deprovision push to device '%s' of user '%s'. Target: %s",
                device_id, username or "Unknown", push_token[:10] + "..."
            )
            push_sent = await _send_push_to_relay(session, payload)
            if not push_sent:
                _LOGGER.warning(
                    "CASA: Deprovision push for device '%s' was not delivered; device will be wiped lazily on next contact.",
                    device_id
                )
        else:
            _LOGGER.warning(
                "CASA: Device '%s' has no push token; skipping deprovision push (device will be wiped lazily on next contact).",
                device_id
            )

        purge_result = await _purge_device(hass, device_id)
        _remove_registry_device(hass, device_id)

        return {
            "status": "success",
            "device_id": device_id,
            "push_sent": push_sent,
            "access_revoked": purge_result.get("access_revoked", False),
        }

    async def handle_delete_device(call: ServiceCall):
        """Delete a device's server-side record and revoke its access.

        Unlike deprovision_device this sends no wipe push — the app keeps its
        local session until its revoked token next fails. Intended for stale or
        orphaned records where the app is already gone.
        """
        await _check_authorization(call)
        device_id = str(call.data.get("device_id", "")).strip()

        if not device_id:
            raise HomeAssistantError("Missing device_id parameter.")

        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info, _, _ = _find_device_record(stored_data, device_id)
        if device_info is None:
            raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")

        purge_result = await _purge_device(hass, device_id)
        _remove_registry_device(hass, device_id)

        return {
            "status": "success",
            "device_id": device_id,
            "access_revoked": purge_result.get("access_revoked", False),
        }

    async def handle_update_wireguard(call: ServiceCall):
        import json

        users = await _check_authorization(call)
        device_id = str(call.data.get("device_id", "")).strip()
        username = str(call.data.get("username", "")).strip()
        action = str(call.data.get("action", "update")).strip().lower()
        silent = call.data.get("silent", True)
        encrypt_config = call.data.get("encrypt_config", True)
        wireguard_config = str(call.data.get("wireguard_config", ""))
        excluded_wifi = str(call.data.get("wireguard_excluded_wifi", "")).strip()
        title = str(call.data.get("title", "")).strip()
        message = str(call.data.get("message", "")).strip()

        if action not in ("update", "revoke"):
            raise HomeAssistantError("Invalid action. Must be 'update' or 'revoke'.")
        if not device_id and not username:
            raise HomeAssistantError("Must provide either device_id or username.")
        if action == "update" and not wireguard_config:
            raise HomeAssistantError("wireguard_config is required for the 'update' action.")

        stored_data = hass.data[DOMAIN]["stored_data"]

        # Resolve target devices as a list of (device_id, device_info, owning_user_id)
        targets = []
        if device_id:
            found = None
            for uid, udata in stored_data.get("users", {}).items():
                if device_id in udata.get("devices", {}):
                    found = (device_id, udata["devices"][device_id], uid)
                    break
            if not found:
                for uid, devices in stored_data.get("native_devices", {}).items():
                    if device_id in devices:
                        found = (device_id, devices[device_id], uid)
                        break
            if not found:
                raise HomeAssistantError(f"Device '{device_id}' not found in registered devices.")
            targets.append(found)
        else:
            target_user = next((u for u in users if u.name and u.name.casefold() == username.casefold()), None)
            if not target_user:
                for u in users:
                    for cred in u.credentials:
                        if cred.auth_provider_type == "homeassistant" and cred.data.get("username", "").casefold() == username.casefold():
                            target_user = u
                            break
                    if target_user:
                        break
            if not target_user:
                raise HomeAssistantError(f"User '{username}' not found.")

            uid = target_user.id
            if uid in stored_data["users"] and not stored_data["users"][uid].get("deleted", False):
                devices = stored_data["users"][uid].get("devices", {})
            else:
                devices = stored_data.get("native_devices", {}).get(uid, {})
            for did, dinfo in devices.items():
                targets.append((did, dinfo, uid))

        if not targets:
            _LOGGER.warning("CASA: No target devices found for wireguard %s.", action)
            return {"success": True, "sent_count": 0, "failed_count": 0, "skipped_count": 0}

        session = async_get_clientsession(hass)
        command = "wireguard_update" if action == "update" else "wireguard_revoke"

        sent_count = 0
        failed_count = 0
        skipped_count = 0

        for did, dinfo, uid in targets:
            push_token = dinfo.get("push_token")
            if not push_token:
                _LOGGER.warning("CASA: Device '%s' has no push token; skipping wireguard %s.", did, action)
                skipped_count += 1
                continue

            # Inner payload is encrypted (or plaintext-base64) end-to-end; the relay only routes it.
            if action == "update":
                inner = {
                    "action": "update",
                    "config": wireguard_config,
                    "excluded_wifi": excluded_wifi,
                    "ts": int(time.time()),
                }
            else:
                inner = {"action": "revoke", "ts": int(time.time())}
            inner_str = json.dumps(inner)

            device_key = stored_data.get("device_key")
            if encrypt_config:
                if not device_key:
                    _LOGGER.error("CASA ERROR: No site device_key available; cannot encrypt wireguard payload.")
                    failed_count += 1
                    continue
                try:
                    wg_payload = _encrypt_push_payload(inner_str, device_key, did)
                except Exception as e:
                    _LOGGER.error("CASA ERROR: Failed to encrypt wireguard payload for device '%s': %s", did, e)
                    failed_count += 1
                    continue
            else:
                wg_payload = base64.b64encode(inner_str.encode("utf-8")).decode("utf-8")

            payload = {
                "target": push_token,
                "site_id": stored_data.get("site_id"),
                "site_key": stored_data.get("site_key"),
                "title": "" if silent else title,
                "message": "" if silent else message,
                "push_type": "background" if silent else "alert",
                "priority": 5 if silent else 10,
                "data": {
                    "command": command,
                    "encrypted": bool(encrypt_config),
                    "wireguard_payload": wg_payload,
                    "device_key_id": _device_key_id(device_key) if (encrypt_config and device_key) else None,
                },
            }

            success = await _send_push_to_relay(session, payload)

            if success:
                sent_count += 1
                _LOGGER.info("CASA: Sent wireguard %s to device '%s' (encrypted=%s, silent=%s).", action, did, encrypt_config, silent)
            else:
                failed_count += 1
                _LOGGER.error("CASA: Failed to deliver wireguard %s to device '%s' after trying all relays.", action, did)

        return {
            "success": failed_count == 0,
            "sent_count": sent_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
        }

    hass.services.async_register(
        DOMAIN, "register_device", handle_register_device,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "notify_user", handle_notify_user,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "clear_ble_beacon", handle_clear_ble_beacon,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "remove_user", handle_remove_user,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "view_casa_users", handle_view_casa_users,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "reload_device", handle_reload_device,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "request_device_report", handle_request_device_report,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "request_heartbeat", handle_request_heartbeat,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "set_device_expiration", handle_set_device_expiration,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "deprovision_device", handle_deprovision_device,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "delete_device", handle_delete_device,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN, "update_wireguard", handle_update_wireguard,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # RELAY RECONCILIATION
    # ==========================================
    async def _reconcile_site() -> dict:
        """Diff the relay's live proxy tokens against HA's records.

        /reconcile is silent-mode-exempt, so it is the source of truth. Tokens the
        relay has but HA doesn't are unregistered; tokens HA has but the relay lost
        are flagged so the device re-registers on its next heartbeat.
        """
        stored_data = hass.data[DOMAIN]["stored_data"]
        site_id = stored_data.get("site_id")
        site_key = stored_data.get("site_key")
        if not site_id or not site_key:
            _LOGGER.warning("CASA: Skipping reconcile — site not registered.")
            return {"error": "Site not registered"}

        session = async_get_clientsession(hass)
        try:
            async with session.post(
                RELAY_RECONCILE_URL,
                json={"site_id": site_id, "site_key": site_key},
                timeout=ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.warning("CASA: /reconcile returned status %s: %s", resp.status, text)
                    return {"error": f"reconcile status {resp.status}"}
                data = await resp.json()
        except Exception as err:
            _LOGGER.warning("CASA: /reconcile request failed: %s", err)
            return {"error": str(err)}

        # Accept either {"proxy_tokens": [...]} or a bare list.
        if isinstance(data, dict):
            relay_tokens = set(data.get("proxy_tokens", []))
        elif isinstance(data, list):
            relay_tokens = set(data)
        else:
            relay_tokens = set()

        # Map every proxy token HA knows about to its (devices_dict, device_id).
        ha_map = {}
        for udata in stored_data.get("users", {}).values():
            for did, dinfo in udata.get("devices", {}).items():
                tok = dinfo.get("push_token")
                if tok:
                    ha_map[tok] = (udata["devices"], did)
        for devices in stored_data.get("native_devices", {}).values():
            for did, dinfo in devices.items():
                tok = dinfo.get("push_token")
                if tok:
                    ha_map[tok] = (devices, did)
        ha_tokens = set(ha_map.keys())

        # Relay has, HA doesn't -> unregister the orphan from the relay.
        orphaned = relay_tokens - ha_tokens
        for tok in orphaned:
            try:
                async with session.post(
                    RELAY_UNREGISTER_URL,
                    json={"proxy_token": tok},
                    timeout=ClientTimeout(total=10),
                ) as r:
                    if r.status not in (200, 404):
                        text = await r.text()
                        _LOGGER.warning("CASA: reconcile /unregister returned %s: %s", r.status, text)
            except Exception as err:
                _LOGGER.warning("CASA: reconcile /unregister failed for an orphan token: %s", err)

        # HA has, relay doesn't -> flag the device to re-register on next heartbeat.
        stale = ha_tokens - relay_tokens
        for tok in stale:
            devices, did = ha_map[tok]
            devices[did]["needs_reregister"] = True

        if orphaned or stale:
            await store.async_save(stored_data)

        result = {
            "live": len(relay_tokens),
            "orphaned_unregistered": len(orphaned),
            "flagged_reregister": len(stale),
        }
        _LOGGER.info(
            "CASA: Reconcile complete — live=%s, unregistered=%s, flagged_reregister=%s.",
            result["live"], result["orphaned_unregistered"], result["flagged_reregister"],
        )
        return result

    async def handle_reconcile(call: ServiceCall):
        await _check_authorization(call)
        return await _reconcile_site()

    hass.services.async_register(
        DOMAIN, "reconcile", handle_reconcile,
        supports_response=SupportsResponse.OPTIONAL
    )

    async def handle_regenerate_site(call: ServiceCall):
        """Rotate the site: remove it on the relay, then register a fresh one.

        Destructive — invalidates every existing device profile (they carry the old
        site_id), so all devices must be re-provisioned afterward.
        """
        await _check_authorization(call)
        stored_data = hass.data[DOMAIN]["stored_data"]
        session = async_get_clientsession(hass)

        old_site_id = stored_data.get("site_id")
        old_site_key = stored_data.get("site_key")
        if old_site_id and old_site_key:
            try:
                async with session.post(
                    RELAY_REMOVE_SITE_URL,
                    json={"site_id": old_site_id, "site_key": old_site_key},
                    timeout=ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        _LOGGER.warning("CASA: regenerate /remove_site returned %s: %s", resp.status, text)
            except Exception as err:
                _LOGGER.warning("CASA: regenerate /remove_site failed: %s", err)

        stored_data.pop("site_id", None)
        stored_data.pop("site_key", None)
        ok = await _register_site(hass, stored_data, store)
        return {"success": bool(ok), "site_id": stored_data.get("site_id")}

    hass.services.async_register(
        DOMAIN, "regenerate_site", handle_regenerate_site,
        supports_response=SupportsResponse.OPTIONAL
    )

    async def _scheduled_reconcile(now):
        await _reconcile_site()

    hass.data[DOMAIN]["reconcile_unsub"] = async_track_time_interval(
        hass, _scheduled_reconcile, timedelta(days=1)
    )

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

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "button"])

    return True

async def _unregister_relay_token(hass: HomeAssistant, proxy_token: str, device_id: str) -> None:
    """Best-effort unregister of a proxy token from the push relay."""
    if not proxy_token:
        return
    try:
        session = async_get_clientsession(hass)
        async with session.post(
            RELAY_UNREGISTER_URL,
            json={"proxy_token": proxy_token},
            timeout=ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                _LOGGER.info("CASA: Unregistered proxy token for device '%s' from relay.", device_id)
            elif resp.status == 404:
                _LOGGER.info("CASA: Relay had no registration for device '%s' (already gone).", device_id)
            else:
                text = await resp.text()
                _LOGGER.warning("CASA: Relay /unregister returned %s for device '%s': %s", resp.status, device_id, text)
    except Exception as err:
        _LOGGER.warning("CASA: Failed to unregister proxy token for device '%s' from relay: %s", device_id, err)


def _remove_registry_device(hass: HomeAssistant, device_id: str) -> None:
    """Best-effort removal of a Casa device from the HA device registry.

    Direct registry removal does not re-invoke async_remove_config_entry_device,
    so nothing is purged twice.
    """
    try:
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        reg_device = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
        if reg_device:
            dev_reg.async_remove_device(reg_device.id)
    except Exception as err:
        _LOGGER.warning("CASA: Failed to remove device '%s' from the HA device registry: %s", device_id, err)


async def _purge_device(hass: HomeAssistant, device_id: str) -> dict:
    """Remove a device's server-side footprint.

    Pops the record from storage (managed and native maps), unregisters the proxy
    token from the relay, revokes the device's HA refresh token, and drops any
    queued updates. Network/auth steps are best-effort.
    Returns {"found", "username", "push_token", "access_revoked"}.
    """
    stored_data = hass.data[DOMAIN]["stored_data"]
    store = hass.data[DOMAIN]["store"]

    owner_user_id = None
    refresh_token_id = None
    proxy_token = None
    username = "Unknown"
    access_revoked = False

    for uid, udata in stored_data.get("users", {}).items():
        devices = udata.get("devices", {})
        if device_id in devices:
            owner_user_id = uid
            refresh_token_id = devices[device_id].get("refresh_token_id")
            proxy_token = devices[device_id].get("push_token")
            username = udata.get("username", uid)
            devices.pop(device_id, None)
            break

    if owner_user_id is None:
        for uid, devices in stored_data.get("native_devices", {}).items():
            if device_id in devices:
                owner_user_id = uid
                refresh_token_id = devices[device_id].get("refresh_token_id")
                proxy_token = devices[device_id].get("push_token")
                username = uid
                devices.pop(device_id, None)
                break

    if owner_user_id is None:
        return {"found": False, "username": username, "push_token": None, "access_revoked": False}

    # Unregister the proxy token from the relay (possession of the token is the auth).
    await _unregister_relay_token(hass, proxy_token, device_id)

    # Revoke this device's HA session so it loses access (and can't silently
    # re-register via heartbeat). Scoped to the device's own token only.
    if refresh_token_id:
        user = await hass.auth.async_get_user(owner_user_id)
        if user:
            token = user.refresh_tokens.get(refresh_token_id)
            if token:
                hass.auth.async_remove_refresh_token(token)
                access_revoked = True
                _LOGGER.info(
                    "CASA: Revoked refresh token for deleted device '%s' (user '%s').",
                    device_id, username,
                )
            else:
                _LOGGER.warning(
                    "CASA: No matching refresh token for deleted device '%s' (user '%s'); session not revoked.",
                    device_id, username,
                )

    # Drop any queued updates addressed to this device.
    qu_data = hass.data[DOMAIN].get("qu_data")
    if qu_data and qu_data.get("updates", {}).pop(device_id, None) is not None:
        qu_store = hass.data[DOMAIN].get("qu_store")
        if qu_store:
            qu_store.async_delay_save(lambda: qu_data, 2.0)

    await store.async_save(stored_data)
    _LOGGER.info("CASA: Deleted device '%s' from storage (user '%s').", device_id, username)
    return {"found": True, "username": username, "push_token": proxy_token, "access_revoked": access_revoked}


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry
) -> bool:
    """Allow deleting a Casa device from the UI.

    HA renders the Delete action (and its confirmation dialog) once this exists.
    On delete we revoke the device's HA refresh token (killing its access) and
    purge it from our storage before allowing the registry removal.
    """
    device_id = next(
        (ident for domain, ident in device_entry.identifiers if domain == DOMAIN),
        None,
    )
    if not device_id:
        return True

    await _purge_device(hass, device_id)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Purge persistent state when the integration is permanently deleted.

    Runs only on entry removal (not reload/unload). Best-effort: revoke each device's
    HA refresh token to cut access, tear down the whole site on the relay via
    /remove_site (destructive cascade, frees the site_id to re-register), then delete
    the integration's Store so a reinstall starts fresh. HA user accounts are left intact.
    """
    store = Store(hass, 1, "casa_users")
    try:
        stored_data = await store.async_load()
    except Exception as err:
        _LOGGER.warning("CASA: Could not load store during entry removal: %s", err)
        stored_data = None

    if stored_data:
        session = async_get_clientsession(hass)

        # 1. Revoke each device's HA refresh token (cut access). HA accounts kept.
        device_entries = []
        for uid, udata in stored_data.get("users", {}).items():
            for dinfo in udata.get("devices", {}).values():
                device_entries.append((uid, dinfo))
        for uid, devices in stored_data.get("native_devices", {}).items():
            for dinfo in devices.values():
                device_entries.append((uid, dinfo))

        for owner_user_id, dinfo in device_entries:
            refresh_token_id = dinfo.get("refresh_token_id")
            if owner_user_id and refresh_token_id:
                try:
                    user = await hass.auth.async_get_user(owner_user_id)
                    if user:
                        token = user.refresh_tokens.get(refresh_token_id)
                        if token:
                            hass.auth.async_remove_refresh_token(token)
                except Exception as err:
                    _LOGGER.warning("CASA: removal token revoke failed for a device: %s", err)

        # 2. Destructive cascade on the relay: remove the whole site in one call.
        # This unregisters all of the site's proxy tokens and frees the site_id.
        site_id = stored_data.get("site_id")
        site_key = stored_data.get("site_key")
        if site_id and site_key:
            try:
                async with session.post(
                    RELAY_REMOVE_SITE_URL,
                    json={"site_id": site_id, "site_key": site_key},
                    timeout=ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        _LOGGER.info(
                            "CASA: Removed site '%s' from relay (removed_count=%s).",
                            site_id, data.get("removed_count"),
                        )
                    else:
                        text = await resp.text()
                        _LOGGER.warning("CASA: /remove_site returned %s: %s", resp.status, text)
            except Exception as err:
                _LOGGER.warning("CASA: /remove_site failed: %s", err)

    # 3. Delete the persistent store so a reinstall starts fresh.
    try:
        await store.async_remove()
        _LOGGER.info("CASA: Removed integration store on entry deletion.")
    except Exception as err:
        _LOGGER.warning("CASA: Failed to remove store on entry deletion: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "button"])
    
    hass.services.async_remove(DOMAIN, "provision")
    hass.services.async_remove(DOMAIN, "generate_qr")
    hass.services.async_remove(DOMAIN, "remove_token")
    hass.services.async_remove(DOMAIN, "create_user")
    hass.services.async_remove(DOMAIN, "list_tokens")
    hass.services.async_remove(DOMAIN, "housekeeping")
    hass.services.async_remove(DOMAIN, "scramble_guest_password")
    hass.services.async_remove(DOMAIN, "provision_ble_beacon")
    hass.services.async_remove(DOMAIN, "clear_ble_beacon")
    hass.services.async_remove(DOMAIN, "remove_user")
    hass.services.async_remove(DOMAIN, "view_casa_users")
    hass.services.async_remove(DOMAIN, "register_device")
    hass.services.async_remove(DOMAIN, "notify_user")
    hass.services.async_remove(DOMAIN, "reload_device")
    hass.services.async_remove(DOMAIN, "request_device_report")
    hass.services.async_remove(DOMAIN, "request_heartbeat")
    hass.services.async_remove(DOMAIN, "set_device_expiration")
    hass.services.async_remove(DOMAIN, "deprovision_device")
    hass.services.async_remove(DOMAIN, "delete_device")
    hass.services.async_remove(DOMAIN, "update_wireguard")
    hass.services.async_remove(DOMAIN, "reconcile")
    hass.services.async_remove(DOMAIN, "regenerate_site")

    reconcile_unsub = hass.data[DOMAIN].get("reconcile_unsub")
    if reconcile_unsub:
        reconcile_unsub()

    lz_stale_unsub = hass.data[DOMAIN].get("lz_stale_unsub")
    if lz_stale_unsub:
        lz_stale_unsub()

    try:
        frontend.async_remove_panel(hass, "casa")
    except Exception:
        pass

    for task in hass.data[DOMAIN].get("timers", {}).values():
        task.cancel()
    for task in hass.data[DOMAIN].get("listeners", {}).values():
        task.cancel()
    hass.data.pop(DOMAIN, None)
    return unload_ok
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
from datetime import datetime, timedelta

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
from .const import CASA_VERSION, DOMAIN, QUEUE_MAX_AGE_DAYS, QUEUE_MAX_PER_DEVICE, CONF_ADMIN_SYSTEM_ONLY, RELAY_BASE_URL, CONF_RELAY_BASE_URL, CONF_CREATE_DEVICES, CONF_SHOW_PANEL, UNIVERSAL_LINK_SETUP_URL, DEVICE_ALIAS_MAX_LEN, DEFAULT_HEARTBEAT_INTERVAL_SECONDS, MIN_HEARTBEAT_INTERVAL_SECONDS, MAX_HEARTBEAT_INTERVAL_SECONDS, DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS, MIN_PROFILE_REPORT_INTERVAL_SECONDS, MAX_PROFILE_REPORT_INTERVAL_SECONDS, LIVE_PROVISIONING_FIELDS, PROFILE_PROVISIONING_FIELDS
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

# hass.data key (outside hass.data[DOMAIN], which unload pops) marking that
# the HTTP views are registered on this HA instance.
_VIEWS_REGISTERED_KEY = f"{DOMAIN}_views_registered"


def _entry_func(hass, key: str):
    """Coroutine that forwards to hass.data[DOMAIN][key] at call time, so a
    view registered once keeps reaching the current entry's closure."""
    async def _call(*args, **kwargs):
        func = (hass.data.get(DOMAIN) or {}).get(key)
        if func is None:
            raise HomeAssistantError("Casa integration is not loaded.")
        return await func(*args, **kwargs)
    return _call

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


def _qr_png_data_uri(text: str) -> str:
    """Render text as a PNG QR and return it as a data: URI (no file on disk)."""
    import io
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


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


def relay_base(hass, entry=None) -> str:
    """Normalised relay base URL (no trailing slash) for this site.

    entry: an explicit ConfigEntry whose options win (needed in
    async_remove_entry, which runs after hass.data[DOMAIN] is gone); else the
    stored config_entry; else the default RELAY_BASE_URL.
    """
    base = RELAY_BASE_URL
    if entry is None:
        entry = (getattr(hass, "data", None) or {}).get(DOMAIN, {}).get("config_entry")
    if entry is not None:
        configured = str((entry.options or {}).get(CONF_RELAY_BASE_URL, "") or "").strip()
        if configured:
            base = configured
    return base.rstrip("/")


def payload_relay_url(hass) -> str | None:
    """relay_url for provisioning payloads: the site's relay base, but only
    when it is not the default (apps assume the default when it is absent)."""
    base = relay_base(hass)
    return base if base != RELAY_BASE_URL.rstrip("/") else None


def relay_url(hass, path: str, entry=None) -> str:
    """Absolute relay URL for path, honouring the per-site relay_base_url option."""
    return relay_base(hass, entry) + "/" + path.lstrip("/")


_LAN_HOST_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+|.+\.local)$"
)


def _validate_relay_base_url(value) -> str | None:
    """Return the form error key for a relay_base_url option value, or None.

    Blank means "use the default relay". https is always accepted; http only
    for localhost, 127.x, RFC1918 (10.x, 172.16-31.x, 192.168.x) or *.local.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urllib.parse.urlparse(text)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "invalid_relay_url"
    if not host:
        return "invalid_relay_url"
    if parsed.scheme == "https":
        return None
    if parsed.scheme == "http" and _LAN_HOST_RE.match(host):
        return None
    return "invalid_relay_url"


def _relay_site_credentials(stored_data: dict, base: str) -> dict | None:
    """The {site_id, site_key} issued by the relay at base, or None."""
    entry = (stored_data.get("relay_sites") or {}).get(base)
    return dict(entry) if entry else None


def _set_relay_site_credentials(stored_data: dict, base: str, site_id, site_key) -> None:
    stored_data.setdefault("relay_sites", {})[base] = {"site_id": site_id, "site_key": site_key}


def _delete_relay_site_credentials(stored_data: dict, base: str) -> None:
    (stored_data.get("relay_sites") or {}).pop(base, None)


def _migrate_legacy_site_credentials(stored_data: dict, default_base: str) -> bool:
    """Pre-26.09.30 stores hold one top-level site_id/site_key, issued by the
    default relay. Move them under relay_sites[default_base] once.
    Returns True when the store changed."""
    if "relay_sites" in stored_data:
        return False
    stored_data["relay_sites"] = {}
    if stored_data.get("site_id") and stored_data.get("site_key"):
        _set_relay_site_credentials(
            stored_data, default_base.rstrip("/"), stored_data["site_id"], stored_data["site_key"]
        )
    return True


def _activate_relay_site(stored_data: dict, base: str) -> None:
    """Mirror relay_sites[base] into the top-level site_id/site_key that the
    rest of the integration reads. No entry for base -> no active credentials
    (the site registers afresh with that relay; other bases are untouched)."""
    creds = _relay_site_credentials(stored_data, base)
    if creds:
        stored_data["site_id"] = creds.get("site_id")
        stored_data["site_key"] = creds.get("site_key")
    else:
        stored_data.pop("site_id", None)
        stored_data.pop("site_key", None)


async def _probe_relay(hass) -> None:
    """Record the relay's version/protocol from GET /health; never blocks setup."""
    data = hass.data[DOMAIN]
    data["relay_version"] = None
    data["relay_protocol"] = None
    try:
        session = async_get_clientsession(hass)
        async with session.get(relay_url(hass, "/health"), timeout=ClientTimeout(total=5)) as resp:
            body = await resp.json(content_type=None)
        data["relay_version"] = body.get("version")
        data["relay_protocol"] = body.get("protocol")
    except Exception as err:
        _LOGGER.debug("CASA: relay /health probe failed: %s", err)
        return
    if data["relay_protocol"] not in (None, 1):
        _LOGGER.warning(
            "CASA: relay reports protocol %s; this integration knows protocol 1.",
            data["relay_protocol"],
        )


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
                relay_url(hass, "/register_site"),
                json={"site_id": site_id},
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    stored_data["site_key"] = data["site_key"]
                    _set_relay_site_credentials(
                        stored_data, relay_base(hass), site_id, data["site_key"]
                    )
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
            relay_url(hass, "/verify_site"),
            json={"site_id": stored_data.get("site_id"), "site_key": stored_data.get("site_key")},
            timeout=ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return
            if resp.status == 403:
                _LOGGER.warning(
                    "CASA: Relay does not recognize our site credentials (403); re-registering site."
                )
                # Only this relay's credentials are dropped; a key issued by
                # another relay base (e.g. production) is never touched.
                stored_data.pop("site_key", None)
                _delete_relay_site_credentials(stored_data, relay_base(hass))
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


async def _login_listener(hass, username, user_id, known_tokens, ttl_seconds, method, on_redeemed=None, provision_id=None, on_tokens=None):
    """Poll for new refresh tokens; fire casa_code_redeemed when one appears.

    on_redeemed: optional coroutine function run once after the first
    redemption event (used to scramble the password so the link is
    single-use). When it is set the listener returns after the first
    redemption; otherwise it keeps reporting until the TTL ends.
    provision_id: echoed in the event (with user_id) so a card can tell its
    own code's redemption from any other.
    on_tokens: optional sync callable given each batch of new refresh token
    ids (records them as fresh device claims, see _has_fresh_claim).
    """
    if ttl_seconds <= 0:
        _LOGGER.warning("CASA: Listener for '%s' skipped — TTL is %s.", username, ttl_seconds)
        return
    try:
        elapsed = 0
        poll_interval = 2
        while elapsed < ttl_seconds:
            if elapsed >= 1800:
                poll_interval = 10  # long single-use windows: back off after 30 min
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
                        "user_id": user_id,
                        "provision_id": provision_id,
                    })
                    _LOGGER.info(
                        "CASA EVENT: Code redeemed by '%s' via %s (client: %s, IP: %s).",
                        username, method, token.client_name, token.last_used_ip
                    )
            known_tokens.update(new_tokens)
            if on_tokens is not None:
                try:
                    on_tokens(set(new_tokens))
                except Exception as err:
                    _LOGGER.error("CASA: Recording redemption for '%s' failed: %s", username, err)

            if on_redeemed is not None:
                try:
                    await on_redeemed()
                except Exception as err:  # never let a scramble failure kill the listener silently
                    _LOGGER.error("CASA: on_redeemed for '%s' failed: %s", username, err)
                return
    except asyncio.CancelledError:
        pass


# Process-level, so an entry reload mid-operation can't hand a second caller
# a fresh, unheld lock for the same device/user.
_LOCKS: dict = {}


def _lock_for(hass, kind: str, key: str) -> asyncio.Lock:
    """Per-(kind, key) asyncio.Lock serializing read-modify-write sequences on
    the queue/store that await in between (e.g. kind "device" or "user").
    Callers that need both always take device before user. Never hold one
    across relay/network calls."""
    lock = _LOCKS.get((kind, key))
    if lock is None:
        lock = _LOCKS[(kind, key)] = asyncio.Lock()
    return lock


def _invalidate_queued_reauths(hass, login_username: str, current_password: str) -> int:
    """Drop queued auth/reauthenticate entries for login_username that carry a
    password other than current_password, and clear the reauth_pending
    markers that reference them. A stale entry can never log in: the device
    would log out to apply it, fail, and treat that as a revoked session.
    Returns the number of entries dropped."""
    data = hass.data.get(DOMAIN) or {}
    qu_data = data.get("qu_data") or {}
    stored_data = data.get("stored_data") or {}
    target = str(login_username or "").casefold()
    dropped = set()
    for device_id in list((qu_data.get("updates") or {}).keys()):
        entries = qu_data["updates"][device_id]
        kept = []
        for e in entries:
            payload = e.get("payload") or {}
            if (
                e.get("type") == "auth"
                and str(payload.get("username", "") or "").casefold() == target
                and payload.get("password") != current_password
            ):
                dropped.add(e.get("id"))
                _LOGGER.warning(
                    "CASA: Dropped queued reauthentication of device '%s' to '%s' — that user's password was rotated since it was queued.",
                    device_id, login_username,
                )
                continue
            kept.append(e)
        if len(kept) != len(entries):
            if kept:
                qu_data["updates"][device_id] = kept
            else:
                qu_data["updates"].pop(device_id, None)
    if not dropped:
        return 0
    for _uid, udata in stored_data.get("users", {}).items():
        for dinfo in (udata.get("devices", {}) or {}).values():
            if (dinfo.get("reauth_pending") or {}).get("update_id") in dropped:
                dinfo.pop("reauth_pending", None)
    for devices in stored_data.get("native_devices", {}).values():
        for dinfo in (devices or {}).values():
            if (dinfo.get("reauth_pending") or {}).get("update_id") in dropped:
                dinfo.pop("reauth_pending", None)
    if data.get("qu_store"):
        data["qu_store"].async_delay_save(lambda: qu_data, 2.0)
    if data.get("store"):
        data["store"].async_delay_save(lambda: stored_data, 2.0)
    return len(dropped)


def _password_fingerprint(stored_data: dict, login_username: str, password: str) -> str:
    """Salted fingerprint of a password the server itself set, so a later
    reauth can tell a verified-current queued password from one an admin
    typed (which the server never set and can't vouch for)."""
    salt = stored_data.setdefault("password_fp_salt", secrets.token_hex(16))
    raw = f"{salt}:{str(login_username).casefold()}:{password}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _queued_reauth_password(hass, login_username: str, exclude_device_id: str | None = None) -> str | None:
    """Password carried by a still-queued reauthenticate entry for
    login_username on another device, or None. Only a password the server
    itself last set on that account (see _set_account_password) is reused —
    a second reauth to the same user then doesn't rotate it out from under
    the first device's entry. Anything unverified returns None (rotate)."""
    data = hass.data.get(DOMAIN) or {}
    qu_data = data.get("qu_data") or {}
    stored_data = data.get("stored_data") or {}
    target = str(login_username or "").casefold()
    current_fp = (stored_data.get("server_passwords") or {}).get(target)
    if not current_fp:
        return None
    for device_id, entries in (qu_data.get("updates") or {}).items():
        if device_id == exclude_device_id:
            continue
        for e in entries:
            payload = e.get("payload") or {}
            if (
                e.get("type") == "auth"
                and str(payload.get("username", "") or "").casefold() == target
                and payload.get("password")
                and _password_fingerprint(stored_data, target, payload["password"]) == current_fp
            ):
                return payload["password"]
    return None


async def _set_account_password(hass, auth_provider, login_username: str, password: str | None = None,
                                window_provision_id: str | None = None) -> str:
    """The one place a homeassistant-provider password is changed. Sets
    password (or a fresh random one) via the provider's async API (bcrypt in
    the executor, lazy data init, save), and:
      - invalidates queued reauth entries still carrying another password;
      - closes any open provisioning window for this login other than
        window_provision_id (the window doing its own scramble): its link is
        dead now, and a later timer/listener scramble of it must not fire a
        false casa_code_redeemed or rotate away a password other devices'
        queued reauths now carry;
      - records a fingerprint of the server-set password (_queued_reauth_password).
    Returns the password now in effect. Raises what async_change_password
    raises (InvalidUser for a gone login)."""
    if not password:
        password = generate_random_password()
    await _close_provision_windows_for_login(hass, login_username, keep_provision_id=window_provision_id)
    # Invalidate before the (executor + save) await, so nothing can deliver a
    # stale entry in between.
    _invalidate_queued_reauths(hass, login_username, password)
    await auth_provider.async_change_password(login_username, password)
    stored_data = (hass.data.get(DOMAIN) or {}).get("stored_data")
    if stored_data is not None:
        key = str(login_username).casefold()
        stored_data.setdefault("server_passwords", {})[key] = _password_fingerprint(stored_data, key, password)
        _save_stored_data(hass)
    return password


_QR_EXPIRED_TEXT = "EXPIRED - Request a new Casa code."
_QR_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _safe_qr_filename(raw) -> str | None:
    """An explicit qr_filename reduced to a safe www/ basename ending in .png,
    or None when it is unusable (empty, a path, '..', or nothing left)."""
    name = str(raw or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name or "\x00" in name:
        return None
    if name.lower().endswith(".png"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not _QR_FILENAME_RE.match(name):
        return None
    return name + ".png"


def _write_qr_file(hass, filename: str, text: str) -> None:
    """Executor job: render text as a QR into www/<filename> (served at /local/)."""
    www_dir = hass.config.path("www")
    os.makedirs(www_dir, exist_ok=True)
    qrcode.make(text).save(os.path.join(www_dir, filename))


def _retire_qr_file(hass, filename: str, mode: str) -> None:
    """Executor job: remove a provisioning QR file from www/, or (mode
    "expire") overwrite it with an EXPIRED placeholder so a dashboard image
    that points at it doesn't break. Only ever touches that one file."""
    path = os.path.join(hass.config.path("www"), filename)
    if mode == "expire":
        if os.path.exists(path):
            qrcode.make(_QR_EXPIRED_TEXT).save(path)
    elif os.path.exists(path):
        os.remove(path)


def _pending_provision(hass, user_id: str, provision_id: str | None = None) -> dict | None:
    """The persisted open provisioning window for user_id (optionally only if
    it is still the one identified by provision_id)."""
    data = hass.data.get(DOMAIN) or {}
    rec = (data.get("stored_data") or {}).get("pending_provisions", {}).get(user_id)
    if rec is None or (provision_id is not None and rec.get("provision_id") != provision_id):
        return None
    return rec


def _save_stored_data(hass) -> None:
    """Delayed save through the CURRENT entry's store and data. Code that
    outlives a reload (setup closures, background tasks) must save this way,
    never through a Store/dict it captured, or it writes stale data back."""
    data = hass.data.get(DOMAIN) or {}
    if data.get("store") is not None:
        data["store"].async_delay_save(lambda: data["stored_data"], 2.0)


async def _save_stored_data_now(hass) -> None:
    data = hass.data.get(DOMAIN) or {}
    if data.get("store") is not None:
        await data["store"].async_save(data["stored_data"])


async def _retire_provision_qr(hass, rec: dict) -> None:
    filename = rec.get("qr_file")
    if not filename:
        return
    rec["qr_file"] = None
    _save_stored_data(hass)
    try:
        await hass.async_add_executor_job(_retire_qr_file, hass, filename, rec.get("qr_expire_mode", "delete"))
        _LOGGER.info("CASA: Provisioning QR file %s retired.", filename)
    except Exception as err:
        _LOGGER.warning("CASA: Could not retire provisioning QR file %s: %s", filename, err)


def _cancel_provision_tasks(hass, user_id: str) -> None:
    """Cancel user_id's provisioning timer/listener (never the calling task)."""
    data = hass.data.get(DOMAIN) or {}
    current = asyncio.current_task()
    for key in ("timers", "listeners"):
        task = (data.get(key) or {}).pop(user_id, None)
        if task is not None and task is not current:
            task.cancel()


async def _close_provision_windows_for_login(hass, login_username: str, keep_provision_id: str | None = None) -> None:
    """Forget every open provisioning window for login_username except
    keep_provision_id: stop its timer/listener and retire its QR file. Used
    when the password changes for any other reason (the link is dead)."""
    data = hass.data.get(DOMAIN) or {}
    pending = (data.get("stored_data") or {}).get("pending_provisions") or {}
    target = str(login_username or "").casefold()
    for user_id, rec in list(pending.items()):
        if str(rec.get("login_username", "")).casefold() != target or rec.get("provision_id") == keep_provision_id:
            continue
        pending.pop(user_id, None)
        _cancel_provision_tasks(hass, user_id)
        _save_stored_data(hass)
        await _retire_provision_qr(hass, rec)
        _LOGGER.info("CASA: Provisioning window for '%s' closed — its password changed.", rec.get("login_username"))


# Fresh-claim windows (see _has_fresh_claim).
_CLAIM_TTL_SECONDS = 30 * 86400
_CLAIM_WINDOW_SECONDS = 24 * 3600
_CLAIM_TOKEN_MAX_AGE_SECONDS = 1800


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


async def _has_fresh_claim(hass, user_id: str, refresh_token_id: str | None) -> bool:
    """True when refresh_token_id redeemed a provisioning window for user_id
    (recorded by the listener / window end), or was created at or after a
    window for user_id opened in the last 24 h and is under 30 min old."""
    if not refresh_token_id:
        return False
    stored_data = hass.data[DOMAIN]["stored_data"]
    now = time.time()
    claimed_at = ((stored_data.get("provision_claims") or {}).get(user_id) or {}).get(refresh_token_id)
    if claimed_at and now - claimed_at <= _CLAIM_TTL_SECONDS:
        return True
    opened = (stored_data.get("provision_opened") or {}).get(user_id)
    if not opened or now - opened > _CLAIM_WINDOW_SECONDS:
        return False
    user = await hass.auth.async_get_user(user_id)
    token = user.refresh_tokens.get(refresh_token_id) if user else None
    created = getattr(token, "created_at", None)
    if created is None:
        return False
    created_ts = created.timestamp()
    return created_ts >= opened and now - created_ts <= _CLAIM_TOKEN_MAX_AGE_SECONDS


def _consume_provision_claim(hass, user_id: str, refresh_token_id: str | None) -> None:
    claims = (hass.data[DOMAIN]["stored_data"].get("provision_claims") or {}).get(user_id)
    if claims and claims.pop(refresh_token_id, None) is not None:
        _save_stored_data(hass)


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
    for key in ("provisioning_profile_id", "provisioning_profile_name", "provisioning_expiration_hours"):
        if new_info.get(key) is None and old_info.get(key) is not None:
            new_info[key] = old_info[key]
    await _purge_device(hass, old_device_id)
    _LOGGER.info("CASA: Device '%s' replaced '%s' after a QR re-provision.", new_device_id, old_device_id)
    return old_device_id


async def _apply_pending_provision(hass, user_id: str, device_info: dict, refresh_token_id: str | None) -> bool:
    """Apply what _provision_internal stashed for user_id (template lineage,
    session length, the name typed in the wizard) to this device record —
    on whichever of register/heartbeat arrives first, and only from the
    session that redeemed the window (on a shared account another device's
    contact must not take it). Entries older than 30 min are dropped.
    Returns True when it consumed the entry (a fresh provision)."""
    pending_by_user = hass.data[DOMAIN].setdefault("pending_profile_by_user", {})
    pending = pending_by_user.get(user_id)
    if not pending or not await _has_fresh_claim(hass, user_id, refresh_token_id):
        return False
    pending_by_user.pop(user_id, None)
    if time.time() - pending.get("set_at", 0) > 1800:
        return False
    device_info["provisioning_profile_id"] = pending.get("profile_id")
    device_info["provisioning_profile_name"] = pending.get("profile_name")
    if "expiration_hours" in pending:
        device_info["provisioning_expiration_hours"] = pending["expiration_hours"]
    alias = str(pending.get("device_alias") or "").strip()
    if alias and not str(device_info.get("alias") or "").strip():
        device_info["alias"] = alias[:DEVICE_ALIAS_MAX_LEN]
    return True


async def _end_provision_window(hass, user_id: str, provision_id: str, reason: str) -> None:
    """Close a provisioning window: scramble the password (the link dies),
    retire its QR file, forget the persisted record and stop its tasks.
    Safe when the user or its login is gone (deleted since provisioning).
    A window superseded while this waited for the user lock (a newer
    provision, or a rotation that closed it) is left alone."""
    rec = _pending_provision(hass, user_id, provision_id)
    if rec is None:
        return
    login_username = rec.get("login_username")
    user = await hass.auth.async_get_user(user_id)
    provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
    if user is None or provider is None:
        _LOGGER.info("CASA: Provisioning window for '%s' closed (%s); user no longer exists.", login_username, reason)
    else:
        async with _lock_for(hass, "user", user_id):
            if _pending_provision(hass, user_id, provision_id) is None:
                return
            # Sessions born during the window (also catches a redemption
            # the listener missed, e.g. across a restart).
            _record_provision_claims(
                hass, user_id, set(user.refresh_tokens.keys()) - set(rec.get("known_token_ids") or []),
                replaces_device_id=rec.get("replaces_device_id"),
            )
            try:
                await _set_account_password(hass, provider, login_username, window_provision_id=provision_id)
                _LOGGER.info("CASA: Password for %s scrambled (%s).", login_username, reason)
            except Exception as err:  # InvalidUser: login removed since provisioning
                _LOGGER.warning("CASA: Could not scramble password for '%s' (%s): %s", login_username, reason, err)
    if _pending_provision(hass, user_id, provision_id) is None:
        return
    await _retire_provision_qr(hass, rec)
    if _pending_provision(hass, user_id, provision_id) is not None:
        hass.data[DOMAIN]["stored_data"]["pending_provisions"].pop(user_id, None)
        _save_stored_data(hass)
    _cancel_provision_tasks(hass, user_id)


async def _provision_timer(hass, user_id: str, provision_id: str) -> None:
    """Drive one persisted provisioning window: retire the QR file when the
    scanning window ends, scramble at scramble_at, and forget the record once
    the redemption listener's time is up too. Each step re-reads the record,
    so a redemption that already closed the window simply ends this task."""
    try:
        while True:
            rec = _pending_provision(hass, user_id, provision_id)
            if rec is None:
                return
            steps = []
            if rec.get("qr_file") and rec.get("window_ends_at"):
                steps.append((rec["window_ends_at"], "qr"))
            if rec.get("scramble_at"):
                steps.append((rec["scramble_at"], "scramble"))
            if not steps:
                steps.append((rec.get("listen_until") or 0, "end"))
            when, action = min(steps)
            wait = when - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            if action == "qr":
                await _retire_provision_qr(hass, rec)
            elif action == "scramble":
                await _end_provision_window(hass, user_id, provision_id, "timer")
                return
            else:
                hass.data[DOMAIN]["stored_data"].get("pending_provisions", {}).pop(user_id, None)
                _save_stored_data(hass)
                listener = hass.data[DOMAIN]["listeners"].pop(user_id, None)
                if listener is not None:
                    listener.cancel()
                hass.data[DOMAIN]["timers"].pop(user_id, None)
                return
    except asyncio.CancelledError:
        pass


def _arm_pending_provision(hass, user_id: str) -> None:
    """(Re)start the timer and redemption listener for user_id's persisted
    provisioning window. Used right after provisioning and again at setup,
    so single-use and expiry survive reloads and restarts; a window whose
    deadlines passed while HA was down is closed immediately by the timer."""
    _cancel_provision_tasks(hass, user_id)
    rec = _pending_provision(hass, user_id)
    if rec is None:
        return
    data = hass.data[DOMAIN]
    provision_id = rec.get("provision_id")
    # Background tasks: long sleepers must not hold up HA startup.
    data["timers"][user_id] = hass.async_create_background_task(
        _provision_timer(hass, user_id, provision_id), name=f"casa provisioning timer {user_id}",
    )
    ttl = int((rec.get("listen_until") or 0) - time.time())
    if ttl <= 0:
        return

    async def _on_redeemed():
        # Single-use link: rotate the password the moment it is used.
        await _end_provision_window(hass, user_id, provision_id, "redeemed")

    data["listeners"][user_id] = hass.async_create_background_task(
        _login_listener(
            hass, rec.get("login_username"), user_id, set(rec.get("known_token_ids") or []), ttl,
            rec.get("method"), on_redeemed=_on_redeemed if rec.get("single_use") else None,
            provision_id=provision_id,
            on_tokens=lambda tids: _record_provision_claims(
                hass, user_id, tids, replaces_device_id=rec.get("replaces_device_id"),
            ),
        ),
        name=f"casa provisioning listener {user_id}",
    )


def _rearm_pending_provisions(hass) -> None:
    """Setup-time: re-arm every persisted provisioning window."""
    pending = hass.data[DOMAIN]["stored_data"].get("pending_provisions") or {}
    for user_id in list(pending.keys()):
        if not isinstance(pending.get(user_id), dict) or not pending[user_id].get("provision_id"):
            pending.pop(user_id, None)
            continue
        _arm_pending_provision(hass, user_id)


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


def _device_owned_by(stored_data: dict, user_id: str, device_id: str) -> bool:
    """True when device_id belongs to user_id (managed, not deleted; or native)."""
    users = stored_data.get("users", {}) if stored_data else {}
    entry = users.get(user_id)
    if entry and not entry.get("deleted", False) and device_id in entry.get("devices", {}):
        return True
    native = stored_data.get("native_devices", {}) if stored_data else {}
    return device_id in native.get(user_id, {})


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
    _save_stored_data(hass)
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


def _queued_before(entry: dict, cutoff) -> bool:
    """True when a queue entry's created_at is older than cutoff (unparseable
    timestamps are kept)."""
    try:
        created = datetime.fromisoformat(str(entry.get("created_at") or ""))
        return created < cutoff
    except (ValueError, TypeError):
        return False


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
        behind by register/heartbeat racing an incomplete reauth);
      - non-auth entries older than QUEUE_MAX_AGE_DAYS are dropped, and a
        device's queue is capped at QUEUE_MAX_PER_DEVICE by dropping its
        oldest non-auth entries (a device that never acks — e.g. an old app
        that can't apply an entry type — must not grow its queue forever).
        WireGuard revokes and each device's newest WireGuard entry are never
        age/cap-pruned; a pruned profile push clears provisioning_pending_push.

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
    cutoff = dt_util.now() - timedelta(days=QUEUE_MAX_AGE_DAYS)

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
        # A long-offline device must still get its WireGuard revoke (and its
        # latest WireGuard state), however old: never age/cap-prune those.
        wg_entries = [e for e in entries if e.get("type") == "wireguard"]
        protected = {id(e) for e in wg_entries if e.get("action") == "revoke"}
        if wg_entries:
            protected.add(id(wg_entries[-1]))
        pruned_profile = False
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
            elif id(e) not in protected and _queued_before(e, cutoff):
                removed += 1
                pruned_profile = pruned_profile or e.get("type") == "profile"
                _LOGGER.info(
                    "CASA: Purged %s/%s update %s for device '%s' — unacknowledged for over %d days.",
                    e.get("type"), e.get("action"), e.get("id"), device_id, QUEUE_MAX_AGE_DAYS,
                )
                continue
            kept.append(e)
        overflow = len(kept) - QUEUE_MAX_PER_DEVICE
        if overflow > 0:
            # Entries are appended in order, so the first non-auth ones are the oldest.
            droppable = [e for e in kept if e.get("type") != "auth" and id(e) not in protected]
            drop_ids = {id(e) for e in droppable[:overflow]}
            pruned_profile = pruned_profile or any(e.get("type") == "profile" for e in droppable[:overflow])
            kept = [e for e in kept if id(e) not in drop_ids]
            removed += len(drop_ids)
            _LOGGER.info(
                "CASA: Device '%s' had more than %d queued updates; dropped the %d oldest.",
                device_id, QUEUE_MAX_PER_DEVICE, len(drop_ids),
            )
        if pruned_profile and not any(e.get("type") == "profile" for e in kept):
            # The push it was waiting to confirm is gone; stop showing it pending.
            if device_info.get("provisioning_pending_push"):
                device_info["provisioning_pending_push"] = False
                stored_changed = True
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

    # The login username must be free in the auth provider itself — linked to
    # some user's credentials or an orphaned provider entry. add_auth would
    # raise only after the HA user already exists.
    login_taken = any(
        cred.auth_provider_type == "homeassistant"
        and str(cred.data.get("username", "")).strip().casefold() == username
        for u in users for cred in u.credentials
    )
    if not login_taken:
        if getattr(provider, "data", None) is None and hasattr(provider, "async_initialize"):
            await provider.async_initialize()  # newer HA loads provider data lazily
        login_taken = any(
            str(entry.get("username", "")).strip().casefold() == username
            for entry in (getattr(provider.data, "users", None) or [])
        )
    if login_taken:
        return None, "A login with this username already exists"

    if not password:
        password = generate_random_password()

    new_user = await hass.auth.async_create_user(
        name=name,
        group_ids=["system-users"],
        local_only=local_only
    )

    auth_added = False
    try:
        await provider.async_add_auth(username, password)
        auth_added = True

        credentials = await provider.async_get_or_create_credentials({"username": username})
        await hass.auth.async_link_user(new_user, credentials)
    except Exception as err:
        # Roll back so a failed credential step never leaves an orphan HA
        # user (or an unlinked login) behind.
        _LOGGER.error("CASA ERROR: Could not create login '%s': %s — rolling back.", username, err)
        if auth_added:
            try:
                await provider.async_remove_auth(username)
            except Exception as rm_err:
                _LOGGER.warning("CASA: Could not remove login '%s' during rollback: %s", username, rm_err)
        try:
            await hass.auth.async_remove_user(new_user)
        except Exception as rm_err:
            _LOGGER.warning("CASA: Could not remove user '%s' during rollback: %s", name, rm_err)
        return None, f"Could not create login credentials: {err}"

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
        if not isinstance(body, dict):
            body = {}
        device_id = str(body.get("device_id", "")).strip()
        if not device_id:
            return self.json({"error": "Missing device_id"}, status_code=400)

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        if not _device_owned_by(stored_data, user.id, device_id):
            return self.json({"error": "Device not found"}, status_code=404)

        # _purge_device persists the store itself.
        result = await _purge_device(self.hass, device_id, owner_user_id=user.id)
        _remove_registry_device(self.hass, device_id)
        _LOGGER.info(
            "CASA: Device '%s' self-deprovisioned by user '%s'.",
            device_id, result.get("username") or user.name or user.id,
        )
        return self.json({"status": "success", "access_revoked": bool(result.get("access_revoked"))})


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
        # Location fields and the zone reconciler only apply to the caller's
        # own device (the heartbeat refuses to touch anyone else's record).
        device_info = None
        if result.get("owned", True):
            device_info, _uid, _uname = _find_device_record(stored_data, device_id)
        if device_info is not None and isinstance(location_state, str):
            if not _apply_location_report(self.hass, device_id, device_info,
                                          location_state, location_reason, location_config_version):
                _LOGGER.warning("CASA: Ignored invalid heartbeat location fields for device '%s'.", device_id)
        # Reconciler: device confirmed a stale config version (or reported no
        # version at all, which also counts as a mismatch) → re-enqueue.
        # Server version "" means no zones: nothing to reconcile.
        device_lz_version = location_config_version if isinstance(location_config_version, str) else ""
        if device_info is not None and server_lz_version and device_lz_version != server_lz_version:
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
            # The relay this site is registered with and its site_id there;
            # a device whose stored values differ re-registers its push token.
            "site_id": stored_data.get("site_id"),
            "relay_url": relay_base(self.hass),
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
        fields = _normalize_reported_fields(fields)
        fields = {k: v for k, v in fields.items() if k in LIVE_PROVISIONING_FIELDS}

        stored_data = self.hass.data[DOMAIN]["stored_data"]
        device_info, _uid, _username = _find_device_record(stored_data, device_id)
        if device_info is None:
            return self.json({"error": "Device not found"}, status_code=404)
        # Only the device itself may report: its pinned session token, or its
        # owning user. Others get the same 404 so device ids aren't confirmed.
        auth_header = request.headers.get("Authorization") or ""
        bearer_rtid = _get_refresh_token_id_from_jwt(auth_header[7:].strip()) if auth_header.startswith("Bearer ") else None
        pinned = bool(bearer_rtid) and bearer_rtid == device_info.get("refresh_token_id")
        if not pinned and not _device_owned_by(stored_data, user.id, device_id):
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
                    "location_state": dinfo.get("location_state"),
                    "location_reason": dinfo.get("location_reason"),
                    "location_reported_at": dinfo.get("location_reported_at"),
                    "location_config_version": dinfo.get("location_config_version"),
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
                    "push_ready": bool(dinfo.get("push_token")) and bool(stored_data.get("device_key")),
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
                        "location_state": dinfo.get("location_state"),
                        "location_reason": dinfo.get("location_reason"),
                        "location_reported_at": dinfo.get("location_reported_at"),
                        "location_config_version": dinfo.get("location_config_version"),
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
                        "push_ready": bool(dinfo.get("push_token")) and bool(stored_data.get("device_key")),
                    "reauth_pending": _reauth(dinfo),
                    })

        accounts = []
        for uid, udata in stored_data.get("users", {}).items():
            if udata.get("deleted", False):
                continue
            accounts.append({
                "user_id": uid,
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
            "relay_version": self.hass.data[DOMAIN].get("relay_version"),
            "relay_protocol": self.hass.data[DOMAIN].get("relay_protocol"),
            "location_config_version": self.hass.data.get(DOMAIN, {}).get("lz_data", {}).get("config_version", ""),
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

        # Saving an empty list after zones existed still pushes once
        # ({anchors: [], config_version: ""}) so devices tear their regions
        # down; with version "" the heartbeat reconciler never re-enqueues it.
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
    encrypted push path). Note this key is site-wide, not per-device: any
    provisioned device that holds it could encrypt a payload and forge a
    report for a different device_id. The per-device HKDF salt only prevents
    replay of a captured ciphertext blob across devices; it does not stop
    forgery by a key holder. The report schema deliberately has no location
    fields, and unknown keys are rejected so none can be smuggled in later."""

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
            _LOGGER.warning("CASA: Rejected location report for device '%s' (invalid state/reason).", device_id)
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
            await _sync_ha_device_name(self.hass, device_id)

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
            if not fields:
                return self.json({"error": "No device-live fields to push"}, status_code=400)

            # Merge: the panel sends only the fields the admin changed (older
            # panels send all of them), so the stored view stays complete.
            device_info["provisioning_fields"] = {**(device_info.get("provisioning_fields") or {}), **fields}
            device_info["provisioning_pending_push"] = True

            qu_data = self.hass.data[DOMAIN]["qu_data"]
            session = async_get_clientsession(self.hass)
            created_by = user.name or user.id
            profile_fields, wg_payload = _split_wireguard_from_profile(fields, self.hass.data[DOMAIN].get("wg_data"))
            if profile_fields:
                payload = {"profile_id": None, "name": "Custom device settings", "fields": profile_fields}
                _update_id, pushed, _skipped = await _enqueue_and_push_update(
                    self.hass,
                    stored_data, qu_data, session, device_id, device_info,
                    "profile", "update", payload, created_by, send_push=True,
                )
            if wg_payload:
                _wg_id, wg_pushed, _skipped = await _enqueue_and_push_update(
                    self.hass,
                    stored_data, qu_data, session, device_id, device_info,
                    "wireguard", "update", wg_payload, created_by, send_push=True,
                )
                pushed = pushed or wg_pushed
            qu_store = self.hass.data[DOMAIN]["qu_store"]
            qu_store.async_delay_save(lambda: qu_data, 2.0)
            # Nudge only when the encrypted push did NOT go out (see the same
            # pattern in CasaAdminQueueUpdateView — spares the relay's small
            # per-device burst budget).
            if not pushed:
                await _nudge_device_checkin(self.hass, session, stored_data, device_info)

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


def _normalize_reported_fields(fields: dict) -> dict:
    """Normalize a device's self-reported live fields so the device editor's
    reported -> form -> pushed round trip is lossless.

    - Older apps report immersive_level as the full "level,mode,color"
      triple; it is split into immersive_level / theme_color_mode /
      custom_color (explicit keys, when also reported, win).
    - Values are coerced to the LIVE_PROVISIONING_FIELDS default's type
      (bools also from "true"/"false" strings; strings from numbers), so a
      bool reported as "false" can't turn truthy in the form.
    - allowed_pages "/*" with no allow_all_pages reported means allow-all.
    """
    out = dict(fields)
    level = out.get("immersive_level")
    if isinstance(level, str) and "," in level:
        parts = [p.strip() for p in level.split(",", 2)]
        out["immersive_level"] = parts[0]
        if len(parts) > 1 and parts[1] and not out.get("theme_color_mode"):
            out["theme_color_mode"] = parts[1]
        if len(parts) > 2 and parts[2] and not out.get("custom_color"):
            out["custom_color"] = parts[2]
    for key, default in LIVE_PROVISIONING_FIELDS.items():
        if key not in out:
            continue
        val = out[key]
        if isinstance(default, bool):
            out[key] = val.strip().lower() == "true" if isinstance(val, str) else bool(val)
        elif isinstance(default, str):
            if val is None:
                out[key] = ""
            elif isinstance(val, bool):
                out[key] = "true" if val else "false"
            elif isinstance(val, float) and val.is_integer():
                out[key] = str(int(val))
            else:
                out[key] = str(val)
    if "allow_all_pages" not in out and out.get("allowed_pages") == "/*":
        out["allow_all_pages"] = True
    return out


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
    recorded_hours = device_info.get("provisioning_expiration_hours")
    if isinstance(recorded_hours, int) and not isinstance(recorded_hours, bool):
        data["expiration_hours"] = recorded_hours
    data["method"] = "qr"
    data["deauthenticate_existing"] = False
    alias = str(device_info.get("alias") or "").strip()
    if alias:
        data["device_alias"] = alias
    return data


def _split_wireguard_from_profile(fields: dict, wg_data: dict):
    """Translate WireGuard keys in a profile push into a separate update.

    The app applies WireGuard only through wireguard/update entries and
    ignores wireguard_config / wireguard_profile_id inside profile/update.
    Returns (profile_fields_without_those_keys, wireguard_payload | None);
    the payload is {config, excluded_wifi} for a linked profile (by id) or
    a pasted config. Nothing resolvable -> None (no revoke is implied)."""
    profile_fields = {k: v for k, v in fields.items() if k not in ("wireguard_config", "wireguard_profile_id")}
    config = ""
    excluded = str(fields.get("wireguard_excluded_wifi", "") or "").strip()
    wg_id = str(fields.get("wireguard_profile_id", "") or "").strip()
    if wg_id:
        wg_profile = next((p for p in (wg_data or {}).get("profiles", []) if p.get("id") == wg_id), None)
        if wg_profile:
            config = str(wg_profile.get("config", "") or "").strip()
            excluded = str(wg_profile.get("excluded_wifi", "") or "").strip()
    if not config:
        config = str(fields.get("wireguard_config", "") or "").strip()
    if not config:
        return profile_fields, None
    return profile_fields, {"config": config, "excluded_wifi": excluded}


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


async def _send_push_to_relay(hass, session, payload) -> bool:
    url = relay_url(hass, "/send")
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


async def _nudge_device_checkin(hass, session, stored_data, device_info, command="request_heartbeat"):
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
    return await _send_push_to_relay(hass, session, payload)


async def _send_encrypted_update_push(hass, stored_data, session, device_id, device_info, update_id, update_type, action, payload) -> bool:
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
    return await _send_push_to_relay(hass, session, {
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


async def _enqueue_and_push_update(hass, stored_data, qu_data, session, device_id, device_info, update_type, action, payload, created_by, send_push=True):
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
            pushed = await _send_encrypted_update_push(hass, stored_data, session, device_id, device_info, update_id, update_type, action, payload)
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
            ok = await _send_encrypted_update_push(hass, stored_data, session, device_id, device_info, update_id, update_type, action, payload)
            if ok:
                pushed += 1
            else:
                # Nudge only when the encrypted push did NOT go out — it's a
                # delivery accelerator, and the relay's per-device rate limit
                # has a small burst budget; a redundant nudge here can starve
                # the visible notify push below.
                await _nudge_device_checkin(hass, session, stored_data, device_info)
        if notify_push and title and message:
            push_token = device_info.get("push_token")
            if push_token:
                ok = await _send_push_to_relay(hass, session, {
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
        wg_payload = None
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
            # WireGuard settings travel as their own wireguard/update entry —
            # the app ignores them inside profile/update.
            fields, wg_payload = _split_wireguard_from_profile(fields, self.hass.data[DOMAIN].get("wg_data"))
            payload = {"profile_id": profile_id, "name": matched.get("name"), "fields": fields} if fields else None

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

        wg_jobs = []
        for did, dinfo in targets:
            if wg_payload is not None:
                wg_jobs.append((did, dinfo, _enqueue_update(qu_data, did, "wireguard", "update", wg_payload, created_by)))
            if payload is None:
                queued += 1
                continue
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

        if (send_update_push or notify_push) and jobs:
            self.hass.async_create_task(_deliver_updates_in_background(
                self.hass, stored_data, jobs, update_type, action, payload,
                send_update_push, notify_push, title, message, created_by,
            ))
        if send_update_push and wg_jobs:
            self.hass.async_create_task(_deliver_updates_in_background(
                self.hass, stored_data, wg_jobs, "wireguard", "update", wg_payload,
                True, False, "", "", created_by,
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

        # Serialize reauths of one device: a double-click must not let the
        # first request queue a password the second has already rotated away.
        # Only the state mutation runs under the locks; the relay push and
        # nudge happen after they are released.
        async with _lock_for(self.hass, "device", device_id):
            response, push = await self._reauth(request, user, body, device_id)
        if push is None:
            return response
        return await self._deliver(**push)

    async def _reauth(self, request, user, body, device_id):
        hass = self.hass
        stored_data = hass.data[DOMAIN]["stored_data"]

        device_info, _old_uid, _old_username = _find_device_record(stored_data, device_id)
        if not device_info:
            return self.json({"error": "Device not found"}, status_code=404), None

        password = str(body.get("password", "") or "").strip()
        send_update_push = bool(body.get("send_update_push", True))
        scramble_old = bool(body.get("scramble_old", False))
        create_user = body.get("create_user")
        created_by = user.name or user.id

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        if not provider:
            return self.json({"error": "Home Assistant core auth provider not found"}, status_code=500), None

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
                return self.json({"error": err}, status_code=400), None
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
                return self.json({"error": "Must provide user_id, username, or create_user"}, status_code=400), None
            if not target_user:
                return self.json({"error": "Target user not found"}, status_code=404), None
            if getattr(target_user, "is_admin", False):
                _LOGGER.error("CASA ERROR: Attempted to reauthenticate a device to an admin user. Blocked.")
                return self.json({"error": "Cannot reauthenticate a device to an admin user"}, status_code=400), None
            if not getattr(target_user, "is_active", True):
                return self.json({"error": "Target user is inactive"}, status_code=400), None

            login_username = None
            for cred in target_user.credentials:
                if cred.auth_provider_type == "homeassistant":
                    login_username = cred.data.get("username")
                    break
            if not login_username:
                return self.json({"error": "No local Home Assistant credentials found for this user"}, status_code=400), None

        async with _lock_for(hass, "user", target_user.id):
            if not created_user:
                if password:
                    # Provisioning semantics: a supplied password is assumed to
                    # already be the account's password and is not changed.
                    login_password = password
                else:
                    # Another device's still-queued reauth to this user holds
                    # the current password; rotating would strand that entry.
                    login_password = _queued_reauth_password(hass, login_username, exclude_device_id=device_id)
                    if not login_password:
                        login_password = await _set_account_password(hass, provider, login_username)
                    revealed_password = login_password
            return await self._queue_reauth(
                device_id, target_user, login_username, login_password, revealed_password,
                created_user, scramble_old, send_update_push, provider, created_by,
            )

    async def _queue_reauth(self, device_id, target_user, login_username, login_password, revealed_password,
                            created_user, scramble_old, send_update_push, provider, created_by):
        hass = self.hass
        data = hass.data[DOMAIN]
        stored_data = data["stored_data"]
        qu_data = data["qu_data"]

        # Re-resolve after the awaits above: the record may have moved or
        # been purged meanwhile.
        device_info, old_uid, old_username = _find_device_record(stored_data, device_id)
        if not device_info:
            return self.json({"error": "Device not found"}, status_code=404), None

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
                    await _set_account_password(hass, provider, old_login)
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

        return None, {
            "device_id": device_id, "update_id": update_id, "login_username": login_username,
            "login_password": login_password, "revealed_password": revealed_password,
            "created_user": created_user, "scrambled_old": scrambled_old,
            "send_update_push": send_update_push, "created_by": created_by,
            "old_label": old_username or old_uid,
        }

    async def _deliver_result(self, device_id, update_id, login_username, login_password, revealed_password,
                       created_user, scrambled_old, send_update_push, created_by, old_label):
        """Best-effort push/nudge for an already-queued reauth (no locks held)."""
        hass = self.hass
        stored_data = hass.data[DOMAIN]["stored_data"]
        device_info, _uid, _name = _find_device_record(stored_data, device_id)
        pushed = False
        push_skipped = False
        if send_update_push:
            if not device_info or not device_info.get("push_token") or not stored_data.get("device_key"):
                push_skipped = True
            else:
                session = async_get_clientsession(hass)
                pushed = await _send_encrypted_update_push(
                    hass,
                    stored_data, session, device_id, device_info,
                    update_id, "auth", "reauthenticate",
                    {"username": login_username, "password": login_password},
                )
                if not pushed:
                    await _nudge_device_checkin(hass, session, stored_data, device_info)

        _LOGGER.info(
            "CASA: Queued reauthentication of device '%s' from user '%s' to '%s' by %s (pushed=%s skipped=%s scrambled_old=%s).",
            device_id, old_label, login_username, created_by, pushed, push_skipped, scrambled_old,
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
        return resp

    async def _deliver(self, **push):
        return self.json(await self._deliver_result(**push))


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
            udata = (stored_data.get("users") or {}).get(owner_uid)
            if not udata or udata.get("deleted", False):
                return self.json({"error": "Re-provision is only available for Casa-managed accounts"}, status_code=400)
            owner = await hass.auth.async_get_user(owner_uid)
            if not owner or getattr(owner, "is_admin", False):
                return self.json({"error": "Cannot re-provision a device on an admin account"}, status_code=400)
            if not getattr(owner, "is_active", True):
                return self.json({"error": "Target user is inactive"}, status_code=400)

            service_data = _reprovision_service_data(hass, device_info, host_url)
            service_data["user_id"] = owner_uid
            service_data["username"] = username
            try:
                result = await self.provision_func(service_data, replaces_device_id=device_id)
            except Exception as err:
                return self.json({"error": str(err) or "Provisioning failed"}, status_code=400)
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
                token = owner.refresh_tokens.get(rtid)
                if token:
                    hass.auth.async_remove_refresh_token(token)
            _save_stored_data(hass)
        return self.json({**result, "method": "qr"})


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
        for _did, dinfo in _iter_all_devices(stored_data):  # managed and native
            await _nudge_device_checkin(self.hass, session, stored_data, dinfo)

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
    hass.data[DOMAIN]["config_entry"] = entry
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

    # Site credentials are kept per relay base (relay_sites[base]); the
    # top-level site_id/site_key mirror the active base's entry.
    migrated = _migrate_legacy_site_credentials(stored_data, RELAY_BASE_URL)
    _activate_relay_site(stored_data, relay_base(hass, entry))
    if migrated:
        await store.async_save(stored_data)

    # Register the site with the relay once, verifying stored credentials against
    # the relay so a stale site_key (relay lost the site) self-heals at startup.
    await _ensure_site_registration(hass, stored_data, store)
    hass.async_create_task(_probe_relay(hass))

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
    # Pre-26.10 stores hashed an empty anchor list to a non-empty version,
    # which kept the reconciler re-enqueueing "no zones" forever.
    if not lz_data.get("anchors") and lz_data.get("config_version"):
        lz_data["config_version"] = ""
        await lz_store.async_save(lz_data)
    hass.data[DOMAIN]["lz_store"] = lz_store
    hass.data[DOMAIN]["lz_data"] = lz_data

    # One record per device_id: collapse duplicates older versions could
    # leave under two live owners (idempotent, so safe on every startup).
    records_migrated = bool(_collapse_duplicate_device_records(stored_data, await hass.auth.async_get_users()))
    # Normalize stored self-reports from older apps (immersive triple, string
    # bools) so the device editor starts from correct values.
    for _did, dinfo in _iter_all_devices(stored_data):
        reported = dinfo.get("provisioning_fields")
        if isinstance(reported, dict) and reported:
            normalized = {k: v for k, v in _normalize_reported_fields(reported).items() if k in LIVE_PROVISIONING_FIELDS}
            if normalized != reported:
                dinfo["provisioning_fields"] = normalized
                records_migrated = True
    if records_migrated:
        await store.async_save(stored_data)

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
                        name=_ha_device_name(device_data, username),
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
                        name=_ha_device_name(device_data, username),
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

        # One owner per device_id: registering a device_id recorded under
        # another live user moves it here only with proof of possession
        # (see _claim_device_for_caller); otherwise it is refused.
        if not await _claim_device_for_caller(hass, device_id, user_id, devices, refresh_token_id):
            _LOGGER.warning(
                "CASA: Refused registration of device '%s' by user '%s' — it belongs to another user.",
                device_id, user_id,
            )
            raise HomeAssistantError("Device is registered to another user.")

        # Checked after the last await: a purge that started meanwhile wins.
        if _device_being_purged(hass, device_id):
            raise HomeAssistantError("Device is being removed.")

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

        # Fresh provision: template lineage, session length and the wizard's
        # name — applied by whichever of register/heartbeat comes first.
        await _apply_pending_provision(hass, user_id, devices[device_id], refresh_token_id)

        # QR re-provision redeemed by a different phone (wiped / replaced):
        # the new record takes over the old one's identity and the old record
        # is purged. No claim (the normal case) → no-op.
        replaced_device_id = await _apply_device_replacement(hass, device_id, devices[device_id], refresh_token_id)

        # The replacement awaited (purge + store save): re-apply the guard above.
        if _device_being_purged(hass, device_id):
            raise HomeAssistantError("Device is being removed.")

        if replaced_device_id:
            _remove_registry_device(hass, replaced_device_id)

        _save_stored_data(hass)
        
        # Register in Home Assistant Device Registry if enabled
        create_devices = entry.options.get(CONF_CREATE_DEVICES, True)
        if create_devices:
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(hass)
            dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, device_id)},
                name=_ha_device_name(devices.get(device_id), username),
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

        # One owner per device_id. A heartbeat with proof of possession (a
        # fresh provisioning claim covers a push-off app re-provisioned to a
        # new user) moves the record; anything else changes nothing.
        claimed = await _claim_device_for_caller(hass, device_id, user_id, devices, refresh_token_id)
        # Checked after the last await: a purge that started meanwhile wins,
        # rather than this heartbeat recreating a ghost record.
        if _device_being_purged(hass, device_id):
            raise HomeAssistantError("Device is being removed.")
        if not claimed:
            _LOGGER.info(
                "CASA: Heartbeat for device '%s' from a user that does not own it; asking it to re-register.",
                device_id,
            )
            other, _ouid, _oname = _find_device_record(stored_data, device_id)
            return {
                "owned": False,
                "reregister": True,
                "updates": False,
                "require_alias": bool(stored_data.get("require_device_alias", False)),
                # The device's actual alias state, so a require-alias prompt
                # isn't raised for a device that already has one.
                "has_alias": bool(((other or {}).get("alias") or "").strip()),
                "heartbeat_interval_seconds": stored_data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
                "profile_report_interval_seconds": stored_data.get("profile_report_interval_seconds", DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS),
            }

        now_iso = dt_util.now().isoformat()

        # Get or initialize existing device info
        device_info = devices.setdefault(device_id, {
            "registered_at": now_iso
        })

        # A fresh provision's first heartbeat can beat its registration;
        # apply the wizard's name now so has_alias is true from the start.
        await _apply_pending_provision(hass, user_id, device_info, refresh_token_id)
        if _device_being_purged(hass, device_id):
            raise HomeAssistantError("Device is being removed.")

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

        _save_stored_data(hass)

        # Ensure registered in Home Assistant Device Registry if enabled
        create_devices = entry.options.get(CONF_CREATE_DEVICES, True)
        if create_devices:
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(hass)
            dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, device_id)},
                name=_ha_device_name(device_info, username),
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
            "owned": True,
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

    # Register the HTTP views once per HA instance: routes can't be removed
    # and survive entry reloads, and a second registration would shadow
    # nothing (the first route wins) — so the views resolve the current
    # entry's register/heartbeat functions through hass.data at call time.
    hass.data[DOMAIN]["register_device_func"] = async_register_device
    hass.data[DOMAIN]["heartbeat_func"] = async_heartbeat
    if not hass.data.get(_VIEWS_REGISTERED_KEY):
        hass.http.register_view(CasaRegisterDeviceView(hass, _entry_func(hass, "register_device_func")))
        hass.http.register_view(CasaDeprovisionView(hass))
        hass.http.register_view(CasaHeartbeatView(hass, _entry_func(hass, "heartbeat_func")))
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
        hass.http.register_view(CasaAdminReprovisionDeviceView(hass, _entry_func(hass, "provision_func")))
        hass.http.register_view(CasaAdminRegenerateKeyView(hass))
        hass.data[_VIEWS_REGISTERED_KEY] = True

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

        def _newest_panel_mtime() -> int:
            newest = 0
            try:
                for dirpath, _dirs, filenames in os.walk(panel_dir_path):
                    for fname in filenames:
                        if fname.endswith(".js"):
                            newest = max(newest, int(os.path.getmtime(os.path.join(dirpath, fname))))
            except OSError:
                pass
            return newest

        # Disk walk off the event loop.
        panel_version = await hass.async_add_executor_job(_newest_panel_mtime)
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
    # UNIFIED SERVICE: PROVISION (QR & BLE)
    # ==========================================
    async def _provision_internal(service_data: dict, users: list = None, *, replaces_device_id: str | None = None) -> dict:
        # replaces_device_id is keyword-only and never read from service_data,
        # so the casa.provision service can't set it — only the admin
        # reprovision view passes it (see _apply_device_replacement).
        method = str(service_data.get("method", "qr")).strip().lower()
        if method not in ("qr", "ble", "deep_link", "manual"):
            return {"error": f"Invalid method: {method}"}

        # Optional QR file under www/ (validated before anything is changed).
        qr_file = None
        qr_expire_mode = "delete"
        if method == "qr" and str(service_data.get("qr_filename", "") or "").strip():
            qr_file = _safe_qr_filename(service_data.get("qr_filename"))
            if not qr_file:
                return {"error": "Invalid qr_filename: use letters, digits, '.', '_' or '-' (no paths)"}
            if not service_data.get("delete_qr_after_window", True):
                qr_expire_mode = "expire"

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
        # In-memory only; self-heals on the next provision if lost to a
        # reload.
        hass.data[DOMAIN].setdefault("pending_profile_by_user", {})[target_user.id] = {
            "profile_id": profile_key or None,
            "profile_name": matched_profile.get("name") if matched_profile else None,
            "device_alias": str(service_data.get("device_alias", "")).strip()[:DEVICE_ALIAS_MAX_LEN] or None,
            "expiration_hours": expiration_hours,
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

        # A supplied password is set on the account too (it used to be
        # embedded as-is, so a wrong one produced a link that could never log
        # in). Either way, queued reauths carrying the old password are dropped.
        async with _lock_for(hass, "user", target_user.id):
            login_password = await _set_account_password(hass, provider, login_username, target_password or None)

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
            deep_link, universal_link = build_links(final_payload, 1)
        else:
            # v2: JSON profile, hybrid encryption (AES-256-GCM body + RSA-wrapped key), base64url.
            # No size cap, '|' is no longer a delimiter, and fields are named instead of positional.
            profile = {
                "v": 2,
                "server_version": CASA_VERSION,
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
            # Optional (26.10.03): the name typed in the wizard / re-provision,
            # so the app knows it is named and never prompts.
            payload_alias = str(service_data.get("device_alias", "") or "").strip()[:DEVICE_ALIAS_MAX_LEN]
            if payload_alias:
                profile["device_alias"] = payload_alias
            if lz_anchors:
                profile["location_zones"] = {
                    "anchors": lz_anchors,
                    "config_version": lz_version,
                }
            # Optional, only for a non-default relay: site_id is only valid
            # at the relay that issued it.
            if payload_relay_url(hass):
                profile["relay_url"] = payload_relay_url(hass)
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
            deep_link, universal_link = build_links(final_payload, 2)

        # QR delivery: the image is returned inline (qr_data_uri). A file under
        # www/ — served unauthenticated at /local/ — is written only when the
        # caller names one, and it is retired when the window closes.
        successful_targets = []
        qr_data_uri = None
        if method == "qr":
            if qr_file:
                await hass.async_add_executor_job(_write_qr_file, hass, qr_file, deep_link)
                _LOGGER.info("CASA: QR Code saved as %s.", qr_file)
            qr_data_uri = await hass.async_add_executor_job(_qr_png_data_uri, deep_link)

        if esphome_targets:
            # Remembered so casa.clear_ble_beacon can default to them.
            hass.data[DOMAIN]["last_ble_targets"] = list(esphome_targets)
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

        if not ((method in ("qr", "deep_link", "manual") and timeout_mins > 0) or password_scramble):
            _LOGGER.warning("CASA: No timeout or password scramble configured. Code is permanent.")

        # Redemption listener lifetime.
        # Single use (scramble on first redemption) applies to every method but
        # BLE: a scrambled password would strand a beacon still broadcasting it.
        single_use = password_scramble and method != "ble"
        if single_use and scramble_timeout_secs > 0:
            # The listener must outlive the scramble window or a late first
            # redemption would go unscrambled until the fallback timer.
            listener_ttl = min(scramble_timeout_secs + 30, 86400)
        else:
            if password_scramble and scramble_timeout_secs > 0:
                listener_ttl = scramble_timeout_secs + 30
            elif expiration_hours > 0:
                listener_ttl = min(expiration_hours * 3600, 86400)
            else:
                listener_ttl = 300
            # E4: Hard cap listener TTL to 30 minutes (1800 seconds)
            listener_ttl = min(listener_ttl, 1800)

        # Persist the window (keyed by HA user id, not the typed username) so
        # single-use and expiry survive reloads and restarts; setup re-arms
        # it. An older window for this user was already closed (QR retired)
        # when _set_account_password rotated the password above.
        now_ts = time.time()
        provision_id = secrets.token_hex(8)
        pending_provisions = stored_data.setdefault("pending_provisions", {})
        # When this user last had a window opened: a session created at or
        # after it (and still young) may take over its device record.
        stored_data.setdefault("provision_opened", {})[target_user.id] = now_ts
        pending_provisions[target_user.id] = {
            "provision_id": provision_id,
            "login_username": login_username,
            "method": method,
            "single_use": single_use,
            "scramble_at": now_ts + scramble_timeout_secs if password_scramble else None,
            "window_ends_at": now_ts + timeout_secs if (method == "qr" and timeout_secs > 0) else None,
            "listen_until": now_ts + listener_ttl,
            "known_token_ids": sorted(target_user.refresh_tokens.keys()),
            "qr_file": qr_file,
            "qr_expire_mode": qr_expire_mode,
            "created_at": now_ts,
            "replaces_device_id": replaces_device_id or None,
        }
        _save_stored_data(hass)
        _arm_pending_provision(hass, target_user.id)

        if method == "manual":
            # Plaintext values for the iOS app's manual provisioning sheet,
            # field-for-field. The password/window expiry still applies.
            return {
                "method": "manual",
                "provision_id": provision_id,
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
                "provision_id": provision_id,
                # Only set when the caller asked for a qr_filename file.
                "filename": qr_file,
                "url_path": f"/local/{qr_file}" if qr_file else None,
                "url_path_deprecated": True,
                "qr_data_uri": qr_data_uri,
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
                "provision_id": provision_id,
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
                "provision_id": provision_id,
                "status": "success",
                "successful_targets": successful_targets,
                "expires_at": expiration_unix,
                "pin_required": bool(target_pin)
            }

    # The admin reprovision view (registered once per process) reaches the
    # current entry's provision closure through hass.data, like register/heartbeat.
    hass.data[DOMAIN]["provision_func"] = _provision_internal

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

        async with _lock_for(hass, "user", target_user.id):
            new_password = await _set_account_password(hass, provider, login_username)

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
            # Default to the beacons the most recent provision broadcast to.
            esphome_targets = list(hass.data[DOMAIN].get("last_ble_targets") or [])
        if not esphome_targets:
            return {"error": "Missing ESPHome target services (none given and no recent BLE provision)"}

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
            await _save_stored_data_now(hass)
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
                        "group_ids": [g.id for g in ha_user.groups],
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
                    url = relay_url(hass, "/send")
                    try:
                        _LOGGER.info("CASA: Posting payload to relay %s", url)
                        async with session.post(url, json=data_payload, timeout=ClientTimeout(total=10)) as response:
                            if response.status == 200:
                                _LOGGER.info("CASA: Notification successfully sent to token %s... via %s", tok[:10], url)
                                success = True
                            else:
                                text = await response.text()
                                _LOGGER.warning("CASA: Relay %s returned status %s for token %s...: %s", url, response.status, tok[:10], text)
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
        url = relay_url(hass, "/send")
        try:
            _LOGGER.info("CASA: Posting reload payload to relay %s", url)
            async with session.post(url, json=payload, timeout=ClientTimeout(total=10)) as response:
                if response.status == 200:
                    _LOGGER.info("CASA: Reload command successfully sent to token %s... via %s", push_token[:10], url)
                    success = True
                else:
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
        await _check_authorization(call)
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
        success = await _nudge_device_checkin(hass, session, stored_data, device_info, command="request_profile_report")
        if not success:
            raise HomeAssistantError("Failed to deliver profile report request to any Casa push relay.")

        return {"status": "success"}

    async def handle_request_heartbeat(call: ServiceCall):
        """Silently ask a device to heartbeat right now, instead of waiting
        for its next scheduled tick. Purely a nudge — the device's own
        sendHeartbeat already pulls any durably-queued profile_updates when
        the response says they're pending, so this is sufficient to make any
        queued admin change land immediately."""
        await _check_authorization(call)
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
        success = await _nudge_device_checkin(hass, session, stored_data, device_info, command="request_heartbeat")
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

        _save_stored_data(hass)
        _LOGGER.info(
            "CASA: Expiration override for device '%s' set to %s.",
            device_id, "permanent" if value == 0 else value
        )
        session = async_get_clientsession(hass)
        await _nudge_device_checkin(hass, session, stored_data, device_info)
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
            push_sent = await _send_push_to_relay(hass, session, payload)
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
        # encrypt_config: false is deprecated and ignored — the push always
        # carries the config end-to-end encrypted (plaintext base64 let the
        # relay read the tunnel's private key).
        if call.data.get("encrypt_config", True) is False:
            _LOGGER.warning(
                "CASA: update_wireguard encrypt_config=false is deprecated and ignored; the WireGuard push is always encrypted."
            )
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
                    "encrypted": True,
                    "wireguard_payload": wg_payload,
                    "device_key_id": _device_key_id(device_key),
                },
            }

            success = await _send_push_to_relay(hass, session, payload)

            if success:
                sent_count += 1
                _LOGGER.info("CASA: Sent wireguard %s to device '%s' (silent=%s).", action, did, silent)
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
                relay_url(hass, "/reconcile"),
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
                    relay_url(hass, "/unregister"),
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
            await _save_stored_data_now(hass)

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
                    relay_url(hass, "/remove_site"),
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
        _delete_relay_site_credentials(stored_data, relay_base(hass))
        ok = await _register_site(hass, stored_data, hass.data[DOMAIN]["store"])
        return {"success": bool(ok), "site_id": stored_data.get("site_id")}

    hass.services.async_register(
        DOMAIN, "regenerate_site", handle_regenerate_site,
        supports_response=SupportsResponse.OPTIONAL
    )

    async def _scheduled_reconcile(now):
        await _reconcile_site()
        # Daily queue hygiene too (stale/oversized queues, stranded markers).
        await _prune_stale_queued_updates(hass)

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

    # Re-arm provisioning windows persisted before a reload/restart (and
    # close the ones whose deadlines passed meanwhile).
    _rearm_pending_provisions(hass)

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
            relay_url(hass, "/unregister"),
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


def _pop_device_record(stored_data: dict, device_id: str, owner_user_id=None):
    """Locate and pop a device record from storage.

    With owner_user_id, only that user's managed devices and native devices are
    searched (so a stale duplicate under another, e.g. deleted, user is never
    popped in its place). Without it, the record _find_device_record would
    return (live managed owner, then native) is popped, and any dead copies
    under deleted owners go with it; only when no live record exists is a
    dead copy popped instead — so admin deprovision/delete can never pop a
    dead copy while the live one stays behind.
    Returns (owner_user_id | None, device_info | None, username).
    """
    users = stored_data.get("users", {})
    natives = stored_data.get("native_devices", {})
    if owner_user_id is not None:
        managed = [(owner_user_id, users[owner_user_id])] if owner_user_id in users else []
        native = [(owner_user_id, natives[owner_user_id])] if owner_user_id in natives else []
        for uid, udata in managed:
            devices = udata.get("devices", {})
            if device_id in devices:
                return uid, devices.pop(device_id), udata.get("username", uid)
        for uid, devices in native:
            if device_id in devices:
                return uid, devices.pop(device_id), uid
        return None, None, "Unknown"

    found = None
    for uid, udata in users.items():
        if not udata.get("deleted", False) and device_id in udata.get("devices", {}):
            found = (uid, udata["devices"].pop(device_id), udata.get("username", uid))
            break
    if found is None:
        for uid, devices in natives.items():
            if device_id in devices:
                found = (uid, devices.pop(device_id), uid)
                break
    for uid, udata in users.items():
        if udata.get("deleted", False) and device_id in udata.get("devices", {}):
            dead = udata["devices"].pop(device_id)
            if found is None:
                found = (uid, dead, udata.get("username", uid))
    return found if found is not None else (None, None, "Unknown")


def _move_device_record(stored_data: dict, qu_data: dict, device_id: str, from_uid: str, dest: dict) -> dict | None:
    """Move device_id's record from from_uid (managed or native) into dest,
    the new owner's devices dict. A device lives under exactly one owner, and
    its queued updates and reauth markers were addressed to the old owner
    (they may carry that owner's credentials), so both are dropped."""
    users = stored_data.get("users", {})
    natives = stored_data.get("native_devices", {})
    info = None
    if from_uid in users and device_id in users[from_uid].get("devices", {}):
        info = users[from_uid]["devices"].pop(device_id)
    elif from_uid in natives and device_id in natives[from_uid]:
        info = natives[from_uid].pop(device_id)
        if not natives[from_uid]:
            natives.pop(from_uid, None)
    if info is None:
        return None
    info.pop("reauth_pending", None)
    dropped = (qu_data.get("updates") or {}).pop(device_id, None)
    dest[device_id] = info
    _LOGGER.info(
        "CASA: Moved device '%s' to a new owner%s.",
        device_id, f"; dropped {len(dropped)} queued update(s) addressed to the old owner" if dropped else "",
    )
    return info


async def _claim_device_for_caller(hass, device_id: str, user_id: str, devices: dict, refresh_token_id=None) -> bool:
    """Enforce one owner per device_id before user_id's register/heartbeat
    touches devices (user_id's own devices dict). Returns False when the
    caller may not take the record over from its current live owner.

    Knowing a device_id is not proof of anything (non-admins can list the
    device registry). A move is allowed only when the caller:
      (a) presents the refresh token pinned on the record;
      (b) holds a fresh claim — a session that redeemed a provisioning
          window for its user, or one created at/after such a window opened
          in the last 24 h and itself under 30 min old (_has_fresh_claim);
      (c) is the target of the record's pending reauthentication.
    On a move the old owner's pinned session is revoked, and the record's
    queue and reauth markers are dropped (_move_device_record)."""
    if device_id in devices:
        return True
    data = hass.data[DOMAIN]
    stored_data = data["stored_data"]
    other, other_uid, _name = _find_device_record(stored_data, device_id)
    if other is None or other_uid == user_id:
        return True
    pinned = bool(refresh_token_id) and other.get("refresh_token_id") == refresh_token_id
    reauth_target = (other.get("reauth_pending") or {}).get("target_user_id") == user_id
    fresh = False
    if not (pinned or reauth_target):
        fresh = await _has_fresh_claim(hass, user_id, refresh_token_id)
        if not fresh:
            return False
        # Re-check after the await: the record may have moved meanwhile.
        if device_id in devices:
            return True
        other, other_uid, _name = _find_device_record(stored_data, device_id)
        if other is None or other_uid == user_id:
            return True
    old_rtid = other.get("refresh_token_id")
    qu_data = data["qu_data"]
    _move_device_record(stored_data, qu_data, device_id, other_uid, devices)
    if data.get("qu_store"):
        data["qu_store"].async_delay_save(lambda: qu_data, 2.0)
    if fresh:
        _consume_provision_claim(hass, user_id, refresh_token_id)
    _LOGGER.info(
        "CASA: Device '%s' moved to user '%s' (%s).", device_id, user_id,
        "pinned session" if pinned else "pending reauthentication" if reauth_target else "fresh provisioning claim",
    )
    # The previous owner's session for this device is no longer its own.
    if old_rtid and old_rtid != refresh_token_id:
        old_user = await hass.auth.async_get_user(other_uid)
        token = old_user.refresh_tokens.get(old_rtid) if old_user else None
        if token:
            hass.auth.async_remove_refresh_token(token)
    return True


def _collapse_duplicate_device_records(stored_data: dict, ha_users) -> int:
    """Startup migration: keep one record per device_id across live owners.

    Older versions let register/heartbeat create a second record under the
    caller while another owner still held one. The copy kept is the one
    whose refresh_token_id is a live token of its owner, then the most
    recently seen. A stranded reauth_pending marker moves onto the keeper.
    Returns the number of copies removed."""
    live_tokens = {u.id: set(u.refresh_tokens.keys()) for u in ha_users}
    copies = {}
    for uid, udata in stored_data.get("users", {}).items():
        if udata.get("deleted", False):
            continue
        for did, dinfo in (udata.get("devices", {}) or {}).items():
            copies.setdefault(did, []).append((uid, udata["devices"], dinfo))
    for uid, devices in stored_data.get("native_devices", {}).items():
        for did, dinfo in (devices or {}).items():
            copies.setdefault(did, []).append((uid, devices, dinfo))

    removed = 0
    emptied = set()
    for did, entries in copies.items():
        if len(entries) < 2:
            continue
        keeper = max(
            entries,
            key=lambda c: (
                c[2].get("refresh_token_id") in live_tokens.get(c[0], ()),
                c[2].get("last_seen_at") or "",
            ),
        )
        for uid, devices, dinfo in entries:
            if dinfo is keeper[2]:
                continue
            if dinfo.get("reauth_pending") and not keeper[2].get("reauth_pending"):
                keeper[2]["reauth_pending"] = dinfo["reauth_pending"]
            devices.pop(did, None)
            emptied.add(uid)
            removed += 1
            _LOGGER.warning(
                "CASA: Removed duplicate record of device '%s' under user '%s' (kept the one under '%s').",
                did, uid, keeper[0],
            )
    natives = stored_data.get("native_devices", {})
    for uid in [u for u, devs in natives.items() if not devs and u in emptied]:
        natives.pop(uid, None)
    return removed


async def _purge_device(hass: HomeAssistant, device_id: str, owner_user_id=None) -> dict:
    """Remove a device's server-side footprint.

    Pops the record from storage (managed and native maps), unregisters the proxy
    token from the relay, revokes the device's HA refresh token, and drops any
    queued updates. Network/auth steps are best-effort.
    owner_user_id: when given, only that user's record is purged (the device
    self-deprovision path); None purges the live record plus any dead copies
    under deleted owners (admin services, see _pop_device_record).
    Returns {"found", "username", "push_token", "access_revoked"}.
    """
    # Mark the device as being purged for the duration: a heartbeat or
    # register arriving during the relay/auth awaits below must not
    # recreate a ghost record (see _device_being_purged).
    purging = hass.data[DOMAIN].setdefault("purging", set())
    purging.add(device_id)
    try:
        return await _purge_device_inner(hass, device_id, owner_user_id)
    finally:
        purging.discard(device_id)


def _device_being_purged(hass, device_id: str) -> bool:
    return device_id in ((hass.data.get(DOMAIN) or {}).get("purging") or ())


async def _purge_device_inner(hass: HomeAssistant, device_id: str, owner_user_id=None) -> dict:
    stored_data = hass.data[DOMAIN]["stored_data"]
    store = hass.data[DOMAIN]["store"]

    access_revoked = False
    owner_user_id, device_info, username = _pop_device_record(stored_data, device_id, owner_user_id)
    refresh_token_id = (device_info or {}).get("refresh_token_id")
    proxy_token = (device_info or {}).get("push_token")

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
                    relay_url(hass, "/remove_site", entry),
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

    # Provisioning windows are persisted (stored_data["pending_provisions"])
    # and re-armed by the next setup, so cancelling their tasks here no
    # longer loses single-use/expiry.
    for task in hass.data[DOMAIN].get("timers", {}).values():
        task.cancel()
    for task in hass.data[DOMAIN].get("listeners", {}).values():
        task.cancel()

    # Flush pending delayed saves with the current data: a reload within the
    # 2 s delay would otherwise load the stale file and drop those writes.
    data = hass.data[DOMAIN]
    for store_key, data_key in (
        ("store", "stored_data"), ("qu_store", "qu_data"), ("wg_store", "wg_data"),
        ("pp_store", "pp_data"), ("lz_store", "lz_data"),
    ):
        if data.get(store_key) is not None and data.get(data_key) is not None:
            try:
                await data[store_key].async_save(data[data_key])
            except Exception as err:
                _LOGGER.warning("CASA: Could not flush %s on unload: %s", store_key, err)
    hass.data.pop(DOMAIN, None)
    return unload_ok
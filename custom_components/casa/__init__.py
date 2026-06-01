import asyncio
import logging
import os
import string
import secrets
import base64
import time
import json
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
from .const import DOMAIN, CONF_ADMIN_SYSTEM_ONLY, RELAY_URLS, RELAY_REGISTER_SITE_URL, RELAY_UNREGISTER_URL, CONF_CREATE_DEVICES
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from aiohttp import ClientTimeout

_LOGGER = logging.getLogger(__name__)

def generate_random_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

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


def _encrypt_wireguard_payload(plaintext: str, refresh_token: str) -> str:
    """End-to-end encrypt a WireGuard push payload using the device's session secret.

    The key is derived from the device's HA refresh token via HKDF-SHA256, so the
    relay (which never sees the token) cannot read or tamper with the config. The
    iOS app derives the same key from its own copy of the refresh token.
    Output is base64(nonce || ciphertext || GCM tag).
    """
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"casa-wireguard-v1",
    ).derive(refresh_token.encode("utf-8"))
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
    """Extract refresh_token_id (jti) from the bearer access token JWT payload."""
    import base64
    import json
    try:
        parts = jwt_str.split('.')
        if len(parts) == 3:
            payload_b64 = parts[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            payload = json.loads(payload_bytes.decode('utf-8'))
            return payload.get("jti")
    except Exception:
        pass
    return None


async def _register_site(hass: HomeAssistant, stored_data: dict, store) -> bool:
    """Register this HA instance's site with the relay and persist the issued site_key.

    site_id is a 124-char [A-Za-z0-9] value (secrets.token_hex(62)). The relay issues
    the site_key exactly once (HTTP 201) and never returns it again, so it must be
    persisted. A 409 means the site_id exists but we hold no key (unrecoverable lockout)
    — recovery is to register a brand-new site_id, not retry the same one.
    """
    session = async_get_clientsession(hass)

    for _attempt in range(3):
        site_id = stored_data.get("site_id")
        if not site_id or len(site_id) != 124:
            site_id = secrets.token_hex(62)  # 124 hex chars
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
                    stored_data["site_id"] = secrets.token_hex(62)
                    continue

                if resp.status == 422:
                    _LOGGER.warning("CASA: Relay rejected site_id as malformed (422); regenerating.")
                    stored_data["site_id"] = secrets.token_hex(62)
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
            return self.json({
                "registered": True,
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
            devices.pop(device_id)
            store = self.hass.data[DOMAIN]["store"]
            store.async_delay_save(lambda: stored_data, 2.0)
            
            # Remove from Home Assistant Device Registry
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(self.hass)
            device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
            if device_entry:
                dev_reg.async_remove_device(device_entry.id)
                
            _LOGGER.info("CASA: Unregistered device '%s' for user '%s'.", device_id, username)
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

        try:
            await self.heartbeat_func(
                user.id,
                device_id,
                last_12_token=last_12_token,
                refresh_token_id=refresh_token_id,
                ip_address=ip_address,
                provisioned_at=provisioned_at,
                expires_at=expires_at,
                current_url=current_url
            )
        except HomeAssistantError as err:
            return self.json({"error": str(err)}, status_code=400)
        except Exception as err:
            _LOGGER.exception("CASA: Unexpected error during heartbeat: %s", err)
            return self.json({"error": "Internal server error"}, status_code=500)

        return self.json({"status": "success"})


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

    # Register the site with the relay once. The relay issues the site_key (never
    # generated locally); we skip registration whenever we already hold a valid one.
    if not stored_data.get("site_key") or len(stored_data.get("site_id", "")) != 124:
        # Drop any legacy/invalid credentials so a fresh 124-char site_id is minted.
        if len(stored_data.get("site_id", "")) != 124:
            stored_data.pop("site_id", None)
            stored_data.pop("site_key", None)
        await _register_site(hass, stored_data, store)

    hass.data[DOMAIN]["stored_data"] = stored_data

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
        
        devices[device_id] = {
            "push_token": final_token,
            "registered_at": devices.get(device_id, {}).get("registered_at", now_iso),
            "last_seen_at": now_iso,
            "last_12_token": final_last_12,
            "refresh_token_id": final_refresh_id,
            "ip_address": final_ip
        }
        
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
        expires_at: str = None,
        current_url: str = None
    ) -> None:
        """Process heartbeat from a device."""
        if DOMAIN not in hass.data:
            raise HomeAssistantError("Casa integration is not loaded.")

        stored_data = hass.data[DOMAIN]["stored_data"]

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

        device_info["last_seen_at"] = now_iso

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

    # Register the HTTP views
    hass.http.register_view(CasaRegisterDeviceView(hass, async_register_device))
    hass.http.register_view(CasaHeartbeatView(hass, async_heartbeat))

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
        if method not in ("qr", "ble", "deep_link"):
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

        final_server_url = str(service_data.get("host_url", "")).strip()
        target_username = str(service_data.get("username", "")).strip()

        if not final_server_url or not target_username:
            _LOGGER.error("CASA ERROR: Missing mandatory host_url or username.")
            return {"error": "Missing mandatory fields"}

        if method == "ble":
            esphome_services_input = service_data.get("esphome_service", [])
            if isinstance(esphome_services_input, list):
                esphome_targets = [str(s).strip() for s in esphome_services_input if str(s).strip()]
            else:
                esphome_targets = [str(esphome_services_input).strip()] if str(esphome_services_input).strip() else []
            if not esphome_targets:
                _LOGGER.error("CASA ERROR: Missing mandatory ESPHome services for BLE method.")
                return {"error": "Missing ESPHome Services"}

        target_pin = str(service_data.get("pin", "")).strip()[:6]
        connect_wifi_ssid = str(service_data.get("connect_wifi_ssid", "")).strip()
        connect_wifi_password = str(service_data.get("connect_wifi_password", "")).strip()

        deauthenticate_existing = service_data.get("deauthenticate_existing", False)

        allow_all_pages = service_data.get("allow_all_pages", False)
        if allow_all_pages:
            allowed_paths_str = "/*"
        else:
            allowed_pages_input = service_data.get("allowed_pages", [])
            if isinstance(allowed_pages_input, list):
                clean_paths = [str(p).strip() for p in allowed_pages_input if str(p).strip()]
                allowed_paths_str = ",".join(clean_paths)
            else:
                allowed_paths_str = str(allowed_pages_input).strip()

        allowed_wifi_input = service_data.get("allowed_wifi", [])
        if isinstance(allowed_wifi_input, list):
            clean_wifi = [str(w).strip() for w in allowed_wifi_input if str(w).strip()]
            allowed_wifi = ",".join(clean_wifi)
        else:
            allowed_wifi = str(allowed_wifi_input).strip()

        default_dashboard = str(service_data.get("default_dashboard", ""))
        welcome_url = str(service_data.get("welcome_url", "")).strip()

        immersive_level = str(service_data.get("immersive_level", "1"))
        theme_color_mode = str(service_data.get("theme_color_mode", "inherit"))
        custom_color = str(service_data.get("custom_color", "#000000")).strip().replace("|", "")


        val_hours = service_data.get("expiration_hours")
        expiration_hours = int(val_hours) if val_hours is not None else 336
        if expiration_hours == 0:
            session_expiration_unix = 0
        else:
            future_dt = dt_util.now() + timedelta(hours=expiration_hours)
            session_expiration_unix = int(future_dt.timestamp())

        # Extract Time Windows
        val_timeout = service_data.get("timeout_minutes")
        if val_timeout is not None:
            timeout_mins = int(val_timeout)
        else:
            timeout_mins = 5

        password_scramble = service_data.get("password_scramble", True)
        val_scramble = service_data.get("password_scramble_in")
        password_scramble_in = int(val_scramble) if val_scramble is not None else 0

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
        val_cache_control = service_data.get("cache_control_hours")
        cache_control_hours_str = str(val_cache_control) if val_cache_control is not None else ""

        if users is None:
            users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name and u.name.casefold() == target_username.casefold()), None)
        if not target_user: 
            return {"error": "User not found"}

        if getattr(target_user, "is_admin", False):
            _LOGGER.error("CASA ERROR: Attempted to provision an admin user '%s'. Blocked.", target_username)
            return {"error": "Cannot provision an admin user"}

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

        target_password = str(service_data.get("password", "")).strip()

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
        push_val = service_data.get("push_notifications", "false")
        if push_val is True or (isinstance(push_val, str) and push_val.lower() == "true"):
            normalized_push = "true"
        elif isinstance(push_val, str) and push_val.lower() == "mandatory":
            normalized_push = "mandatory"
        else:
            normalized_push = "false"

        allow_wireguard = service_data.get("allow_wireguard", False)
        normalized_wireguard = "true" if (allow_wireguard is True or (isinstance(allow_wireguard, str) and allow_wireguard.lower() == "true")) else "false"

        wireguard_config_raw = service_data.get("wireguard_config", "")
        if wireguard_config_raw:
            wireguard_config_encoded = base64.b64encode(str(wireguard_config_raw).encode("utf-8")).decode("utf-8")
        else:
            wireguard_config_encoded = ""

        wireguard_excluded_wifi = str(service_data.get("wireguard_excluded_wifi", "")).strip().replace("|", "")

        try:
            payload_version = int(service_data.get("payload_version", 2))
        except (TypeError, ValueError):
            payload_version = 2

        payload_decrypted = service_data.get("payload_decrypted", False)

        if payload_version == 1:
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

        elif method == "ble":
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

        if (method in ("qr", "deep_link") and timeout_mins > 0) or password_scramble:
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

        if method == "qr":
            return {
                "method": "qr",
                "filename": final_filename,
                "url_path": f"/local/{final_filename}",
                "expires_at": expiration_unix,
                "deep_link": deep_link
            }
        elif method == "deep_link":
            return {
                "method": "deep_link",
                "deep_link": deep_link,
                "expires_at": expiration_unix
            }
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
            
        target_user = next((u for u in users if u.name and u.name.casefold() == target_username.casefold()), None)
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
        target_name = str(call.data.get("name", "")).strip()
        target_username = str(call.data.get("username", "")).strip().casefold()
        target_password = str(call.data.get("password", "")).strip()
        
        local_only = call.data.get("local_only", True)
        
        if not target_name or not target_username:
            return {"error": "Missing mandatory name or username"}

        if any(u.name and u.name.casefold() == target_username for u in users) or any(u.name and u.name.casefold() == target_name.casefold() for u in users):
            return {"error": "User with this name or username already exists"}

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        if not provider:
            return {"error": "Home Assistant core auth provider not found"}

        if not target_password:
            target_password = generate_random_password()

        group_ids = ["system-users"]

        new_user = await hass.auth.async_create_user(
            name=target_name, 
            group_ids=group_ids, 
            local_only=local_only
        )
        
        provider.data.add_auth(target_username, target_password)
        await provider.data.async_save()

        credentials = await provider.async_get_or_create_credentials({"username": target_username})
        await hass.auth.async_link_user(new_user, credentials)

        _LOGGER.info("CASA: New local user '%s' created (Local Only: %s).", target_username, local_only)

        # Track the creation in the store
        creator = await _get_context_creator(call.context)
        stored_data = hass.data[DOMAIN]["stored_data"]
        stored_data["users"][new_user.id] = {
            "user_id": new_user.id,
            "username": target_username,
            "name": target_name,
            "created_at": dt_util.now().isoformat(),
            "created_by": creator,
            "deleted": False,
            "deleted_at": None,
            "deleted_by": None,
        }
        await hass.data[DOMAIN]["store"].async_save(stored_data)

        return {
            "name": target_name,
            "username": target_username,
            "password": target_password,
            "user_id": new_user.id,
            "is_local_only": local_only
        }

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

        target_user = next((u for u in users if u.name and u.name.casefold() == target_username.casefold()), None)
        
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

        target_user = next((u for u in users if u.name and u.name.casefold() == target_username.casefold()), None)
        
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

        target_user = next((u for u in users if u.name and u.name.casefold() == target_username.casefold()), None)
        if not target_user:
            # Let's check credentials username too, just in case
            for u in users:
                for cred in u.credentials:
                    if cred.auth_provider_type == "homeassistant" and cred.data.get("username", "").casefold() == target_username.casefold():
                        target_user = u
                        break
                if target_user:
                    break

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

        if not username or not title or not message:
            raise HomeAssistantError("Missing username, title, or message.")

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
        stored_data = hass.data[DOMAIN]["stored_data"]

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
            "command": "clear_cache_and_reload"
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

            if encrypt_config:
                refresh_token_id = dinfo.get("refresh_token_id")
                token_value = None
                if refresh_token_id:
                    user_obj = next((u for u in users if u.id == uid), None)
                    if user_obj:
                        rt = user_obj.refresh_tokens.get(refresh_token_id)
                        if rt:
                            token_value = rt.token
                if not token_value:
                    _LOGGER.warning(
                        "CASA: No active refresh token for device '%s'; cannot encrypt wireguard payload. Skipping.",
                        did,
                    )
                    skipped_count += 1
                    continue
                try:
                    wg_payload = _encrypt_wireguard_payload(inner_str, token_value)
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
                "command": command,
                "encrypted": bool(encrypt_config),
                "wireguard_payload": wg_payload,
                "title": "" if silent else title,
                "message": "" if silent else message,
            }

            success = False
            for url in RELAY_URLS:
                try:
                    async with session.post(url, json=payload, timeout=ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            success = True
                            break
                        text = await response.text()
                        _LOGGER.warning("CASA: Relay %s returned status %s for wireguard %s on device '%s': %s", url, response.status, action, did, text)
                        if response.status < 500:
                            break
                except Exception as err:
                    _LOGGER.warning("CASA: Failed to connect to relay %s for wireguard %s on device '%s': %s", url, action, did, err)

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
        DOMAIN, "update_wireguard", handle_update_wireguard,
        supports_response=SupportsResponse.OPTIONAL
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "button"])

    return True

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

    stored_data = hass.data[DOMAIN]["stored_data"]
    store = hass.data[DOMAIN]["store"]

    owner_user_id = None
    refresh_token_id = None
    proxy_token = None
    username = "Unknown"

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

    # Unregister the proxy token from the relay (possession of the token is the auth).
    if proxy_token:
        try:
            session = async_get_clientsession(hass)
            async with session.post(
                RELAY_UNREGISTER_URL,
                json={"proxy_token": proxy_token},
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.info("CASA: Unregistered proxy token for deleted device '%s' from relay.", device_id)
                elif resp.status == 404:
                    _LOGGER.info("CASA: Relay had no registration for deleted device '%s' (already gone).", device_id)
                else:
                    text = await resp.text()
                    _LOGGER.warning("CASA: Relay /unregister returned %s for device '%s': %s", resp.status, device_id, text)
        except Exception as err:
            _LOGGER.warning("CASA: Failed to unregister proxy token for device '%s' from relay: %s", device_id, err)

    # Revoke the HA session/refresh token so the device loses access (and can't
    # silently re-register via heartbeat afterwards).
    if owner_user_id and refresh_token_id:
        user = await hass.auth.async_get_user(owner_user_id)
        if user:
            token = user.refresh_tokens.get(refresh_token_id)
            if token:
                hass.auth.async_remove_refresh_token(token)
                _LOGGER.info(
                    "CASA: Revoked refresh token for deleted device '%s' (user '%s').",
                    device_id, username,
                )

    await store.async_save(stored_data)
    _LOGGER.info("CASA: Deleted device '%s' from storage (user '%s').", device_id, username)
    return True


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
    hass.services.async_remove(DOMAIN, "update_wireguard")

    for task in hass.data[DOMAIN].get("timers", {}).values():
        task.cancel()
    for task in hass.data[DOMAIN].get("listeners", {}).values():
        task.cancel()
    hass.data.pop(DOMAIN, None)
    return unload_ok
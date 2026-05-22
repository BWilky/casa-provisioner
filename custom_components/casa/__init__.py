import asyncio
import logging
import os
import string
import secrets
import base64
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
import qrcode

from homeassistant.exceptions import HomeAssistantError
from .const import DOMAIN, CONF_ADMIN_SYSTEM_ONLY

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

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("timers", {})
    hass.data[DOMAIN].setdefault("listeners", {})

    async def _check_authorization(call: ServiceCall):
        """Check if the service call is authorized."""
        if not entry.options.get(CONF_ADMIN_SYSTEM_ONLY, True):
            return

        # System/Script contexts are allowed:
        # - call.context.parent_id is set when called from script/automation
        # - call.context.user_id is None when triggered by the system/time triggers
        if call.context.parent_id is not None or call.context.user_id is None:
            return

        # Directly called by a user. Verify that they are an admin.
        users = await hass.auth.async_get_users()
        calling_user = next((u for u in users if u.id == call.context.user_id), None)
        if not calling_user or not getattr(calling_user, "is_admin", False):
            _LOGGER.warning(
                "CASA SECURITY: Blocked unauthorized service call to '%s' by user '%s' (ID: %s).",
                call.service,
                getattr(calling_user, "name", "Unknown") if calling_user else "Unknown",
                call.context.user_id,
            )
            raise HomeAssistantError("Admin or system context is required to execute this service.")

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
    async def _provision_internal(service_data: dict) -> dict:
        method = str(service_data.get("method", "qr")).strip().lower()
        if method not in ("qr", "ble"):
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
        immersive_payload = f"{immersive_level},{theme_color_mode},{custom_color}"

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
            timeout_mins = 0 if method == "qr" else 5

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

        # Construct Raw Payload (13 Variables)
        raw_payload_array = [
            str(final_server_url), str(login_username), str(login_password), allowed_paths_str,
            allowed_wifi, default_dashboard, immersive_payload, str(session_expiration_unix), str(expiration_unix), welcome_url,
            target_pin, connect_wifi_ssid, connect_wifi_password
        ]
        payload_string = "|".join(raw_payload_array)

        # Encrypt in executor
        try:
            final_encrypted_b64 = await hass.async_add_executor_job(
                _encrypt_payload, payload_string, public_key_data
            )
        except Exception as e:
            _LOGGER.error("CASA ERROR: Failed to encrypt payload. Error: %s", str(e))
            return {"error": "Encryption failed"}

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

            await hass.async_add_executor_job(create_qr_images, final_encrypted_b64)
            _LOGGER.info("CASA: QR Code saved as %s.", final_filename)

        elif method == "ble":
            for target in esphome_targets:
                try:
                    domain, service = target.split(".")
                    await hass.services.async_call(
                        domain,
                        service,
                        {
                            "payload": final_encrypted_b64,
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

        if (method == "qr" and timeout_mins > 0) or password_scramble:
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
        await _check_authorization(call)
        return await _provision_internal(call.data)

    async def handle_generate_qr_legacy(call: ServiceCall):
        await _check_authorization(call)
        _LOGGER.warning("CASA: generate_qr service is deprecated. Please use the provision service with method='qr' instead.")
        data = dict(call.data)
        data["method"] = "qr"
        if "qr_timeout_minutes" in data:
            data["timeout_minutes"] = data.pop("qr_timeout_minutes")
        return await _provision_internal(data)

    async def handle_provision_ble_beacon_legacy(call: ServiceCall):
        await _check_authorization(call)
        _LOGGER.warning("CASA: provision_ble_beacon service is deprecated. Please use the provision service with method='ble' instead.")
        data = dict(call.data)
        data["method"] = "ble"
        if "ble_timeout_minutes" in data:
            data["timeout_minutes"] = data.pop("ble_timeout_minutes")
        return await _provision_internal(data)

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
        await _check_authorization(call)
        token_id = str(call.data.get("token_id", "")).strip()
        target_username = str(call.data.get("username", "")).strip()
        
        if not token_id or not target_username:
            return
            
        users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name and u.name.casefold() == target_username.casefold()), None)
        if not target_user:
            return
            
        if token_id == "*":
            for token in list(target_user.refresh_tokens.values()):
                hass.auth.async_remove_refresh_token(token)
            _LOGGER.info("CASA: All active sessions terminated for %s.", target_username)
        else:
            token_to_remove = target_user.refresh_tokens.get(token_id)
            if token_to_remove:
                hass.auth.async_remove_refresh_token(token_to_remove)

    hass.services.async_register(DOMAIN, "remove_token", handle_remove_token)

    # ==========================================
    # SERVICE 3: CREATE USER
    # ==========================================
    async def handle_create_user(call: ServiceCall):
        await _check_authorization(call)
        target_name = str(call.data.get("name", "")).strip()
        target_username = str(call.data.get("username", "")).strip().casefold()
        target_password = str(call.data.get("password", "")).strip()
        
        local_only = call.data.get("local_only", True)
        
        if not target_name or not target_username:
            return {"error": "Missing mandatory name or username"}

        users = await hass.auth.async_get_users()
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
        await _check_authorization(call)
        target_username = str(call.data.get("username", "")).strip()
        
        if not target_username:
            return {"error": "Missing mandatory username"}

        users = await hass.auth.async_get_users()
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
        await _check_authorization(call)
        target_username = str(call.data.get("username", "")).strip()
        deauthenticate = call.data.get("deauthenticate", True)

        if not target_username:
            return {"error": "Missing mandatory username"}

        users = await hass.auth.async_get_users()
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
            "new_password": new_password,
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

    hass.services.async_register(
        DOMAIN, "clear_ble_beacon", handle_clear_ble_beacon,
        supports_response=SupportsResponse.OPTIONAL
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "provision")
    hass.services.async_remove(DOMAIN, "generate_qr")
    hass.services.async_remove(DOMAIN, "remove_token")
    hass.services.async_remove(DOMAIN, "create_user")
    hass.services.async_remove(DOMAIN, "list_tokens")
    hass.services.async_remove(DOMAIN, "housekeeping")
    hass.services.async_remove(DOMAIN, "scramble_guest_password")
    hass.services.async_remove(DOMAIN, "provision_ble_beacon")
    hass.services.async_remove(DOMAIN, "clear_ble_beacon")
    
    for task in hass.data[DOMAIN].get("timers", {}).values():
        task.cancel()
    for task in hass.data[DOMAIN].get("listeners", {}).values():
        task.cancel()
    hass.data.pop(DOMAIN, None)
    return True
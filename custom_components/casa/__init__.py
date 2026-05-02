import asyncio
import logging
import os
import string
import random
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

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("timers", {})
    hass.data[DOMAIN].setdefault("listeners", {})

    # ==========================================
    # SHARED: LOGIN LISTENER
    # ==========================================
    async def _login_listener(username, user_id, known_tokens, ttl_seconds, method):
        """Poll for new refresh tokens and fire casa_code_redeemed when detected."""
        try:
            elapsed = 0
            poll_interval = 1
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
                            _LOGGER.warning(
                                "CASA EVENT: Code redeemed by '%s' via %s (client: %s, IP: %s).",
                                username, method, token.client_name, token.last_used_ip
                            )
                    known_tokens.update(new_tokens)
        except asyncio.CancelledError:
            pass

    # ==========================================
    # SERVICE 1: GENERATE QR
    # ==========================================
    async def handle_generate_qr(call: ServiceCall):
        service_data = call.data
        
        _LOGGER.warning("CASA DEBUG: Service triggered. Starting generation sequence.")
        
        current_dir = os.path.dirname(__file__)
        public_key_path = os.path.join(current_dir, "casa_public.pem")
        
        def read_public_key():
            with open(public_key_path, "rb") as key_file:
                return key_file.read()
                
        try:
            public_key_data = await hass.async_add_executor_job(read_public_key)
            public_key = serialization.load_pem_public_key(public_key_data)
        except Exception as e:
            _LOGGER.error("CASA CRITICAL CRASH: Failed to load public key. Error: %s", str(e))
            return {"error": "Missing Public Key"}
        
        final_server_url = str(service_data.get("host_url", "")).strip()
        target_username = str(service_data.get("username", "")).strip()
        
        if not final_server_url or not target_username:
            _LOGGER.error("CASA ERROR: Missing mandatory host_url or username.")
            return {"error": "Missing mandatory fields"}

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
        
        expiration_hours = int(service_data.get("expiration_hours", 336))
        if expiration_hours == 0:
            expiration_unix = "0"
        else:
            future_dt = dt_util.now() + timedelta(hours=expiration_hours)
            expiration_unix = str(int(future_dt.timestamp()))

        # Extract Time Windows
        qr_timeout_mins = int(service_data.get("qr_timeout_minutes", 0))
        password_scramble = service_data.get("password_scramble", True)
        password_scramble_in = int(service_data.get("password_scramble_in", 0))

        # Inheritance & Validation Logic
        if password_scramble:
            if password_scramble_in > 0:
                scramble_timeout_secs = password_scramble_in * 60
            elif qr_timeout_mins > 0:
                scramble_timeout_secs = qr_timeout_mins * 60
            else:
                scramble_timeout_secs = 120 # Fallback on 2 minutes
        else:
            scramble_timeout_secs = 0

        if qr_timeout_mins > 0:
            qr_timeout_secs = qr_timeout_mins * 60
            qr_dead_dt = dt_util.now() + timedelta(seconds=qr_timeout_secs)
            qr_expiration_unix = str(int(qr_dead_dt.timestamp()))
            delete_qr = service_data.get("delete_qr_after_window", True)
        else:
            qr_expiration_unix = "0"
            qr_timeout_secs = 0
            delete_qr = False

        users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name.casefold() == target_username.casefold()), None)
        if not target_user: 
            return {"error": "User not found"}
        
        login_username = None
        for cred in target_user.credentials:
            if cred.auth_provider_type == "homeassistant":
                login_username = cred.data.get("username")
                break
        if not login_username: 
            return {"error": "No credentials"}

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        
        def generate_random_password(length=12):
            chars = string.ascii_letters + string.digits
            return ''.join(random.choice(chars) for _ in range(length))

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
            _LOGGER.warning("CASA DEBUG: All existing sessions for '%s' terminated.", target_username)
        
        # Construct Raw Payload (13 Variables)
        raw_payload_array = [
            str(final_server_url), str(login_username), str(login_password), allowed_paths_str,
            allowed_wifi, default_dashboard, immersive_payload, expiration_unix, qr_expiration_unix, welcome_url,
            target_pin, connect_wifi_ssid, connect_wifi_password
        ]
        payload_string = "|".join(raw_payload_array)

        # Encrypt
        ciphertext = public_key.encrypt(
            payload_string.encode('utf-8'),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        final_encrypted_b64 = base64.b64encode(ciphertext).decode('utf-8')

        # Filename Logic
        qr_filename_input = str(service_data.get("qr_filename", "")).strip()
        if qr_filename_input:
            final_filename = qr_filename_input if qr_filename_input.endswith(".png") else f"{qr_filename_input}.png"
        else:
            final_filename = f"qr_{login_username}_{int(time.time())}.png"

        def create_qr_images(text):
            www_dir = hass.config.path("www")
            os.makedirs(www_dir, exist_ok=True)
            custom_path = os.path.join(www_dir, final_filename)
            dashboard_path = os.path.join(www_dir, "casa_qr.png") 
            
            img = qrcode.make(text)
            img.save(custom_path)
            img.save(dashboard_path)
            return final_filename

        await hass.async_add_executor_job(create_qr_images, final_encrypted_b64)
        _LOGGER.warning("CASA SUCCESS: QR Code saved as %s.", final_filename)

        # Detach Auto-Destruct Timer
        async def _auto_destruct_sequence(username, auth_provider, qr_time, scramble_time, do_scramble, do_delete, filename):
            try:
                current_time = 0
                events = []
                
                # Only add QR actions if a timeout exists
                if qr_time > 0:
                    events.append({"time": qr_time, "action": "qr"})
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
                            _LOGGER.warning("CASA SUCCESS: QR Code file %s physically deleted.", filename)
                        else:
                            await hass.async_add_executor_job(create_qr_images, "EXPIRED - Request a new Casa code.")
                            _LOGGER.warning("CASA SUCCESS: QR Code %s wiped from dashboard.", filename)
                            
                    elif event["action"] == "scramble":
                        scrambled_password = generate_random_password()
                        auth_provider.data.change_password(username, scrambled_password)
                        await auth_provider.data.async_save()
                        _LOGGER.warning("CASA SUCCESS: Password for %s scrambled.", username)
            except asyncio.CancelledError:
                pass

        if target_username in hass.data[DOMAIN]["timers"]:
            hass.data[DOMAIN]["timers"][target_username].cancel()
            
        if qr_timeout_mins > 0 or password_scramble:
            countdown_task = hass.async_create_task(
                _auto_destruct_sequence(login_username, provider, qr_timeout_secs, scramble_timeout_secs, password_scramble, delete_qr, final_filename)
            )
            hass.data[DOMAIN]["timers"][target_username] = countdown_task
        else:
            _LOGGER.warning("CASA DEBUG: Both QR Timeout and Password Scramble are disabled. Permanent Code.")

        # Start login listener to detect code redemption
        known_token_ids = set(target_user.refresh_tokens.keys())
        if password_scramble and scramble_timeout_secs > 0:
            listener_ttl = scramble_timeout_secs + 30
        elif expiration_hours > 0:
            listener_ttl = min(expiration_hours * 3600, 86400)
        else:
            listener_ttl = 86400

        if target_username in hass.data[DOMAIN]["listeners"]:
            hass.data[DOMAIN]["listeners"][target_username].cancel()

        listener_task = hass.async_create_task(
            _login_listener(login_username, target_user.id, known_token_ids, listener_ttl, "qr")
        )
        hass.data[DOMAIN]["listeners"][target_username] = listener_task

        return {
            "filename": final_filename,
            "url_path": f"/local/{final_filename}",
            "qr_expires_at": int(qr_expiration_unix)
        }

    hass.services.async_register(
        DOMAIN, "generate_qr", handle_generate_qr,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 2: REMOVE TOKEN
    # ==========================================
    async def handle_remove_token(call: ServiceCall):
        token_id = str(call.data.get("token_id", "")).strip()
        target_username = str(call.data.get("username", "")).strip()
        
        if not token_id or not target_username:
            return
            
        users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name.casefold() == target_username.casefold()), None)
        if not target_user:
            return
            
        if token_id == "*":
            for token in list(target_user.refresh_tokens.values()):
                hass.auth.async_remove_refresh_token(token)
            _LOGGER.warning("CASA: All active sessions terminated for %s.", target_username)
        else:
            token_to_remove = target_user.refresh_tokens.get(token_id)
            if token_to_remove:
                hass.auth.async_remove_refresh_token(token_to_remove)

    hass.services.async_register(DOMAIN, "remove_token", handle_remove_token)

    # ==========================================
    # SERVICE 3: CREATE USER
    # ==========================================
    async def handle_create_user(call: ServiceCall):
        target_name = str(call.data.get("name", "")).strip()
        target_username = str(call.data.get("username", "")).strip().casefold()
        target_password = str(call.data.get("password", "")).strip()
        
        local_only = call.data.get("local_only", True)
        
        if not target_name or not target_username:
            return {"error": "Missing mandatory name or username"}

        users = await hass.auth.async_get_users()
        if any(u.name.casefold() == target_username for u in users) or any(u.name.casefold() == target_name.casefold() for u in users):
            return {"error": "User with this name or username already exists"}

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        if not provider:
            return {"error": "Home Assistant core auth provider not found"}

        if not target_password:
            chars = string.ascii_letters + string.digits
            target_password = ''.join(random.choice(chars) for _ in range(12))

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

        _LOGGER.warning("CASA SUCCESS: New local user '%s' created (Local Only: %s).", target_username, local_only)

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
        target_username = str(call.data.get("username", "")).strip()
        
        if not target_username:
            return {"error": "Missing mandatory username"}

        users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name.casefold() == target_username.casefold()), None)
        
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
        hours_old = float(call.data.get("hours_old", 24))
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
        _LOGGER.warning("CASA SUCCESS: Housekeeping deleted %s old files matching prefix '%s'.", deleted_count, prefix)

        return {"deleted_count": deleted_count}

    hass.services.async_register(
        DOMAIN, "housekeeping", handle_housekeeping,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 6: SCRAMBLE USER PASSWORD
    # ==========================================
    async def handle_scramble_guest_password(call: ServiceCall):
        target_username = str(call.data.get("username", "")).strip()
        deauthenticate = call.data.get("deauthenticate", True)

        if not target_username:
            return {"error": "Missing mandatory username"}

        users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name.casefold() == target_username.casefold()), None)
        
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

        chars = string.ascii_letters + string.digits
        new_password = ''.join(random.choice(chars) for _ in range(12))

        provider.data.change_password(login_username, new_password)
        await provider.data.async_save()
        
        _LOGGER.warning("CASA SUCCESS: Password for user '%s' manually scrambled.", target_username)

        if deauthenticate:
            for token in list(target_user.refresh_tokens.values()):
                hass.auth.async_remove_refresh_token(token)
            _LOGGER.warning("CASA SUCCESS: All active sessions for '%s' terminated.", target_username)

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
    # SERVICE 7: PROVISION BLE BEACON
    # ==========================================
    async def handle_provision_ble_beacon(call: ServiceCall):
        service_data = call.data
        
        _LOGGER.warning("CASA DEBUG: BLE Provisioning triggered.")
        
        current_dir = os.path.dirname(__file__)
        public_key_path = os.path.join(current_dir, "casa_public.pem")
        
        def read_public_key():
            with open(public_key_path, "rb") as key_file:
                return key_file.read()
                
        try:
            public_key_data = await hass.async_add_executor_job(read_public_key)
            public_key = serialization.load_pem_public_key(public_key_data)
        except Exception as e:
            return {"error": "Missing Public Key"}
        
        esphome_services_input = service_data.get("esphome_service", [])
        if isinstance(esphome_services_input, list):
            esphome_targets = [str(s).strip() for s in esphome_services_input if str(s).strip()]
        else:
            esphome_targets = [str(esphome_services_input).strip()] if str(esphome_services_input).strip() else []

        final_server_url = str(service_data.get("host_url", "")).strip()
        target_username = str(service_data.get("username", "")).strip()
        
        if not final_server_url or not target_username or not esphome_targets:
            return {"error": "Missing mandatory fields (URL, Username, or ESPHome Services)"}

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
        
        expiration_hours = int(service_data.get("expiration_hours", 336))
        if expiration_hours == 0:
            expiration_unix = "0"
        else:
            future_dt = dt_util.now() + timedelta(hours=expiration_hours)
            expiration_unix = str(int(future_dt.timestamp()))

        # Extract Time Windows
        ble_timeout_mins = int(service_data.get("ble_timeout_minutes", 0))
        password_scramble = service_data.get("password_scramble", True)
        password_scramble_in = int(service_data.get("password_scramble_in", 0))

        # Inheritance & Validation Logic
        if password_scramble:
            if password_scramble_in > 0:
                scramble_timeout_secs = password_scramble_in * 60
            elif ble_timeout_mins > 0:
                scramble_timeout_secs = ble_timeout_mins * 60
            else:
                scramble_timeout_secs = 120 # Fallback on 2 minutes
        else:
            scramble_timeout_secs = 0

        if ble_timeout_mins > 0:
            ble_timeout_secs = ble_timeout_mins * 60
            ble_dead_dt = dt_util.now() + timedelta(seconds=ble_timeout_secs)
            ble_expiration_unix = str(int(ble_dead_dt.timestamp()))
        else:
            ble_expiration_unix = "0"
            ble_timeout_secs = 0

        users = await hass.auth.async_get_users()
        target_user = next((u for u in users if u.name.casefold() == target_username.casefold()), None)
        if not target_user: 
            return {"error": "User not found"}
        
        login_username = None
        for cred in target_user.credentials:
            if cred.auth_provider_type == "homeassistant":
                login_username = cred.data.get("username")
                break
        if not login_username: 
            return {"error": "No credentials"}

        provider = next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None)
        
        def generate_random_password(length=12):
            chars = string.ascii_letters + string.digits
            return ''.join(random.choice(chars) for _ in range(length))

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

        # Construct Raw Payload (13 Variables)
        raw_payload_array = [
            str(final_server_url), str(login_username), str(login_password), allowed_paths_str,
            allowed_wifi, default_dashboard, immersive_payload, expiration_unix, ble_expiration_unix, welcome_url,
            target_pin, connect_wifi_ssid, connect_wifi_password
        ]
        payload_string = "|".join(raw_payload_array)

        ciphertext = public_key.encrypt(
            payload_string.encode('utf-8'),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        final_encrypted_b64 = base64.b64encode(ciphertext).decode('utf-8')

        successful_targets = []
        for target in esphome_targets:
            try:
                domain, service = target.split(".")
                await hass.services.async_call(
                    domain, 
                    service, 
                    {
                        "payload": final_encrypted_b64,
                        "expires_at": int(ble_expiration_unix),
                        "pin": target_pin
                    }, 
                    blocking=False
                )
                successful_targets.append(target)
                _LOGGER.warning("CASA SUCCESS: Pushed payload and PIN to %s.", target)
            except Exception as e:
                _LOGGER.error("CASA ERROR: Failed to call ESPHome service %s. Error: %s", target, str(e))

        async def _scramble_sequence(username, auth_provider, scramble_time):
            try:
                await asyncio.sleep(scramble_time)
                scrambled_password = generate_random_password()
                auth_provider.data.change_password(username, scrambled_password)
                await auth_provider.data.async_save()
                _LOGGER.warning("CASA SUCCESS: Password for %s scrambled.", username)
            except asyncio.CancelledError:
                pass

        if target_username in hass.data[DOMAIN]["timers"]:
            hass.data[DOMAIN]["timers"][target_username].cancel()
            
        if password_scramble:
            countdown_task = hass.async_create_task(
                _scramble_sequence(login_username, provider, scramble_timeout_secs)
            )
            hass.data[DOMAIN]["timers"][target_username] = countdown_task

        # Start login listener to detect code redemption
        known_token_ids = set(target_user.refresh_tokens.keys())
        if password_scramble and scramble_timeout_secs > 0:
            listener_ttl = scramble_timeout_secs + 30
        elif expiration_hours > 0:
            listener_ttl = min(expiration_hours * 3600, 86400)
        else:
            listener_ttl = 86400

        if target_username in hass.data[DOMAIN]["listeners"]:
            hass.data[DOMAIN]["listeners"][target_username].cancel()

        listener_task = hass.async_create_task(
            _login_listener(login_username, target_user.id, known_token_ids, listener_ttl, "ble")
        )
        hass.data[DOMAIN]["listeners"][target_username] = listener_task

        return {
            "status": "success",
            "successful_targets": successful_targets,
            "ble_expires_at": int(ble_expiration_unix),
            "pin_required": bool(target_pin)
        }

    hass.services.async_register(
        DOMAIN, "provision_ble_beacon", handle_provision_ble_beacon,
        supports_response=SupportsResponse.OPTIONAL
    )

    # ==========================================
    # SERVICE 8: CLEAR BLE BEACON
    # ==========================================
    async def handle_clear_ble_beacon(call: ServiceCall):
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
                _LOGGER.warning("CASA SUCCESS: Manually cleared BLE beacon at %s.", target)
            except Exception as e:
                _LOGGER.error("CASA ERROR: Failed to clear %s: %s", target, str(e))
                
        return {"status": "cleared", "successful_targets": successful_targets}

    hass.services.async_register(
        DOMAIN, "clear_ble_beacon", handle_clear_ble_beacon,
        supports_response=SupportsResponse.OPTIONAL
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
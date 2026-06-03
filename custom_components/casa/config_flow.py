import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CONF_ADMIN_SYSTEM_ONLY, CONF_CREATE_DEVICES, CONF_SHOW_PANEL

class CasaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Casa."""

    VERSION = 1.1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Prevent the user from installing the integration more than once
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # Create the config entry with the options saved
            return self.async_create_entry(title="Casa", data={}, options=user_input)

        # Show confirmation form with Admin / System Only option
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ADMIN_SYSTEM_ONLY, default=True): bool,
                vol.Required(CONF_CREATE_DEVICES, default=True): bool,
                vol.Required(CONF_SHOW_PANEL, default=False): bool,
            })
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return CasaOptionsFlowHandler()


class CasaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Casa."""

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            if user_input.pop("regenerate_site_id", False):
                import logging
                from aiohttp import ClientTimeout
                from homeassistant.helpers.aiohttp_client import async_get_clientsession
                from . import _register_site
                from .const import RELAY_REMOVE_SITE_URL

                _LOGGER = logging.getLogger(__name__)
                stored_data = self.hass.data.get(DOMAIN, {}).get("stored_data")
                store = self.hass.data.get(DOMAIN, {}).get("store")

                if stored_data is not None and store is not None:
                    # Tear down the existing site on the relay so the old site_id is freed.
                    old_site_id = stored_data.get("site_id")
                    old_site_key = stored_data.get("site_key")
                    if old_site_id and old_site_key:
                        try:
                            session = async_get_clientsession(self.hass)
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

                    # Drop local creds and mint a fresh relay-issued site.
                    stored_data.pop("site_id", None)
                    stored_data.pop("site_key", None)
                    await _register_site(self.hass, stored_data, store)

            return self.async_create_entry(title="", data=user_input)

        stored_data = self.hass.data.get(DOMAIN, {}).get("stored_data", {})
        site_id = stored_data.get("site_id", "Not Generated")

        # Format registered devices list
        devices_list = []
        for user_id, user_data in stored_data.get("users", {}).items():
            if user_data.get("deleted", False):
                continue
            username = user_data.get("username", "Unknown User")
            for device_id, device_info in user_data.get("devices", {}).items():
                try:
                    last_seen = device_info.get("last_seen_at", "Never")
                    if last_seen != "Never":
                        try:
                            last_seen = str(last_seen).split(".")[0].replace("T", " ")
                        except Exception:
                            pass
                    ip = device_info.get("ip_address", "unknown IP")
                    token_suffix = device_info.get("last_12_token", "no token")
                    push_token = device_info.get("push_token")
                    if isinstance(push_token, str) and push_token:
                        push_info = f"push: ...{push_token[-12:]}"
                    else:
                        push_info = "push: Not Registered"
                    devices_list.append(f"- {username}: {device_id} (IP: {ip}, token: ...{token_suffix}, {push_info}, seen {last_seen})")
                except Exception as err:
                    devices_list.append(f"- {username}: {device_id} (Error loading: {err})")

        native_devices = stored_data.get("native_devices", {})
        if native_devices:
            users = await self.hass.auth.async_get_users()
            user_map = {u.id: (u.name or u.id) for u in users}
            for user_id, devices in native_devices.items():
                username = user_map.get(user_id) or f"Native User {user_id[:6]}"
                for device_id, device_info in devices.items():
                    try:
                        last_seen = device_info.get("last_seen_at", "Never")
                        if last_seen != "Never":
                            try:
                                last_seen = str(last_seen).split(".")[0].replace("T", " ")
                            except Exception:
                                pass
                        ip = device_info.get("ip_address", "unknown IP")
                        token_suffix = device_info.get("last_12_token", "no token")
                        push_token = device_info.get("push_token")
                        if isinstance(push_token, str) and push_token:
                            push_info = f"push: ...{push_token[-12:]}"
                        else:
                            push_info = "push: Not Registered"
                        devices_list.append(f"- {username}: {device_id} (IP: {ip}, token: ...{token_suffix}, {push_info}, seen {last_seen})")
                    except Exception as err:
                        devices_list.append(f"- {username}: {device_id} (Error loading: {err})")

        devices_str = "\n".join(devices_list) if devices_list else "No devices registered."

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ADMIN_SYSTEM_ONLY,
                    default=self.config_entry.options.get(CONF_ADMIN_SYSTEM_ONLY, True)
                ): bool,
                vol.Required(
                    CONF_CREATE_DEVICES,
                    default=self.config_entry.options.get(CONF_CREATE_DEVICES, True)
                ): bool,
                vol.Required(
                    CONF_SHOW_PANEL,
                    default=self.config_entry.options.get(CONF_SHOW_PANEL, False)
                ): bool,
                vol.Optional("regenerate_site_id", default=False): bool,
            }),
            description_placeholders={
                "site_id": site_id,
                "devices": devices_str
            }
        )
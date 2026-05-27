import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CONF_ADMIN_SYSTEM_ONLY, CONF_CREATE_DEVICES

class CasaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Casa."""

    VERSION = 1

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
            })
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return CasaOptionsFlowHandler(config_entry)


class CasaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Casa."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            if user_input.pop("regenerate_site_id", False):
                import string
                import secrets
                
                stored_data = self.hass.data.get(DOMAIN, {}).get("stored_data", {})
                chars = string.ascii_letters + string.digits
                stored_data["site_id"] = "".join(secrets.choice(chars) for _ in range(32))
                stored_data["site_key"] = "".join(secrets.choice(chars) for _ in range(32))
                await self.hass.data[DOMAIN]["store"].async_save(stored_data)

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
                last_seen = device_info.get("last_seen_at", "Never")
                if last_seen != "Never":
                    try:
                        last_seen = last_seen.split(".")[0].replace("T", " ")
                    except Exception:
                        pass
                ip = device_info.get("ip_address", "unknown IP")
                token_suffix = device_info.get("last_12_token", "no token")
                devices_list.append(f"- {username}: {device_id} (IP: {ip}, token: ...{token_suffix}, seen {last_seen})")

        native_devices = stored_data.get("native_devices", {})
        if native_devices:
            users = await self.hass.auth.async_get_users()
            user_map = {u.id: (u.name or u.id) for u in users}
            for user_id, devices in native_devices.items():
                username = user_map.get(user_id) or f"Native User {user_id[:6]}"
                for device_id, device_info in devices.items():
                    last_seen = device_info.get("last_seen_at", "Never")
                    if last_seen != "Never":
                        try:
                            last_seen = last_seen.split(".")[0].replace("T", " ")
                        except Exception:
                            pass
                    ip = device_info.get("ip_address", "unknown IP")
                    token_suffix = device_info.get("last_12_token", "no token")
                    devices_list.append(f"- {username}: {device_id} (IP: {ip}, token: ...{token_suffix}, seen {last_seen})")

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
                vol.Optional("regenerate_site_id", default=False): bool,
            }),
            description_placeholders={
                "site_id": site_id,
                "devices": devices_str
            }
        )
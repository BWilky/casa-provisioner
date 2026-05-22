import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CONF_ADMIN_SYSTEM_ONLY

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
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ADMIN_SYSTEM_ONLY,
                    default=self.config_entry.options.get(CONF_ADMIN_SYSTEM_ONLY, True)
                ): bool,
            })
        )
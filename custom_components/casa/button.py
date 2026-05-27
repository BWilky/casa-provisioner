import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, CONF_CREATE_DEVICES

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Casa buttons from a config entry."""
    if not entry.options.get(CONF_CREATE_DEVICES, True):
        _LOGGER.debug("CASA: Device entries disabled, skipping button entities setup.")
        return

    stored_data = hass.data[DOMAIN]["stored_data"]
    added_devices = set()

    def create_buttons_for_device(device_id, username, is_native=False):
        if device_id in added_devices:
            return []
        added_devices.add(device_id)
        
        return [
            CasaDeviceReloadButton(hass, device_id, username, is_native),
        ]

    existing_entities = []

    # 1. Register existing integration-managed devices
    for user_id, user_entry in stored_data.get("users", {}).items():
        if not user_entry.get("deleted", False):
            username = user_entry.get("username", "Unknown")
            for device_id in user_entry.get("devices", {}).keys():
                existing_entities.extend(create_buttons_for_device(device_id, username, is_native=False))

    # 2. Register existing native devices
    native_devices = stored_data.get("native_devices", {})
    if native_devices:
        users = await hass.auth.async_get_users()
        user_map = {u.id: (u.name or u.id) for u in users}
        for user_id, devices in native_devices.items():
            username = user_map.get(user_id) or f"Native User {user_id[:6]}"
            for device_id in devices.keys():
                existing_entities.extend(create_buttons_for_device(device_id, username, is_native=True))

    if existing_entities:
        async_add_entities(existing_entities)

    # 3. Setup dispatcher listener for dynamically added devices
    async def async_device_added_listener(device_id, username, is_native):
        _LOGGER.debug("CASA: Dynamic device reload button added for device %s", device_id)
        entities = create_buttons_for_device(device_id, username, is_native)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            "casa_device_added",
            async_device_added_listener
        )
    )


class CasaDeviceReloadButton(ButtonEntity):
    """Button to reload URL and clear cache of a Casa device."""

    def __init__(self, hass, device_id, username, is_native):
        self.hass = hass
        self.device_id = device_id
        self.username = username
        self.is_native = is_native
        self._attr_has_entity_name = True
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_unique_id = f"casa_{device_id}_reload"
        self._attr_icon = "mdi:cached"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": f"Casa Device ({username})",
            "model": "Casa Push Client",
            "manufacturer": "Casa Integration",
            "sw_version": "1.0",
        }

    @property
    def name(self):
        return "Reload & Clear Cache"

    async def async_press(self) -> None:
        """Handle button press."""
        # Find the device's push token
        stored_data = self.hass.data[DOMAIN]["stored_data"]
        device_info = {}
        if not self.is_native:
            for u in stored_data.get("users", {}).values():
                if self.device_id in u.get("devices", {}):
                    device_info = u["devices"][self.device_id]
                    break
        else:
            for devs in stored_data.get("native_devices", {}).values():
                if self.device_id in devs:
                    device_info = devs[self.device_id]
                    break

        push_token = device_info.get("push_token")
        if not push_token:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(f"Cannot reload: No push notification token registered for this device.")

        # Trigger reload via push relay
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from aiohttp import ClientTimeout
        from .const import RELAY_URLS

        session = async_get_clientsession(self.hass)
        
        payload = {
            "title": "",
            "message": "",
            "target": push_token,
            "site_id": stored_data.get("site_id"),
            "site_key": stored_data.get("site_key"),
            "command": "clear_cache_and_reload"
        }

        _LOGGER.info(
            "CASA: Sending silent reload push to device '%s' of user '%s'. Target (obfuscated): %s",
            self.device_id, self.username, push_token[:10] + "..."
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
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError("Failed to deliver reload command to any Casa push relay.")

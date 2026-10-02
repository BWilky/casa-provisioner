import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, CONF_CREATE_DEVICES
from . import _find_device_record, _ha_device_name

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
            "name": _ha_device_name(_find_device_record(hass.data[DOMAIN]["stored_data"], device_id)[0], username),
            "model": "Casa Push Client",
            "manufacturer": "Casa Integration",
            "sw_version": "1.0",
        }

    @property
    def name(self):
        return "Reload & Clear Cache"

    async def async_press(self) -> None:
        """Handle button press."""
        from . import _queue_app_reload

        await _queue_app_reload(self.hass, self.device_id, created_by="HA button")

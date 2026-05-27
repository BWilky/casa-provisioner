import logging
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from .const import DOMAIN, CONF_CREATE_DEVICES

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Casa sensors from a config entry."""
    if not entry.options.get(CONF_CREATE_DEVICES, True):
        _LOGGER.debug("CASA: Device entries disabled, skipping sensor entities setup.")
        return

    stored_data = hass.data[DOMAIN]["stored_data"]
    added_devices = set()

    def create_sensors_for_device(device_id, username, is_native=False):
        if device_id in added_devices:
            return []
        added_devices.add(device_id)
        
        return [
            CasaDeviceIPSensor(hass, device_id, username, is_native),
            CasaDeviceTokenSensor(hass, device_id, username, is_native),
            CasaDeviceLastSeenSensor(hass, device_id, username, is_native),
            CasaDeviceExpiresSensor(hass, device_id, username, is_native),
        ]

    existing_entities = []

    # 1. Register existing integration-managed devices
    for user_id, user_entry in stored_data.get("users", {}).items():
        if not user_entry.get("deleted", False):
            username = user_entry.get("username", "Unknown")
            for device_id in user_entry.get("devices", {}).keys():
                existing_entities.extend(create_sensors_for_device(device_id, username, is_native=False))

    # 2. Register existing native devices
    native_devices = stored_data.get("native_devices", {})
    if native_devices:
        users = await hass.auth.async_get_users()
        user_map = {u.id: (u.name or u.id) for u in users}
        for user_id, devices in native_devices.items():
            username = user_map.get(user_id) or f"Native User {user_id[:6]}"
            for device_id in devices.keys():
                existing_entities.extend(create_sensors_for_device(device_id, username, is_native=True))

    if existing_entities:
        async_add_entities(existing_entities)

    # 3. Setup dispatcher listener for dynamically added devices
    async def async_device_added_listener(device_id, username, is_native):
        _LOGGER.debug("CASA: Dynamic device added signal received for device %s", device_id)
        entities = create_sensors_for_device(device_id, username, is_native)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            "casa_device_added",
            async_device_added_listener
        )
    )


class CasaDeviceSensorBase(SensorEntity):
    """Base class for Casa device sensors."""

    def __init__(self, hass, device_id, username, is_native):
        self.hass = hass
        self.device_id = device_id
        self.username = username
        self.is_native = is_native
        self._attr_has_entity_name = True
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = f"casa_{device_id}_{self.sensor_type}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": f"Casa Device ({username})",
            "model": "Casa Push Client",
            "manufacturer": "Casa Integration",
            "sw_version": "1.0",
        }

    async def async_added_to_hass(self):
        """Register update listener."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"casa_device_updated_{self.device_id}",
                self.async_write_ha_state
            )
        )


class CasaDeviceIPSensor(CasaDeviceSensorBase):
    """Sensor for client IP address."""
    sensor_type = "ip"
    _attr_icon = "mdi:ip-network"

    @property
    def name(self):
        return "IP Address"

    @property
    def native_value(self):
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
        return device_info.get("ip_address", "unknown")


class CasaDeviceTokenSensor(CasaDeviceSensorBase):
    """Sensor for token last 12 suffix."""
    sensor_type = "token"
    _attr_icon = "mdi:key-variant"

    @property
    def name(self):
        return "Token Suffix"

    @property
    def native_value(self):
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
        token = device_info.get("last_12_token")
        return f"...{token}" if token else "no token"


class CasaDeviceLastSeenSensor(CasaDeviceSensorBase):
    """Sensor for last seen timestamp."""
    sensor_type = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def name(self):
        return "Last Seen"

    @property
    def native_value(self):
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
        last_seen = device_info.get("last_seen_at")
        if last_seen:
            return dt_util.parse_datetime(last_seen)
        return None


class CasaDeviceExpiresSensor(CasaDeviceSensorBase):
    """Sensor for profile expiration timestamp."""
    sensor_type = "expires"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def name(self):
        return "Profile Expires"

    @property
    def native_value(self):
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
        expires = device_info.get("expires_at")
        if expires:
            try:
                if isinstance(expires, (int, float)):
                    return dt_util.utc_from_timestamp(expires)
                elif str(expires).isdigit():
                    return dt_util.utc_from_timestamp(int(expires))
                else:
                    return dt_util.parse_datetime(str(expires))
            except Exception:
                pass
        return None

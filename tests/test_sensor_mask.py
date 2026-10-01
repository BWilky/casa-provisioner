import sys
import types


def _stub_sensor_platform():
    if "homeassistant.components.sensor" in sys.modules:
        return
    mod = types.ModuleType("homeassistant.components.sensor")

    class SensorEntity:
        def async_on_remove(self, *a):
            pass

    mod.SensorEntity = SensorEntity
    mod.SensorDeviceClass = types.SimpleNamespace(TIMESTAMP="timestamp")
    sys.modules["homeassistant.components.sensor"] = mod


def test_push_token_sensor_masks_the_proxy_token():
    _stub_sensor_platform()
    from custom_components.casa.sensor import CasaDevicePushTokenSensor
    from types import SimpleNamespace

    token = "a" * 58 + "123456"
    hass = SimpleNamespace(data={"casa": {"stored_data": {"users": {
        "u1": {"devices": {"D1": {"push_token": token}}},
    }}}})
    sensor = CasaDevicePushTokenSensor(hass, "D1", "alice", False)
    assert sensor.native_value == "…123456"
    assert token not in str(sensor.native_value)
    hass.data["casa"]["stored_data"]["users"]["u1"]["devices"]["D1"].pop("push_token")
    assert sensor.native_value == "Not Registered"

import asyncio
import sys
import types
from types import SimpleNamespace

from custom_components.casa import CasaAdminDeviceView, _ha_device_name, _sync_ha_device_name
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user


class FakeDeviceRegistry:
    def __init__(self):
        self.devices = {}  # identifier tuple -> SimpleNamespace(id, name)
        self.updates = []

    def add(self, device_id, name):
        self.devices[("casa", device_id)] = SimpleNamespace(id="reg-" + device_id, name=name)

    def async_get_device(self, identifiers):
        (ident,) = tuple(identifiers)
        return self.devices.get(ident)

    def async_update_device(self, reg_id, name):
        self.updates.append((reg_id, name))
        for dev in self.devices.values():
            if dev.id == reg_id:
                dev.name = name


def _install_registry(monkeypatch, registry):
    mod = types.ModuleType("homeassistant.helpers.device_registry")
    mod.async_get = lambda hass: registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", mod)
    import homeassistant.helpers as helpers
    monkeypatch.setattr(helpers, "device_registry", mod, raising=False)


def test_name_prefers_alias():
    assert _ha_device_name({"alias": " Kitchen iPad "}, "kiosk") == "Kitchen iPad"


def test_name_falls_back_to_account():
    assert _ha_device_name({"alias": ""}, "kiosk") == "Casa Device (kiosk)"
    assert _ha_device_name(None, "kiosk") == "Casa Device (kiosk)"


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kiosk", "devices": {"D1": {"alias": "Kitchen iPad"}}}
    return hass


def test_sync_renames_registry_entry(monkeypatch):
    hass, reg = _hass(), FakeDeviceRegistry()
    reg.add("D1", "Casa Device (kiosk)")
    _install_registry(monkeypatch, reg)
    asyncio.run(_sync_ha_device_name(hass, "D1"))
    assert reg.updates == [("reg-D1", "Kitchen iPad")]


def test_sync_is_noop_when_name_matches_or_device_missing(monkeypatch):
    hass, reg = _hass(), FakeDeviceRegistry()
    reg.add("D1", "Kitchen iPad")
    _install_registry(monkeypatch, reg)
    asyncio.run(_sync_ha_device_name(hass, "D1"))
    asyncio.run(_sync_ha_device_name(hass, "NOPE"))
    assert reg.updates == []


def test_sync_native_device_uses_ha_user_name(monkeypatch):
    hass, reg = FakeHass(), FakeDeviceRegistry()
    hass.auth.add_user(make_user("n1", login="den", name="Den Tablet User"))
    hass.casa["stored_data"]["native_devices"] = {"n1": {"N1": {"alias": ""}}}
    reg.add("N1", "Old")
    _install_registry(monkeypatch, reg)
    asyncio.run(_sync_ha_device_name(hass, "N1"))
    assert reg.updates == [("reg-N1", "Casa Device (Den Tablet User)")]


def test_admin_alias_change_renames_registry_entry(monkeypatch):
    hass, reg = _hass(), FakeDeviceRegistry()
    reg.add("D1", "Kitchen iPad")
    _install_registry(monkeypatch, reg)
    view = bind_view(CasaAdminDeviceView(hass))
    status, _ = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), {"device_id": "D1", "alias": "Hall iPad"})))
    assert status == 200
    assert reg.updates == [("reg-D1", "Hall iPad")]

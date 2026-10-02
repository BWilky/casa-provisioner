import sys
import time
import types
from types import SimpleNamespace

from custom_components.casa import _rename_casa_entity, _rename_device_entities
from tests.fakes import FakeHass


class FakeEntityRegistry:
    def __init__(self, entries):
        self.entries = {e.entity_id: e for e in entries}
        self.updates = []

    def async_generate_entity_id(self, domain, suggested_object_id, known_object_ids=None):
        base = suggested_object_id.lower().replace(" ", "_").replace("&", "").replace("__", "_").strip("_")
        cand, n = f"{domain}.{base}", 1
        while cand in self.entries:
            n += 1
            cand = f"{domain}.{base}_{n}"
        return cand

    def async_update_entity(self, entity_id, new_entity_id):
        e = self.entries.pop(entity_id)
        e.entity_id = new_entity_id
        self.entries[new_entity_id] = e
        self.updates.append((entity_id, new_entity_id))
        return e


def _entry(entity_id, name, device_reg_id="reg-D1", unique_id="casa_D1_ip"):
    return SimpleNamespace(entity_id=entity_id, original_name=name, name=None, platform="casa",
                           device_id=device_reg_id, unique_id=unique_id)


def test_auto_id_is_renamed_from_alias():
    reg = FakeEntityRegistry([_entry("sensor.casa_device_guest_ip_address_2", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.casa_device_guest_ip_address_2"], "Bryce Mobile") == "sensor.bryce_mobile_ip_address"


def test_custom_id_is_left_alone():
    reg = FakeEntityRegistry([_entry("sensor.my_phone_ip", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.my_phone_ip"], "Bryce Mobile") is None
    assert reg.updates == []


def test_collision_gets_suffix():
    reg = FakeEntityRegistry([
        _entry("sensor.bryce_mobile_ip_address", "IP Address", unique_id="casa_OTHER_ip"),
        _entry("sensor.casa_device_x_ip_address", "IP Address"),
    ])
    assert _rename_casa_entity(reg, reg.entries["sensor.casa_device_x_ip_address"], "Bryce Mobile") == "sensor.bryce_mobile_ip_address_2"


def test_already_named_correctly_is_noop():
    reg = FakeEntityRegistry([_entry("sensor.bryce_mobile_ip_address", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.bryce_mobile_ip_address"], "Bryce Mobile") is None


def _install(monkeypatch, ent_reg):
    import homeassistant.helpers as helpers
    dev = types.ModuleType("homeassistant.helpers.device_registry")
    dev_reg = SimpleNamespace(async_get_device=lambda identifiers: SimpleNamespace(id="reg-D1"))
    dev.async_get = lambda hass: dev_reg
    ent = types.ModuleType("homeassistant.helpers.entity_registry")
    ent.async_get = lambda hass: ent_reg
    ent.async_entries_for_device = lambda registry, device_id, include_disabled_entities=False: [
        e for e in registry.entries.values() if e.device_id == device_id
    ]
    for name, mod in (("device_registry", dev), ("entity_registry", ent)):
        monkeypatch.setitem(sys.modules, f"homeassistant.helpers.{name}", mod)
        monkeypatch.setattr(helpers, name, mod, raising=False)


def _hass(until):
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {
        "username": "guest",
        "devices": {"D1": {"alias": "Bryce Mobile", "entity_rename_until": until}},
    }
    return hass


def test_rename_device_entities_inside_window(monkeypatch):
    reg = FakeEntityRegistry([_entry("sensor.casa_device_guest_ip_address_2", "IP Address")])
    _install(monkeypatch, reg)
    _rename_device_entities(_hass(time.time() + 600), "D1")
    assert reg.updates == [("sensor.casa_device_guest_ip_address_2", "sensor.bryce_mobile_ip_address")]


def test_rename_device_entities_after_window_is_noop(monkeypatch):
    reg = FakeEntityRegistry([_entry("sensor.casa_device_guest_ip_address_2", "IP Address")])
    _install(monkeypatch, reg)
    _rename_device_entities(_hass(time.time() - 1), "D1")
    assert reg.updates == []


def test_already_slugged_name_is_noop_even_with_casa_device_alias():
    reg = FakeEntityRegistry([_entry("sensor.casa_device_guest_ip_address", "IP Address")])
    assert _rename_casa_entity(reg, reg.entries["sensor.casa_device_guest_ip_address"], "Casa Device Guest") is None
    assert reg.updates == []

"""Deleting the integration removes every casa store, not just casa_users:
WireGuard private keys, queued reauth passwords and saved templates live in
the others and would otherwise stay in .storage and every backup."""
import asyncio
from types import SimpleNamespace

import custom_components.casa as casa
from custom_components.casa import CASA_STORE_KEYS


def test_uninstall_removes_every_casa_store(monkeypatch):
    removed = []

    class FakeStore:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            return None

        async def async_remove(self):
            removed.append(self.key)

    monkeypatch.setattr(casa, "Store", FakeStore)
    hass = SimpleNamespace(data={})
    asyncio.run(casa.async_remove_entry(hass, SimpleNamespace(options={})))
    assert sorted(removed) == sorted(CASA_STORE_KEYS)
    assert set(CASA_STORE_KEYS) == {
        "casa_users", "Casa_WireGuardProfiles", "casa_provision_profiles",
        "casa_queued_updates", "casa_location_zones", "casa_notification_actions",
    }

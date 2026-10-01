import asyncio

import pytest

from custom_components.casa import _entry_func
from tests.fakes import FakeHass


def test_views_reach_the_current_entry_function():
    hass = FakeHass()
    call = _entry_func(hass, "heartbeat_func")

    async def first(*a, **kw):
        return "first"

    async def second(*a, **kw):
        return "second"

    hass.casa["heartbeat_func"] = first
    assert asyncio.run(call("u", "d")) == "first"
    hass.casa["heartbeat_func"] = second  # entry reloaded
    assert asyncio.run(call("u", "d")) == "second"
    hass.data.pop("casa")  # entry unloaded
    with pytest.raises(Exception, match="not loaded"):
        asyncio.run(call("u", "d"))


def test_saves_go_through_the_current_entry_store():
    from custom_components.casa import _save_stored_data
    from tests.fakes import FakeStore

    hass = FakeHass()
    old_store = hass.casa["store"]
    # Entry reloaded: new store and data objects.
    hass.casa["store"] = new_store = FakeStore()
    hass.casa["stored_data"] = new_data = {"users": {}}
    _save_stored_data(hass)
    assert old_store.delayed == 0 and new_store.delayed == 1
    assert new_store.pending() is new_data

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

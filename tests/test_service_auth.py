"""Service authorization: password-revealing and session-removing services are
admin/system only even when the admin_system_only option is off."""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.casa import HomeAssistantError, _authorize_service, ALWAYS_ADMIN_SERVICES
from tests.fakes import FakeHass, make_user


def _call(service, user_id, parent_id=None):
    return SimpleNamespace(service=service, context=SimpleNamespace(user_id=user_id, parent_id=parent_id))


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("admin", login="root", admin=True))
    hass.auth.add_user(make_user("guest", login="guest"))
    return hass


@pytest.mark.parametrize("service", ["scramble_guest_password", "provision", "generate_qr", "remove_token"])
def test_non_admin_blocked_even_with_option_off(service):
    hass = _hass()
    with pytest.raises(HomeAssistantError):
        asyncio.run(_authorize_service(hass, _call(service, "guest"), admin_system_only=False))


def test_always_admin_services_listed():
    assert {"scramble_guest_password", "provision", "generate_qr", "remove_token"} <= ALWAYS_ADMIN_SERVICES


def test_other_services_follow_the_option():
    hass = _hass()
    users = asyncio.run(_authorize_service(hass, _call("notify_user", "guest"), admin_system_only=False))
    assert {u.id for u in users} == {"admin", "guest"}
    with pytest.raises(HomeAssistantError):
        asyncio.run(_authorize_service(hass, _call("notify_user", "guest"), admin_system_only=True))


@pytest.mark.parametrize("service", ["scramble_guest_password", "notify_user"])
def test_admin_and_system_contexts_allowed(service):
    hass = _hass()
    assert asyncio.run(_authorize_service(hass, _call(service, "admin"), admin_system_only=True))
    assert asyncio.run(_authorize_service(hass, _call(service, None), admin_system_only=True))
    assert asyncio.run(_authorize_service(hass, _call(service, "guest", parent_id="auto"), admin_system_only=True))

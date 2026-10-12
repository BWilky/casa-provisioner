"""notify_user with data.actions: one pending record per device, server-only
keys stripped from the pushed copy, category set."""
from custom_components.casa import _action_push_fields
from custom_components.casa.actions import CATEGORY, validate_actions
from tests.fakes import FakeHass

DATA = {"tag": "door", "action_data": {"c": 1}, "nav_path": "/x",
        "actions": [{"action": "UNLOCK", "title": "Unlock", "service": "lock.unlock", "target": {"entity_id": "lock.a"}},
                    {"action": "IGNORE", "title": "Ignore", "destructive": True}]}


def test_push_fields_strip_server_keys_and_record_pending():
    hass = FakeHass()
    actions = validate_actions(DATA)
    out = _action_push_fields(hass, device_id="D1", user_id="u1", username="alice", parsed_data=DATA, actions=actions)
    nid = out["notification_id"]
    assert out["nav_path"] == "/x" and out["tag"] == "door"
    assert out["actions"] == [{"action": "UNLOCK", "title": "Unlock"}, {"action": "IGNORE", "title": "Ignore", "destructive": True}]
    rec = hass.casa["na_data"]["pending"][nid]
    assert rec["device_id"] == "D1" and rec["actions"][0]["service"] == "lock.unlock"
    assert hass.casa["na_store"].delayed == 1


def test_two_devices_two_records():
    hass = FakeHass()
    actions = validate_actions(DATA)
    a = _action_push_fields(hass, device_id="D1", user_id="u1", username="alice", parsed_data=DATA, actions=actions)
    b = _action_push_fields(hass, device_id="D2", user_id="u1", username="alice", parsed_data=DATA, actions=actions)
    assert a["notification_id"] != b["notification_id"]
    assert {r["device_id"] for r in hass.casa["na_data"]["pending"].values()} == {"D1", "D2"}


def test_category_constant_is_what_the_app_registers():
    assert CATEGORY == "CASA_DYNAMIC"


# --- send-time guard: only admins (or system/automation contexts) may attach a service ---
import asyncio

import pytest

from custom_components.casa import HomeAssistantError, _assert_may_attach_service
from tests.fakes import make_user

SERVICE_ACTIONS = validate_actions(DATA)
PLAIN_ACTIONS = validate_actions({"actions": [{"action": "OK", "title": "OK"}]})


def _guard_hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("kid", login="kid"))
    hass.auth.add_user(make_user("boss", login="boss", admin=True))
    return hass


def test_non_admin_cannot_attach_service():
    with pytest.raises(HomeAssistantError, match="Only administrators can attach a service"):
        asyncio.run(_assert_may_attach_service(_guard_hass(), "kid", SERVICE_ACTIONS))


def test_unknown_user_cannot_attach_service():
    with pytest.raises(HomeAssistantError):
        asyncio.run(_assert_may_attach_service(_guard_hass(), "ghost", SERVICE_ACTIONS))


def test_non_admin_may_send_actions_without_service():
    asyncio.run(_assert_may_attach_service(_guard_hass(), "kid", PLAIN_ACTIONS))


def test_admin_may_attach_service():
    asyncio.run(_assert_may_attach_service(_guard_hass(), "boss", SERVICE_ACTIONS))


def test_system_context_may_attach_service():
    asyncio.run(_assert_may_attach_service(_guard_hass(), None, SERVICE_ACTIONS))


def test_no_actions_is_always_ok():
    asyncio.run(_assert_may_attach_service(_guard_hass(), "kid", None))

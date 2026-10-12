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


# --- handler wiring: the relay body built per device, and data parsing ---
import json

from custom_components.casa import _notify_relay_body, _parse_notify_data

STORED = {"site_id": "S1", "site_key": "K1", "users": {}, "native_devices": {}}
BASE = {"title": "T", "message": "M", "target": "tok", "site_id": "S1", "site_key": "K1"}


def _body(hass, raw):
    parsed, actions = _parse_notify_data(raw)
    return _notify_relay_body(
        hass, push_token="tok", title="T", message="M", stored_data=STORED,
        parsed_data=parsed, actions=actions, device_id="D1", user_id="u1", username="alice",
    )


def test_no_actions_body_without_data_is_the_old_shape():
    hass = FakeHass()
    assert _body(hass, None) == BASE
    assert hass.casa["na_data"]["pending"] == {} and hass.casa["na_store"].delayed == 0


def test_no_actions_body_with_dict_data_passes_it_through():
    hass = FakeHass()
    assert _body(hass, {"nav_path": "/x", "tag": "t"}) == {**BASE, "data": {"nav_path": "/x", "tag": "t"}}
    assert hass.casa["na_data"]["pending"] == {}


def test_no_actions_body_with_non_json_string_passes_the_string():
    hass = FakeHass()
    assert _body(hass, "not json") == {**BASE, "data": "not json"}


def test_no_actions_body_with_json_string_is_parsed():
    hass = FakeHass()
    assert _body(hass, '{"nav_path": "/x"}') == {**BASE, "data": {"nav_path": "/x"}}


def _assert_action_body(hass, body):
    assert body["category"] == "CASA_DYNAMIC"
    assert {k: body[k] for k in BASE} == BASE
    data = body["data"]
    nid = data["notification_id"]
    assert data["actions"] == [{"action": "UNLOCK", "title": "Unlock"},
                               {"action": "IGNORE", "title": "Ignore", "destructive": True}]
    assert data["tag"] == "door" and data["nav_path"] == "/x"
    rec = hass.casa["na_data"]["pending"][nid]
    assert rec["device_id"] == "D1" and rec["user_id"] == "u1" and rec["username"] == "alice"
    assert rec["actions"][0]["service"] == "lock.unlock"


def test_actions_body_has_category_notification_id_and_stripped_actions():
    hass = FakeHass()
    _assert_action_body(hass, _body(hass, DATA))


def test_actions_given_as_json_string_take_the_same_path():
    hass = FakeHass()
    _assert_action_body(hass, _body(hass, json.dumps(DATA)))


def test_parse_notify_data_invalid_actions_raises():
    with pytest.raises(HomeAssistantError) as exc:
        _parse_notify_data({"actions": [{"action": "A"}]})
    assert str(exc.value).startswith("Invalid actions:")


def test_parse_notify_data_invalid_actions_in_json_string_raises():
    with pytest.raises(HomeAssistantError) as exc:
        _parse_notify_data(json.dumps({"actions": []}))
    assert str(exc.value).startswith("Invalid actions:")


def test_parse_notify_data_without_actions_returns_none_actions():
    assert _parse_notify_data(None) == (None, None)
    assert _parse_notify_data("x") == ("x", None)
    assert _parse_notify_data({"a": 1}) == ({"a": 1}, None)
    assert _parse_notify_data([1]) == ([1], None)


# --- every write prunes expired / long-consumed records ---
from custom_components.casa import _save_notification_actions
from custom_components.casa.actions import create_pending


def test_save_prunes_before_scheduling_write():
    hass = FakeHass()
    na = hass.casa["na_data"]
    old = create_pending(na, device_id="D1", user_id="u1", username="a", actions=SERVICE_ACTIONS)
    na["pending"][old]["created_at"] -= 24 * 3600 + 1
    fresh = create_pending(na, device_id="D1", user_id="u1", username="a", actions=SERVICE_ACTIONS)
    _save_notification_actions(hass)
    assert set(na["pending"]) == {fresh}
    assert hass.casa["na_store"].delayed == 1
